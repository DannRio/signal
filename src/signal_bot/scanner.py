from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Protocol

from .engine import RegimeSignalEngine
from .futures import ContractPlanError, ContractSpec, build_contract_plan
from .models import MarketSignal, SignalAction, to_dict
from .service import CCXTMarketDataClient, LiveMarketRequest, RegimeBotService
from .storage import SignalStore
from .validation import timeframe_to_timedelta
from .venue import (
    enforce_venue,
    normalize_quote_currencies,
    normalize_symbols,
    validate_notional_multiple,
)

_CONFIRMED = {SignalAction.LONG, SignalAction.SHORT}


class ScanInProgressError(RuntimeError):
    code = "scan_in_progress"


@dataclass(frozen=True)
class MarketCandidate:
    symbol: str
    base: str
    quote: str
    quote_volume: float
    spread_bps: float | None
    last_price: float | None
    bid: float | None = None
    ask: float | None = None
    quote_timestamp: str | None = None
    observed_at: str | None = None
    metadata_observed_at: str | None = None
    volume_source: str = "unavailable"
    funding_rate: float | None = None
    funding_interval_hours: float | None = None
    next_funding_at: str | None = None
    mark_price: float | None = None
    index_price: float | None = None
    contract_spec: ContractSpec | None = None
    metadata_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {key: value for key, value in vars(self).items() if key != "contract_spec"}
        result["contract_spec"] = self.contract_spec.to_dict() if self.contract_spec else None
        return result


@dataclass(frozen=True)
class MarketRejection:
    symbol: str
    code: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class UniverseRequest:
    exchange_id: str
    market_type: str = "swap"
    symbols: tuple[str, ...] = ()
    quote_currencies: tuple[str, ...] = ("USDT",)
    max_markets: int = 20
    minimum_quote_volume: float = 1_000_000.0
    maximum_spread_bps: float | None = 25.0
    include_leveraged_tokens: bool = False
    maximum_quote_age_seconds: float = 30.0

    def validate(self) -> None:
        enforce_venue(self.exchange_id, self.market_type)
        normalize_quote_currencies(self.quote_currencies)
        normalize_symbols(self.symbols)
        if isinstance(self.max_markets, bool) or not isinstance(self.max_markets, int) or not 1 <= self.max_markets <= 500:
            raise ValueError("max_markets must be an integer between 1 and 500.")
        if not math.isfinite(self.minimum_quote_volume) or self.minimum_quote_volume < 0:
            raise ValueError("minimum_quote_volume must be finite and nonnegative.")
        if self.maximum_spread_bps is not None and (
            not math.isfinite(self.maximum_spread_bps) or self.maximum_spread_bps <= 0
        ):
            raise ValueError("maximum_spread_bps must be positive and finite, or null.")
        if not math.isfinite(self.maximum_quote_age_seconds) or not 0 < self.maximum_quote_age_seconds <= 300:
            raise ValueError("maximum_quote_age_seconds must be greater than 0 and at most 300.")


@dataclass(frozen=True)
class UniverseSnapshot:
    candidates: tuple[MarketCandidate, ...]
    rejections: tuple[MarketRejection, ...] = ()
    considered_markets: int = 0
    excluded_by_limit: int = 0


class UniverseProvider(Protocol):
    def discover(self, request: UniverseRequest) -> list[MarketCandidate]: ...


class CCXTUniverseProvider:
    """Join fresh BBO quotes with contract turnover and eligibility metadata."""

    def __init__(self, *, client: CCXTMarketDataClient | None = None, now_factory=None) -> None:
        self.client = client
        self.now_factory = now_factory or (lambda: datetime.now(UTC))
        self._markets: dict[str, dict] = {}
        self._metadata_observed_at: datetime | None = None

    def discover(self, request: UniverseRequest) -> list[MarketCandidate]:
        return list(self.discover_report(request).candidates)

    def discover_report(self, request: UniverseRequest) -> UniverseSnapshot:
        request.validate()
        if self.client is None:
            exchange_id, market_type = enforce_venue(request.exchange_id, request.market_type)
            self.client = CCXTMarketDataClient(exchange_id, market_type)
        # Contracts/active contains rolling volume and status; refresh every cycle.
        self._markets = self.client.load_markets(reload=True)
        if not isinstance(self._markets, dict) or not self._markets:
            raise RuntimeError("Exchange returned no usable contract metadata.")
        self._metadata_observed_at = _utc(self.now_factory())
        tickers = self._fetch_quotes()
        observed = _utc(self.now_factory())
        symbols = normalize_symbols(request.symbols) if request.symbols else tuple(sorted(self._markets))
        candidates: list[MarketCandidate] = []
        rejections: list[MarketRejection] = []
        for symbol in symbols:
            market = self._markets.get(symbol)
            if market is None:
                rejections.append(MarketRejection(symbol, "unknown_symbol", "Symbol is absent from KuCoin contract metadata."))
                continue
            try:
                rejection = self._market_rejection(symbol, market, request)
                if rejection is not None:
                    rejections.append(rejection)
                    continue
                candidate = self._candidate(market, tickers.get(symbol, {}), observed)
                rejection = candidate_rejection(candidate, request, now=observed)
                if rejection is not None:
                    rejections.append(rejection)
                else:
                    candidates.append(candidate)
            except (TypeError, ValueError, AttributeError, KeyError) as exc:
                rejections.append(MarketRejection(symbol, "malformed_market", str(exc)))
        candidates.sort(key=lambda item: (-item.quote_volume, item.spread_bps, item.symbol))
        limited = max(0, len(candidates) - request.max_markets)
        return UniverseSnapshot(tuple(candidates[:request.max_markets]), tuple(rejections), len(symbols), limited)

    def refresh_quotes(self, candidates: list[MarketCandidate]) -> dict[str, MarketCandidate]:
        tickers = self._fetch_quotes()
        observed = _utc(self.now_factory())
        refreshed = {}
        for candidate in candidates:
            try:
                refreshed[candidate.symbol] = self._candidate(self._markets[candidate.symbol], tickers.get(candidate.symbol, {}), observed)
            except (TypeError, ValueError, AttributeError, KeyError):
                refreshed[candidate.symbol] = replace(candidate, bid=None, ask=None, spread_bps=None, quote_timestamp=None)
        return refreshed

    def _fetch_quotes(self) -> dict[str, dict]:
        if self.client is None:
            raise RuntimeError("Universe client has not been initialized.")
        exchange = self.client.exchange
        if exchange.has.get("fetchBidsAsks"):
            result = self.client.call_with_retries(exchange.fetch_bids_asks)
        # Older CCXT versions need the BBO endpoint explicitly; the default
        # fetch_tickers endpoint is contracts/active, without bid and ask.
        elif exchange.has.get("fetchTickers"):
            result = self.client.call_with_retries(
                exchange.fetch_tickers, params={"method": "futuresPublicGetAllTickers"}
            )
        else:
            raise NotImplementedError("KuCoin bulk bid/ask quotes are unavailable in this CCXT adapter.")
        if not isinstance(result, dict) or not result:
            raise RuntimeError("Exchange returned no usable bid/ask quotes.")
        return result

    @staticmethod
    def _market_rejection(symbol: str, market: dict, request: UniverseRequest) -> MarketRejection | None:
        info = market.get("info") or {}
        checks = (
            (market.get("active") is True and str(info.get("status", "Open")).lower() == "open", "inactive_contract", "Contract is not confirmed active and open."),
            (market.get("swap") is True and not market.get("future") and not market.get("expiry"), "not_perpetual", "Only perpetual swaps are supported."),
            (market.get("linear") is True and not market.get("inverse"), "not_linear", "Only linear contracts are supported."),
            (str(market.get("quote", "")).upper() == "USDT" and str(market.get("settle", "")).upper() == "USDT", "wrong_settlement", "Both quote and settlement currencies must be USDT."),
            (str(info.get("marketStage", "NORMAL")).upper() == "NORMAL", "nonstandard_market_stage", "Pre-market and other non-normal contract stages are excluded."),
            (request.include_leveraged_tokens or info.get("isLeveragedToken") is not True, "leveraged_token", "Leveraged-token underlying is excluded."),
        )
        for passed, code, reason in checks:
            if not passed:
                return MarketRejection(symbol, code, reason)
        return None

    def _candidate(self, market: dict, ticker: dict, observed: datetime) -> MarketCandidate:
        info = market.get("info") or {}
        raw_quote = ticker.get("info") or {}
        bid = _finite_or_none(ticker.get("bid"))
        ask = _finite_or_none(ticker.get("ask"))
        last = _finite_or_none(ticker.get("last"))
        if last is None:
            last = _finite_or_none(ticker.get("close"))
        turnover = _finite_or_none(info.get("turnoverOf24h"))
        source = "contracts/active.turnoverOf24h"
        if turnover is None:
            turnover = _finite_or_none(ticker.get("quoteVolume"))
            source = "ticker.quoteVolume" if turnover is not None else "unavailable"
        # Do not substitute baseVolume * last: it is not measured quote turnover.
        timestamp = _finite_or_none(ticker.get("timestamp"))
        if timestamp is None:
            nanoseconds = _finite_or_none(raw_quote.get("ts"))
            timestamp = nanoseconds / 1_000_000 if nanoseconds is not None else None
        quote_time = _milliseconds_to_iso(timestamp)
        spread = None
        if bid is not None and ask is not None and 0 < bid <= ask:
            spread = (ask - bid) / ((ask + bid) / 2.0) * 10_000.0
        metadata_error = None
        spec = None
        try:
            spec = ContractSpec.from_market(market)
        except ContractPlanError as exc:
            metadata_error = str(exc)
        interval = _finite_or_none(info.get("currentFundingRateGranularity"))
        if interval is None:
            interval = _finite_or_none(info.get("fundingRateGranularity"))
        next_funding = _milliseconds_to_iso(_finite_or_none(info.get("nextFundingRateDateTime")))
        return MarketCandidate(
            symbol=str(market.get("symbol")), base=str(market.get("base") or ""), quote=str(market.get("quote") or ""),
            quote_volume=turnover if turnover is not None else 0.0, spread_bps=spread, last_price=last,
            bid=bid, ask=ask, quote_timestamp=quote_time, observed_at=observed.isoformat(), volume_source=source,
            metadata_observed_at=self._metadata_observed_at.isoformat() if self._metadata_observed_at else None,
            funding_rate=_finite_or_none(info.get("fundingFeeRate")),
            funding_interval_hours=interval / 3_600_000 if interval is not None and interval > 0 else None,
            next_funding_at=next_funding, mark_price=_finite_or_none(info.get("markPrice")),
            index_price=_finite_or_none(info.get("indexPrice")), contract_spec=spec, metadata_error=metadata_error,
        )


def candidate_rejection(candidate: MarketCandidate, request: UniverseRequest, *, now: datetime) -> MarketRejection | None:
    def reject(code: str, reason: str) -> MarketRejection:
        return MarketRejection(candidate.symbol, code, reason)

    if candidate.contract_spec is None:
        return reject("invalid_contract_metadata", candidate.metadata_error or "Contract lot, tick, and size metadata are required.")
    if candidate.volume_source == "unavailable" or not math.isfinite(candidate.quote_volume) or candidate.quote_volume < 0:
        return reject("missing_volume", "Measured 24-hour quote turnover is unavailable or invalid.")
    if candidate.quote_volume < request.minimum_quote_volume:
        return reject("low_volume", f"24-hour USDT turnover {candidate.quote_volume:.0f} is below {request.minimum_quote_volume:.0f}.")
    if candidate.bid is None or candidate.ask is None or not all(math.isfinite(v) and v > 0 for v in (candidate.bid, candidate.ask)) or candidate.ask < candidate.bid:
        return reject("invalid_quote", "A finite positive, uncrossed bid and ask are required.")
    if candidate.spread_bps is None or not math.isfinite(candidate.spread_bps):
        return reject("missing_spread", "Spread could not be established from the bid and ask.")
    if request.maximum_spread_bps is not None and candidate.spread_bps > request.maximum_spread_bps:
        return reject("wide_spread", f"Spread {candidate.spread_bps:.2f} bps exceeds {request.maximum_spread_bps:.2f} bps.")
    if not candidate.quote_timestamp:
        return reject("missing_quote_timestamp", "The exchange did not supply a quote timestamp.")
    try:
        quote_time = datetime.fromisoformat(candidate.quote_timestamp)
        if quote_time.tzinfo is None:
            raise ValueError("Quote timezone is missing.")
    except (TypeError, ValueError):
        return reject("invalid_quote_timestamp", "Quote timestamp must be a valid timezone-aware ISO timestamp.")
    age = (_utc(now) - quote_time).total_seconds()
    if age < -5:
        return reject("future_quote", "Quote is more than 5 seconds in the future; check the computer clock.")
    if age > request.maximum_quote_age_seconds:
        return reject("stale_quote", f"Quote age {age:.1f}s exceeds {request.maximum_quote_age_seconds:.1f}s.")
    return None


@dataclass(frozen=True)
class ScanRequest:
    exchange_id: str
    market_type: str = "swap"
    execution_timeframe: str = "15m"
    context_timeframes: tuple[str, ...] = ("1h", "4h")
    symbols: tuple[str, ...] = ()
    auto_universe: bool = False
    quote_currencies: tuple[str, ...] = ("USDT",)
    max_markets: int = 20
    minimum_quote_volume: float = 1_000_000.0
    maximum_spread_bps: float | None = 25.0
    limit: int = 500
    equity: float = 10_000.0
    risk_percent: float | None = None
    maximum_notional_multiple: float | None = None
    maximum_quote_age_seconds: float = 30.0
    maximum_staleness_bars: int = 1
    scan_timeout_seconds: float = 180.0

    def normalized(self) -> ScanRequest:
        exchange_id, market_type = enforce_venue(self.exchange_id, self.market_type)
        return replace(self, exchange_id=exchange_id, market_type=market_type, symbols=normalize_symbols(self.symbols),
                       execution_timeframe=self.execution_timeframe.strip().lower(),
                       context_timeframes=tuple(dict.fromkeys(value.strip().lower() for value in self.context_timeframes)),
                       quote_currencies=normalize_quote_currencies(self.quote_currencies))

    def universe_request(self) -> UniverseRequest:
        return UniverseRequest(exchange_id=self.exchange_id, market_type=self.market_type, symbols=self.symbols,
                               quote_currencies=self.quote_currencies, max_markets=self.max_markets,
                               minimum_quote_volume=self.minimum_quote_volume, maximum_spread_bps=self.maximum_spread_bps,
                               maximum_quote_age_seconds=self.maximum_quote_age_seconds)

    def validate(self) -> None:
        self.universe_request().validate()
        if not self.symbols and not self.auto_universe:
            raise ValueError("Provide symbols or enable auto_universe.")
        if self.symbols and self.auto_universe:
            raise ValueError("Use explicit symbols or auto_universe, not both.")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= 500:
            raise ValueError("KuCoin Futures limit must be an integer between 1 and 500.")
        if not math.isfinite(self.equity) or self.equity <= 0:
            raise ValueError("equity must be positive and finite.")
        if self.risk_percent is not None and (not math.isfinite(self.risk_percent) or self.risk_percent <= 0):
            raise ValueError("risk_percent must be positive and finite.")
        validate_notional_multiple(self.maximum_notional_multiple)
        if isinstance(self.maximum_staleness_bars, bool) or not isinstance(self.maximum_staleness_bars, int) or not 1 <= self.maximum_staleness_bars <= 3:
            raise ValueError("maximum_staleness_bars must be an integer between 1 and 3.")
        if not math.isfinite(self.scan_timeout_seconds) or not 10 <= self.scan_timeout_seconds <= 900:
            raise ValueError("scan_timeout_seconds must be between 10 and 900.")
        duration = timeframe_to_timedelta(self.execution_timeframe)
        for value in self.context_timeframes:
            if timeframe_to_timedelta(value) < duration:
                raise ValueError("Context timeframes cannot be lower than the execution timeframe.")


@dataclass(frozen=True)
class ScanItem:
    rank: int
    candidate: MarketCandidate
    signal: MarketSignal | None
    error: str | None
    status: str = "analyzed"
    error_code: str | None = None
    actionable: bool = False
    blocked_reasons: tuple[str, ...] = ()
    valid_until: str | None = None
    contract_plan: dict[str, Any] | None = None
    data_quality: dict[str, Any] = field(default_factory=dict)

    def is_actionable(self, now: datetime | None = None) -> bool:
        return bool(self.actionable and self.valid_until and _utc(now or datetime.now(UTC)) < datetime.fromisoformat(self.valid_until))

    def to_dict(self) -> dict[str, Any]:
        return {"rank": self.rank, "candidate": self.candidate.to_dict(),
                "signal": to_dict(self.signal) if self.signal is not None else None, "error": self.error,
                "status": self.status, "error_code": self.error_code, "actionable": self.actionable,
                "blocked_reasons": list(self.blocked_reasons), "valid_until": self.valid_until,
                "contract_plan": self.contract_plan, "data_quality": self.data_quality}


@dataclass(frozen=True)
class ScanReport:
    generated_at: str
    exchange_id: str
    market_type: str
    execution_timeframe: str
    requested_markets: int
    analyzed_markets: int
    failed_markets: int
    items: tuple[ScanItem, ...]
    run_id: str | None = None
    status: str = "complete"
    started_at: str | None = None
    duration_seconds: float = 0.0
    considered_markets: int = 0
    rejected_markets: int = 0
    excluded_by_limit: int = 0
    actionable_markets: int = 0
    blocked_markets: int = 0
    rejections: tuple[MarketRejection, ...] = ()
    errors: tuple[dict[str, str], ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = {key: value for key, value in vars(self).items() if key not in {"items", "rejections"}}
        result["items"] = [item.to_dict() for item in self.items]
        result["rejections"] = [item.to_dict() for item in self.rejections]
        result["errors"] = list(self.errors)
        return result


class MarketScanner:
    def __init__(self, engine: RegimeSignalEngine | None = None, *, store: SignalStore | None = None,
                 universe_provider: UniverseProvider | None = None, service_factory=None,
                 now_factory=None, monotonic_factory=None) -> None:
        self.engine = engine or RegimeSignalEngine()
        self.store = store
        self.universe_provider = universe_provider
        self.service_factory = service_factory
        self.now_factory = now_factory or (lambda: datetime.now(UTC))
        self.monotonic_factory = monotonic_factory or time.monotonic
        self._client: CCXTMarketDataClient | None = None
        self._lock = threading.Lock()

    def scan(self, request: ScanRequest) -> ScanReport:
        request = request.normalized()
        request.validate()
        if request.limit < self.engine.config.minimum_bars:
            raise ValueError(f"limit must be at least {self.engine.config.minimum_bars} candles.")
        if request.risk_percent is not None and request.risk_percent > self.engine.config.risk.maximum_risk_percent:
            raise ValueError(f"risk_percent exceeds the configured cap of {self.engine.config.risk.maximum_risk_percent}%.")
        validate_notional_multiple(request.maximum_notional_multiple if request.maximum_notional_multiple is not None else self.engine.config.risk.maximum_notional_multiple)
        if not self._lock.acquire(blocking=False):
            raise ScanInProgressError("A scan is already running. Wait for it to finish before starting another.")
        try:
            return self._scan(request)
        finally:
            self._lock.release()

    def _scan(self, request: ScanRequest) -> ScanReport:
        started = _utc(self.now_factory())
        timer = self.monotonic_factory()
        universe_request = request.universe_request()
        errors: list[dict[str, str]] = []
        snapshot = UniverseSnapshot(())
        items: list[ScanItem] = []
        provenance = self._provenance(request)
        try:
            if self.universe_provider is None or self.service_factory is None:
                if self._client is None:
                    self._client = CCXTMarketDataClient(request.exchange_id, request.market_type)
            if self.universe_provider is None:
                self.universe_provider = CCXTUniverseProvider(client=self._client, now_factory=self.now_factory)
            provider = self.universe_provider
            if hasattr(provider, "discover_report"):
                snapshot = provider.discover_report(universe_request)
            else:
                candidates = tuple(provider.discover(universe_request))
                snapshot = UniverseSnapshot(candidates, considered_markets=len(candidates))
            service = self.service_factory(self.engine) if self.service_factory else RegimeBotService(
                self.engine, client_factory=lambda _exchange, _market: self._client
            )
            for candidate in snapshot.candidates:
                if self.monotonic_factory() - timer >= request.scan_timeout_seconds:
                    items.append(ScanItem(0, candidate, None, "Scan time budget exhausted before this market was analyzed.",
                                          status="failed", error_code="scan_budget_exceeded"))
                    continue
                try:
                    signal = service.analyze_live(
                        LiveMarketRequest(exchange_id=request.exchange_id, market_type=request.market_type,
                                          symbol=candidate.symbol, execution_timeframe=request.execution_timeframe,
                                          context_timeframes=request.context_timeframes, limit=request.limit,
                                          maximum_staleness_bars=request.maximum_staleness_bars),
                        equity=request.equity, risk_percent=request.risk_percent,
                        maximum_notional_multiple=request.maximum_notional_multiple, as_of=started,
                    )
                    items.append(ScanItem(0, candidate, signal, None,
                                          data_quality=dict(getattr(service, "last_frame_metadata", {}))))
                except Exception as exc:  # isolate per-market failures; never convert them to NO_TRADE.
                    items.append(ScanItem(0, candidate, None, str(exc), status="failed",
                                          error_code=getattr(exc, "code", "analysis_failed")))
            # A quote captured before a long OHLCV scan may already be stale. Refresh
            # immediately before evaluating actionability, using the same limiter.
            refresh_error = None
            if items and hasattr(provider, "refresh_quotes"):
                try:
                    refreshed = provider.refresh_quotes([item.candidate for item in items])
                    items = [replace(item, candidate=refreshed.get(item.candidate.symbol, item.candidate)) for item in items]
                except Exception as exc:
                    refresh_error = "Fresh quote verification failed: " + str(exc)
                    errors.append({"code": "quote_refresh_failed", "message": refresh_error})
            finished = _utc(self.now_factory())
            qualified = []
            for item in items:
                try:
                    qualified.append(self._qualify(item, request, finished, refresh_error))
                except Exception as exc:
                    qualified.append(replace(item, status="failed", actionable=False,
                                             error=str(exc), error_code="qualification_failed"))
            items = qualified
        except Exception as exc:
            errors.append({"code": getattr(exc, "code", "universe_unavailable"), "message": str(exc)})
        finished = _utc(self.now_factory())
        items.sort(key=_rank_key)
        items = [replace(item, rank=rank) for rank, item in enumerate(items, 1)]
        analyzed = sum(item.signal is not None for item in items)
        failed = sum(item.status == "failed" for item in items)
        blocked = sum(item.status == "blocked" for item in items)
        status = "complete"
        if failed or errors:
            status = "degraded" if analyzed else "failed"
        elif not items:
            status = "empty"
        report = ScanReport(
            generated_at=finished.isoformat(), exchange_id=request.exchange_id, market_type=request.market_type,
            execution_timeframe=request.execution_timeframe, requested_markets=len(snapshot.candidates),
            analyzed_markets=analyzed, failed_markets=failed, items=tuple(items), status=status,
            started_at=started.isoformat(), duration_seconds=round(self.monotonic_factory() - timer, 3),
            considered_markets=snapshot.considered_markets, rejected_markets=len(snapshot.rejections),
            excluded_by_limit=snapshot.excluded_by_limit, actionable_markets=sum(item.actionable for item in items),
            blocked_markets=blocked, rejections=snapshot.rejections, errors=tuple(errors), provenance=provenance,
        )
        if self.store is not None:
            # Persist analyzed signals once, after qualification. A database failure
            # must be visible; silently losing the audit trail is not a successful run.
            for item in items:
                if item.signal is not None and item.status == "analyzed":
                    self.store.record_signal(item.signal, exchange_id=request.exchange_id, market_type=request.market_type)
            run_id = self.store.record_scan(exchange_id=request.exchange_id, market_type=request.market_type,
                                            request=asdict(request), result=report.to_dict())
            report = replace(report, run_id=run_id)
        return report

    @staticmethod
    def _qualify(item: ScanItem, request: ScanRequest, now: datetime, refresh_error: str | None) -> ScanItem:
        if item.signal is None:
            return item
        signal = item.signal
        problems: list[str] = []
        code = None
        if refresh_error:
            problems.append(refresh_error)
            code = "quote_refresh_failed"
        rejection = candidate_rejection(item.candidate, request.universe_request(), now=now)
        if rejection is not None:
            problems.append(rejection.reason)
            code = code or rejection.code
        expiry = _utc(signal.generated_at) + timeframe_to_timedelta(request.execution_timeframe)
        if now >= expiry:
            problems.append("Decision candle has expired; a new closed-candle scan is required.")
            code = code or "signal_expired"
        contract_plan = None
        if signal.action in _CONFIRMED:
            if signal.entry_plan is None:
                problems.append("Confirmed signal has no entry plan.")
                code = code or "missing_entry_plan"
            elif item.candidate.contract_spec is not None:
                try:
                    contract_plan = build_contract_plan(signal.entry_plan, item.candidate.contract_spec).to_dict()
                    current_price = item.candidate.ask if signal.action == SignalAction.LONG else item.candidate.bid
                    if current_price is None or not signal.entry_plan.entry_low <= current_price <= signal.entry_plan.entry_high:
                        problems.append("Current executable quote is outside the planned entry zone; do not chase the candle signal.")
                        code = code or "outside_entry_zone"
                except ContractPlanError as exc:
                    problems.append(str(exc))
                    code = code or exc.code
        if contract_plan is not None and contract_plan["net_reward_to_risk_1"] <= 0:
            problems.append("The first target does not cover estimated trading costs after contract rounding.")
            code = code or "nonpositive_net_reward"
        if rejection is None and item.candidate.quote_timestamp:
            quote_expiry = datetime.fromisoformat(item.candidate.quote_timestamp) + timedelta(seconds=request.maximum_quote_age_seconds)
            expiry = min(expiry, quote_expiry)
        actionable = signal.action in _CONFIRMED and not problems and contract_plan is not None and now < expiry
        return replace(item, status="blocked" if problems else "analyzed", error_code=code,
                       actionable=actionable, blocked_reasons=tuple(problems), valid_until=expiry.isoformat(),
                       contract_plan=contract_plan)

    def _provenance(self, request: ScanRequest) -> dict[str, Any]:
        config = asdict(self.engine.config)
        payload = {"engine": config, "scan": asdict(request)}
        fingerprint = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        versions = {}
        for package in ("market-signal-bot", "ccxt", "pandas", "numpy"):
            try:
                versions[package] = version(package)
            except PackageNotFoundError:
                versions[package] = "unavailable"
        return {"schema_version": 2, "config_sha256": fingerprint, "engine_config": config,
                "versions": versions, "ranking": "actionability_then_rule_alignment_not_expected_return",
                "confidence_is_probability": False, "execution_enabled": False,
                "funding_in_risk_estimate": False, "portfolio_exposure_checked": False}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _milliseconds_to_iso(value: float | None) -> str | None:
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _rank_key(item: ScanItem) -> tuple:
    signal = item.signal
    if signal is None:
        priority = -2
    elif item.status == "blocked":
        priority = -1
    elif item.actionable:
        priority = 5
    elif signal.action in {SignalAction.WATCH_LONG, SignalAction.WATCH_SHORT}:
        priority = 3
    else:
        priority = 1
    return (-priority, -(signal.confidence if signal else 0), -(signal.consensus if signal else 0),
            -abs(signal.score if signal else 0), -item.candidate.quote_volume,
            item.candidate.spread_bps if item.candidate.spread_bps is not None else math.inf,
            item.candidate.symbol)
