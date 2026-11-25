from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from signal_bot.futures import ContractSpec
from signal_bot.models import (
    EntryPlan,
    MarketRegime,
    MarketSignal,
    PositionSide,
    SetupType,
    SignalAction,
)
from signal_bot.scanner import MarketCandidate, MarketScanner, ScanRequest
from signal_bot.storage import SignalStore

NOW = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
DECISION_CLOSE = datetime(2026, 1, 1, tzinfo=UTC)
AAA = "AAA/USDT:USDT"
BBB = "BBB/USDT:USDT"


def entry_plan() -> EntryPlan:
    return EntryPlan(
        side=PositionSide.LONG, setup_type=SetupType.BREAKOUT,
        trigger_price=100.0, entry_low=99.8, entry_high=100.2,
        reference_entry=100.0, stop_loss=95.0,
        take_profit_1=105.0, take_profit_2=110.0, take_profit_3=115.0,
        risk_per_unit=5.0, target_risk_amount=60.0,
        actual_risk_amount=52.925, position_size=10.0, notional_value=1000.0,
        estimated_round_trip_cost=2.925,
        reward_to_risk_1=1.0, reward_to_risk_2=2.0, reward_to_risk_3=3.0,
        move_stop_to_break_even_after=1.0, trailing_stop_after=2.0,
        trailing_atr_multiple=2.0, max_holding_bars=96,
    )


def make_candidate(symbol: str = AAA, **changes) -> MarketCandidate:
    candidate = MarketCandidate(
        symbol=symbol, base=symbol.split("/")[0], quote="USDT",
        quote_volume=2_000_000.0, spread_bps=2.0, last_price=100.0,
        bid=99.99, ask=100.01, quote_timestamp=NOW.isoformat(), observed_at=NOW.isoformat(),
        metadata_observed_at=NOW.isoformat(), volume_source="contracts/active.turnoverOf24h",
        contract_spec=ContractSpec(symbol, "0.001", "1", "0.01", max_contracts="100000"),
    )
    return replace(candidate, **changes)


def make_signal(symbol: str = AAA, *, action=SignalAction.LONG, confidence=90.0, generated_at=DECISION_CLOSE) -> MarketSignal:
    confirmed = action in {SignalAction.LONG, SignalAction.SHORT}
    return MarketSignal.create(
        symbol=symbol, generated_at=generated_at, execution_timeframe="15m",
        action=action, regime=MarketRegime.BULLISH, score=80.0, confidence=confidence,
        consensus=1.0, setup_type=SetupType.BREAKOUT if confirmed else SetupType.NONE,
        trigger_price=100.0, entry_plan=entry_plan() if confirmed else None,
        timeframe_states=(), reasons=(), warnings=(),
    )


class FakeUniverse:
    def __init__(self, candidates=None):
        self.candidates = candidates if candidates is not None else [make_candidate(AAA), make_candidate(BBB)]
        self.requests = []

    def discover(self, request):
        self.requests.append(request)
        return list(self.candidates)


class FakeService:
    def __init__(self, signals=None, errors=None):
        self.signals = signals or {}
        self.errors = errors or {}
        self.requests = []
        self.last_frame_metadata = {}

    def analyze_live(self, request, **kwargs):
        self.requests.append((request, kwargs))
        self.last_frame_metadata = {}
        if request.symbol in self.errors:
            raise self.errors[request.symbol]
        self.last_frame_metadata = {
            "15m": {"rows": 500, "last_close": DECISION_CLOSE.isoformat(), "sha256": "a" * 64}
        }
        return self.signals.get(request.symbol) or make_signal(
            request.symbol,
            action=SignalAction.LONG if request.symbol == BBB else SignalAction.WATCH_LONG,
        )


def test_scanner_ranks_confirmed_signals_first() -> None:
    store = SignalStore(":memory:")
    service = FakeService()
    scanner = MarketScanner(
        store=store, universe_provider=FakeUniverse(),
        service_factory=lambda _engine: service, now_factory=lambda: NOW,
    )
    report = scanner.scan(ScanRequest(exchange_id="kucoinfutures", symbols=(AAA, BBB)))
    assert report.items[0].candidate.symbol == BBB
    assert report.items[0].signal.action == SignalAction.LONG
    assert report.items[0].actionable
    assert report.status == "complete"
    assert report.analyzed_markets == 2
    assert report.run_id is not None
    assert len(store.recent_signals()) == 2
    assert all(kwargs["as_of"] == NOW for _, kwargs in service.requests)
