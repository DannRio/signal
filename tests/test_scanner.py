from __future__ import annotations

from datetime import UTC, datetime

from signal_bot.models import (
    MarketRegime,
    MarketSignal,
    SetupType,
    SignalAction,
)
from signal_bot.scanner import (
    MarketCandidate,
    MarketScanner,
    ScanRequest,
)
from signal_bot.storage import SignalStore


class FakeUniverse:
    def discover(self, request):
        return [
            MarketCandidate("AAA/USDT", "AAA", "USDT", 1_000_000, 5.0, 10.0),
            MarketCandidate("BBB/USDT", "BBB", "USDT", 2_000_000, 4.0, 20.0),
        ]


class FakeService:
    def analyze_live(self, request, **kwargs):
        confirmed = request.symbol.startswith("BBB")
        return MarketSignal.create(
            symbol=request.symbol,
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            execution_timeframe=request.execution_timeframe,
            action=SignalAction.LONG if confirmed else SignalAction.WATCH_LONG,
            regime=MarketRegime.STRONG_BULLISH if confirmed else MarketRegime.BULLISH,
            score=80.0 if confirmed else 50.0,
            confidence=90.0 if confirmed else 70.0,
            consensus=1.0,
            setup_type=SetupType.BREAKOUT if confirmed else SetupType.NONE,
            trigger_price=21.0,
            entry_plan=None,
            timeframe_states=(),
            reasons=(),
            warnings=(),
        )


def test_scanner_ranks_confirmed_signals_first() -> None:
    store = SignalStore(":memory:")
    scanner = MarketScanner(
        store=store,
        universe_provider=FakeUniverse(),
        service_factory=lambda _engine: FakeService(),
    )
    report = scanner.scan(
        ScanRequest(
            exchange_id="fake",
            symbols=("AAA/USDT", "BBB/USDT"),
        )
    )
    assert report.items[0].candidate.symbol == "BBB/USDT"
    assert report.items[0].signal.action == SignalAction.LONG
    assert report.run_id is not None
    assert len(store.recent_signals()) == 2
