from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Protocol, TypeVar

import pandas as pd

from .candle_cache import CandleCache, CandleKey
from .engine import RegimeSignalEngine
from .models import MarketSignal, SignalAction
from .runtime import (
    RequestDeadline,
    RequestDeadlineExceeded,
    RuntimeMetrics,
    current_operation,
    metrics_delta,
    operation_context,
)
from .validation import (
    DataValidationError,
    drop_incomplete_candle,
    normalize_ccxt_ohlcv,
    prepare_ohlcv,
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
        self.metrics_capabilities = {"http_requests": False, "rate_limit_wait": False}
        self._instrument_transport()

    def _instrument_transport(self) -> None:
        """Instrument supported synchronous boundaries on this owned instance.

        Top-level CCXT operations may use zero or multiple HTTP requests. Fetch
        and throttle wrappers measure those actual boundaries separately.
        """
        transport = getattr(self.exchange, "fetch", None)
        if callable(transport):
            self.metrics_capabilities["http_requests"] = True
            if not getattr(transport, "_signal_runtime_measured", False):
                @wraps(transport)
                def measured_fetch(*args: Any, **kwargs: Any):
                    deadline, metrics = current_operation()
                    _check_deadline(deadline, "HTTP transport")
                    previous_timeout = self.exchange.timeout
                    if deadline is not None:
                        self.exchange.timeout = min(previous_timeout, max(1, int(deadline.remaining() * 1000)))
                    if metrics is not None:
                        metrics.increment("http_requests")
                    try:
                        with metrics.measure("http_transport") if metrics is not None else nullcontext():
                            result = transport(*args, **kwargs)
                        _check_deadline(deadline, "HTTP transport")
                        return result
                    finally:
                        self.exchange.timeout = previous_timeout
                measured_fetch._signal_runtime_measured = True
                self.exchange.fetch = measured_fetch
        throttle = getattr(self.exchange, "throttle", None)
        if callable(throttle):
            self.metrics_capabilities["rate_limit_wait"] = True
            if not getattr(throttle, "_signal_runtime_measured", False):
                @wraps(throttle)
                def measured_throttle(*args: Any, **kwargs: Any):
                    deadline, metrics = current_operation()
                    _check_deadline(deadline, "rate limiter")
                    with metrics.measure("rate_limit_wait") if metrics is not None else nullcontext():
                        result = throttle(*args, **kwargs)
                    _check_deadline(deadline, "rate limiter")
                    return result
                measured_throttle._signal_runtime_measured = True
                self.exchange.throttle = measured_throttle

    @property
    def timeframes(self) -> set[str]:
        advertised = getattr(self.exchange, "timeframes", None) or {}
        return set(advertised)

    def operation_context(
        self, *, deadline: RequestDeadline | None = None, metrics: RuntimeMetrics | None = None
    ):
        return operation_context(deadline=deadline, metrics=metrics)

    def call_with_retries(
        self, operation: Callable[..., _T], *args: Any,
        deadline: RequestDeadline | None = None, metrics: RuntimeMetrics | None = None,
        **kwargs: Any,
    ) -> _T:
        """Retry transient failures inside the enclosing request's deadline.

        The serial coordinator owns this synchronous exchange. Its transport
        timeout is reduced to the remaining budget and restored afterward.
        Deadline checks also cover successful calls and retry waits.
        """
        with self.operation_context(deadline=deadline, metrics=metrics):
            active_deadline, active_metrics = current_operation()
            active_metrics = active_metrics or RuntimeMetrics()
            original_timeout = self.exchange.timeout
            try:
                for attempt in range(1, self.max_attempts + 1):
                    if active_deadline is not None:
                        active_deadline.check("exchange request")
                        self.exchange.timeout = min(original_timeout, max(1, int(active_deadline.remaining() * 1000)))
                    active_metrics.increment("exchange_operation_attempts")
                    try:
                        with active_metrics.measure("network"):
                            result = operation(*args, **kwargs)
                        if active_deadline is not None:
                            active_deadline.check("exchange request")
                        return result
                    except self._retryable_errors as exc:
                        if active_deadline is not None:
                            active_deadline.check("exchange request")
                        headers = (
                            getattr(self.exchange, "last_response_headers", None) or {}
                            if isinstance(exc, self._rate_limit_errors) else {}
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
                                attempts=attempt, retry_after_seconds=retry_after,
                            ) from exc
                        if active_deadline is not None and delay >= active_deadline.remaining():
                            raise RequestDeadlineExceeded(
                                "Scan deadline leaves no time for the required retry delay."
                            ) from exc
                        active_metrics.increment("network_retries")
                        active_metrics.increment("retry_delay_seconds", delay)
                        with active_metrics.measure("retry_wait"):
                            self._sleep(delay)
            finally:
                self.exchange.timeout = original_timeout
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


@dataclass(frozen=True)
class FrameFetchResult:
    frames: dict[str, pd.DataFrame]
    metadata: dict[str, dict[str, Any]]
    metrics: dict[str, Any]
    cache_stats: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class LiveAnalysisResult:
    signal: MarketSignal
    frames: dict[str, pd.DataFrame]
    metadata: dict[str, dict[str, Any]]
    metrics: dict[str, Any]
    cache_stats: dict[str, dict[str, Any]]


class RegimeBotService:
    _MAX_OHLCV_PAGES = 10
    _MAX_PAGE_SIZE = 500
    _CACHE_OVERLAP_BARS = 2

    def __init__(
        self,
        engine: RegimeSignalEngine | None = None,
        *,
        client_factory: ClientFactory = CCXTMarketDataClient,
        now_factory: NowFactory | None = None,
        cache: CandleCache | None = None,
        signal_cache_size: int = 256,
    ) -> None:
        if isinstance(signal_cache_size, bool) or not isinstance(signal_cache_size, int) or signal_cache_size < 0:
            raise ValueError("signal_cache_size must be a nonnegative integer.")
        self.engine = engine or RegimeSignalEngine()
        self.client_factory = client_factory
        self.now_factory = now_factory or (lambda: pd.Timestamp(datetime.now(UTC)))
        self.cache = cache
        self.signal_cache_size = signal_cache_size
        self._signals: OrderedDict[str, MarketSignal] = OrderedDict()
        self._signal_lock = threading.Lock()
        # Compatibility for older clients only. New callers consume explicit
        # result metadata and do not depend on per-symbol mutable side channels.
        self.last_frame_metadata: dict[str, dict[str, Any]] = {}

    def fetch_frames(
        self, request: LiveMarketRequest, *, as_of: datetime | pd.Timestamp | None = None,
        deadline: RequestDeadline | None = None, metrics: RuntimeMetrics | None = None,
    ) -> dict[str, pd.DataFrame]:
        return self.fetch_frames_result(request, as_of=as_of, deadline=deadline, metrics=metrics).frames

    def fetch_frames_result(
        self, request: LiveMarketRequest, *, as_of: datetime | pd.Timestamp | None = None,
        deadline: RequestDeadline | None = None, metrics: RuntimeMetrics | None = None,
    ) -> FrameFetchResult:
        self.last_frame_metadata = {}
        inherited_deadline, inherited_metrics = current_operation()
        deadline = deadline if deadline is not None else inherited_deadline
        metrics = metrics if metrics is not None else inherited_metrics or RuntimeMetrics()
        baseline = metrics.to_dict()
        with operation_context(deadline=deadline, metrics=metrics):
            deadline, _ = current_operation()
            with metrics.measure("data_fetch"):
                request.validate()
                if request.limit < self.engine.config.minimum_bars:
                    raise ValueError(f"limit must be at least {self.engine.config.minimum_bars} candles for this mode.")
                now = _utc_timestamp(as_of if as_of is not None else self.now_factory())
                client = self.client_factory(request.exchange_id, request.market_type)
                _check_deadline(deadline, "market metadata")
                with metrics.measure("market_metadata"):
                    markets = client.load_markets()
                _check_deadline(deadline, "market metadata")
                if request.symbol not in markets:
                    raise ValueError(
                        f"{request.symbol!r} is not available on {request.exchange_id} "
                        f"for market type {request.market_type!r}."
                    )
                advertised_timeframes = client.timeframes
                frames: dict[str, pd.DataFrame] = {}
                cache_stats: dict[str, dict[str, Any]] = {}
                for timeframe in request.timeframes:
                    _check_deadline(deadline, f"{timeframe} collection")
                    if advertised_timeframes and timeframe not in advertised_timeframes:
                        raise ValueError(f"{request.exchange_id} does not advertise timeframe {timeframe!r}.")
                    frame, cache_info = self._fetch_frame(
                        client, request, timeframe, now=now, deadline=deadline, metrics=metrics,
                    )
                    frames[timeframe] = frame
                    cache_stats[timeframe] = cache_info
                with metrics.measure("frame_hash"):
                    metadata = {timeframe: _frame_metadata(frame, timeframe) for timeframe, frame in frames.items()}
                _check_deadline(deadline, "candle validation")
        self.last_frame_metadata = metadata
        return FrameFetchResult(frames, metadata, metrics_delta(baseline, metrics.to_dict()), cache_stats)

    def _fetch_frame(
        self, client: MarketDataClient, request: LiveMarketRequest, timeframe: str, *,
        now: pd.Timestamp, deadline: RequestDeadline | None, metrics: RuntimeMetrics,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        key = CandleKey(request.exchange_id, request.market_type, request.symbol, timeframe)
        use_cache = self.cache is not None and request.closed_candles_only
        cached = CandleCache.empty_frame()
        cache_info: dict[str, Any] = {"status": "disabled"}
        covered_limit = 0
        validated_at_ms = None
        validated_rows = 0
        expected_open = _expected_open_ms(timeframe, now, closed=True)
        duration_ms = int(timeframe_to_timedelta(timeframe).total_seconds() * 1000)
        if use_cache:
            try:
                with metrics.measure("cache_read"):
                    snapshot = self.cache.read(key, as_of=now, limit=request.limit)
                cached = snapshot.frame
                covered_limit = snapshot.requested_limit
                validated_at_ms = snapshot.validated_at_ms
                validated_rows = snapshot.validated_rows
                cache_info = {"status": "miss", "recovery_reason": snapshot.invalidation_reason}
                if not cached.empty:
                    validate_live_window(
                        cached, timeframe, now=now, minimum_bars=1,
                        maximum_staleness_bars=0, closed_candles_only=True,
                    )
            except DataValidationError as exc:
                self.cache.invalidate(key, f"invalid_cached_window:{exc.code}")
                metrics.increment("cache_invalidations")
                cache_info = {"status": "recovery", "recovery_reason": exc.code}
                cached = CandleCache.empty_frame()
            # A tolerated one-bar-old window is NOT a cache hit: retry retrieval
            # until the latest expected closed bar has actually been published.
            if (
                not cached.empty and covered_limit >= request.limit
                and len(cached) >= self.engine.config.minimum_bars
                and (
                    len(cached) >= request.limit
                    or (len(cached) == validated_rows and validated_at_ms is not None and _expected_open_ms(
                        timeframe, pd.Timestamp(validated_at_ms, unit="ms", tz="UTC"), closed=True
                    ) == expected_open)
                )
                and int(cached.iloc[-1]["timestamp"].timestamp() * 1000) == expected_open
            ):
                with metrics.measure("validation"):
                    self._validate_frame(cached, request, timeframe, now)
                metrics.increment("cache_hits")
                return cached.tail(request.limit).reset_index(drop=True), {**cache_info, "status": "hit", "rows": len(cached)}
            metrics.increment("cache_misses")
        start_ms = None
        if (
            use_cache and not cached.empty and covered_limit >= request.limit
            and len(cached) >= self.engine.config.minimum_bars
            and (len(cached) >= request.limit or len(cached) == validated_rows)
        ):
            last_open = int(cached.iloc[-1]["timestamp"].timestamp() * 1000)
            if 0 < expected_open - last_open < request.limit * duration_ms:
                start_ms = last_open - (self._CACHE_OVERLAP_BARS - 1) * duration_ms
                cache_info["status"] = "incremental"
        with metrics.measure("ohlcv_fetch"):
            candles = self._fetch_recent_ohlcv(
                client=client, symbol=request.symbol, timeframe=timeframe, limit=request.limit,
                now=now, include_extra_open_candle=request.closed_candles_only,
                start_ms=start_ms, deadline=deadline, metrics=metrics,
            )
        with metrics.measure("validation"):
            fetched = normalize_ccxt_ohlcv(candles)
            if request.closed_candles_only:
                fetched = drop_incomplete_candle(fetched, timeframe, now=now)
            if use_cache and start_ms is not None and not cached.empty:
                # Compare actual overlap before deduplication. A corrected row
                # cannot silently alter a cached strategy input.
                if _has_conflicting_overlap(cached, fetched):
                    self.cache.invalidate(key, "conflicting_closed_candle")
                    metrics.increment("cache_invalidations")
                    raise DataValidationError(
                        "Closed exchange candles conflict with cached history; the series "
                        "was invalidated and requires a fresh bootstrap.", code="cache_conflict",
                    )
                combined = pd.concat([cached, fetched], ignore_index=True)
                frame = prepare_ohlcv(combined).tail(request.limit).reset_index(drop=True)
            else:
                frame = fetched.tail(request.limit).reset_index(drop=True)
            self._validate_frame(frame, request, timeframe, now)
        if use_cache:
            with metrics.measure("cache_write"):
                written = self.cache.write_validated(key, frame, as_of=now, requested_limit=request.limit)
            metrics.increment("cache_rows_written", written["inserted_rows"])
            metrics.increment("cache_rows_pruned", written["pruned_rows"])
            cache_info.update(written)
            cache_info["rows"] = len(frame)
            if cache_info["status"] == "miss":
                cache_info["status"] = "bootstrap"
        return frame, cache_info

    def _validate_frame(self, frame: pd.DataFrame, request: LiveMarketRequest, timeframe: str, now: pd.Timestamp) -> None:
        validate_live_window(
            frame, timeframe, now=now, minimum_bars=self.engine.config.minimum_bars,
            maximum_staleness_bars=request.maximum_staleness_bars,
            closed_candles_only=request.closed_candles_only,
        )

    def _fetch_recent_ohlcv(
        self,
        *,
        client: MarketDataClient,
        symbol: str,
        timeframe: str,
        limit: int,
        now: pd.Timestamp,
        include_extra_open_candle: bool,
        start_ms: int | None = None,
        deadline: RequestDeadline | None = None,
        metrics: RuntimeMetrics | None = None,
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
        cursor = start_ms if start_ms is not None else current_open_ms - duration_ms * (target_rows + safety_rows)
        rows_by_timestamp: dict[int, list[float]] = {}

        for _ in range(self._MAX_OHLCV_PAGES):
            _check_deadline(deadline, f"{timeframe} pagination")
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
            _check_deadline(deadline, f"{timeframe} pagination")
            if metrics is not None:
                metrics.increment("ohlcv_pages")
                metrics.increment("ohlcv_rows", len(batch))
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
        self, request: LiveMarketRequest, *, equity: float = 10_000.0,
        risk_percent: float | None = None, maximum_notional_multiple: float | None = None,
        as_of: datetime | pd.Timestamp | None = None,
        deadline: RequestDeadline | None = None, metrics: RuntimeMetrics | None = None,
    ) -> MarketSignal:
        return self.analyze_live_result(
            request, equity=equity, risk_percent=risk_percent,
            maximum_notional_multiple=maximum_notional_multiple, as_of=as_of,
            deadline=deadline, metrics=metrics,
        ).signal

    def analyze_live_result(
        self, request: LiveMarketRequest, *, equity: float = 10_000.0,
        risk_percent: float | None = None, maximum_notional_multiple: float | None = None,
        as_of: datetime | pd.Timestamp | None = None,
        deadline: RequestDeadline | None = None, metrics: RuntimeMetrics | None = None,
    ) -> LiveAnalysisResult:
        self.last_frame_metadata = {}
        inherited_deadline, inherited_metrics = current_operation()
        deadline = deadline if deadline is not None else inherited_deadline
        metrics = metrics if metrics is not None else inherited_metrics or RuntimeMetrics()
        baseline = metrics.to_dict()
        with operation_context(deadline=deadline, metrics=metrics):
            deadline, _ = current_operation()
            fetched = self.fetch_frames_result(request, as_of=as_of, deadline=deadline, metrics=metrics)
            _check_deadline(deadline, "signal analysis")
            key_payload = {
                "symbol": request.symbol, "exchange": request.exchange_id, "market_type": request.market_type,
                "execution_timeframe": request.execution_timeframe,
                "frames": fetched.metadata, "config": asdict(self.engine.config),
                "equity": equity, "risk_percent": risk_percent,
                "maximum_notional_multiple": maximum_notional_multiple,
            }
            signal_key = hashlib.sha256(json.dumps(key_payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
            with self._signal_lock:
                signal = self._signals.get(signal_key)
                if signal is not None:
                    self._signals.move_to_end(signal_key)
            signal_reused = signal is not None
            if signal is None:
                metrics.increment("signal_cache_misses")
                with metrics.measure("signal_analysis"):
                    signal = self.engine.analyze(
                        symbol=request.symbol, frames=fetched.frames,
                        execution_timeframe=request.execution_timeframe, equity=equity,
                        risk_percent=risk_percent, maximum_notional_multiple=maximum_notional_multiple,
                    )
                    if request.market_type.lower() == "spot" and signal.action in {SignalAction.SHORT, SignalAction.WATCH_SHORT}:
                        signal = replace(signal, warnings=signal.warnings + (
                            "Bearish/short signal is informational on a spot market; verify margin or derivatives eligibility before execution.",
                        ))
                _check_deadline(deadline, "signal analysis")
                if self.signal_cache_size:
                    with self._signal_lock:
                        self._signals[signal_key] = signal
                        self._signals.move_to_end(signal_key)
                        while len(self._signals) > self.signal_cache_size:
                            self._signals.popitem(last=False)
            else:
                metrics.increment("signal_cache_hits")
            _check_deadline(deadline, "signal analysis")
        cache_stats = {**fetched.cache_stats, "analysis": {"status": "hit" if signal_reused else "computed"}}
        return LiveAnalysisResult(signal, fetched.frames, fetched.metadata,
                                  metrics_delta(baseline, metrics.to_dict()), cache_stats)


def _check_deadline(deadline: RequestDeadline | None, stage: str) -> None:
    if deadline is not None:
        deadline.check(stage)


def _utc_timestamp(value: datetime | pd.Timestamp) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise ValueError("The live-data cutoff must be a valid timestamp.")
    return result.tz_localize("UTC") if result.tzinfo is None else result.tz_convert("UTC")


def _expected_open_ms(timeframe: str, now: pd.Timestamp, *, closed: bool) -> int:
    duration_ms = int(timeframe_to_timedelta(timeframe).total_seconds() * 1000)
    now_ms = int(now.timestamp() * 1000)
    origin_ms = int(pd.Timestamp("1970-01-05T00:00:00Z").timestamp() * 1000) if timeframe.endswith("w") else 0
    return ((now_ms - origin_ms) // duration_ms) * duration_ms + origin_ms - (duration_ms if closed else 0)


def _frame_metadata(frame: pd.DataFrame, timeframe: str) -> dict[str, Any]:
    return {
        "rows": len(frame), "first_open": frame.iloc[0]["timestamp"].isoformat(),
        "last_open": frame.iloc[-1]["timestamp"].isoformat(),
        "last_close": (frame.iloc[-1]["timestamp"] + timeframe_to_timedelta(timeframe)).isoformat(),
        "sha256": hashlib.sha256(frame.to_csv(
            index=False, date_format="%Y-%m-%dT%H:%M:%S%z", lineterminator="\n", float_format="%.17g",
        ).encode("utf-8")).hexdigest(),
    }


def _has_conflicting_overlap(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    old = {row[0]: tuple(row[1:]) for row in left.itertuples(index=False, name=None)}
    return any(row[0] in old and old[row[0]] != tuple(row[1:]) for row in right.itertuples(index=False, name=None))


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
