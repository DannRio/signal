from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from signal_bot.candle_cache import CandleCache, CandleKey
from signal_bot.engine import RegimeSignalEngine
from signal_bot.models import to_dict
from signal_bot.service import LiveMarketRequest, RegimeBotService
from signal_bot.storage import SignalStore
from signal_bot.validation import (
    DataValidationError,
    normalize_ccxt_ohlcv,
    timeframe_to_timedelta,
)

NOW = pd.Timestamp("2026-01-01T00:01:00Z")
SYMBOL = "BTC/USDT:USDT"
KEY = CandleKey("kucoinfutures", "swap", SYMBOL, "15m")


def request(**changes):
    values = {
        "exchange_id": "kucoinfutures", "market_type": "swap", "symbol": SYMBOL,
        "execution_timeframe": "15m", "context_timeframes": ("1h", "4h"),
        "limit": 500, "maximum_staleness_bars": 1,
    }
    values.update(changes)
    return LiveMarketRequest(**values)


class ClockedCandles:
    timeframes = {"15m", "1h", "4h"}

    def __init__(self, *, cap=200):
        self.now = NOW
        self.cap = cap
        self.calls = []
        self.rows = {}
        for timeframe in self.timeframes:
            duration = timeframe_to_timedelta(timeframe)
            start = NOW.floor(duration) - 650 * duration
            times = pd.date_range(start=start, periods=1000, freq=duration)
            self.rows[timeframe] = [
                [int(timestamp.timestamp() * 1000), 100 + index * 0.1,
                 101 + index * 0.1, 99 + index * 0.1, 100.2 + index * 0.1, 1000.0]
                for index, timestamp in enumerate(times)
            ]

    def load_markets(self):
        return {SYMBOL: {"active": True}, "ETH/USDT:USDT": {"active": True}}

    def fetch_ohlcv(self, symbol, timeframe, limit, since=None):
        self.calls.append((symbol, timeframe, since, limit))
        end_ms = int(self.now.timestamp() * 1000)
        rows = [deepcopy(row) for row in self.rows[timeframe] if since <= row[0] <= end_ms]
        return rows[: min(limit, self.cap)]


class CountingEngine(RegimeSignalEngine):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def analyze(self, **kwargs):
        self.calls += 1
        return super().analyze(**kwargs)


def make_service(client, cache=None, engine=None):
    return RegimeBotService(
        engine, cache=cache, client_factory=lambda *_: client, now_factory=lambda: client.now,
    )


def test_warm_cache_avoids_ohlcv_until_each_timeframe_closes():
    client = ClockedCandles()
    cache = CandleCache(SignalStore(":memory:"))
    service = make_service(client, cache)
    initial = service.fetch_frames_result(request())
    initial_calls = len(client.calls)
    assert initial_calls == 9
    assert all(len(frame) == 500 for frame in initial.frames.values())
    assert cache.stats()["rows"] == 1500
    again = service.fetch_frames_result(request())
    assert len(client.calls) == initial_calls
    assert again.metadata == initial.metadata
    assert all(info["status"] == "hit" for info in again.cache_stats.values())
    assert again.metrics["counters"]["cache_hits"] == 3

    client.now += pd.Timedelta(minutes=15)
    next_result = service.fetch_frames_result(request())
    assert Counter(call[1] for call in client.calls[initial_calls:]) == {"15m": 1}
    assert next_result.cache_stats["15m"]["status"] == "incremental"
    assert next_result.cache_stats["1h"]["status"] == "hit"
    assert next_result.cache_stats["4h"]["status"] == "hit"
    assert next_result.metadata["1h"] == initial.metadata["1h"]
    assert next_result.metadata["15m"]["last_close"] == "2026-01-01T00:15:00+00:00"
    last_open = int(initial.frames["15m"].iloc[-1]["timestamp"].timestamp() * 1000)
    assert client.calls[-1][2] == last_open - 15 * 60 * 1000


def test_hour_and_four_hour_rollovers_refresh_only_due_series():
    client = ClockedCandles()
    service = make_service(client, CandleCache(SignalStore(":memory:")))
    service.fetch_frames_result(request())
    before = len(client.calls)
    client.now = NOW + pd.Timedelta(hours=1)
    service.fetch_frames_result(request())
    assert Counter(call[1] for call in client.calls[before:]) == {"15m": 1, "1h": 1}
    before = len(client.calls)
    client.now = NOW + pd.Timedelta(hours=4)
    service.fetch_frames_result(request())
    assert set(call[1] for call in client.calls[before:]) == {"15m", "1h", "4h"}


def test_persistent_cache_survives_restart_and_catches_up(tmp_path):
    path = tmp_path / "journal.db"
    client = ClockedCandles()
    original_store = SignalStore(path)
    first = make_service(client, CandleCache(original_store)).fetch_frames_result(request())
    original_store.close()
    client.calls.clear()
    restarted = make_service(client, CandleCache(SignalStore(path)))
    restored = restarted.fetch_frames_result(request())
    assert client.calls == []
    assert restored.metadata == first.metadata
    client.now += pd.Timedelta(hours=2)
    caught_up = restarted.fetch_frames_result(request())
    assert caught_up.metadata["15m"]["last_close"] == "2026-01-01T02:00:00+00:00"
    assert caught_up.metadata["1h"]["last_close"] == "2026-01-01T02:00:00+00:00"
    assert Counter(call[1] for call in client.calls) == {"15m": 1, "1h": 1}


@pytest.mark.parametrize("limit", [220, 300, 500])
def test_uncached_bootstrap_hit_and_incremental_results_have_identical_signals(limit):
    cached_client = ClockedCandles()
    fresh_client = ClockedCandles()
    cached = make_service(cached_client, CandleCache(SignalStore(":memory:")))
    fresh = make_service(fresh_client)
    chosen = request(limit=limit)
    for offset in (0, 0, 15, 60, 240):
        cached_client.now = fresh_client.now = NOW + pd.Timedelta(minutes=offset)
        left = cached.analyze_live_result(chosen)
        right = fresh.analyze_live_result(chosen)
        assert to_dict(left.signal) == to_dict(right.signal)
        assert left.metadata == right.metadata
        assert all(len(frame) == limit for frame in left.frames.values())


def test_smaller_then_larger_window_bootstraps_missing_history_and_preserves_exact_tail():
    client = ClockedCandles()
    service = make_service(client, CandleCache(SignalStore(":memory:"), retention_bars=220))
    small = service.fetch_frames_result(request(limit=220))
    assert len(small.frames["15m"]) == 220
    before = len(client.calls)
    large = service.fetch_frames_result(request(limit=500))
    assert len(client.calls) > before
    assert all(len(frame) == 500 for frame in large.frames.values())
    before = len(client.calls)
    small_again = service.fetch_frames_result(request(limit=300))
    assert len(client.calls) == before
    pd.testing.assert_frame_equal(small_again.frames["15m"], large.frames["15m"].tail(300).reset_index(drop=True))


def test_conflicting_closed_overlap_fails_then_next_request_explicitly_bootstraps():
    client = ClockedCandles()
    cache = CandleCache(SignalStore(":memory:"))
    service = make_service(client, cache)
    chosen = request(context_timeframes=())
    first = service.fetch_frames_result(chosen)
    last_open = int(first.frames["15m"].iloc[-1]["timestamp"].timestamp() * 1000)
    for row in client.rows["15m"]:
        if row[0] == last_open:
            row[4] += 0.1
    client.now += pd.Timedelta(minutes=15)
    with pytest.raises(DataValidationError) as error:
        service.fetch_frames_result(chosen)
    assert error.value.code == "cache_conflict"
    assert cache.read(KEY, as_of=client.now, limit=500).frame.empty
    assert service.last_frame_metadata == {}
    recovered = service.fetch_frames_result(chosen)
    assert recovered.cache_stats["15m"]["recovery_reason"] == "conflicting_closed_candle"
    assert recovered.cache_stats["15m"]["status"] == "bootstrap"
    assert len(recovered.frames["15m"]) == 500


@pytest.mark.parametrize("corruption", ["gap", "invalid_ohlc"])
def test_corrupt_cache_is_revalidated_and_repaired_from_exchange(corruption):
    client = ClockedCandles()
    store = SignalStore(":memory:")
    cache = CandleCache(store)
    service = make_service(client, cache)
    chosen = request(context_timeframes=())
    original = service.fetch_frames_result(chosen)
    timestamp = int(original.frames["15m"].iloc[-20]["timestamp"].timestamp() * 1000)
    with store._connection() as connection:
        if corruption == "gap":
            connection.execute("DELETE FROM candle_cache WHERE open_ms=?", (timestamp,))
        else:
            connection.execute("UPDATE candle_cache SET low=high+1 WHERE open_ms=?", (timestamp,))
    before = len(client.calls)
    repaired = service.fetch_frames_result(chosen)
    assert len(client.calls) > before
    assert repaired.metadata == original.metadata
    assert repaired.cache_stats["15m"]["status"] == "recovery"
    assert repaired.metrics["counters"]["cache_invalidations"] == 1


def test_delayed_publication_is_retried_instead_of_becoming_a_cache_hit():
    client = ClockedCandles()
    duration_ms = 15 * 60 * 1000
    expected_open = int(NOW.floor("15min").timestamp() * 1000) - duration_ms
    hidden = next(row for row in client.rows["15m"] if row[0] == expected_open)
    client.rows["15m"] = [row for row in client.rows["15m"] if row[0] < expected_open]
    # At an exact boundary, the previous candle is one tolerated bar old.
    client.now = NOW.floor("15min")
    cache = CandleCache(SignalStore(":memory:"))
    service = make_service(client, cache)
    chosen = request(context_timeframes=())
    stale = service.fetch_frames_result(chosen)
    assert stale.frames["15m"].iloc[-1]["timestamp"] < pd.Timestamp(expected_open, unit="ms", tz="UTC")
    before = len(client.calls)
    client.rows["15m"].append(hidden)
    current = service.fetch_frames_result(chosen)
    assert len(client.calls) > before
    assert current.frames["15m"].iloc[-1]["timestamp"] == pd.Timestamp(expected_open, unit="ms", tz="UTC")
    before = len(client.calls)
    service.fetch_frames_result(chosen)
    assert len(client.calls) == before


@pytest.mark.parametrize("use_cache", [False, True])
@pytest.mark.parametrize("failure", ["gap", "bad_price", "stale"])
def test_bad_new_exchange_data_fails_with_or_without_cache(use_cache, failure):
    client = ClockedCandles()
    cache = CandleCache(SignalStore(":memory:")) if use_cache else None
    service = make_service(client, cache)
    chosen = request(context_timeframes=())
    service.fetch_frames_result(chosen)
    client.now = NOW + pd.Timedelta(minutes=45)
    target = int((NOW.floor("15min") + pd.Timedelta(minutes=15)).timestamp() * 1000)
    if failure == "gap":
        client.rows["15m"] = [row for row in client.rows["15m"] if row[0] != target]
    elif failure == "bad_price":
        next(row for row in client.rows["15m"] if row[0] == target)[2] = -1
    else:
        client.rows["15m"] = [row for row in client.rows["15m"] if row[0] < int(NOW.floor("15min").timestamp() * 1000)]
    with pytest.raises(DataValidationError) as error:
        service.fetch_frames_result(chosen)
    assert error.value.code == {"gap": "candle_gap", "bad_price": "invalid_ohlcv", "stale": "stale_candles"}[failure]
    assert service.last_frame_metadata == {}


def test_cache_retention_never_shrinks_below_largest_requested_window():
    client = ClockedCandles()
    cache = CandleCache(SignalStore(":memory:"), retention_bars=250)
    service = make_service(client, cache)
    chosen = request(context_timeframes=(), limit=300)
    service.fetch_frames_result(chosen)
    for offset in (15, 30, 45):
        client.now = NOW + pd.Timedelta(minutes=offset)
        service.fetch_frames_result(chosen)
        assert cache.stats()["rows"] == 300
    cached = cache.read(KEY, as_of=client.now, limit=500)
    assert len(cached.frame) == 300
    assert cached.requested_limit == 300


def test_cache_rejects_open_rows_and_invalid_atomic_batches():
    client = ClockedCandles()
    cache = CandleCache(SignalStore(":memory:"))
    frame = normalize_ccxt_ohlcv(client.rows["15m"][-2:])
    with pytest.raises(DataValidationError) as error:
        cache.write_validated(KEY, frame, as_of=NOW, requested_limit=500)
    assert error.value.code == "future_candle"
    assert cache.stats()["rows"] == 0
    good = make_service(client).fetch_frames(request(context_timeframes=()))["15m"]
    cache.write_validated(KEY, good, as_of=NOW, requested_limit=500)
    bad = good.copy()
    bad.loc[bad.index[-1], "volume"] = -1
    with pytest.raises(DataValidationError):
        cache.write_validated(KEY, bad, as_of=NOW, requested_limit=500)
    pd.testing.assert_frame_equal(cache.read(KEY, as_of=NOW, limit=500).frame, good)


def test_same_symbol_isolated_by_venue_market_and_timeframe_keys():
    client = ClockedCandles()
    cache = CandleCache(SignalStore(":memory:"))
    frame = make_service(client).fetch_frames(request(context_timeframes=()))["15m"]
    cache.write_validated(KEY, frame, as_of=NOW, requested_limit=500)
    other = replace(KEY, exchange_id="another")
    assert cache.read(other, as_of=NOW, limit=500).frame.empty
    assert cache.read(replace(KEY, market_type="spot"), as_of=NOW, limit=500).frame.empty
    assert cache.read(replace(KEY, symbol="ETH/USDT:USDT"), as_of=NOW, limit=500).frame.empty
    assert cache.read(replace(KEY, timeframe="1h"), as_of=NOW, limit=500).frame.empty


def test_signal_reuse_is_bounded_and_invalidated_by_inputs_or_risk_settings():
    client = ClockedCandles()
    engine = CountingEngine()
    service = make_service(client, CandleCache(SignalStore(":memory:")), engine)
    chosen = request(context_timeframes=())
    first = service.analyze_live_result(chosen)
    assert engine.calls == 1
    repeated = service.analyze_live_result(chosen)
    assert engine.calls == 1
    assert repeated.cache_stats["analysis"]["status"] == "hit"
    assert repeated.metadata == first.metadata
    assert repeated.metrics["counters"]["signal_cache_hits"] == 1
    service.analyze_live_result(chosen, equity=20_000)
    assert engine.calls == 2
    service.analyze_live_result(chosen, risk_percent=0.75)
    assert engine.calls == 3
    client.now += pd.Timedelta(minutes=15)
    service.analyze_live_result(chosen)
    assert engine.calls == 4
    service.engine.config = replace(service.engine.config, signals=replace(service.engine.config.signals, minimum_volume_ratio=1.2))
    service.analyze_live_result(chosen)
    assert engine.calls == 5

    service.signal_cache_size = 1
    service.analyze_live_result(chosen, equity=30_000)
    assert len(service._signals) == 1
    service.analyze_live_result(chosen, equity=40_000)
    assert len(service._signals) == 1


def test_explicit_results_keep_frame_metadata_when_later_calls_fail():
    client = ClockedCandles()
    service = make_service(client, CandleCache(SignalStore(":memory:")))
    result = service.analyze_live_result(request())
    original = deepcopy(result.metadata)
    with pytest.raises(ValueError):
        service.fetch_frames_result(request(symbol="MISSING/USDT:USDT"))
    assert service.last_frame_metadata == {}
    assert result.metadata == original


def test_open_candle_mode_bypasses_closed_only_cache():
    client = ClockedCandles()
    cache = CandleCache(SignalStore(":memory:"))
    service = make_service(client, cache)
    result = service.fetch_frames_result(request(context_timeframes=(), closed_candles_only=False))
    assert result.cache_stats["15m"]["status"] == "disabled"
    assert cache.stats()["rows"] == 0
    assert result.frames["15m"].iloc[-1]["timestamp"] == NOW.floor("15min")


def test_earlier_frozen_cutoff_does_not_reuse_a_truncated_cached_window():
    client = ClockedCandles()
    service = make_service(client, CandleCache(SignalStore(":memory:")))
    chosen = request(context_timeframes=())
    service.fetch_frames_result(chosen)
    before = len(client.calls)
    cutoff = NOW - pd.Timedelta(hours=2)
    cached = service.fetch_frames_result(chosen, as_of=cutoff)
    fresh = make_service(client).fetch_frames_result(chosen, as_of=cutoff)
    assert len(client.calls) > before
    assert len(cached.frames["15m"]) == 500
    assert cached.metadata == fresh.metadata


@pytest.mark.parametrize("advance", [0, 15])
def test_missing_cache_prefix_cannot_shorten_a_previously_validated_window(advance):
    client = ClockedCandles()
    store = SignalStore(":memory:")
    service = make_service(client, CandleCache(store))
    chosen = request(context_timeframes=())
    first = service.fetch_frames_result(chosen)
    cutoff = int(first.frames["15m"].iloc[10]["timestamp"].timestamp() * 1000)
    with store._connection() as connection:
        connection.execute("DELETE FROM candle_cache WHERE open_ms < ?", (cutoff,))
    client.now += pd.Timedelta(minutes=advance)
    recovered = service.fetch_frames_result(chosen)
    uncached = make_service(client).fetch_frames_result(chosen)
    assert len(recovered.frames["15m"]) == 500
    assert recovered.metadata == uncached.metadata


def test_legacy_cache_schema_is_upgraded_without_losing_rows():
    store = SignalStore(":memory:")
    with store._connection() as connection:
        connection.execute("""CREATE TABLE candle_cache_state (
            exchange_id TEXT NOT NULL, market_type TEXT NOT NULL, symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL, requested_limit INTEGER NOT NULL, retention_bars INTEGER NOT NULL,
            validated_at_ms INTEGER, invalidation_reason TEXT,
            PRIMARY KEY(exchange_id, market_type, symbol, timeframe)) WITHOUT ROWID""")
        connection.execute("INSERT INTO candle_cache_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*KEY.values, 500, 2000, 1, None))
    cache = CandleCache(store)
    snapshot = cache.read(KEY, as_of=NOW, limit=500)
    assert snapshot.requested_limit == 500
    assert snapshot.validated_rows == 0
    assert cache.stats()["series"] == 1


@pytest.mark.parametrize("limit", [-1, 0, True])
def test_cache_rejects_unbounded_or_boolean_limits(limit):
    cache = CandleCache(SignalStore(":memory:"))
    with pytest.raises(ValueError):
        cache.read(KEY, as_of=NOW, limit=limit)
