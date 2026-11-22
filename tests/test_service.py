from __future__ import annotations

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
    ) -> list[list[float]]:
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
