from __future__ import annotations

from datetime import UTC, datetime, timedelta

from signal_bot.models import (
    MarketRegime,
    MarketSignal,
    SetupType,
    SignalAction,
)
from signal_bot.storage import SignalStore


def sample_signal() -> MarketSignal:
    return MarketSignal.create(
        symbol="BTC/USDT",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        execution_timeframe="15m",
        action=SignalAction.WATCH_LONG,
        regime=MarketRegime.BULLISH,
        score=55.0,
        confidence=70.0,
        consensus=0.8,
        setup_type=SetupType.NONE,
        trigger_price=101.0,
        entry_plan=None,
        timeframe_states=(),
        reasons=("Bullish alignment.",),
        warnings=(),
    )


def test_store_journals_and_deduplicates(tmp_path) -> None:
    store = SignalStore(tmp_path / "journal.db")
    signal = sample_signal()
    assert store.record_signal(
        signal,
        exchange_id="kraken",
        market_type="spot",
    )
    assert not store.record_signal(
        signal,
        exchange_id="kraken",
        market_type="spot",
    )
    rows = store.recent_signals()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC/USDT"

    metadata = {
        "dataset_key": "kraken|spot|BTC/USDT|15m",
        "exchange_id": "kraken",
        "market_type": "spot",
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "path": "data/history/btc.csv",
        "first_timestamp": "2025-01-01T00:00:00+00:00",
        "last_timestamp": "2025-02-01T00:00:00+00:00",
        "rows": 1000,
        "gaps": 0,
        "duplicates_removed": 2,
        "sha256": "abc",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    store.upsert_dataset(metadata)
    assert store.list_datasets()[0]["rows"] == 1000

    run_id = store.record_backtest(
        request={
            "symbol": "BTC/USDT",
            "execution_timeframe": "15m",
        },
        result={"total_return_percent": 4.2},
    )
    assert store.recent_backtests()[0]["run_id"] == run_id


def test_alert_cooldown_is_persistent() -> None:
    store = SignalStore(":memory:")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert store.alert_due("key", cooldown=timedelta(minutes=60), now=now)
    store.mark_alert("key", {"ok": True}, sent_at=now)
    assert not store.alert_due(
        "key",
        cooldown=timedelta(minutes=60),
        now=now + timedelta(minutes=30),
    )
    assert store.alert_due(
        "key",
        cooldown=timedelta(minutes=60),
        now=now + timedelta(minutes=61),
    )
