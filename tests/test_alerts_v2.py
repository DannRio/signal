from __future__ import annotations

from datetime import UTC, datetime

from signal_bot.alerts import AlertDispatcher, AlertPolicy
from signal_bot.models import (
    MarketRegime,
    MarketSignal,
    SetupType,
    SignalAction,
)
from signal_bot.storage import SignalStore


class CaptureSink:
    name = "capture"

    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)


def confirmed_signal() -> MarketSignal:
    return MarketSignal.create(
        symbol="BTC/USDT",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        execution_timeframe="15m",
        action=SignalAction.LONG,
        regime=MarketRegime.STRONG_BULLISH,
        score=80.0,
        confidence=90.0,
        consensus=1.0,
        setup_type=SetupType.BREAKOUT,
        trigger_price=100.0,
        entry_plan=None,
        timeframe_states=(),
        reasons=(),
        warnings=(),
    )


def test_dispatcher_sends_once_then_applies_cooldown() -> None:
    store = SignalStore(":memory:")
    sink = CaptureSink()
    dispatcher = AlertDispatcher(
        store,
        policy=AlertPolicy(cooldown_minutes=60),
        sinks=(sink,),
    )
    first = dispatcher.dispatch(
        confirmed_signal(), exchange_id="kraken", market_type="spot"
    )
    second = dispatcher.dispatch(
        confirmed_signal(), exchange_id="kraken", market_type="spot"
    )
    assert first.sent_to == ("capture",)
    assert second.sent_to == ()
    assert second.suppressed_reason == "Alert cooldown is active."
    assert len(sink.payloads) == 1
