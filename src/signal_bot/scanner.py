from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from .engine import RegimeSignalEngine
from .models import MarketSignal, SignalAction, to_dict
from .service import (
    CCXTMarketDataClient,
    LiveMarketRequest,
    RegimeBotService,
)
from .storage import SignalStore
from .venue import normalize_quote_currencies

_LEVERAGED_TOKEN = re.compile(
    r"(?:UP|DOWN|BULL|BEAR|[235]L|[235]S)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MarketCandidate:
    symbol: str
    base: str
    quote: str
    quote_volume: float
    spread_bps: float | None
    last_price: float | None

    def to_dict(self) -> dict[str, Any]:
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

    def validate(self) -> None:
        if not self.exchange_id.strip():
            raise ValueError("exchange_id cannot be empty.")
        if not self.market_type.strip():
            raise ValueError("market_type cannot be empty.")
        if self.max_markets <= 0 or self.max_markets > 500:
            raise ValueError("max_markets must be between 1 and 500.")
        if self.minimum_quote_volume < 0:
            raise ValueError("minimum_quote_volume cannot be negative.")
        if self.maximum_spread_bps is not None and self.maximum_spread_bps <= 0:
            raise ValueError("maximum_spread_bps must be positive or null.")
        if not self.symbols and not self.quote_currencies:
            raise ValueError("quote_currencies cannot be empty in auto-universe mode.")
        normalize_quote_currencies(self.quote_currencies)


class UniverseProvider(Protocol):
    def discover(self, request: UniverseRequest) -> list[MarketCandidate]: ...


class CCXTUniverseProvider:
    """Discover and rank liquid markets using exchange metadata and tickers."""

    def discover(self, request: UniverseRequest) -> list[MarketCandidate]:
        request.validate()
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover - packaging boundary.
            raise RuntimeError("CCXT is required for universe discovery.") from exc

        exchange_id = request.exchange_id.strip().lower()
        if not hasattr(ccxt, exchange_id):
            raise ValueError(f"Unsupported CCXT exchange: {exchange_id!r}.")
        exchange = getattr(ccxt, exchange_id)(
            {
                "enableRateLimit": True,
                "options": {"defaultType": request.market_type.strip().lower()},
            }
        )
        markets = exchange.load_markets()
        tickers: dict[str, dict] = {}
        if exchange.has.get("fetchTickers"):
            tickers = exchange.fetch_tickers()

        if request.symbols:
            missing = [symbol for symbol in request.symbols if symbol not in markets]
            if missing:
                raise ValueError(f"Unknown market symbols: {missing}")
            candidates = [
                self._candidate(markets[symbol], tickers.get(symbol, {}))
                for symbol in dict.fromkeys(request.symbols)
            ]
            return candidates[: request.max_markets]

        if not tickers:
            raise NotImplementedError(
                f"{exchange_id} does not provide fetchTickers; use explicit symbols."
            )

        quotes = {value.strip().upper() for value in request.quote_currencies}
        candidates: list[MarketCandidate] = []
        for symbol, market in markets.items():
            if not self._matches_market_type(market, request.market_type):
                continue
            if market.get("active") is False:
                continue
            base = str(market.get("base") or "")
            quote = str(market.get("quote") or "")
            if not base or quote.upper() not in quotes:
                continue
            settle = str(market.get("settle") or quote).upper()
            if settle != "USDT":
                continue
            if (
                not request.include_leveraged_tokens
                and _LEVERAGED_TOKEN.search(base)
            ):
                continue
            candidate = self._candidate(market, tickers.get(symbol, {}))
            if candidate.quote_volume < request.minimum_quote_volume:
                continue
            if (
                request.maximum_spread_bps is not None
                and candidate.spread_bps is not None
                and candidate.spread_bps > request.maximum_spread_bps
            ):
                continue
            candidates.append(candidate)

        candidates.sort(
            key=lambda item: (
                item.quote_volume,
                -(item.spread_bps if item.spread_bps is not None else math.inf),
            ),
            reverse=True,
        )
        return candidates[: request.max_markets]

    @staticmethod
    def _matches_market_type(market: dict, market_type: str) -> bool:
        value = market_type.strip().lower()
        if value == "spot":
            return bool(market.get("spot") or market.get("type") == "spot")
        if value in {"swap", "perpetual"}:
            return bool(market.get("swap") or market.get("type") == "swap")
        if value in {"future", "futures"}:
            return bool(market.get("future") or market.get("type") == "future")
        return str(market.get("type") or "").lower() == value

    @staticmethod
    def _candidate(market: dict, ticker: dict) -> MarketCandidate:
        bid = _finite_or_none(ticker.get("bid"))
        ask = _finite_or_none(ticker.get("ask"))
        last = _finite_or_none(ticker.get("last") or ticker.get("close"))
        quote_volume = _finite_or_none(ticker.get("quoteVolume"))
        if quote_volume is None:
            base_volume = _finite_or_none(ticker.get("baseVolume"))
            quote_volume = (
                base_volume * last
                if base_volume is not None and last is not None
                else 0.0
            )
        spread_bps: float | None = None
        if bid is not None and ask is not None and bid > 0 and ask >= bid:
            midpoint = (bid + ask) / 2.0
            spread_bps = (ask - bid) / midpoint * 10_000.0
        return MarketCandidate(
            symbol=str(market.get("symbol")),
            base=str(market.get("base") or ""),
            quote=str(market.get("quote") or ""),
            quote_volume=float(quote_volume or 0.0),
            spread_bps=spread_bps,
            last_price=last,
        )


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

    def validate(self) -> None:
        if not self.symbols and not self.auto_universe:
            raise ValueError("Provide symbols or enable auto_universe.")
        if self.symbols and self.auto_universe:
            raise ValueError("Use explicit symbols or auto_universe, not both.")
        if self.limit <= 0 or self.limit > 500:
            raise ValueError("KuCoin Futures limit must be between 1 and 500.")
        if self.equity <= 0 or not math.isfinite(self.equity):
            raise ValueError("equity must be positive and finite.")


@dataclass(frozen=True)
class ScanItem:
    rank: int
    candidate: MarketCandidate
    signal: MarketSignal | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "candidate": self.candidate.to_dict(),
            "signal": to_dict(self.signal) if self.signal is not None else None,
            "error": self.error,
        }


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "exchange_id": self.exchange_id,
            "market_type": self.market_type,
            "execution_timeframe": self.execution_timeframe,
            "requested_markets": self.requested_markets,
            "analyzed_markets": self.analyzed_markets,
            "failed_markets": self.failed_markets,
            "run_id": self.run_id,
            "items": [item.to_dict() for item in self.items],
        }


class MarketScanner:
    def __init__(
        self,
        engine: RegimeSignalEngine | None = None,
        *,
        store: SignalStore | None = None,
        universe_provider: UniverseProvider | None = None,
        service_factory=None,
    ) -> None:
        self.engine = engine or RegimeSignalEngine()
        self.store = store
        self.universe_provider = universe_provider or CCXTUniverseProvider()
        self.service_factory = service_factory

    def scan(self, request: ScanRequest) -> ScanReport:
        request.validate()
        candidates = self.universe_provider.discover(
            UniverseRequest(
                exchange_id=request.exchange_id,
                market_type=request.market_type,
                symbols=request.symbols,
                quote_currencies=request.quote_currencies,
                max_markets=request.max_markets,
                minimum_quote_volume=request.minimum_quote_volume,
                maximum_spread_bps=request.maximum_spread_bps,
            )
        )
        if not candidates:
            raise ValueError("No markets passed the universe filters.")

        if self.service_factory is not None:
            service = self.service_factory(self.engine)
        else:
            shared_client = CCXTMarketDataClient(
                request.exchange_id,
                request.market_type,
            )
            service = RegimeBotService(
                self.engine,
                client_factory=lambda _exchange, _market: shared_client,
            )

        analyzed: list[tuple[MarketCandidate, MarketSignal]] = []
        failures: list[tuple[MarketCandidate, str]] = []
        for candidate in candidates:
            try:
                signal = service.analyze_live(
                    LiveMarketRequest(
                        exchange_id=request.exchange_id,
                        market_type=request.market_type,
                        symbol=candidate.symbol,
                        execution_timeframe=request.execution_timeframe,
                        context_timeframes=request.context_timeframes,
                        limit=request.limit,
                    ),
                    equity=request.equity,
                    risk_percent=request.risk_percent,
                    maximum_notional_multiple=request.maximum_notional_multiple,
                )
                analyzed.append((candidate, signal))
                if self.store is not None:
                    self.store.record_signal(
                        signal,
                        exchange_id=request.exchange_id,
                        market_type=request.market_type,
                    )
            except Exception as exc:  # isolate per-market operational failures.
                failures.append((candidate, str(exc)))

        analyzed.sort(key=lambda pair: _rank_key(pair[0], pair[1]), reverse=True)
        items: list[ScanItem] = []
        rank = 1
        for candidate, signal in analyzed:
            items.append(ScanItem(rank, candidate, signal, None))
            rank += 1
        for candidate, error in failures:
            items.append(ScanItem(rank, candidate, None, error))
            rank += 1

        report = ScanReport(
            generated_at=datetime.now(UTC).isoformat(),
            exchange_id=request.exchange_id,
            market_type=request.market_type,
            execution_timeframe=request.execution_timeframe,
            requested_markets=len(candidates),
            analyzed_markets=len(analyzed),
            failed_markets=len(failures),
            items=tuple(items),
        )
        if self.store is not None:
            payload = report.to_dict()
            run_id = self.store.record_scan(
                exchange_id=request.exchange_id,
                market_type=request.market_type,
                request=asdict(request),
                result=payload,
            )
            report = replace(report, run_id=run_id)
        return report


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rank_key(
    candidate: MarketCandidate,
    signal: MarketSignal,
) -> tuple[float, ...]:
    priority = {
        SignalAction.LONG: 5.0,
        SignalAction.SHORT: 5.0,
        SignalAction.WATCH_LONG: 3.0,
        SignalAction.WATCH_SHORT: 3.0,
        SignalAction.NO_TRADE: 1.0,
    }[signal.action]
    spread_quality = (
        -candidate.spread_bps
        if candidate.spread_bps is not None
        else -10_000.0
    )
    return (
        priority,
        signal.confidence,
        signal.consensus * 100.0,
        abs(signal.score),
        math.log10(max(candidate.quote_volume, 1.0)),
        spread_quality,
    )
