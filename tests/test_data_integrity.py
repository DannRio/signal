from __future__ import annotations

import sys
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from signal_bot.service import (
    CCXTMarketDataClient,
    LiveMarketRequest,
    MarketDataUnavailableError,
    RegimeBotService,
)
from signal_bot.validation import (
    DataValidationError,
    drop_incomplete_candle,
    normalize_ccxt_ohlcv,
)

NOW = pd.Timestamp("2026-01-01T00:01:00Z")
DURATION_MS = 15 * 60 * 1000


def make_rows(count: int = 520) -> list[list[float]]:
    times = pd.date_range(end=NOW.floor("15min"), periods=count, freq="15min")
    return [
        [int(timestamp.timestamp() * 1000), 100.0 + index, 102.0 + index, 99.0 + index, 101.0 + index, 1000.0]
        for index, timestamp in enumerate(times)
    ]


class WindowClient:
    timeframes = {"15m"}

    def __init__(self, rows, *, cap=200, overlap=False, conflicting_overlap=False):
        self.rows = rows
        self.cap = cap
        self.calls = 0
        self.overlap = overlap
        self.conflicting_overlap = conflicting_overlap

    def load_markets(self):
        return {"BTC/USDT": {"active": True}}

    def fetch_ohlcv(self, symbol, timeframe, limit, since=None):
        self.calls += 1
        rows = [deepcopy(row) for row in self.rows if row[0] >= since]
        if self.overlap and self.calls > 1:
            previous = [deepcopy(row) for row in self.rows if row[0] == since - DURATION_MS]
            if previous:
                if self.conflicting_overlap:
                    previous[0][4] -= 0.5
                rows = previous + rows
        return rows[: min(self.cap, limit)]


def request(**kwargs):
    values = {
        "exchange_id": "fake",
        "market_type": "swap",
        "symbol": "BTC/USDT",
        "execution_timeframe": "15m",
        "limit": 300,
        "maximum_staleness_bars": 1,
    }
    values.update(kwargs)
    return LiveMarketRequest(**values)


def service_for(client):
    return RegimeBotService(client_factory=lambda *_: client, now_factory=lambda: NOW)


def test_live_window_accepts_exact_overlap_and_hashes_only_usable_bars():
    client = WindowClient(make_rows(), overlap=True)
    service = service_for(client)
    frame = service.fetch_frames(request())["15m"]
    assert len(frame) == 300
    assert frame["timestamp"].is_unique
    assert frame.iloc[-1]["timestamp"] == NOW.floor("15min") - timedelta(minutes=15)
    metadata = service.last_frame_metadata["15m"]
    assert metadata["rows"] == 300
    assert metadata["last_close"] == NOW.floor("15min").isoformat()
    assert len(metadata["sha256"]) == 64

    # Request paging/overlap do not change the hash of the analyzed window.
    other = service_for(WindowClient(make_rows(), cap=500))
    other.fetch_frames(request())
    assert other.last_frame_metadata == service.last_frame_metadata


def test_conflicting_overlap_is_not_silently_replaced():
    service = service_for(WindowClient(make_rows(), overlap=True, conflicting_overlap=True))
    with pytest.raises(DataValidationError) as exc:
        service.fetch_frames(request())
    assert exc.value.code == "conflicting_candle"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda rows: rows.pop(-10), "candle_gap"),
        (lambda rows: rows[-10].__setitem__(0, rows[-10][0] + 60_000), "misaligned_candle"),
        (lambda rows: rows[-10].__setitem__(2, float("inf")), "invalid_ohlcv"),
        (lambda rows: rows[-10].__setitem__(2, 1.0), "invalid_ohlcv"),
        (lambda rows: rows[-10].__setitem__(5, -1.0), "invalid_ohlcv"),
        (lambda rows: rows.__setitem__(-10, rows[-10][:4]), "malformed_candle"),
    ],
)
def test_invalid_live_data_is_rejected(mutation, expected_code):
    rows = make_rows()
    mutation(rows)
    service = service_for(WindowClient(rows))
    with pytest.raises(DataValidationError) as exc:
        service.fetch_frames(request())
    assert exc.value.code == expected_code
    assert service.last_frame_metadata == {}


def test_stale_and_insufficient_history_have_distinct_codes():
    service = service_for(WindowClient(make_rows()[:-5]))
    with pytest.raises(DataValidationError) as exc:
        service.fetch_frames(request())
    assert exc.value.code == "stale_candles"

    service = service_for(WindowClient(make_rows(100)))
    with pytest.raises(DataValidationError) as exc:
        service.fetch_frames(request())
    assert exc.value.code == "insufficient_history"


def test_failed_symbol_clears_previous_frame_metadata():
    client = WindowClient(make_rows())
    service = service_for(client)
    service.fetch_frames(request())
    assert service.last_frame_metadata
    client.rows.pop(-5)
    with pytest.raises(DataValidationError):
        service.fetch_frames(request())
    assert service.last_frame_metadata == {}


def test_cutoff_excludes_all_later_bars_before_trimming_window():
    rows = make_rows()
    for _ in range(1, 10):
        rows.append([rows[-1][0] + DURATION_MS, 100, 102, 99, 101, 1000])
    service = service_for(WindowClient(rows, cap=500))
    frame = service.fetch_frames(request(), as_of=NOW - timedelta(hours=1))["15m"]
    assert len(frame) == 300
    assert frame.iloc[-1]["timestamp"] + timedelta(minutes=15) == NOW.floor("15min") - timedelta(hours=1)


def test_drop_incomplete_candle_filters_multiple_future_rows():
    frame = normalize_ccxt_ohlcv(make_rows(10))
    cutoff = frame.iloc[-4]["timestamp"] + timedelta(minutes=15)
    result = drop_incomplete_candle(frame, "15m", now=cutoff)
    assert len(result) == 7
    assert (result["timestamp"] + timedelta(minutes=15) <= cutoff).all()


def test_pagination_failure_is_explicit_and_bounded():
    class RepeatedPage(WindowClient):
        def fetch_ohlcv(self, symbol, timeframe, limit, since=None):
            self.calls += 1
            return self.rows[:200]

    client = RepeatedPage(make_rows())
    with pytest.raises(DataValidationError) as exc:
        service_for(client).fetch_frames(request())
    assert exc.value.code == "pagination_stalled"
    assert client.calls <= 2


def test_pagination_request_budget_is_enforced():
    client = WindowClient(make_rows(), cap=1)
    with pytest.raises(DataValidationError) as exc:
        service_for(client).fetch_frames(request())
    assert exc.value.code == "pagination_exhausted"
    assert client.calls == RegimeBotService._MAX_OHLCV_PAGES


class FakeNetworkError(Exception):
    pass


class FakeRateLimitError(FakeNetworkError):
    pass


def retry_client(monkeypatch, *, headers=None, max_attempts=3):
    # Model CCXT's documented exception hierarchy while remaining offline.
    monkeypatch.setitem(sys.modules, "ccxt", SimpleNamespace(
        kucoinfutures=lambda _: None,
        NetworkError=FakeNetworkError,
        RateLimitExceeded=FakeRateLimitError,
        DDoSProtection=FakeRateLimitError,
    ))
    exchange = SimpleNamespace(last_response_headers=headers or {}, timeout=0)
    waits = []
    client = CCXTMarketDataClient(
        "kucoinfutures", "swap", exchange=exchange, max_attempts=max_attempts, sleep=waits.append,
    )
    return client, waits


def test_transient_retry_succeeds_with_bounded_backoff(monkeypatch):
    client, waits = retry_client(monkeypatch)
    calls = []

    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise FakeNetworkError("temporary outage")
        return {"ok": True}

    assert client.call_with_retries(operation) == {"ok": True}
    assert waits == [0.5, 1.0]
    assert client.exchange.timeout == 10_000


def test_non_transient_errors_are_never_retried(monkeypatch):
    client, waits = retry_client(monkeypatch)

    def operation():
        raise ValueError("invalid symbol")

    with pytest.raises(ValueError, match="invalid symbol"):
        client.call_with_retries(operation)
    assert waits == []


def test_rate_limit_header_is_honored_and_long_reset_fails_without_retry(monkeypatch):
    client, waits = retry_client(monkeypatch, headers={"gw-ratelimit-reset": "2000"})
    calls = []

    def operation():
        calls.append(1)
        if len(calls) == 1:
            raise FakeRateLimitError("429")
        return 42

    assert client.call_with_retries(operation) == 42
    assert waits == [2.0]

    client, waits = retry_client(monkeypatch, headers={"gw-ratelimit-reset": "30000"})

    def blocked():
        raise FakeRateLimitError("429")

    with pytest.raises(MarketDataUnavailableError) as exc:
        client.call_with_retries(blocked)
    assert exc.value.code == "market_data_unavailable"
    assert exc.value.attempts == 1
    assert exc.value.retry_after_seconds == 30.0
    assert waits == []


def test_network_outage_does_not_reuse_a_stale_rate_limit_header(monkeypatch):
    client, waits = retry_client(monkeypatch, headers={"gw-ratelimit-reset": "30000"})

    def operation():
        raise FakeNetworkError("timeout")

    with pytest.raises(MarketDataUnavailableError) as exc:
        client.call_with_retries(operation)
    assert exc.value.attempts == 3
    assert waits == [0.5, 1.0]
