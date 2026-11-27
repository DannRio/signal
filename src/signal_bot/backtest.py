from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import numpy as np
import pandas as pd

from .engine import RegimeSignalEngine
from .futures import ContractSpec
from .futures_research import (
    FundingSettlement,
    FuturesResearchError,
    FuturesResearchInputs,
    closing_chunks,
    contract_bounds,
    decimal_value,
    entry_contracts,
    parse_futures_inputs,
    prepare_futures_frame,
    round_step,
    rounded_fill,
)
from .models import (
    BacktestResult,
    BacktestTrade,
    EntryPlan,
    MarketRegime,
    MarketSignal,
    PositionSide,
    SetupType,
    SignalAction,
)
from .validation import prepare_ohlcv, timeframe_to_timedelta
from .venue import normalize_symbol


@dataclass
class _PendingEntry:
    index: int
    signal: MarketSignal


@dataclass
class _Position:
    side: PositionSide
    setup_type: SetupType
    signal_time: datetime
    entry_time: datetime
    entry_price: float
    initial_stop: float
    stop: float
    target1: float
    target2: float
    target3: float
    original_quantity: float
    remaining_quantity: float
    initial_risk: float
    opening_fee: float
    accumulated_fees: float
    gross_pnl: float = 0.0
    closed_value_sum: float = 0.0
    closed_quantity: float = 0.0
    tp1_filled: bool = False
    tp2_filled: bool = False
    highest_high: float = -math.inf
    lowest_low: float = math.inf
    bars_held: int = 0
    last_exit_reason: str = ""
    contract_spec: ContractSpec | None = None
    original_contracts: Decimal | None = None
    remaining_contracts: Decimal | None = None
    funding_pnl: float = 0.0
    funding_payments: list[dict] = field(default_factory=list)
    exit_fills: list[dict] = field(default_factory=list)
    partial_exit_skips: list[dict] = field(default_factory=list)
    tp1_processed: bool = False
    tp2_processed: bool = False


class RegimeBacktester:
    """Closed-candle, next-bar-entry backtester for the regime engine.

    Signals are formed only after a candle closes. Entries and regime-flip
    exits execute at the following candle open. If a stop and target are both
    touched inside one candle, the stop is assumed to fill first.
    """

    def __init__(self, engine: RegimeSignalEngine | None = None):
        self.engine = engine or RegimeSignalEngine()

    def run(
        self,
        *,
        symbol: str,
        frames: Mapping[str, pd.DataFrame],
        execution_timeframe: str,
        starting_equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
        futures_inputs: FuturesResearchInputs | Mapping | None = None,
    ) -> BacktestResult:
        if starting_equity <= 0 or not math.isfinite(starting_equity):
            raise ValueError(
                "starting_equity must be positive and finite."
            )
        futures = parse_futures_inputs(futures_inputs)
        if futures is not None and normalize_symbol(symbol) != futures.contract_spec.symbol:
            raise FuturesResearchError("futures_symbol_mismatch", "Funding/specification evidence belongs to a different contract symbol.")
        execution = execution_timeframe.strip().lower()
        raw_normalized = self.engine._normalize_frames(frames)
        normalized = {
            timeframe: (
                prepare_futures_frame(frame, timeframe, minimum_bars=self.engine.config.minimum_bars)
                if futures is not None else prepare_ohlcv(frame)
            )
            for timeframe, frame in raw_normalized.items()
        }
        self.engine._validate_timeframe_hierarchy(
            tuple(normalized), execution
        )
        if execution not in normalized:
            raise ValueError(
                f"Execution timeframe {execution!r} is not available."
            )
        required_execution_bars = self.engine.config.minimum_bars + 2
        if len(normalized[execution]) < required_execution_bars:
            raise ValueError(
                "Backtest needs at least "
                f"{required_execution_bars} execution candles."
            )
        short_contexts = {
            timeframe: len(frame)
            for timeframe, frame in normalized.items()
            if timeframe != execution
            and len(frame) < self.engine.config.minimum_bars
        }
        if short_contexts:
            raise ValueError(
                "Every requested context timeframe needs at least "
                f"{self.engine.config.minimum_bars} candles; "
                f"received {short_contexts}."
            )

        feature_frames = self.engine.prepare_feature_frames(
            normalized,
            execution_timeframe=execution,
        )
        execution_frame = feature_frames[execution].reset_index(
            drop=True
        )
        execution_delta = timeframe_to_timedelta(execution)
        terminal_time = (execution_frame.iloc[-1]["timestamp"] + execution_delta).to_pydatetime()
        funding_events = futures.funding.events_for_window(
            execution_frame.iloc[0]["timestamp"].to_pydatetime(), terminal_time,
            [value.to_pydatetime() for value in execution_frame["timestamp"]],
        ) if futures is not None else {}
        risk_pct = (
            self.engine.config.risk.risk_percent
            if risk_percent is None
            else float(risk_percent)
        )
        notional_multiple = (
            self.engine.config.risk.maximum_notional_multiple
            if maximum_notional_multiple is None
            else float(maximum_notional_multiple)
        )
        if not (
            0 < risk_pct
            <= self.engine.config.risk.maximum_risk_percent
        ):
            raise ValueError(
                "risk_percent is outside configured limits."
            )
        if notional_multiple <= 0 or not math.isfinite(notional_multiple):
            raise ValueError(
                "maximum_notional_multiple must be positive."
            )

        cash_equity = float(starting_equity)
        position: _Position | None = None
        pending_entry: _PendingEntry | None = None
        pending_regime_exit = False
        trades: list[BacktestTrade] = []
        equity_curve: list[tuple[datetime, float]] = []
        fee_rate = self.engine.config.risk.fee_bps / 10_000.0
        slippage_rate = (
            self.engine.config.risk.slippage_bps / 10_000.0
        )
        minimum_index = self.engine.config.minimum_bars - 1
        unfilled_entry_attempts = 0

        for index, row in execution_frame.iterrows():
            timestamp = row["timestamp"].to_pydatetime()
            bar_close = (row["timestamp"] + execution_delta).to_pydatetime()
            if position is not None and timestamp in funding_events:
                cash_equity = self._apply_funding(position, funding_events[timestamp], cash_equity)

            if position is not None and pending_regime_exit:
                exit_price = self._slipped_price(
                    float(row["open"]),
                    position.side,
                    is_entry=False,
                    slippage_rate=slippage_rate,
                    contract_spec=position.contract_spec,
                )
                cash_equity = self._close_quantity(
                    position,
                    position.remaining_quantity,
                    exit_price,
                    fee_rate,
                    cash_equity,
                    "regime_flip",
                    timestamp=timestamp,
                )
                trades.append(
                    self._finalize_trade(position, timestamp)
                )
                position = None
                pending_regime_exit = False

            if (
                position is None
                and pending_entry is not None
                and pending_entry.index == index
            ):
                plan = pending_entry.signal.entry_plan
                if plan is not None:
                    position, cash_equity = self._open_position(
                        plan=plan,
                        signal=pending_entry.signal,
                        row=row,
                        equity=cash_equity,
                        risk_percent=risk_pct,
                        maximum_notional_multiple=notional_multiple,
                        fee_rate=fee_rate,
                        slippage_rate=slippage_rate,
                        futures_inputs=futures,
                    )
                    if position is None:
                        unfilled_entry_attempts += 1
                pending_entry = None

            if position is not None:
                position.bars_held += 1
                position.highest_high = max(
                    position.highest_high,
                    float(row["high"]),
                )
                position.lowest_low = min(
                    position.lowest_low,
                    float(row["low"]),
                )
                position, cash_equity, completed = (
                    self._manage_position(
                        position=position,
                        row=row,
                        timestamp=timestamp,
                        equity=cash_equity,
                        fee_rate=fee_rate,
                        slippage_rate=slippage_rate,
                        bar_close=bar_close,
                        close_funding=funding_events.get(bar_close),
                    )
                )
                if completed is not None:
                    trades.append(completed)

            decision_signal: MarketSignal | None = None
            if (
                index >= minimum_index
                and index < len(execution_frame) - 1
            ):
                decision_time = row["timestamp"] + execution_delta
                decision_frames = self._frames_at_decision(
                    feature_frames,
                    execution_timeframe=execution,
                    execution_index=index,
                    decision_time=decision_time,
                )
                if decision_frames is not None:
                    decision_signal = self.engine.analyze_prepared(
                        symbol=symbol,
                        feature_frames=decision_frames,
                        execution_timeframe=execution,
                        equity=max(cash_equity, 1e-9),
                        risk_percent=risk_pct,
                        maximum_notional_multiple=notional_multiple,
                    )

            if position is not None and decision_signal is not None:
                pending_regime_exit = self._opposite_regime(
                    position.side,
                    decision_signal.regime,
                    decision_signal.score,
                )
            elif (
                position is None
                and pending_entry is None
                and decision_signal is not None
                and decision_signal.entry_plan is not None
                and decision_signal.action
                in {SignalAction.LONG, SignalAction.SHORT}
            ):
                pending_entry = _PendingEntry(
                    index=index + 1,
                    signal=decision_signal,
                )

            marked_equity = cash_equity
            if position is not None:
                marked_equity += self._unrealized_pnl(
                    position,
                    float(row["close"]),
                )
            equity_curve.append(
                (bar_close if futures is not None else timestamp, float(marked_equity))
            )

        if position is not None:
            final_row = execution_frame.iloc[-1]
            final_time = terminal_time if futures is not None else final_row["timestamp"].to_pydatetime()
            if final_time in funding_events:
                cash_equity = self._apply_funding(position, funding_events[final_time], cash_equity)
            exit_price = self._slipped_price(
                float(final_row["close"]),
                position.side,
                is_entry=False,
                slippage_rate=slippage_rate,
                contract_spec=position.contract_spec,
            )
            cash_equity = self._close_quantity(
                position,
                position.remaining_quantity,
                exit_price,
                fee_rate,
                cash_equity,
                "end_of_data",
                timestamp=final_time,
            )
            trades.append(
                self._finalize_trade(position, final_time)
            )
            equity_curve[-1] = (final_time, cash_equity)

        return self._build_result(
            symbol=symbol,
            execution_timeframe=execution,
            starting_equity=starting_equity,
            ending_equity=cash_equity,
            trades=trades,
            equity_curve=equity_curve,
            simulation_metadata={
                **(futures.metadata(settlements_in_window=len(funding_events)) if futures is not None else {
                    "mode": "legacy_fractional_base_quantity", "funding_modeled": False,
                    "contract_rounding_modeled": False, "margin_and_liquidation_modeled": False,
                    "limitations": ["Historical funding and contract-lot constraints are not modeled.", "Margin, liquidation and order-book capacity are not modeled."],
                    "live_ready": False,
                }),
                "funding_payments_applied": sum(len(trade.funding_payments) for trade in trades),
                "unfilled_entry_attempts": unfilled_entry_attempts,
                "intrabar_fill_timestamps": "bar_open_identifies_the_bar_not_exact_execution_time",
            },
        )

    def _frames_at_decision(
        self,
        feature_frames: Mapping[str, pd.DataFrame],
        *,
        execution_timeframe: str,
        execution_index: int,
        decision_time: pd.Timestamp,
    ) -> dict[str, pd.DataFrame] | None:
        selected: dict[str, pd.DataFrame] = {}
        for timeframe, frame in feature_frames.items():
            if timeframe == execution_timeframe:
                selected[timeframe] = frame.iloc[
                    : execution_index + 1
                ]
                continue
            close_times = (
                frame["timestamp"]
                + timeframe_to_timedelta(timeframe)
            )
            available = frame.loc[close_times <= decision_time]
            if len(available) < self.engine.config.minimum_bars:
                return None
            selected[timeframe] = available
        return selected

    def _open_position(
        self,
        *,
        plan: EntryPlan,
        signal: MarketSignal,
        row: pd.Series,
        equity: float,
        risk_percent: float,
        maximum_notional_multiple: float,
        fee_rate: float,
        slippage_rate: float,
        futures_inputs: FuturesResearchInputs | None = None,
    ) -> tuple[_Position | None, float]:
        if futures_inputs is not None:
            return self._open_futures_position(
                plan=plan, signal=signal, row=row, equity=equity,
                risk_percent=risk_percent, maximum_notional_multiple=maximum_notional_multiple,
                fee_rate=fee_rate, slippage_rate=slippage_rate, spec=futures_inputs.contract_spec,
            )
        side = plan.side
        raw_open = float(row["open"])
        if not (plan.entry_low <= raw_open <= plan.entry_high):
            return None, equity
        entry = self._slipped_price(
            raw_open,
            side,
            is_entry=True,
            slippage_rate=slippage_rate,
        )
        if not (plan.entry_low <= entry <= plan.entry_high):
            return None, equity
        stop = plan.stop_loss
        if side == PositionSide.LONG:
            risk_distance = entry - stop
        else:
            risk_distance = stop - entry
        if risk_distance <= 0 or not math.isfinite(risk_distance):
            return None, equity
        direction = (
            1.0 if side == PositionSide.LONG else -1.0
        )
        target1 = (
            entry + direction * plan.reward_to_risk_1 * risk_distance
        )
        target2 = (
            entry + direction * plan.reward_to_risk_2 * risk_distance
        )
        target3 = (
            entry + direction * plan.reward_to_risk_3 * risk_distance
        )

        target_risk = equity * risk_percent / 100.0
        total_cost_rate = (
            self.engine.config.risk.fee_bps
            + self.engine.config.risk.slippage_bps
        ) / 10_000.0
        estimated_cost_per_unit = (
            entry + stop
        ) * total_cost_rate
        total_risk_per_unit = (
            risk_distance + estimated_cost_per_unit
        )
        quantity_by_risk = target_risk / total_risk_per_unit
        quantity_by_notional = (
            equity * maximum_notional_multiple / entry
        )
        quantity = min(
            quantity_by_risk, quantity_by_notional
        )
        if quantity <= 0 or not math.isfinite(quantity):
            return None, equity
        opening_fee = entry * quantity * fee_rate
        equity_after_fee = equity - opening_fee
        if equity_after_fee <= 0:
            return None, equity

        position = _Position(
            side=side,
            setup_type=plan.setup_type,
            signal_time=signal.generated_at,
            entry_time=row["timestamp"].to_pydatetime(),
            entry_price=entry,
            initial_stop=stop,
            stop=stop,
            target1=target1,
            target2=target2,
            target3=target3,
            original_quantity=quantity,
            remaining_quantity=quantity,
            initial_risk=quantity * total_risk_per_unit,
            opening_fee=opening_fee,
            accumulated_fees=opening_fee,
            highest_high=float(row["open"]),
            lowest_low=float(row["open"]),
        )
        return position, equity_after_fee

    def _open_futures_position(
        self,
        *,
        plan: EntryPlan,
        signal: MarketSignal,
        row: pd.Series,
        equity: float,
        risk_percent: float,
        maximum_notional_multiple: float,
        fee_rate: float,
        slippage_rate: float,
        spec: ContractSpec,
    ) -> tuple[_Position | None, float]:
        side = PositionSide(plan.side)
        low = decimal_value(plan.entry_low, "entry_low", positive=True)
        high = decimal_value(plan.entry_high, "entry_high", positive=True)
        raw = decimal_value(row["open"], "candle open", positive=True)
        if not low <= raw <= high:
            return None, equity
        entry = rounded_fill(raw, side, is_entry=True, slippage_rate=slippage_rate, spec=spec)
        if not low <= entry <= high:
            return None, equity
        long = side == PositionSide.LONG
        stop = round_step(decimal_value(plan.stop_loss, "initial stop", positive=True), spec.tick_size, up=not long)
        distance = entry - stop if long else stop - entry
        if stop <= 0 or distance <= 0:
            return None, equity
        direction = Decimal(1) if long else Decimal(-1)
        targets = [
            round_step(entry + direction * decimal_value(value, "target R", positive=True) * distance,
                       spec.tick_size, up=not long)
            for value in (plan.reward_to_risk_1, plan.reward_to_risk_2, plan.reward_to_risk_3)
        ]
        if min(targets) <= 0 or not (stop < entry < targets[0] < targets[1] < targets[2] if long
                else targets[2] < targets[1] < targets[0] < entry < stop):
            return None, equity
        stop_fill = rounded_fill(stop, side, is_entry=False, slippage_rate=slippage_rate, spec=spec)
        contracts, risk_per_unit = entry_contracts(
            spec, entry=entry, stop_fill=stop_fill, equity=equity, risk_percent=risk_percent,
            notional_multiple=maximum_notional_multiple, fee_rate=fee_rate,
        )
        if contracts <= 0:
            return None, equity
        quantity = contracts * spec.contract_size
        opening_fee = entry * quantity * decimal_value(fee_rate, "fee rate")
        after_fee = equity - float(opening_fee)
        if after_fee <= 0:
            return None, equity
        position = _Position(
            side=side, setup_type=plan.setup_type, signal_time=signal.generated_at,
            entry_time=row["timestamp"].to_pydatetime(), entry_price=float(entry),
            initial_stop=float(stop), stop=float(stop), target1=float(targets[0]),
            target2=float(targets[1]), target3=float(targets[2]),
            original_quantity=float(quantity), remaining_quantity=float(quantity),
            initial_risk=float(quantity * risk_per_unit), opening_fee=float(opening_fee),
            accumulated_fees=float(opening_fee), highest_high=float(row["open"]),
            lowest_low=float(row["open"]), contract_spec=spec,
            original_contracts=contracts, remaining_contracts=contracts,
        )
        return position, after_fee

    @staticmethod
    def _apply_funding(position: _Position, event: FundingSettlement, equity: float) -> float:
        if position.contract_spec is None or position.remaining_contracts is None:
            raise FuturesResearchError("invalid_futures_position", "Funding requires an exact remaining contract position.")
        quantity = position.remaining_contracts * position.contract_spec.contract_size
        cashflow = event.cashflow(position.side, quantity)
        position.funding_pnl += float(cashflow)
        position.funding_payments.append({
            **event.to_dict(), "contracts": str(position.remaining_contracts),
            "base_quantity": str(quantity), "cashflow": str(cashflow),
        })
        equity += float(cashflow)
        marked_equity = equity + (
            (float(event.mark_price) - position.entry_price) * float(quantity)
            * (1 if position.side == PositionSide.LONG else -1)
        )
        if not math.isfinite(equity) or not math.isfinite(marked_equity) or min(equity, marked_equity) <= 0:
            raise FuturesResearchError("unsupported_insolvency", "Funding/mark valuation exhausted modeled equity. Margin and liquidation mechanics are unavailable; this simulation cannot continue honestly.")
        return equity

    @staticmethod
    def _partial_exit_quantity(position: _Position, fraction: float, price: float, timestamp: datetime, reason: str) -> float | Decimal:
        if position.contract_spec is None:
            return min(position.remaining_quantity, position.original_quantity * fraction)
        spec = position.contract_spec
        if position.original_contracts is None or position.remaining_contracts is None:
            raise FuturesResearchError("invalid_futures_position", "Exact contract counts are missing.")
        contracts = min(position.remaining_contracts, round_step(
            position.original_contracts * decimal_value(fraction, "partial exit fraction"),
            spec.lot_size, up=False,
        ))
        remaining = position.remaining_contracts - contracts
        minimum, _ = contract_bounds(spec, decimal_value(price, "partial fill price", positive=True))
        try:
            closing_chunks(spec, contracts, decimal_value(price, "partial fill price", positive=True))
            if remaining and remaining < minimum:
                raise FuturesResearchError("unplaceable_contract_exit", "Partial exit would leave a below-minimum position.")
        except FuturesResearchError as exc:
            position.partial_exit_skips.append({"bar_open": timestamp.isoformat(), "reason": reason, "code": exc.code,
                                                "requested_contracts": str(contracts), "remaining_contracts": str(position.remaining_contracts)})
            return Decimal(0)
        return contracts * spec.contract_size

    def _manage_position(
        self,
        *,
        position: _Position,
        row: pd.Series,
        timestamp: datetime,
        equity: float,
        fee_rate: float,
        slippage_rate: float,
        bar_close: datetime | None = None,
        close_funding: FundingSettlement | None = None,
    ) -> tuple[
        _Position | None,
        float,
        BacktestTrade | None,
    ]:
        high = float(row["high"])
        low = float(row["low"])
        long_side = position.side == PositionSide.LONG
        stop_hit = (
            low <= position.stop
            if long_side
            else high >= position.stop
        )

        # Conservative treatment of ambiguous intrabar paths. A gap
        # through the stop fills from the opening price rather than the
        # unavailable stop level.
        if stop_hit:
            raw_stop_fill = position.stop
            if long_side and float(row["open"]) < position.stop:
                raw_stop_fill = float(row["open"])
            elif (
                not long_side
                and float(row["open"]) > position.stop
            ):
                raw_stop_fill = float(row["open"])
            fill = self._slipped_price(
                raw_stop_fill,
                position.side,
                is_entry=False,
                slippage_rate=slippage_rate,
                contract_spec=position.contract_spec,
            )
            equity = self._close_quantity(
                position,
                position.remaining_quantity,
                fill,
                fee_rate,
                equity,
                (
                    "stop_loss"
                    if not position.tp1_filled
                    else "trailing_or_break_even_stop"
                ),
                timestamp=timestamp,
            )
            return (
                None,
                equity,
                self._finalize_trade(position, timestamp),
            )

        target1_hit = (
            high >= position.target1
            if long_side
            else low <= position.target1
        )
        target2_hit = (
            high >= position.target2
            if long_side
            else low <= position.target2
        )
        target3_hit = (
            high >= position.target3
            if long_side
            else low <= position.target3
        )
        risk = self.engine.config.risk

        if target1_hit and not position.tp1_filled and not position.tp1_processed:
            fill = self._slipped_price(
                position.target1, position.side, is_entry=False,
                slippage_rate=slippage_rate, contract_spec=position.contract_spec,
            )
            quantity = self._partial_exit_quantity(position, risk.take_profit_1_fraction, fill, timestamp, "take_profit_1")
            if quantity > 0:
                equity = self._close_quantity(position, quantity, fill, fee_rate, equity, "take_profit_1", timestamp=timestamp)
                position.tp1_filled = True
                position.stop = position.entry_price
            position.tp1_processed = True

        if target2_hit and not position.tp2_filled and not position.tp2_processed and position.remaining_quantity > 0:
            fill = self._slipped_price(
                position.target2, position.side, is_entry=False,
                slippage_rate=slippage_rate, contract_spec=position.contract_spec,
            )
            quantity = self._partial_exit_quantity(position, risk.take_profit_2_fraction, fill, timestamp, "take_profit_2")
            if quantity > 0:
                equity = self._close_quantity(position, quantity, fill, fee_rate, equity, "take_profit_2", timestamp=timestamp)
                position.tp2_filled = True
            position.tp2_processed = True

        if target3_hit and position.remaining_quantity > 0:
            fill = self._slipped_price(
                position.target3,
                position.side,
                is_entry=False,
                slippage_rate=slippage_rate,
                contract_spec=position.contract_spec,
            )
            equity = self._close_quantity(
                position,
                position.remaining_quantity,
                fill,
                fee_rate,
                equity,
                "take_profit_3",
                timestamp=timestamp,
            )
            return (
                None,
                equity,
                self._finalize_trade(position, timestamp),
            )

        if (
            position.bars_held >= risk.max_holding_bars
            and position.remaining_quantity > 0
        ):
            if close_funding is not None:
                equity = self._apply_funding(position, close_funding, equity)
            fill = self._slipped_price(
                float(row["close"]),
                position.side,
                is_entry=False,
                slippage_rate=slippage_rate,
                contract_spec=position.contract_spec,
            )
            equity = self._close_quantity(
                position,
                position.remaining_quantity,
                fill,
                fee_rate,
                equity,
                "time_stop",
                timestamp=bar_close if position.contract_spec is not None and bar_close is not None else timestamp,
            )
            return (
                None,
                equity,
                self._finalize_trade(position, bar_close if position.contract_spec is not None and bar_close is not None else timestamp),
            )

        # Update a trailing stop only after the candle closes; it becomes
        # active on the next candle and cannot use an unknown intrabar path.
        if position.tp2_filled and position.remaining_quantity > 0:
            atr_value = float(row["atr"])
            if long_side:
                position.stop = max(
                    position.stop,
                    position.highest_high
                    - risk.trailing_atr_multiple * atr_value,
                )
            else:
                position.stop = min(
                    position.stop,
                    position.lowest_low
                    + risk.trailing_atr_multiple * atr_value,
                )

            if position.contract_spec is not None:
                position.stop = float(round_step(decimal_value(position.stop, "trailing stop", positive=True),
                                                 position.contract_spec.tick_size, up=not long_side))

        return position, equity, None

    @staticmethod
    def _slipped_price(
        price: float,
        side: PositionSide,
        *,
        is_entry: bool,
        slippage_rate: float,
        contract_spec: ContractSpec | None = None,
    ) -> float:
        if contract_spec is not None:
            return float(rounded_fill(price, side, is_entry=is_entry, slippage_rate=slippage_rate, spec=contract_spec))
        if side == PositionSide.LONG:
            multiplier = (
                1.0 + slippage_rate
                if is_entry
                else 1.0 - slippage_rate
            )
        else:
            multiplier = (
                1.0 - slippage_rate
                if is_entry
                else 1.0 + slippage_rate
            )
        return price * multiplier

    @staticmethod
    def _close_quantity(
        position: _Position,
        quantity: float | Decimal,
        exit_price: float,
        fee_rate: float,
        equity: float,
        reason: str,
        *,
        timestamp: datetime | None = None,
    ) -> float:
        if position.contract_spec is not None:
            spec = position.contract_spec
            if position.remaining_contracts is None or position.original_contracts is None or timestamp is None:
                raise FuturesResearchError("invalid_futures_position", "A contract close requires exact counts and an observation timestamp.")
            price = decimal_value(exit_price, "exit price", positive=True)
            if price % spec.tick_size != 0:
                raise FuturesResearchError("invalid_contract_exit", "Exit price is not aligned to the supplied tick size.")
            contracts = (
                position.remaining_contracts if quantity == position.remaining_quantity
                else decimal_value(quantity, "close quantity", positive=True) / spec.contract_size
            )
            contracts = min(contracts, position.remaining_contracts)
            chunks = closing_chunks(spec, contracts, price)
            direction = Decimal(1) if position.side == PositionSide.LONG else Decimal(-1)
            for chunk in chunks:
                base_quantity = chunk * spec.contract_size
                gross = direction * (price - Decimal(str(position.entry_price))) * base_quantity
                closing_fee = price * base_quantity * Decimal(str(fee_rate))
                position.gross_pnl += float(gross)
                position.accumulated_fees += float(closing_fee)
                position.closed_value_sum += float(price * base_quantity)
                position.remaining_contracts -= chunk
                position.remaining_quantity = float(position.remaining_contracts * spec.contract_size)
                position.closed_quantity = float((position.original_contracts - position.remaining_contracts) * spec.contract_size)
                position.last_exit_reason = reason
                position.exit_fills.append({
                    "timestamp": timestamp.isoformat(), "reason": reason,
                    "contracts": str(chunk), "base_quantity": str(base_quantity),
                    "price": str(price), "fee": str(closing_fee),
                    "timestamp_precision": "bar" if reason.startswith("take_profit") or "stop" in reason and reason != "time_stop" else "boundary",
                })
                equity += float(gross - closing_fee)
            if not math.isfinite(equity) or equity <= 0:
                raise FuturesResearchError("unsupported_insolvency", "Modeled equity is exhausted; an unmodeled liquidation must not be represented as an ordinary continuing backtest.")
            return equity
        quantity = min(float(quantity), position.remaining_quantity)
        if quantity <= 0:
            return equity
        if position.side == PositionSide.LONG:
            gross = (exit_price - position.entry_price) * quantity
        else:
            gross = (position.entry_price - exit_price) * quantity
        closing_fee = exit_price * quantity * fee_rate
        position.gross_pnl += gross
        position.accumulated_fees += closing_fee
        position.closed_value_sum += exit_price * quantity
        position.closed_quantity += quantity
        position.remaining_quantity -= quantity
        position.last_exit_reason = reason
        return equity + gross - closing_fee

    @staticmethod
    def _unrealized_pnl(
        position: _Position,
        price: float,
    ) -> float:
        if position.side == PositionSide.LONG:
            return (
                price - position.entry_price
            ) * position.remaining_quantity
        return (
            position.entry_price - price
        ) * position.remaining_quantity

    def _opposite_regime(
        self,
        side: PositionSide,
        regime: MarketRegime,
        score: float,
    ) -> bool:
        thresholds = self.engine.config.signals
        if side == PositionSide.LONG:
            return (
                regime
                in {
                    MarketRegime.BEARISH,
                    MarketRegime.STRONG_BEARISH,
                }
                or score <= thresholds.bearish_score
            )
        return (
            regime
            in {
                MarketRegime.BULLISH,
                MarketRegime.STRONG_BULLISH,
            }
            or score >= thresholds.bullish_score
        )

    @staticmethod
    def _finalize_trade(
        position: _Position,
        exit_time: datetime,
    ) -> BacktestTrade:
        average_exit = (
            position.closed_value_sum
            / position.closed_quantity
            if position.closed_quantity > 0
            else position.entry_price
        )
        net = position.gross_pnl - position.accumulated_fees + position.funding_pnl
        r_multiple = (
            net / position.initial_risk
            if position.initial_risk > 0
            else 0.0
        )
        return BacktestTrade(
            side=position.side,
            setup_type=position.setup_type,
            signal_time=position.signal_time,
            entry_time=position.entry_time,
            exit_time=exit_time,
            entry_price=position.entry_price,
            exit_price=average_exit,
            initial_stop=position.initial_stop,
            quantity=position.original_quantity,
            gross_pnl=position.gross_pnl,
            fees=position.accumulated_fees,
            net_pnl=net,
            initial_risk=position.initial_risk,
            r_multiple=r_multiple,
            exit_reason=(position.last_exit_reason or "unknown"),
            bars_held=position.bars_held,
            funding_pnl=position.funding_pnl if position.contract_spec is not None else None,
            funding_payments=tuple(position.funding_payments),
            contract_size=float(position.contract_spec.contract_size) if position.contract_spec is not None else None,
            entry_contracts=float(position.original_contracts) if position.original_contracts is not None else None,
            exit_fills=tuple(position.exit_fills),
            partial_exit_skips=tuple(position.partial_exit_skips),
        )

    @staticmethod
    def _build_result(
        *,
        symbol: str,
        execution_timeframe: str,
        starting_equity: float,
        ending_equity: float,
        trades: list[BacktestTrade],
        equity_curve: list[tuple[datetime, float]],
        simulation_metadata: dict | None = None,
    ) -> BacktestResult:
        net_pnl = ending_equity - starting_equity
        total_return = net_pnl / starting_equity * 100.0
        net_values = np.array(
            [trade.net_pnl for trade in trades],
            dtype=float,
        )
        r_values = np.array(
            [trade.r_multiple for trade in trades],
            dtype=float,
        )
        wins = int(np.count_nonzero(net_values > 0))
        losses = int(np.count_nonzero(net_values <= 0))
        trade_count = len(trades)
        win_rate = (
            wins / trade_count * 100.0
            if trade_count
            else 0.0
        )
        gross_profit = (
            float(net_values[net_values > 0].sum())
            if trade_count
            else 0.0
        )
        gross_loss = (
            float(-net_values[net_values < 0].sum())
            if trade_count
            else 0.0
        )
        if gross_loss > 0:
            profit_factor: float | None = (
                gross_profit / gross_loss
            )
        elif gross_profit > 0:
            profit_factor = None
        else:
            profit_factor = 0.0
        expectancy = (
            float(r_values.mean()) if trade_count else 0.0
        )

        curve_values = np.array(
            [value for _, value in equity_curve],
            dtype=float,
        )
        if len(curve_values):
            running_peak = np.maximum.accumulate(curve_values)
            drawdowns = np.where(
                running_peak > 0,
                (running_peak - curve_values)
                / running_peak
                * 100.0,
                0.0,
            )
            max_drawdown = float(np.nanmax(drawdowns))
        else:
            max_drawdown = 0.0

        return BacktestResult(
            symbol=symbol,
            execution_timeframe=execution_timeframe,
            starting_equity=starting_equity,
            ending_equity=ending_equity,
            net_pnl=net_pnl,
            total_return_percent=total_return,
            max_drawdown_percent=max_drawdown,
            trades=trade_count,
            wins=wins,
            losses=losses,
            win_rate_percent=win_rate,
            profit_factor=profit_factor,
            expectancy_r=expectancy,
            average_r=expectancy,
            trades_detail=tuple(trades),
            equity_curve=tuple(equity_curve),
            funding_pnl=(sum(trade.funding_pnl or 0.0 for trade in trades) if (simulation_metadata or {}).get("funding_modeled") else None),
            simulation_metadata=simulation_metadata or {},
        )
