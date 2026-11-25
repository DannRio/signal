from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

import numpy as np
import pandas as pd

from .backtest import RegimeBacktester
from .config import RegimeBotConfig
from .engine import RegimeSignalEngine
from .models import (
    BacktestResult,
    BacktestTrade,
    SetupType,
    SignalAction,
    to_dict,
)
from .validation import prepare_ohlcv, timeframe_to_timedelta


@dataclass(frozen=True)
class ValidationThresholds:
    minimum_trades: int = 200
    minimum_profit_factor: float = 1.20
    minimum_expectancy_r: float = 0.05
    maximum_drawdown_percent: float = 25.0
    maximum_monte_carlo_loss_probability: float = 0.35
    minimum_oos_trades: int = 50
    minimum_cost_stress_multiplier: float = 2.0

    def validate(self) -> None:
        for name in ("minimum_trades", "minimum_oos_trades"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        for name in ("minimum_profit_factor", "minimum_expectancy_r"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite.")
        if not 0 < self.maximum_drawdown_percent <= 100:
            raise ValueError("maximum_drawdown_percent must be in (0, 100].")
        if not 0 <= self.maximum_monte_carlo_loss_probability <= 1:
            raise ValueError("maximum_monte_carlo_loss_probability must be in [0, 1].")
        if (
            not math.isfinite(self.minimum_cost_stress_multiplier)
            or self.minimum_cost_stress_multiplier <= 1
        ):
            raise ValueError("minimum_cost_stress_multiplier must be finite and > 1.")


@dataclass(frozen=True)
class _EvaluationWindow:
    label: str
    start: pd.Timestamp
    end: pd.Timestamp
    bars: int
    frames: dict[str, pd.DataFrame]


class ResearchService:
    """Research diagnostics around the production signal/backtest engine.

    The service never promotes parameters automatically. It exposes evidence so
    a human can decide whether a fixed configuration is robust enough to paper
    trade. Each chronological split is evaluated independently to avoid using
    later data for an earlier result.
    """

    def __init__(
        self,
        config: RegimeBotConfig | None = None,
        *,
        thresholds: ValidationThresholds | None = None,
    ) -> None:
        self.config = config or RegimeBotConfig()
        self.config.validate()
        self.thresholds = thresholds or ValidationThresholds()
        self.thresholds.validate()

    def backtest(
        self,
        *,
        symbol: str,
        frames: Mapping[str, pd.DataFrame],
        execution_timeframe: str,
        starting_equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
        config: RegimeBotConfig | None = None,
        allow_short: bool = True,
        evaluation_start: pd.Timestamp | None = None,
    ) -> BacktestResult:
        engine = _ResearchEngine(
            config or self.config,
            allow_short=allow_short,
            evaluation_start=evaluation_start,
        )
        result = RegimeBacktester(engine).run(
            symbol=symbol,
            frames=frames,
            execution_timeframe=execution_timeframe,
            starting_equity=starting_equity,
            risk_percent=risk_percent,
            maximum_notional_multiple=maximum_notional_multiple,
        )
        if evaluation_start is not None:
            result = replace(
                result,
                equity_curve=tuple(
                    (timestamp, equity)
                    for timestamp, equity in result.equity_curve
                    if timestamp >= engine.evaluation_start
                ),
            )
        return result

    def diagnostics(self, result: BacktestResult) -> dict[str, Any]:
        trades = list(result.trades_detail)
        r_values = np.asarray([trade.r_multiple for trade in trades], dtype=float)
        pnl_values = np.asarray([trade.net_pnl for trade in trades], dtype=float)
        wins = r_values[r_values > 0]
        losses = r_values[r_values <= 0]
        average_win = float(wins.mean()) if len(wins) else 0.0
        average_loss = float(losses.mean()) if len(losses) else 0.0
        payoff_ratio = (
            average_win / abs(average_loss)
            if average_win > 0 and average_loss < 0
            else None
        )
        standard_deviation = (
            float(r_values.std(ddof=1)) if len(r_values) > 1 else 0.0
        )
        sqn = (
            math.sqrt(len(r_values)) * float(r_values.mean()) / standard_deviation
            if len(r_values) > 1 and standard_deviation > 0
            else None
        )
        return {
            "summary": to_dict(result),
            "trade_quality": {
                "average_win_r": average_win,
                "average_loss_r": average_loss,
                "payoff_ratio": payoff_ratio,
                "median_r": float(np.median(r_values)) if len(r_values) else 0.0,
                "r_standard_deviation": standard_deviation,
                "system_quality_number": sqn,
                "largest_win_r": float(r_values.max()) if len(r_values) else 0.0,
                "largest_loss_r": float(r_values.min()) if len(r_values) else 0.0,
                "longest_win_streak": _longest_streak(pnl_values > 0),
                "longest_loss_streak": _longest_streak(pnl_values <= 0),
                "average_holding_bars": (
                    float(np.mean([trade.bars_held for trade in trades]))
                    if trades
                    else 0.0
                ),
            },
            "yearly": _group_trades(trades, lambda trade: str(trade.exit_time.year)),
            "by_side": _group_trades(trades, lambda trade: trade.side.value),
            "by_setup": _group_trades(trades, lambda trade: trade.setup_type.value),
            "by_exit_reason": _group_trades(trades, lambda trade: trade.exit_reason),
            "drawdown_curve": _drawdown_curve(result),
        }

    def chronological_oos(
        self,
        *,
        symbol: str,
        frames: Mapping[str, pd.DataFrame],
        execution_timeframe: str,
        starting_equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
        train_fraction: float = 0.60,
        validation_fraction: float = 0.20,
        allow_short: bool = True,
    ) -> dict[str, Any]:
        windows = _chronological_windows(
            frames,
            execution_timeframe,
            self.config.minimum_bars,
            train_fraction,
            validation_fraction,
        )
        outputs, _ = self._evaluate_windows(
            windows,
            symbol=symbol,
            execution_timeframe=execution_timeframe,
            starting_equity=starting_equity,
            risk_percent=risk_percent,
            maximum_notional_multiple=maximum_notional_multiple,
            allow_short=allow_short,
        )
        return outputs

    def _evaluate_windows(
        self,
        windows: list[_EvaluationWindow],
        **backtest_options: Any,
    ) -> tuple[dict[str, Any], dict[str, BacktestResult]]:
        outputs: dict[str, Any] = {}
        results: dict[str, BacktestResult] = {}
        for window in windows:
            result = self.backtest(
                frames=window.frames,
                evaluation_start=window.start,
                **backtest_options,
            )
            results[window.label] = result
            outputs[window.label] = {
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "bars": window.bars,
                "warmup_bars": {
                    timeframe: int(
                        ((frame["timestamp"] + timeframe_to_timedelta(timeframe))
                         <= window.start).sum()
                    )
                    for timeframe, frame in window.frames.items()
                },
                "starts_flat": True,
                "result": _compact_result(result),
                "diagnostics": _compact_diagnostics(self.diagnostics(result)),
            }
        return outputs, results

    def walk_forward(
        self,
        *,
        symbol: str,
        frames: Mapping[str, pd.DataFrame],
        execution_timeframe: str,
        window_bars: int,
        step_bars: int,
        starting_equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
        allow_short: bool = True,
    ) -> dict[str, Any]:
        minimum = self.config.minimum_bars + 2
        if window_bars < minimum:
            raise ValueError(f"window_bars must be at least {minimum}.")
        if step_bars <= 0:
            raise ValueError("step_bars must be positive.")
        normalized = {
            timeframe.strip().lower(): prepare_ohlcv(frame)
            for timeframe, frame in frames.items()
        }
        execution = execution_timeframe.strip().lower()
        execution_frame = normalized[execution]
        windows: list[dict[str, Any]] = []
        index = 0
        while index + window_bars <= len(execution_frame):
            end_index = index + window_bars
            start_time = execution_frame.iloc[index]["timestamp"]
            if end_index < len(execution_frame):
                end_time = execution_frame.iloc[end_index]["timestamp"]
            else:
                end_time = (
                    execution_frame.iloc[-1]["timestamp"]
                    + timeframe_to_timedelta(execution)
                )
            segment = _slice_frames(normalized, end_time)
            try:
                result = self.backtest(
                    symbol=symbol,
                    frames=segment,
                    evaluation_start=start_time,
                    execution_timeframe=execution,
                    starting_equity=starting_equity,
                    risk_percent=risk_percent,
                    maximum_notional_multiple=maximum_notional_multiple,
                    allow_short=allow_short,
                )
                windows.append(
                    {
                        "index": len(windows) + 1,
                        "start": start_time.isoformat(),
                        "end": end_time.isoformat(),
                        "result": _compact_result(result),
                    }
                )
            except ValueError as exc:
                windows.append(
                    {
                        "index": len(windows) + 1,
                        "start": start_time.isoformat(),
                        "end": end_time.isoformat(),
                        "error": str(exc),
                    }
                )
            index += step_bars
        valid = [window["result"] for window in windows if "result" in window]
        positive = sum(
            1 for result in valid if result["expectancy_r"] > 0
        )
        return {
            "method": "rolling_fixed_configuration",
            "optimized": False,
            "overlapping_windows": step_bars < window_bars,
            "window_bars": window_bars,
            "step_bars": step_bars,
            "windows": windows,
            "valid_windows": len(valid),
            "positive_expectancy_windows": positive,
            "positive_window_rate": positive / len(valid) if valid else 0.0,
        }

    def cost_stress(
        self,
        *,
        symbol: str,
        frames: Mapping[str, pd.DataFrame],
        execution_timeframe: str,
        starting_equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
        multipliers: tuple[float, ...] = (1.0, 1.5, 2.0, 3.0),
        allow_short: bool = True,
        evaluation_start: pd.Timestamp | None = None,
        evidence_scope: str = "provided_window",
    ) -> list[dict[str, Any]]:
        if not multipliers:
            raise ValueError("At least one cost multiplier is required.")
        if any(not math.isfinite(value) or value <= 0 for value in multipliers):
            raise ValueError("Cost multipliers must be positive and finite.")
        outputs: list[dict[str, Any]] = []
        for multiplier in sorted(set(multipliers)):
            stressed_risk = replace(
                self.config.risk,
                fee_bps=self.config.risk.fee_bps * multiplier,
                slippage_bps=self.config.risk.slippage_bps * multiplier,
            )
            stressed_config = replace(self.config, risk=stressed_risk)
            result = self.backtest(
                symbol=symbol,
                frames=frames,
                execution_timeframe=execution_timeframe,
                starting_equity=starting_equity,
                risk_percent=risk_percent,
                maximum_notional_multiple=maximum_notional_multiple,
                config=stressed_config,
                allow_short=allow_short,
                evaluation_start=evaluation_start,
            )
            outputs.append(
                {
                    "scope": evidence_scope,
                    "cost_multiplier": multiplier,
                    "fee_bps": stressed_risk.fee_bps,
                    "slippage_bps": stressed_risk.slippage_bps,
                    "result": _compact_result(result),
                }
            )
        return outputs

    def monte_carlo(
        self,
        result: BacktestResult,
        *,
        starting_equity: float | None = None,
        risk_percent: float | None = None,
        simulations: int = 2000,
        seed: int = 42,
        evidence_scope: str = "provided_result",
    ) -> dict[str, Any]:
        if (
            isinstance(simulations, bool)
            or not isinstance(simulations, int)
            or simulations < 100
            or simulations > 100_000
        ):
            raise ValueError("simulations must be an integer between 100 and 100000.")
        equity = float(result.starting_equity if starting_equity is None else starting_equity)
        risk_percent_value = float(
            self.config.risk.risk_percent if risk_percent is None else risk_percent
        )
        if not math.isfinite(equity) or equity <= 0:
            raise ValueError("starting_equity must be positive and finite.")
        if not 0 < risk_percent_value <= self.config.risk.maximum_risk_percent:
            raise ValueError("risk_percent is outside configured limits.")
        risk = risk_percent_value / 100.0
        r_values = np.asarray(
            [trade.r_multiple for trade in result.trades_detail],
            dtype=float,
        )
        metadata = {
            "method": "iid_trade_bootstrap",
            "scope": evidence_scope,
            "simulations": simulations,
            "trades_per_path": len(r_values),
            "seed": seed,
            "risk_percent": risk_percent_value,
            "assumption": "Trades are resampled independently; serial dependence is not preserved.",
        }
        if not len(r_values):
            return {**metadata, "insufficient_data": True}
        if not np.isfinite(r_values).all():
            raise ValueError("Monte Carlo trade R multiples must be finite.")
        rng = np.random.default_rng(seed)
        ending_values = np.empty(simulations, dtype=float)
        drawdowns = np.empty(simulations, dtype=float)
        for index in range(simulations):
            sequence = rng.choice(r_values, size=len(r_values), replace=True)
            value = equity
            peak = equity
            maximum_drawdown = 0.0
            for trade_r in sequence:
                value *= max(0.0, 1.0 + float(trade_r) * risk)
                if not math.isfinite(value):
                    raise ValueError("Monte Carlo produced non-finite equity; inspect trade R multiples.")
                peak = max(peak, value)
                if peak > 0:
                    maximum_drawdown = max(
                        maximum_drawdown,
                        (peak - value) / peak * 100.0,
                    )
            ending_values[index] = value
            drawdowns[index] = maximum_drawdown
        return {
            **metadata,
            "ending_equity": _percentiles(ending_values),
            "max_drawdown_percent": _percentiles(drawdowns),
            "probability_ending_below_start": float(
                np.mean(ending_values < equity)
            ),
            "probability_drawdown_over_25_percent": float(
                np.mean(drawdowns > 25.0)
            ),
        }

    def validation_gate(
        self,
        *,
        base_result: BacktestResult,
        oos: dict[str, Any] | None = None,
        cost_stress: list[dict[str, Any]] | None = None,
        monte_carlo: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        thresholds = self.thresholds
        checks: list[dict[str, Any]] = []

        def add(
            name: str,
            passed: bool,
            value: Any,
            requirement: str,
            *,
            available: bool = True,
            evidence: bool = False,
        ) -> None:
            checks.append({
                "name": name,
                "passed": bool(passed and available),
                "available": available,
                "category": "evidence" if evidence else "performance",
                "value": _json_finite(value),
                "requirement": requirement,
            })

        def numeric(
            name: str,
            raw: Any,
            predicate,
            requirement: str,
            *,
            evidence: bool = False,
        ) -> None:
            value = _finite_number(raw)
            add(
                name,
                value is not None and predicate(value),
                value,
                requirement,
                available=raw is not None,
                evidence=evidence,
            )

        def result_checks(
            label: str,
            result: Mapping[str, Any],
            minimum_trades: int,
        ) -> None:
            numeric(
                f"{label}_minimum_trades", result.get("trades"),
                lambda value: value.is_integer() and value >= minimum_trades,
                f">= {minimum_trades}", evidence=True,
            )
            numeric(
                f"{label}_expectancy", result.get("expectancy_r"),
                lambda value: value >= thresholds.minimum_expectancy_r,
                f">= {thresholds.minimum_expectancy_r:.2f}R",
            )
            raw_pf = result.get("profit_factor")
            pf = _finite_number(raw_pf)
            # None is the backtest representation of an unbounded PF only
            # when actual winning trades and no losing trades establish it.
            no_losses = (
                raw_pf is None
                and "profit_factor" in result
                and (_finite_number(result.get("trades")) or 0) > 0
                and (_finite_number(result.get("wins")) or 0) > 0
                and _finite_number(result.get("gross_loss")) == 0
                and (_finite_number(result.get("gross_profit")) or 0) > 0
                and (_finite_number(result.get("net_pnl")) or 0) > 0
            )
            add(
                f"{label}_profit_factor",
                no_losses or (pf is not None and pf >= thresholds.minimum_profit_factor),
                "unbounded_no_losses" if no_losses else pf,
                f">= {thresholds.minimum_profit_factor:.2f}",
                available="profit_factor" in result,
            )
            numeric(
                f"{label}_drawdown", result.get("max_drawdown_percent"),
                lambda value: 0 <= value <= thresholds.maximum_drawdown_percent,
                f"<= {thresholds.maximum_drawdown_percent:.1f}%",
            )
            numeric(
                f"{label}_return", result.get("total_return_percent"),
                lambda value: value > 0,
                "> 0%",
            )

        result_checks("base", _compact_result(base_result), thresholds.minimum_trades)
        for label in ("validation", "test"):
            section = (oos or {}).get(label, {})
            result = section.get("result", {}) if isinstance(section, Mapping) else {}
            if not isinstance(result, Mapping):
                result = {}
            result_checks(f"held_out_{label}", result, thresholds.minimum_oos_trades)

        stress_rows = cost_stress or []
        scopes_present = bool(stress_rows) and all(
            isinstance(row, Mapping) and "scope" in row for row in stress_rows
        )
        add(
            "cost_stress_test_scope",
            scopes_present and all(row.get("scope") == "test" for row in stress_rows),
            "test" if scopes_present and all(row.get("scope") == "test" for row in stress_rows) else None,
            "Stress the held-out test period", available=scopes_present, evidence=True,
        )
        multipliers = [
            _finite_number(row.get("cost_multiplier"))
            for row in stress_rows if isinstance(row, Mapping)
        ]
        valid_multipliers = [value for value in multipliers if value is not None and value > 0]
        add(
            "cost_stress_coverage",
            len(valid_multipliers) == len(stress_rows)
            and bool(valid_multipliers)
            and max(valid_multipliers) >= thresholds.minimum_cost_stress_multiplier,
            max(valid_multipliers) if valid_multipliers else None,
            f"Maximum multiplier >= {thresholds.minimum_cost_stress_multiplier:g}",
            available=bool(stress_rows), evidence=True,
        )
        # Every tested scenario must survive; the order supplied is irrelevant.
        stress_results = [
            row.get("result", {}) for row in stress_rows if isinstance(row, Mapping)
        ]
        stress_expectancies = [
            _finite_number(result.get("expectancy_r"))
            if isinstance(result, Mapping) else None for result in stress_results
        ]
        add(
            "cost_stress_positive_expectancy",
            len(stress_expectancies) == len(stress_rows)
            and bool(stress_expectancies)
            and all(value is not None and value > 0 for value in stress_expectancies),
            min(stress_expectancies) if stress_expectancies and all(
                value is not None for value in stress_expectancies
            ) else None,
            "> 0R in every tested cost scenario", available=bool(stress_rows),
        )
        stress_trades = [
            _finite_number(result.get("trades"))
            if isinstance(result, Mapping) else None for result in stress_results
        ]
        add(
            "cost_stress_minimum_trades",
            len(stress_trades) == len(stress_rows)
            and bool(stress_trades)
            and all(value is not None and value.is_integer()
                    and value >= thresholds.minimum_oos_trades for value in stress_trades),
            min(stress_trades) if stress_trades and all(
                value is not None for value in stress_trades
            ) else None,
            f">= {thresholds.minimum_oos_trades} in every cost scenario",
            available=bool(stress_rows) and all(value is not None for value in stress_trades),
            evidence=True,
        )
        stress_drawdowns = [
            _finite_number(result.get("max_drawdown_percent"))
            if isinstance(result, Mapping) else None for result in stress_results
        ]
        add(
            "cost_stress_drawdown",
            len(stress_drawdowns) == len(stress_rows)
            and bool(stress_drawdowns)
            and all(value is not None and 0 <= value <= thresholds.maximum_drawdown_percent
                    for value in stress_drawdowns),
            max(stress_drawdowns) if stress_drawdowns and all(
                value is not None for value in stress_drawdowns
            ) else None,
            f"<= {thresholds.maximum_drawdown_percent:.1f}% in every cost scenario",
            available=bool(stress_rows) and all(value is not None for value in stress_drawdowns),
        )
        monte = monte_carlo or {}
        add(
            "monte_carlo_test_scope", monte.get("scope") == "test",
            monte.get("scope"), "Resample held-out test trades",
            available="scope" in monte, evidence=True,
        )
        numeric(
            "monte_carlo_simulations", monte.get("simulations"),
            lambda value: value.is_integer() and value >= 100,
            ">= 100", evidence=True,
        )
        numeric(
            "monte_carlo_minimum_trades", monte.get("trades_per_path"),
            lambda value: value.is_integer() and value >= thresholds.minimum_oos_trades,
            f">= {thresholds.minimum_oos_trades}", evidence=True,
        )
        numeric(
            "monte_carlo_loss_probability",
            None if monte.get("insufficient_data") else monte.get("probability_ending_below_start"),
            lambda value: 0 <= value <= thresholds.maximum_monte_carlo_loss_probability,
            f"<= {thresholds.maximum_monte_carlo_loss_probability:.0%}",
        )
        missing = [check["name"] for check in checks if not check["available"]]
        inadequate = [
            check["name"] for check in checks
            if check["category"] == "evidence" and not check["passed"]
        ]
        performance_failures = [
            check for check in checks if check["available"]
            and check["category"] == "performance" and not check["passed"]
        ]
        if performance_failures:
            status = "FAIL"
        elif missing or inadequate:
            status = "INSUFFICIENT_EVIDENCE"
        else:
            status = "PASS_FOR_PAPER_VALIDATION"
        return {
            "status": status,
            "live_ready": False,
            "checks": checks,
            "passed": sum(1 for check in checks if check["passed"]),
            "failed": sum(1 for check in checks if check["available"] and not check["passed"]),
            "missing_evidence": missing,
            "insufficient_evidence": inadequate,
            "thresholds": asdict(thresholds),
            "limitations": list(_RESEARCH_LIMITATIONS),
            "important": (
                "Thresholds are configurable research screening rules, not empirically "
                "calibrated guarantees. A pass permits further paper validation only. "
                "It does not establish future profitability or authorize live execution."
            ),
        }

    def full_validation(
        self,
        *,
        symbol: str,
        frames: Mapping[str, pd.DataFrame],
        execution_timeframe: str,
        starting_equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
        monte_carlo_simulations: int = 2000,
        allow_short: bool = True,
    ) -> dict[str, Any]:
        options = {
            "symbol": symbol,
            "execution_timeframe": execution_timeframe,
            "starting_equity": starting_equity,
            "risk_percent": risk_percent,
            "maximum_notional_multiple": maximum_notional_multiple,
            "allow_short": allow_short,
        }
        normalized = {
            timeframe.strip().lower(): prepare_ohlcv(frame)
            for timeframe, frame in frames.items()
        }
        base = self.backtest(frames=normalized, **options)
        windows = _chronological_windows(
            normalized, execution_timeframe, self.config.minimum_bars, 0.60, 0.20
        )
        oos, results = self._evaluate_windows(windows, **options)
        test_window = windows[-1]
        stress = self.cost_stress(
            frames=test_window.frames,
            evaluation_start=test_window.start,
            evidence_scope="test",
            **options,
        )
        monte = self.monte_carlo(
            results["test"],
            starting_equity=starting_equity,
            risk_percent=risk_percent,
            simulations=monte_carlo_simulations,
            evidence_scope="test",
        )
        return {
            "base": self.diagnostics(base),
            "oos": oos,
            "cost_stress": stress,
            "monte_carlo": monte,
            "provenance": _research_provenance(
                normalized, self.config, self.thresholds, options
            ),
            "gate": self.validation_gate(
                base_result=base,
                oos=oos,
                cost_stress=stress,
                monte_carlo=monte,
            ),
        }



class _ResearchEngine(RegimeSignalEngine):
    """Optionally enforce long-only evaluation for spot-market research."""

    def __init__(
        self,
        config: RegimeBotConfig,
        *,
        allow_short: bool,
        evaluation_start: pd.Timestamp | None = None,
    ) -> None:
        super().__init__(config)
        self.allow_short = allow_short
        self.evaluation_start = (
            pd.to_datetime(evaluation_start, utc=True)
            if evaluation_start is not None else None
        )
        if self.evaluation_start is not None and pd.isna(self.evaluation_start):
            raise ValueError("evaluation_start must be a valid timestamp.")

    def analyze_prepared(self, **kwargs):
        execution = kwargs["execution_timeframe"].strip().lower()
        latest = kwargs["feature_frames"][execution].iloc[-1]["timestamp"]
        if self.evaluation_start is not None and latest < self.evaluation_start:
            return self._safe_no_trade(
                symbol=kwargs["symbol"],
                execution_timeframe=execution,
                generated_at=(latest + timeframe_to_timedelta(execution)).to_pydatetime(),
                warnings=("Earlier candles are indicator warm-up only; trading starts in the evaluation window.",),
            )
        signal = super().analyze_prepared(**kwargs)
        if self.allow_short or signal.action not in {
            SignalAction.SHORT,
            SignalAction.WATCH_SHORT,
        }:
            return signal
        return replace(
            signal,
            action=SignalAction.NO_TRADE,
            setup_type=SetupType.NONE,
            trigger_price=None,
            entry_plan=None,
            reasons=signal.reasons
            + ("Short setup suppressed for long-only spot research.",),
        )



def _compact_result(result: BacktestResult) -> dict[str, Any]:
    return {
        "symbol": result.symbol,
        "execution_timeframe": result.execution_timeframe,
        "starting_equity": result.starting_equity,
        "ending_equity": result.ending_equity,
        "net_pnl": result.net_pnl,
        "total_return_percent": result.total_return_percent,
        "max_drawdown_percent": result.max_drawdown_percent,
        "trades": result.trades,
        "wins": result.wins,
        "losses": result.losses,
        "win_rate_percent": result.win_rate_percent,
        "profit_factor": result.profit_factor,
        "gross_profit": sum(trade.net_pnl for trade in result.trades_detail if trade.net_pnl > 0),
        "gross_loss": -sum(trade.net_pnl for trade in result.trades_detail if trade.net_pnl < 0),
        "expectancy_r": result.expectancy_r,
        "average_r": result.average_r,
    }


def _compact_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in diagnostics.items()
        if key not in {"summary", "drawdown_curve"}
    }

def _slice_frames(
    frames: Mapping[str, pd.DataFrame],
    end: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    """Include causal prior history for indicators; the engine suppresses trading."""
    return {
        timeframe: frame.loc[frame["timestamp"] < end].reset_index(drop=True)
        for timeframe, frame in frames.items()
    }


def _chronological_windows(
    frames: Mapping[str, pd.DataFrame],
    execution_timeframe: str,
    minimum_bars: int,
    train_fraction: float,
    validation_fraction: float,
) -> list[_EvaluationWindow]:
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1:
        raise ValueError("train and validation fractions must each be in (0, 1).")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train and validation fractions must sum to less than 1.")
    normalized = {
        timeframe.strip().lower(): prepare_ohlcv(frame)
        for timeframe, frame in frames.items()
    }
    execution = execution_timeframe.strip().lower()
    if execution not in normalized:
        raise ValueError("Execution timeframe is missing from frames.")
    execution_frame = normalized[execution]
    count = len(execution_frame)
    boundaries = (0, int(count * train_fraction), int(count * (train_fraction + validation_fraction)), count)
    minimum = minimum_bars + 2
    if any(end - start < minimum for start, end in zip(boundaries[:-1], boundaries[1:], strict=True)):
        raise ValueError(
            f"Each chronological split needs at least {minimum} execution candles; "
            "download a longer history."
        )
    windows = []
    for label, start_index, end_index in zip(
        ("train", "validation", "test"), boundaries[:-1], boundaries[1:], strict=True
    ):
        start = execution_frame.iloc[start_index]["timestamp"]
        end = (
            execution_frame.iloc[end_index]["timestamp"] if end_index < count
            else execution_frame.iloc[-1]["timestamp"] + timeframe_to_timedelta(execution)
        )
        windows.append(_EvaluationWindow(
            label, start, end, end_index - start_index, _slice_frames(normalized, end)
        ))
    return windows


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _json_finite(value: Any) -> Any:
    if isinstance(value, (float, np.floating)) and not math.isfinite(value):
        return None
    return value


_RESEARCH_LIMITATIONS = [
    "Historical funding settlements, contract-lot rounding, maintenance margin and liquidation are not modeled in P&L.",
    "A chronological holdout is untouched only if the caller did not use it to select or alter the strategy.",
    "IID trade bootstrap does not model serial dependence, regime shifts, or unseen tail events.",
    "A single-symbol study does not establish portfolio capacity, diversification, or live fill quality.",
]


def _research_provenance(
    frames: Mapping[str, pd.DataFrame],
    config: RegimeBotConfig,
    thresholds: ValidationThresholds,
    options: dict[str, Any],
) -> dict[str, Any]:
    configuration = {"strategy": asdict(config), "thresholds": asdict(thresholds), **options}
    configuration_json = json.dumps(configuration, sort_keys=True, separators=(",", ":"), allow_nan=False)
    datasets = {}
    for timeframe, frame in sorted(frames.items()):
        content = frame.to_csv(index=False, float_format="%.17g", lineterminator="\n")
        datasets[timeframe] = {
            "rows": len(frame),
            "start": frame.iloc[0]["timestamp"].isoformat() if len(frame) else None,
            "end": frame.iloc[-1]["timestamp"].isoformat() if len(frame) else None,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    return {
        "schema_version": 2,
        "configuration": configuration,
        "configuration_sha256": hashlib.sha256(configuration_json.encode("utf-8")).hexdigest(),
        "datasets": datasets,
        "parameter_selection": "fixed_configuration_no_optimization",
        "split_fractions": {"train": 0.6, "validation": 0.2, "test": 0.2},
        "warmup_policy": "prior_candles_only_no_trades_before_evaluation_start",
    }


def _group_trades(
    trades: list[BacktestTrade],
    key_function,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[BacktestTrade]] = {}
    for trade in trades:
        groups.setdefault(str(key_function(trade)), []).append(trade)
    output: dict[str, dict[str, Any]] = {}
    for key, group in groups.items():
        pnl = np.asarray([trade.net_pnl for trade in group], dtype=float)
        r_values = np.asarray([trade.r_multiple for trade in group], dtype=float)
        gross_profit = float(pnl[pnl > 0].sum())
        gross_loss = float(-pnl[pnl < 0].sum())
        output[key] = {
            "trades": len(group),
            "wins": int(np.count_nonzero(pnl > 0)),
            "win_rate_percent": float(np.mean(pnl > 0) * 100.0),
            "net_pnl": float(pnl.sum()),
            "average_r": float(r_values.mean()) if len(r_values) else 0.0,
            "profit_factor": (
                gross_profit / gross_loss
                if gross_loss > 0
                else None if gross_profit > 0 else 0.0
            ),
        }
    return output


def _drawdown_curve(result: BacktestResult) -> list[dict[str, Any]]:
    values = np.asarray([value for _, value in result.equity_curve], dtype=float)
    if not len(values):
        return []
    peaks = np.maximum.accumulate(values)
    drawdowns = np.where(peaks > 0, (peaks - values) / peaks * 100.0, 0.0)
    return [
        {"timestamp": timestamp.isoformat(), "drawdown_percent": float(drawdown)}
        for (timestamp, _), drawdown in zip(
            result.equity_curve,
            drawdowns,
            strict=True,
        )
    ]


def _longest_streak(flags: np.ndarray) -> int:
    longest = 0
    current = 0
    for flag in flags:
        current = current + 1 if bool(flag) else 0
        longest = max(longest, current)
    return longest


def _percentiles(values: np.ndarray) -> dict[str, float]:
    return {
        "minimum": float(np.min(values)),
        "p05": float(np.percentile(values, 5)),
        "p25": float(np.percentile(values, 25)),
        "median": float(np.percentile(values, 50)),
        "p75": float(np.percentile(values, 75)),
        "p95": float(np.percentile(values, 95)),
        "maximum": float(np.max(values)),
    }
