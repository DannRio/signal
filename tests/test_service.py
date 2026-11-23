from __future__ import annotations

from collections import defaultdict

import pandas as pd
import pytest

from signal_bot.service import LiveMarketRequest, RegimeBotService


class FakeClient:
    def __init__(self, exchange_id: str, market_type: str):
        self.exchange_id = exchange_id
        self.market_type = market_type

    @property
    def timeframes(self) -> set[str]:
        return {"15m", "1h", "4h"}

    def load_markets(self) -> dict:
        return {"BTC/USDT": {"active": True}}

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        since: int | None = None,
    ) -> list[list[float]]:
        del symbol, since
        duration = {
            "15m": pd.Timedelta(minutes=15),
            "1h": pd.Timedelta(hours=1),
            "4h": pd.Timedelta(hours=4),
        }[timeframe]
        end = pd.Timestamp("2026-01-01T00:00:00Z")
        times = [
            end - duration * (limit - index)
            for index in range(limit)
        ]
        return _candles(times)


class CappedPaginatedClient(FakeClient):
    """Simulates KuCoin returning no more than 200 rows per request."""

    def __init__(self, exchange_id: str, market_type: str):
        super().__init__(exchange_id, market_type)
        self.calls: dict[str, int] = defaultdict(int)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        since: int | None = None,
    ) -> list[list[float]]:
        del symbol
        self.calls[timeframe] += 1
        duration = {
            "15m": pd.Timedelta(minutes=15),
            "1h": pd.Timedelta(hours=1),
            "4h": pd.Timedelta(hours=4),
        }[timeframe]
        latest_open = pd.Timestamp("2026-01-01T00:01:00Z") - duration
        start = (
            pd.Timestamp(since, unit="ms", tz="UTC")
            if since is not None
            else latest_open - duration * max(limit - 1, 0)
        )
        times: list[pd.Timestamp] = []
        current = start
        while current <= latest_open and len(times) < min(limit, 200):
            times.append(current)
            current += duration
        return _candles(times)


def _candles(times: list[pd.Timestamp]) -> list[list[float]]:
    return [
        [
            int(timestamp.timestamp() * 1000),
            100.0 + index * 0.01,
            100.5 + index * 0.01,
            99.5 + index * 0.01,
            100.2 + index * 0.01,
            1000.0,
        ]
        for index, timestamp in enumerate(times)
    ]


def test_service_fetches_all_requested_closed_frames() -> None:
    service = RegimeBotService(
        client_factory=FakeClient,
        now_factory=lambda: pd.Timestamp("2026-01-01T00:01:00Z"),
    )
    request = LiveMarketRequest(
        exchange_id="fake",
        market_type="spot",
        symbol="BTC/USDT",
        execution_timeframe="15m",
        context_timeframes=("1h", "4h"),
        limit=500,
        maximum_staleness_bars=3,
    )
    frames = service.fetch_frames(request)
    assert set(frames) == {"15m", "1h", "4h"}
    assert all(len(frame) == 500 for frame in frames.values())


def test_service_paginates_when_exchange_caps_each_page() -> None:
    client = CappedPaginatedClient("fake", "swap")
    service = RegimeBotService(
        client_factory=lambda _exchange, _market: client,
        now_factory=lambda: pd.Timestamp("2026-01-01T00:01:00Z"),
    )
    request = LiveMarketRequest(
        exchange_id="fake",
        market_type="swap",
        symbol="BTC/USDT",
        execution_timeframe="15m",
        context_timeframes=("1h", "4h"),
        limit=500,
        maximum_staleness_bars=3,
    )

    frames = service.fetch_frames(request)

    assert all(len(frame) == 500 for frame in frames.values())
    assert all(client.calls[timeframe] >= 3 for timeframe in request.timeframes)
    assert all(frame["timestamp"].is_unique for frame in frames.values())


def test_service_rejects_unknown_symbol() -> None:
    service = RegimeBotService(
        client_factory=FakeClient,
        now_factory=lambda: pd.Timestamp("2026-01-01T00:01:00Z"),
    )
    request = LiveMarketRequest(
        exchange_id="fake",
        market_type="spot",
        symbol="ETH/USDT",
        execution_timeframe="15m",
        limit=500,
    )
    with pytest.raises(ValueError, match="not available"):
        service.fetch_frames(request)
