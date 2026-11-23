from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

import pandas as pd

from .engine import RegimeSignalEngine
from .models import MarketSignal, SignalAction
from .validation import (
    drop_incomplete_candle,
    normalize_ccxt_ohlcv,
    timeframe_to_timedelta,
)


class MarketDataClient(Protocol):
    @property
    def timeframes(self) -> set[str]: ...

    def load_markets(self) -> dict: ...

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        since: int | None = None,
    ) -> list[list[float]]: ...


class CCXTMarketDataClient:
    """Unauthenticated CCXT market-data adapter."""

    def __init__(
        self,
        exchange_id: str,
        market_type: str,
    ) -> None:
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover - packaging boundary.
            raise RuntimeError(
                "CCXT is required for live market data. Install the project "
                "with `python -m pip install -e .`."
            ) from exc

        normalized_exchange = exchange_id.strip().lower()
        if not normalized_exchange or not hasattr(ccxt, normalized_exchange):
            raise ValueError(
                f"Unsupported CCXT exchange: {exchange_id!r}."
            )
        normalized_market_type = market_type.strip().lower()
        if not normalized_market_type:
            raise ValueError("market_type cannot be empty.")

        exchange_class = getattr(ccxt, normalized_exchange)
        self.exchange = exchange_class(
            {
                "enableRateLimit": True,
                "options": {
                    "defaultType": normalized_market_type,
                },
            }
        )

    @property
    def timeframes(self) -> set[str]:
        advertised = getattr(self.exchange, "timeframes", None) or {}
        return set(advertised)

    def load_markets(self) -> dict:
        return self.exchange.load_markets()

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        since: int | None = None,
    ) -> list[list[float]]:
        if not self.exchange.has.get("fetchOHLCV"):
            raise NotImplementedError(
                f"{self.exchange.id} does not support OHLCV retrieval."
            )
        return self.exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            since=since,
            limit=limit,
        )


@dataclass(frozen=True)
class LiveMarketRequest:
    exchange_id: str
    market_type: str
    symbol: str
    execution_timeframe: str
    context_timeframes: tuple[str, ...] = ()
    limit: int = 500
    closed_candles_only: bool = True
    maximum_staleness_bars: int = 3

    @property
    def timeframes(self) -> tuple[str, ...]:
        values = [self.execution_timeframe, *self.context_timeframes]
        unique: list[str] = []
        for value in values:
            normalized = value.strip().lower()
            timeframe_to_timedelta(normalized)
            if normalized not in unique:
                unique.append(normalized)
        return tuple(unique)

    def validate(self) -> None:
        if not self.exchange_id.strip():
            raise ValueError("exchange_id cannot be empty.")
        if not self.market_type.strip():
            raise ValueError("market_type cannot be empty.")
        if not self.symbol.strip() or "/" not in self.symbol:
            raise ValueError(
                "symbol must use CCXT notation, for example BTC/USDT."
            )
        if self.limit <= 0:
            raise ValueError("limit must be positive.")
        if self.maximum_staleness_bars < 0:
            raise ValueError(
                "maximum_staleness_bars cannot be negative."
            )
        _ = self.timeframes


ClientFactory = Callable[[str, str], MarketDataClient]
NowFactory = Callable[[], pd.Timestamp]


class RegimeBotService:
    _MAX_OHLCV_PAGES = 10
    _MAX_PAGE_SIZE = 500

    def __init__(
        self,
        engine: RegimeSignalEngine | None = None,
        *,
        client_factory: ClientFactory = CCXTMarketDataClient,
        now_factory: NowFactory | None = None,
    ) -> None:
        self.engine = engine or RegimeSignalEngine()
        self.client_factory = client_factory
        self.now_factory = now_factory or (
            lambda: pd.Timestamp(datetime.now(UTC))
        )

    def fetch_frames(
        self,
        request: LiveMarketRequest,
    ) -> dict[str, pd.DataFrame]:
        request.validate()
        minimum_limit = self.engine.config.minimum_bars + (
            1 if request.closed_candles_only else 0
        )
        if request.limit < minimum_limit:
            raise ValueError(
                f"limit must be at least {minimum_limit} candles "
                "for this mode."
            )

        client = self.client_factory(
            request.exchange_id,
            request.market_type,
        )
        markets = client.load_markets()
        if request.symbol not in markets:
            raise ValueError(
                f"{request.symbol!r} is not available on "
                f"{request.exchange_id} for market type "
                f"{request.market_type!r}."
            )

        advertised_timeframes = client.timeframes
        frames: dict[str, pd.DataFrame] = {}
        now = self.now_factory()
        if now.tzinfo is None:
            now = now.tz_localize("UTC")
        else:
            now = now.tz_convert("UTC")

        for timeframe in request.timeframes:
            if (
                advertised_timeframes
                and timeframe not in advertised_timeframes
            ):
                raise ValueError(
                    f"{request.exchange_id} does not advertise "
                    f"timeframe {timeframe!r}."
                )
            candles = self._fetch_recent_ohlcv(
                client=client,
                symbol=request.symbol,
                timeframe=timeframe,
                limit=request.limit,
                now=now,
                include_extra_open_candle=request.closed_candles_only,
            )
            frame = normalize_ccxt_ohlcv(candles)
            if request.closed_candles_only:
                frame = drop_incomplete_candle(
                    frame,
                    timeframe,
                    now=now,
                )
            frame = frame.tail(request.limit).reset_index(drop=True)
            if frame.empty:
                raise ValueError(
                    f"{request.exchange_id} returned no usable closed "
                    f"candles for {request.symbol} {timeframe}."
                )
            if len(frame) < self.engine.config.minimum_bars:
                raise ValueError(
                    f"{request.exchange_id} returned only {len(frame)} usable "
                    f"{timeframe} candles for {request.symbol}; at least "
                    f"{self.engine.config.minimum_bars} are required."
                )
            if request.maximum_staleness_bars:
                duration = timeframe_to_timedelta(timeframe)
                latest_close = frame.iloc[-1]["timestamp"] + duration
                if (
                    now - latest_close
                    > duration * request.maximum_staleness_bars
                ):
                    raise ValueError(
                        f"Latest {timeframe} candle is stale by more than "
                        f"{request.maximum_staleness_bars} bars."
                    )
            frames[timeframe] = frame
        return frames

    def _fetch_recent_ohlcv(
        self,
        *,
        client: MarketDataClient,
        symbol: str,
        timeframe: str,
        limit: int,
        now: pd.Timestamp,
        include_extra_open_candle: bool,
    ) -> list[list[float]]:
        """Assemble a recent OHLCV window across exchange page-size caps.

        KuCoin Futures currently returns at most 200 rows through CCXT for a
        single public Kline request even when a larger limit is requested. The
        signal engine needs at least 220 closed rows, so one request is not a
        usable live-data contract. This method paginates forward from a bounded
        `since` timestamp, deduplicates exchange overlap, and returns the most
        recent requested window.
        """

        duration = timeframe_to_timedelta(timeframe)
        duration_ms = int(duration.total_seconds() * 1000)
        now_ms = int(now.timestamp() * 1000)
        target_rows = limit + (1 if include_extra_open_candle else 0)
        safety_rows = 3
        cursor = now_ms - duration_ms * (target_rows + safety_rows)
        rows_by_timestamp: dict[int, list[float]] = {}

        for _ in range(self._MAX_OHLCV_PAGES):
            remaining = target_rows + safety_rows - len(rows_by_timestamp)
            page_limit = min(
                self._MAX_PAGE_SIZE,
                max(1, remaining),
            )
            batch = client.fetch_ohlcv(
                symbol=symbol,
                timeframe=timeframe,
                limit=page_limit,
                since=cursor,
            )
            if not batch:
                break

            latest_timestamp: int | None = None
            for row in batch:
                if len(row) < 6:
                    continue
                timestamp = int(row[0])
                rows_by_timestamp[timestamp] = list(row[:6])
                latest_timestamp = (
                    timestamp
                    if latest_timestamp is None
                    else max(latest_timestamp, timestamp)
                )

            if latest_timestamp is None:
                break
            next_cursor = latest_timestamp + duration_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if cursor > now_ms:
                break

        ordered = [
            rows_by_timestamp[timestamp]
            for timestamp in sorted(rows_by_timestamp)
        ]
        return ordered[-target_rows:]

    def analyze_live(
        self,
        request: LiveMarketRequest,
        *,
        equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
    ) -> MarketSignal:
        frames = self.fetch_frames(request)
        signal = self.engine.analyze(
            symbol=request.symbol,
            frames=frames,
            execution_timeframe=request.execution_timeframe,
            equity=equity,
            risk_percent=risk_percent,
            maximum_notional_multiple=maximum_notional_multiple,
        )
        if request.market_type.lower() == "spot" and signal.action in {
            SignalAction.SHORT,
            SignalAction.WATCH_SHORT,
        }:
            signal = replace(
                signal,
                warnings=signal.warnings
                + (
                    "Bearish/short signal is informational on a spot "
                    "market; verify margin or derivatives eligibility "
                    "before execution.",
                ),
            )
        return signal


def parse_timeframes(
    value: str | Iterable[str],
) -> tuple[str, ...]:
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    normalized: list[str] = []
    for item in items:
        timeframe = str(item).strip().lower()
        if not timeframe:
            continue
        timeframe_to_timedelta(timeframe)
        if timeframe not in normalized:
            normalized.append(timeframe)
    return tuple(normalized)
