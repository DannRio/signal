from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from signal_bot.history import HistoryRequest, HistoryService
from signal_bot.validation import DataValidationError


class FakeHistoryClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    @property
    def timeframes(self):
        return {"1h"}

    def load_markets(self):
        return {"BTC/USDT": {"symbol": "BTC/USDT"}}

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.calls += 1
        matching = [row for row in self.rows if row[0] >= since]
        return matching[:limit]


def make_rows(count=250, *, gap_index=None):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        offset = index + (1 if gap_index is not None and index >= gap_index else 0)
        timestamp = int((start + timedelta(hours=offset)).timestamp() * 1000)
        price = 100.0 + index * 0.1
        rows.append([timestamp, price, price + 1, price - 1, price + 0.2, 1000])
    return rows


def test_paginated_download_is_atomic_and_resumable(tmp_path) -> None:
    client = FakeHistoryClient(make_rows())
    service = HistoryService(client_factory=lambda *_: client)
    request = HistoryRequest(
        exchange_id="fake",
        market_type="spot",
        symbol="BTC/USDT",
        timeframe="1h",
        start="2025-01-01",
        end="2025-01-11T10:00:00Z",
        limit=60,
        output_dir=tmp_path,
    )
    frame, result = service.download(request)
    assert len(frame) == 250
    assert result.rows == 250
    assert result.requests >= 4
    assert result.gaps == 0
    assert (tmp_path / "fake" / "spot" / "BTC_USDT" / "1h.csv").exists()

    calls_before = client.calls
    frame_again, resumed = service.download(request)
    assert len(frame_again) == 250
    assert resumed.resumed_rows == 250
    assert client.calls == calls_before


def test_strict_gap_mode_rejects_missing_candles(tmp_path) -> None:
    client = FakeHistoryClient(make_rows(240, gap_index=100))
    service = HistoryService(client_factory=lambda *_: client)
    with pytest.raises(DataValidationError, match="timestamp gap"):
        service.download(
            HistoryRequest(
                exchange_id="fake",
                market_type="spot",
                symbol="BTC/USDT",
                timeframe="1h",
                start="2025-01-01",
                end="2025-01-12",
                limit=100,
                output_dir=tmp_path,
                strict_gaps=True,
            )
        )
