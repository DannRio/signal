from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping

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
from .validation import prepare_ohlcv


@dataclass(frozen=True)
class ValidationThresholds:
    minimum_trades: int = 200
    minimum_profit_factor: float = 1.20
    minimum_expectancy_r: float = 0.05
    maximum_drawdown_percent: float = 25.0
    maximum_monte_carlo_loss_probability: float = 0.35


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
    ) -> BacktestResult:
        engine = _ResearchEngine(config or self.config, allow_short=allow_short)
        return RegimeBacktester(engine).run(
            symbol=symbol,
            frames=frames,
            execution_timeframe=execution_timeframe,
            starting_equity=starting_equity,
            risk_percent=risk_percent,
            maximum_notional_multiple=maximum_notional_multiple,
        )

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
        if not 0.0 < train_fraction < 1.0:
            raise ValueError("train_fraction must be in (0, 1).")
        if not 0.0 < validation_fraction < 1.0:
            raise ValueError("validation_fraction must be in (0, 1).")
        if train_fraction + validation_fraction >= 1.0:
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
        train_end_index = int(count * train_fraction)
        validation_end_index = int(count * (train_fraction + validation_fraction))
        minimum = self.config.minimum_bars + 2
        boundaries = (0, train_end_index, validation_end_index, count)
        if any(
            boundaries[index + 1] - boundaries[index] < minimum
            for index in range(3)
        ):
            raise ValueError(
                "Each chronological split needs at least "
                f"{minimum} execution candles; download a longer history."
            )

        labels = ("train", "validation", "test")
        outputs: dict[str, Any] = {}
        for label, start_index, end_index in zip(
            labels,
            boundaries[:-1],
            boundaries[1:],
            strict=True,
        ):
            start_time = execution_frame.iloc[start_index]["timestamp"]
            if end_index < count:
                end_time = execution_frame.iloc[end_index]["timestamp"]
            else:
                end_time = execution_frame.iloc[-1]["timestamp"] + pd.Timedelta(
                    microseconds=1
                )
            segment = _slice_frames(normalized, start_time, end_time)
            result = self.backtest(
                symbol=symbol,
                frames=segment,
                execution_timeframe=execution,
                starting_equity=starting_equity,
                risk_percent=risk_percent,
                maximum_notional_multiple=maximum_notional_multiple,
                allow_short=allow_short,
            )
            outputs[label] = {
                "start": start_time.isoformat(),
                "end": end_time.isoformat(),
                "bars": len(segment[execution]),
                "result": _compact_result(result),
                "diagnostics": _compact_diagnostics(self.diagnostics(result)),
            }
        return outputs

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
                end_time = execution_frame.iloc[-1]["timestamp"] + pd.Timedelta(
                    microseconds=1
                )
            segment = _slice_frames(normalized, start_time, end_time)
            try:
                result = self.backtest(
                    symbol=symbol,
                    frames=segment,
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
    ) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        for multiplier in multipliers:
            if multiplier <= 0:
                raise ValueError("Cost multipliers must be positive.")
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
            )
            outputs.append(
                {
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
    ) -> dict[str, Any]:
        if simulations < 100 or simulations > 100_000:
            raise ValueError("simulations must be between 100 and 100000.")
        r_values = np.asarray(
            [trade.r_multiple for trade in result.trades_detail],
            dtype=float,
        )
        if not len(r_values):
            return {
                "simulations": simulations,
                "trades_per_path": 0,
                "insufficient_data": True,
            }
        equity = float(starting_equity or result.starting_equity)
        risk = float(risk_percent or self.config.risk.risk_percent) / 100.0
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
                peak = max(peak, value)
                if peak > 0:
                    maximum_drawdown = max(
                        maximum_drawdown,
                        (peak - value) / peak * 100.0,
                    )
            ending_values[index] = value
            drawdowns[index] = maximum_drawdown
        return {
            "simulations": simulations,
            "trades_per_path": len(r_values),
            "seed": seed,
            "risk_percent": risk * 100.0,
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

        def add(name: str, passed: bool, value: Any, requirement: str) -> None:
            checks.append(
                {
                    "name": name,
                    "passed": bool(passed),
                    "value": value,
                    "requirement": requirement,
                }
            )

        add(
            "minimum_trades",
            base_result.trades >= thresholds.minimum_trades,
            base_result.trades,
            f">= {thresholds.minimum_trades}",
        )
        add(
            "positive_expectancy",
            base_result.expectancy_r >= thresholds.minimum_expectancy_r,
            base_result.expectancy_r,
            f">= {thresholds.minimum_expectancy_r:.2f}R",
        )
        profit_factor = base_result.profit_factor or math.inf
        add(
            "profit_factor",
            profit_factor >= thresholds.minimum_profit_factor,
            base_result.profit_factor,
            f">= {thresholds.minimum_profit_factor:.2f}",
        )
        add(
            "drawdown",
            base_result.max_drawdown_percent
            <= thresholds.maximum_drawdown_percent,
            base_result.max_drawdown_percent,
            f"<= {thresholds.maximum_drawdown_percent:.1f}%",
        )
        if oos is not None and "test" in oos:
            test_result = oos["test"]["result"]
            add(
                "untouched_test_expectancy",
                test_result["expectancy_r"] > 0,
                test_result["expectancy_r"],
                "> 0R",
            )
            add(
                "untouched_test_return",
                test_result["total_return_percent"] > 0,
                test_result["total_return_percent"],
                "> 0%",
            )
        if cost_stress:
            worst = cost_stress[-1]["result"]
            add(
                "maximum_cost_stress_expectancy",
                worst["expectancy_r"] > 0,
                worst["expectancy_r"],
                "> 0R",
            )
        if monte_carlo and not monte_carlo.get("insufficient_data"):
            probability = monte_carlo["probability_ending_below_start"]
            add(
                "monte_carlo_loss_probability",
                probability
                <= thresholds.maximum_monte_carlo_loss_probability,
                probability,
                "<= "
                f"{thresholds.maximum_monte_carlo_loss_probability:.0%}",
            )

        required_evidence = 7
        if len(checks) < required_evidence:
            status = "INSUFFICIENT_EVIDENCE"
        elif all(check["passed"] for check in checks):
            status = "PASS_FOR_PAPER_VALIDATION"
        else:
            status = "FAIL"
        return {
            "status": status,
            "checks": checks,
            "passed": sum(1 for check in checks if check["passed"]),
            "failed": sum(1 for check in checks if not check["passed"]),
            "important": (
                "This gate is a research standard, not proof of future profit "
                "and not authorization for live execution."
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
        base = self.backtest(
            symbol=symbol,
            frames=frames,
            execution_timeframe=execution_timeframe,
            starting_equity=starting_equity,
            risk_percent=risk_percent,
            maximum_notional_multiple=maximum_notional_multiple,
            allow_short=allow_short,
        )
        oos = self.chronological_oos(
            symbol=symbol,
            frames=frames,
            execution_timeframe=execution_timeframe,
            starting_equity=starting_equity,
            risk_percent=risk_percent,
            maximum_notional_multiple=maximum_notional_multiple,
            allow_short=allow_short,
        )
        stress = self.cost_stress(
            symbol=symbol,
            frames=frames,
            execution_timeframe=execution_timeframe,
            starting_equity=starting_equity,
            risk_percent=risk_percent,
            maximum_notional_multiple=maximum_notional_multiple,
            allow_short=allow_short,
        )
        monte = self.monte_carlo(
            base,
            starting_equity=starting_equity,
            risk_percent=risk_percent,
            simulations=monte_carlo_simulations,
        )
        return {
            "base": self.diagnostics(base),
            "oos": oos,
            "cost_stress": stress,
            "monte_carlo": monte,
            "gate": self.validation_gate(
                base_result=base,
                oos=oos,
                cost_stress=stress,
                monte_carlo=monte,
            ),
        }



class _ResearchEngine(RegimeSignalEngine):
    """Optionally enforce long-only evaluation for spot-market research."""

    def __init__(self, config: RegimeBotConfig, *, allow_short: bool) -> None:
        super().__init__(config)
        self.allow_short = allow_short

    def analyze_prepared(self, **kwargs):
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
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    sliced: dict[str, pd.DataFrame] = {}
    for timeframe, frame in frames.items():
        segment = frame.loc[
            (frame["timestamp"] >= start) & (frame["timestamp"] < end)
        ].reset_index(drop=True)
        sliced[timeframe] = segment
    return sliced


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
