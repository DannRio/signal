from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

import pandas as pd

from .engine import RegimeSignalEngine
from .models import MarketSignal, SignalAction
from .validation import (
    DataValidationError,
    drop_incomplete_candle,
    normalize_ccxt_ohlcv,
    timeframe_to_timedelta,
    validate_live_window,
)

_T = TypeVar("_T")


class MarketDataUnavailableError(RuntimeError):
    """A bounded transient-error retry budget has been exhausted."""

    code = "market_data_unavailable"

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.retry_after_seconds = retry_after_seconds


class MarketDataClient(Protocol):
    @property
    def timeframes(self) -> set[str]: ...

    def load_markets(self, reload: bool = False) -> dict: ...

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
        *,
        exchange: Any | None = None,
        request_timeout_ms: int = 10_000,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
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

        if isinstance(request_timeout_ms, bool) or not isinstance(request_timeout_ms, int) or not 1_000 <= request_timeout_ms <= 60_000:
            raise ValueError("request_timeout_ms must be an integer between 1000 and 60000.")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be an integer between 1 and 5.")
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._retryable_errors = (ccxt.NetworkError,)
        self._rate_limit_errors = (ccxt.RateLimitExceeded, ccxt.DDoSProtection)

        exchange_class = getattr(ccxt, normalized_exchange)
        self.exchange = exchange if exchange is not None else exchange_class(
            {
                "enableRateLimit": True,
                "timeout": request_timeout_ms,
                "options": {
                    "defaultType": normalized_market_type,
                },
            }
        )
        self.exchange.timeout = request_timeout_ms

    @property
    def timeframes(self) -> set[str]:
        advertised = getattr(self.exchange, "timeframes", None) or {}
        return set(advertised)

    def call_with_retries(self, operation: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Retry transient transport failures, never invalid symbols or auth.

        KuCoin's rate-limit reset header is in milliseconds. If a server asks
        for a wait beyond our 10-second delay budget, surface the outage
        instead of issuing another request before that reset.
        """
        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation(*args, **kwargs)
            except self._retryable_errors as exc:
                # Successful KuCoin responses also carry reset headers. Those
                # must not be mistaken for a fresh 429 after a network timeout.
                headers = (
                    getattr(self.exchange, "last_response_headers", None) or {}
                    if isinstance(exc, self._rate_limit_errors)
                    else {}
                )
                headers = {str(key).lower(): value for key, value in headers.items()}
                retry_after = _positive_float(headers.get("retry-after"))
                reset_ms = _positive_float(headers.get("gw-ratelimit-reset"))
                if reset_ms is not None:
                    retry_after = max(retry_after or 0.0, reset_ms / 1_000.0)
                delay = max(0.5 * 2 ** (attempt - 1), retry_after or 0.0)
                if attempt == self.max_attempts or delay > 10.0:
                    raise MarketDataUnavailableError(
                        f"{type(exc).__name__}: market-data request failed after "
                        f"{attempt} attempt(s).",
                        attempts=attempt,
                        retry_after_seconds=retry_after,
                    ) from exc
                self._sleep(delay)
        raise AssertionError("Unreachable retry state.")  # pragma: no cover

    def load_markets(self, reload: bool = False) -> dict:
        return self.call_with_retries(self.exchange.load_markets, reload=reload)

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
        return self.call_with_retries(
            self.exchange.fetch_ohlcv,
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
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit <= 0:
            raise ValueError("limit must be a positive integer.")
        if isinstance(self.maximum_staleness_bars, bool) or not isinstance(self.maximum_staleness_bars, int) or self.maximum_staleness_bars < 0:
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
        self.last_frame_metadata: dict[str, dict[str, Any]] = {}

    def fetch_frames(
        self,
        request: LiveMarketRequest,
        *,
        as_of: datetime | pd.Timestamp | None = None,
    ) -> dict[str, pd.DataFrame]:
        # Clear before any operation so a failed symbol cannot inherit the
        # preceding symbol's audit metadata in a shared scanning service.
        self.last_frame_metadata = {}
        request.validate()
        minimum_limit = self.engine.config.minimum_bars
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
        now = pd.Timestamp(as_of if as_of is not None else self.now_factory())
        if pd.isna(now):
            raise ValueError("The live-data cutoff must be a valid timestamp.")
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
            validate_live_window(
                frame,
                timeframe,
                now=now,
                minimum_bars=self.engine.config.minimum_bars,
                maximum_staleness_bars=request.maximum_staleness_bars,
                closed_candles_only=request.closed_candles_only,
            )
            frames[timeframe] = frame
        self.last_frame_metadata = {
            timeframe: {
                "rows": len(frame),
                "first_open": frame.iloc[0]["timestamp"].isoformat(),
                "last_open": frame.iloc[-1]["timestamp"].isoformat(),
                "last_close": (
                    frame.iloc[-1]["timestamp"] + timeframe_to_timedelta(timeframe)
                ).isoformat(),
                "sha256": hashlib.sha256(
                    frame.to_csv(
                        index=False,
                        date_format="%Y-%m-%dT%H:%M:%S%z",
                        lineterminator="\n",
                        float_format="%.17g",
                    ).encode("utf-8")
                ).hexdigest(),
            }
            for timeframe, frame in frames.items()
        }
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

        A page may be shorter than the requested limit. Advance by observed
        timestamps rather than treating a short page as end-of-history. Exact
        overlap is safe; conflicting closed rows are rejected. The scan's
        cutoff is fixed even when collection crosses a timeframe boundary.
        """

        duration = timeframe_to_timedelta(timeframe)
        duration_ms = int(duration.total_seconds() * 1000)
        now_ms = int(now.timestamp() * 1000)
        target_rows = limit + (1 if include_extra_open_candle else 0)
        safety_rows = 3
        origin_ms = (
            int(pd.Timestamp("1970-01-05T00:00:00Z").timestamp() * 1000)
            if timeframe.strip().lower().endswith("w")
            else 0
        )
        current_open_ms = ((now_ms - origin_ms) // duration_ms) * duration_ms + origin_ms
        latest_expected_ms = current_open_ms - (duration_ms if include_extra_open_candle else 0)
        cursor = current_open_ms - duration_ms * (target_rows + safety_rows)
        rows_by_timestamp: dict[int, list[float]] = {}

        for _ in range(self._MAX_OHLCV_PAGES):
            # Keep enough room for overlap and forward progress even near the
            # end of a window. A one-row remainder can return only the previous
            # candle on APIs with inclusive/overlapping page boundaries.
            page_limit = self._MAX_PAGE_SIZE
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
                if not isinstance(row, (list, tuple)) or len(row) < 6:
                    raise DataValidationError(
                        f"Malformed {timeframe} OHLCV row returned by the exchange.",
                        code="malformed_candle",
                    )
                try:
                    raw_timestamp = float(row[0])
                    if isinstance(row[0], bool) or not math.isfinite(raw_timestamp) or not raw_timestamp.is_integer() or raw_timestamp <= 0:
                        raise ValueError("Invalid candle timestamp.")
                    timestamp = int(raw_timestamp)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise DataValidationError(
                        f"Invalid {timeframe} candle timestamp.",
                        code="malformed_candle",
                    ) from exc
                normalized_row = [timestamp, *row[1:6]]
                existing = rows_by_timestamp.get(timestamp)
                if existing is not None and existing != normalized_row and timestamp + duration_ms <= now_ms:
                    raise DataValidationError(
                        f"Conflicting closed {timeframe} candles at timestamp {timestamp}.",
                        code="conflicting_candle",
                    )
                rows_by_timestamp[timestamp] = normalized_row
                latest_timestamp = (
                    timestamp
                    if latest_timestamp is None
                    else max(latest_timestamp, timestamp)
                )

            if latest_timestamp is None:  # pragma: no cover - empty batches handled above.
                break
            if latest_timestamp >= latest_expected_ms:
                break
            next_cursor = latest_timestamp + duration_ms
            if next_cursor <= cursor:
                raise DataValidationError(
                    f"{timeframe} pagination stopped making progress before the latest candle.",
                    code="pagination_stalled",
                )
            cursor = next_cursor
        else:
            raise DataValidationError(
                f"{timeframe} pagination exhausted its {self._MAX_OHLCV_PAGES}-request budget.",
                code="pagination_exhausted",
            )

        ordered = [
            rows_by_timestamp[timestamp]
            for timestamp in sorted(rows_by_timestamp)
        ]
        # Keep the bounded collection until the frozen-cutoff filter has run.
        # Trimming first could let post-cutoff bars crowd out usable history.
        return ordered

    def analyze_live(
        self,
        request: LiveMarketRequest,
        *,
        equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
        as_of: datetime | pd.Timestamp | None = None,
    ) -> MarketSignal:
        frames = self.fetch_frames(request, as_of=as_of)
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


def _positive_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


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
