from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Iterable, Protocol

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
    ) -> list[list[float]]:
        if not self.exchange.has.get("fetchOHLCV"):
            raise NotImplementedError(
                f"{self.exchange.id} does not support OHLCV retrieval."
            )
        return self.exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
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
            lambda: pd.Timestamp(datetime.now(timezone.utc))
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
            candles = client.fetch_ohlcv(
                symbol=request.symbol,
                timeframe=timeframe,
                limit=request.limit,
            )
            frame = normalize_ccxt_ohlcv(candles)
            if request.closed_candles_only:
                frame = drop_incomplete_candle(
                    frame,
                    timeframe,
                    now=now,
                )
            if frame.empty:
                raise ValueError(
                    f"{request.exchange_id} returned no usable closed "
                    f"candles for {request.symbol} {timeframe}."
                )
            if request.maximum_staleness_bars:
                duration = timeframe_to_timedelta(timeframe)
                latest_close = (
                    frame.iloc[-1]["timestamp"] + duration
                )
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
