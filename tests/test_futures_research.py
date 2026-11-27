from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from test_futures import entry
from test_research import sample_result

from signal_bot.backtest import RegimeBacktester
from signal_bot.config import RegimeBotConfig
from signal_bot.engine import RegimeSignalEngine
from signal_bot.futures import ContractSpec
from signal_bot.futures_research import (
    FundingHistory,
    FundingSettlement,
    FuturesResearchError,
    FuturesResearchInputs,
    closing_chunks,
    rounded_fill,
)
from signal_bot.models import (
    MarketRegime,
    MarketSignal,
    PositionSide,
    SetupType,
    SignalAction,
    to_dict,
)
from signal_bot.research import ResearchService

SYMBOL = "BTC/USDT:USDT"


def candles(count=226):
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2024-01-01", periods=count, freq="15min", tz="UTC"
            ),
            "open": [100.0] * count,
            "high": [100.1] * count,
            "low": [99.9] * count,
            "close": [100.0] * count,
            "volume": [1000.0] * count,
        }
    )


def evidence(frame, events=(), **spec_changes):
    event_rows = [
        {
            "timestamp": timestamp.isoformat(),
            "rate": str(rate),
            "mark_price": str(mark),
            "source": "synthetic rate and historical mark fixture",
        }
        for timestamp, rate, mark in events
    ]
    return {
        "schema_version": 1,
        "contract_spec": {
            "symbol": SYMBOL,
            "contract_size": "1",
            "lot_size": "1",
            "tick_size": "0.1",
            "min_contracts": "1",
            **spec_changes,
        },
        "contract_spec_source": "synthetic fixed specification fixture",
        "contract_spec_assumption": "fixed_over_backtest_window",
        "funding": {
            "coverage_start": frame.iloc[0]["timestamp"].isoformat(),
            "coverage_end": (
                frame.iloc[-1]["timestamp"] + pd.Timedelta(minutes=15)
            ).isoformat(),
            "expected_settlement_timestamps": [row["timestamp"] for row in event_rows],
            "events": event_rows,
            "source": "synthetic settlement/mark evidence",
            "schedule_source": "explicit synthetic historical schedule; no assumed interval",
        },
    }


class ControlledEngine(RegimeSignalEngine):
    """One predictable entry exercises the production backtester accounting."""

    def __init__(
        self,
        side=PositionSide.LONG,
        *,
        fee_bps=0.0,
        slippage_bps=0.0,
        max_holding_bars=96,
    ):
        base = RegimeBotConfig()
        super().__init__(
            replace(
                base,
                risk=replace(
                    base.risk,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                    max_holding_bars=max_holding_bars,
                ),
            )
        )
        self.side = side
        self.seen = []

    def prepare_feature_frames(self, frames, execution_timeframe=None):
        return {timeframe: frame.assign(atr=1.0) for timeframe, frame in frames.items()}

    def analyze_prepared(self, **kwargs):
        self.seen.append(kwargs)
        frame = kwargs["feature_frames"]["15m"]
        confirmed = len(frame) == 220
        long = self.side == PositionSide.LONG
        plan = entry(
            self.side,
            reference_entry=100.0,
            entry_low=99.5,
            entry_high=100.5,
            stop_loss=95.0 if long else 105.0,
        )
        return MarketSignal.create(
            symbol=SYMBOL,
            generated_at=(
                frame.iloc[-1]["timestamp"] + pd.Timedelta(minutes=15)
            ).to_pydatetime(),
            execution_timeframe="15m",
            action=(SignalAction.LONG if long else SignalAction.SHORT)
            if confirmed
            else SignalAction.NO_TRADE,
            regime=MarketRegime.BULLISH if long else MarketRegime.BEARISH,
            score=70.0 if long else -70.0,
            confidence=90.0,
            consensus=1.0,
            setup_type=SetupType.BREAKOUT if confirmed else SetupType.NONE,
            trigger_price=100.0 if confirmed else None,
            entry_plan=plan if confirmed else None,
            timeframe_states=(),
            reasons=(),
            warnings=(),
        )


def run(frame, payload, engine=None):
    return RegimeBacktester(engine or ControlledEngine()).run(
        symbol=SYMBOL,
        frames={"15m": frame},
        execution_timeframe="15m",
        futures_inputs=payload,
    )


@pytest.mark.parametrize(
    "side,rate,expected",
    [
        (PositionSide.LONG, "0.001", "-1.50"),
        (PositionSide.SHORT, "0.001", "1.50"),
        (PositionSide.LONG, "-0.001", "1.50"),
        (PositionSide.SHORT, "-0.001", "-1.50"),
        (PositionSide.LONG, "0", "0"),
    ],
)
def test_settlement_uses_actual_mark_remaining_quantity_and_correct_direction(
    side, rate, expected
):
    event = FundingSettlement(
        datetime(2024, 1, 1, tzinfo=UTC),
        Decimal(rate),
        Decimal("250"),
        "known mark/rate",
    )
    assert event.cashflow(side, Decimal("6")) == Decimal(expected)


def test_inputs_roundtrip_retains_exact_decimal_evidence_and_fixed_spec_disclosure():
    frame = candles()
    payload = evidence(
        frame, [(frame.iloc[221]["timestamp"], "0.000123456789", "123.456789")]
    )
    inputs = FuturesResearchInputs.from_dict(payload)
    serialized = inputs.to_dict()
    assert serialized["funding"]["events"][0]["rate"] == "0.000123456789"
    assert FuturesResearchInputs.from_dict(serialized) == inputs
    metadata = inputs.metadata(settlements_in_window=1)
    assert metadata["contract_spec_assumption"] == "fixed_over_backtest_window"
    assert metadata["margin_and_liquidation_modeled"] is False
    assert metadata["live_ready"] is False
    json.dumps(metadata, allow_nan=False)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda p: p["funding"]["events"].clear(), "incomplete_funding_coverage"),
        (
            lambda p: p["funding"]["events"].append(p["funding"]["events"][0].copy()),
            "duplicate_funding_settlement",
        ),
        (
            lambda p: p["funding"]["events"][0].update(mark_price=None),
            "invalid_futures_evidence",
        ),
        (
            lambda p: p["funding"]["events"][0].update(rate="NaN"),
            "invalid_futures_evidence",
        ),
        (
            lambda p: p["funding"]["events"][0].update(source=""),
            "missing_futures_evidence",
        ),
        (lambda p: p["funding"].update(schedule_source=""), "missing_futures_evidence"),
        (
            lambda p: p.update(
                contract_spec_assumption="current_metadata_is_historical_truth"
            ),
            "unacknowledged_contract_assumption",
        ),
    ],
)
def test_missing_or_invalid_evidence_never_becomes_zero_funding(mutation, code):
    frame = candles()
    payload = evidence(frame, [(frame.iloc[221]["timestamp"], "0.001", "200")])
    mutation(payload)
    with pytest.raises(FuturesResearchError) as error:
        FuturesResearchInputs.from_dict(payload)
    assert error.value.code == code


def test_funding_coverage_and_symbol_must_match_entire_run():
    frame = candles()
    payload = evidence(frame)
    payload["funding"]["coverage_end"] = frame.iloc[-1]["timestamp"].isoformat()
    with pytest.raises(FuturesResearchError) as error:
        run(frame, payload)
    assert error.value.code == "incomplete_funding_coverage"
    payload = evidence(frame, symbol="ETH/USDT:USDT")
    with pytest.raises(FuturesResearchError) as error:
        run(frame, payload)
    assert error.value.code == "futures_symbol_mismatch"


def test_intrabar_settlement_is_rejected_instead_of_guessing_position_ownership():
    frame = candles()
    payload = evidence(
        frame,
        [(frame.iloc[221]["timestamp"] + pd.Timedelta(minutes=1), "0.001", "200")],
    )
    with pytest.raises(FuturesResearchError) as error:
        run(frame, payload)
    assert error.value.code == "unaligned_funding_settlement"


@pytest.mark.parametrize(
    "side,sign", [(PositionSide.LONG, -1), (PositionSide.SHORT, 1)]
)
def test_actual_funding_uses_remaining_contracts_and_terminal_settlement(side, sign):
    frame = candles()
    long = side == PositionSide.LONG
    frame.loc[221, ["open", "high", "low", "close"]] = (
        [100.5, 105.1, 100.1, 104.0] if long else [99.5, 99.9, 94.9, 96.0]
    )
    frame.loc[222:, ["open", "high", "low", "close"]] = (
        [104.0, 104.1, 103.9, 104.0] if long else [96.0, 96.1, 95.9, 96.0]
    )
    end = frame.iloc[-1]["timestamp"] + pd.Timedelta(minutes=15)
    payload = evidence(
        frame,
        [
            (
                frame.iloc[220]["timestamp"],
                "0.9",
                "300",
            ),  # Flat before new entry: no charge.
            (frame.iloc[221]["timestamp"], "0.001", "150"),  # Ten contracts before TP1.
            (
                frame.iloc[222]["timestamp"],
                "0.001",
                "200",
            ),  # Eight contracts after TP1.
            (end, "0.001", "250"),  # Eight still held before terminal close.
        ],
    )
    result = run(frame, payload, ControlledEngine(side))
    assert result.trades == 1
    trade = result.trades_detail[0]
    assert trade.entry_contracts == 10
    assert [payment["contracts"] for payment in trade.funding_payments] == [
        "10",
        "8",
        "8",
    ]
    assert [Decimal(payment["cashflow"]) for payment in trade.funding_payments] == [
        Decimal(sign) * value
        for value in (Decimal("1.5"), Decimal("1.6"), Decimal("2.0"))
    ]
    assert trade.funding_pnl == pytest.approx(sign * 5.1)
    assert result.funding_pnl == pytest.approx(sign * 5.1)
    assert trade.net_pnl == pytest.approx(
        trade.gross_pnl - trade.fees + trade.funding_pnl
    )
    assert result.ending_equity == pytest.approx(result.starting_equity + trade.net_pnl)
    assert [Decimal(fill["contracts"]) for fill in trade.exit_fills] == [
        Decimal(2),
        Decimal(8),
    ]
    assert trade.exit_time == end
    assert result.simulation_metadata["funding_payments_applied"] == 3
    json.dumps(to_dict(result), allow_nan=False)


def test_future_funding_values_do_not_change_prior_signal_or_entry_sizing():
    frame = candles()
    timestamp = frame.iloc[224]["timestamp"]
    first_engine = ControlledEngine()
    first = run(frame, evidence(frame, [(timestamp, "0.001", "200")]), first_engine)
    second_engine = ControlledEngine()
    second = run(frame, evidence(frame, [(timestamp, "0.01", "200")]), second_engine)
    a, b = first.trades_detail[0], second.trades_detail[0]
    assert (a.entry_time, a.entry_price, a.entry_contracts, a.initial_risk) == (
        b.entry_time,
        b.entry_price,
        b.entry_contracts,
        b.initial_risk,
    )
    assert first_engine.seen[0].keys() == second_engine.seen[0].keys()
    assert "funding" not in first_engine.seen[0]
    assert "futures_inputs" not in first_engine.seen[0]
    assert first.funding_pnl != second.funding_pnl


def test_partial_below_minimum_is_skipped_without_fictitious_break_even_move():
    frame = candles()
    frame.loc[221, ["open", "high", "low", "close"]] = [100.0, 105.1, 99.9, 104.0]
    # This would hit a fabricated break-even stop, but not the original stop.
    frame.loc[222, ["open", "high", "low", "close"]] = [100.0, 100.1, 99.0, 100.0]
    result = run(frame, evidence(frame, min_contracts="3"))
    trade = result.trades_detail[0]
    assert trade.entry_contracts == 10
    assert trade.exit_reason == "end_of_data"
    assert len(trade.exit_fills) == 1
    assert Decimal(trade.exit_fills[0]["contracts"]) == 10
    assert trade.partial_exit_skips[0]["reason"] == "take_profit_1"


@pytest.mark.parametrize("side", [PositionSide.LONG, PositionSide.SHORT])
def test_gap_stops_and_slippage_are_adverse_and_tick_valid(side):
    frame = candles()
    frame.loc[221:, ["open", "high", "low", "close"]] = (
        [90.0, 91.0, 89.0, 90.0]
        if side == PositionSide.LONG
        else [110.0, 111.0, 109.0, 110.0]
    )
    result = run(
        frame, evidence(frame), ControlledEngine(side, fee_bps=10.0, slippage_bps=10.0)
    )
    trade = result.trades_detail[0]
    assert trade.exit_reason == "stop_loss"
    assert (
        trade.exit_price < 90.0
        if side == PositionSide.LONG
        else trade.exit_price > 110.0
    )
    assert Decimal(str(trade.entry_price)) % Decimal("0.1") == 0
    assert all(
        Decimal(fill["price"]) % Decimal("0.1") == 0 for fill in trade.exit_fills
    )
    assert trade.initial_risk <= 50.0
    assert (
        -trade.net_pnl > trade.initial_risk
    )  # A gap can exceed the planned stop risk.
    assert trade.fees > 0


def test_unaffordable_minimum_contract_does_not_create_fractional_position():
    frame = candles()
    result = run(frame, evidence(frame, contract_size="100"))
    assert result.trades == 0
    assert result.simulation_metadata["unfilled_entry_attempts"] == 1
    assert result.funding_pnl == 0.0


def test_short_entry_cannot_create_nonpositive_contract_target_prices():
    class WideStopEngine(ControlledEngine):
        def analyze_prepared(self, **kwargs):
            signal = super().analyze_prepared(**kwargs)
            if signal.entry_plan is not None:
                return replace(
                    signal, entry_plan=replace(signal.entry_plan, stop_loss=150.0)
                )
            return signal

    frame = candles()
    result = run(frame, evidence(frame), WideStopEngine(PositionSide.SHORT))
    assert result.trades == 0
    assert result.simulation_metadata["unfilled_entry_attempts"] == 1


def test_closing_orders_split_with_valid_lots_and_no_invalid_remainder():
    spec = ContractSpec(
        SYMBOL,
        Decimal(1),
        Decimal(1),
        Decimal("0.1"),
        min_contracts=Decimal(3),
        max_contracts=Decimal(7),
    )
    assert closing_chunks(spec, Decimal(16), Decimal(100)) == (
        Decimal(7),
        Decimal(6),
        Decimal(3),
    )
    with pytest.raises(FuturesResearchError):
        closing_chunks(spec, Decimal("2.5"), Decimal(100))
    impossible = replace(spec, min_contracts=Decimal(4), max_contracts=Decimal(5))
    with pytest.raises(FuturesResearchError):
        closing_chunks(impossible, Decimal(6), Decimal(100))


@pytest.mark.parametrize(
    "side,is_entry,expected",
    [
        (PositionSide.LONG, True, "100.1"),
        (PositionSide.SHORT, True, "100.0"),
        (PositionSide.LONG, False, "100.0"),
        (PositionSide.SHORT, False, "100.1"),
    ],
)
def test_tick_rounding_is_always_adverse_for_order_direction(side, is_entry, expected):
    spec = ContractSpec(SYMBOL, Decimal(1), Decimal(1), Decimal("0.1"))
    assert rounded_fill(
        "100.04", side, is_entry=is_entry, slippage_rate=0, spec=spec
    ) == Decimal(expected)


def test_funding_insolvency_is_explicit_without_inventing_liquidation():
    frame = candles()
    payload = evidence(frame, [(frame.iloc[221]["timestamp"], "10", "1000")])
    with pytest.raises(FuturesResearchError) as error:
        run(frame, payload)
    assert error.value.code == "unsupported_insolvency"


def test_legacy_backtest_explicitly_reports_funding_unmodeled():
    frame = candles()
    result = RegimeBacktester(ControlledEngine()).run(
        symbol=SYMBOL, frames={"15m": frame}, execution_timeframe="15m"
    )
    assert result.funding_pnl is None
    assert result.trades_detail[0].funding_pnl is None
    assert result.simulation_metadata["funding_modeled"] is False
    assert result.simulation_metadata["contract_rounding_modeled"] is False


def test_research_backtest_accepts_validated_futures_inputs():
    frame = candles()
    result = ResearchService().backtest(
        symbol=SYMBOL,
        frames={"15m": frame},
        execution_timeframe="15m",
        futures_inputs=evidence(frame),
    )
    assert result.simulation_metadata["funding_modeled"] is True
    assert result.simulation_metadata["contract_rounding_modeled"] is True


@pytest.mark.parametrize(
    "method", ["chronological_oos", "cost_stress", "walk_forward", "full_validation"]
)
def test_every_research_path_forwards_same_futures_evidence(monkeypatch, method):
    frame = candles(1500)
    payload = evidence(frame)
    service = ResearchService()
    calls = []

    def fake_backtest(**kwargs):
        inputs = kwargs.get("futures_inputs")
        assert inputs is not None
        validated = (
            inputs
            if isinstance(inputs, FuturesResearchInputs)
            else FuturesResearchInputs.from_dict(inputs)
        )
        calls.append(validated.to_dict())
        return replace(
            sample_result(),
            funding_pnl=0.0,
            simulation_metadata=validated.metadata(settlements_in_window=0),
        )

    monkeypatch.setattr(service, "backtest", fake_backtest)
    kwargs = {
        "symbol": SYMBOL,
        "frames": {"15m": frame},
        "execution_timeframe": "15m",
        "futures_inputs": payload,
    }
    if method == "walk_forward":
        kwargs.update(window_bars=300, step_bars=300)
    if method == "full_validation":
        kwargs.update(monte_carlo_simulations=100)
    output = getattr(service, method)(**kwargs)
    assert calls
    assert all(
        value == FuturesResearchInputs.from_dict(payload).to_dict() for value in calls
    )
    if method == "full_validation":
        assert output["gate"]["live_ready"] is False
        assert output["gate"]["simulation_metadata"]["funding_modeled"] is True
        assert not any(
            "Historical funding settlements" in item
            for item in output["gate"]["limitations"]
        )


def test_funding_event_changes_input_provenance():
    frame = candles()
    payload = evidence(frame, [(frame.iloc[221]["timestamp"], "0.001", "200")])
    first = FuturesResearchInputs.from_dict(payload).metadata(settlements_in_window=1)
    changed = copy.deepcopy(payload)
    changed["funding"]["events"][0]["mark_price"] = "201"
    second = FuturesResearchInputs.from_dict(changed).metadata(settlements_in_window=1)
    assert first["inputs_sha256"] != second["inputs_sha256"]


def test_funding_schedule_can_change_intervals_without_current_rate_substitution():
    start = datetime(2024, 1, 1, tzinfo=UTC)
    times = (
        start + timedelta(hours=1),
        start + timedelta(hours=5),
        start + timedelta(hours=13),
    )
    history = FundingHistory(
        start,
        start + timedelta(hours=16),
        times,
        tuple(
            FundingSettlement(
                t, Decimal("0.001"), Decimal(100), "actual historical observations"
            )
            for t in times
        ),
        "historical events",
        "documented interval changes",
    )
    assert history.expected_settlement_timestamps == times
    assert (
        len(
            history.events_for_window(
                start,
                start + timedelta(hours=16),
                [start + timedelta(hours=i) for i in range(17)],
            )
        )
        == 3
    )


def test_time_stop_pays_same_boundary_funding_once_before_closing():
    frame = candles()
    settlement = frame.iloc[221]["timestamp"]
    result = run(
        frame,
        evidence(frame, [(settlement, "0.001", "150")]),
        ControlledEngine(max_holding_bars=1),
    )
    trade = result.trades_detail[0]
    assert trade.exit_reason == "time_stop"
    assert trade.exit_time == settlement
    assert len(trade.funding_payments) == 1
    assert trade.funding_pnl == pytest.approx(-1.5)
    assert result.ending_equity == pytest.approx(9998.5)


@pytest.mark.parametrize("method", ["chronological_oos", "walk_forward"])
def test_futures_research_cannot_hide_conflicting_duplicates_during_slicing(method):
    frame = candles(1500)
    duplicate = frame.iloc[[500]].copy()
    duplicate["volume"] += 1
    damaged = pd.concat([frame, duplicate], ignore_index=True)
    kwargs = {
        "symbol": SYMBOL,
        "frames": {"15m": damaged},
        "execution_timeframe": "15m",
        "futures_inputs": evidence(frame),
    }
    if method == "walk_forward":
        kwargs.update(window_bars=300, step_bars=300)
    with pytest.raises(ValueError, match="conflicting duplicate"):
        getattr(ResearchService(), method)(**kwargs)
