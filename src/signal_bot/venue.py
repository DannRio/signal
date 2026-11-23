from __future__ import annotations

import math
from collections.abc import Iterable

DEFAULT_EXCHANGE_ID = "kucoinfutures"
DEFAULT_MARKET_TYPE = "swap"
DEFAULT_SYMBOL = "BTC/USDT:USDT"
DEFAULT_QUOTE_CURRENCIES = ("USDT",)
KUCOIN_MAX_KLINE_LIMIT = 500
MAX_NOTIONAL_MULTIPLE = 5.0


class VenueConfigurationError(ValueError):
    """Raised when a user-facing request escapes the KuCoin Futures scope."""


def normalize_exchange_id(value: str | None = None) -> str:
    normalized = (value or DEFAULT_EXCHANGE_ID).strip().lower()
    normalized = {
        "kucoin-futures": DEFAULT_EXCHANGE_ID,
        "kucoin_futures": DEFAULT_EXCHANGE_ID,
    }.get(normalized, normalized)
    if normalized != DEFAULT_EXCHANGE_ID:
        raise VenueConfigurationError(
            "This signal project is restricted to KuCoin Futures. "
            "Use exchange_id='kucoinfutures'."
        )
    return normalized


def normalize_market_type(value: str | None = None) -> str:
    normalized = (value or DEFAULT_MARKET_TYPE).strip().lower()
    if normalized in {"perp", "perpetual"}:
        normalized = DEFAULT_MARKET_TYPE
    if normalized != DEFAULT_MARKET_TYPE:
        raise VenueConfigurationError(
            "This signal project supports KuCoin USDT-margined perpetual "
            "swaps only. Use market_type='swap'."
        )
    return normalized


def enforce_venue(
    exchange_id: str | None = None,
    market_type: str | None = None,
) -> tuple[str, str]:
    return normalize_exchange_id(exchange_id), normalize_market_type(market_type)


def normalize_symbol(value: str) -> str:
    symbol = str(value).strip().upper().replace(" ", "")
    if not symbol:
        raise VenueConfigurationError("symbol cannot be empty.")
    if ":" in symbol:
        pair, settle = symbol.split(":", 1)
    else:
        pair, settle = symbol, "USDT"
    if "/" not in pair:
        raise VenueConfigurationError(
            "Use CCXT futures notation such as BTC/USDT:USDT. KuCoin raw "
            "contract IDs such as XBTUSDTM are not accepted."
        )
    base, quote = pair.split("/", 1)
    if not base or quote != "USDT" or settle != "USDT":
        raise VenueConfigurationError(
            "Only KuCoin USDT-margined perpetual symbols are supported, "
            "for example BTC/USDT:USDT."
        )
    return f"{base}/{quote}:{settle}"


def normalize_symbols(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(normalize_symbol(value) for value in values))


def normalize_quote_currencies(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(
        dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip())
    )
    if normalized != DEFAULT_QUOTE_CURRENCIES:
        raise VenueConfigurationError(
            "Only USDT-settled KuCoin perpetuals are supported. "
            "Use quote_currencies=('USDT',)."
        )
    return normalized


def validate_kline_limit(value: int | str) -> int:
    limit = int(value)
    if not 1 <= limit <= KUCOIN_MAX_KLINE_LIMIT:
        raise VenueConfigurationError(
            "KuCoin Futures candle requests must be between 1 and "
            f"{KUCOIN_MAX_KLINE_LIMIT}."
        )
    return limit


def validate_notional_multiple(
    value: float | str | None,
) -> float | None:
    if value is None or value == "":
        return None
    multiple = float(value)
    if not math.isfinite(multiple) or not 0 < multiple <= MAX_NOTIONAL_MULTIPLE:
        raise VenueConfigurationError(
            "maximum_notional_multiple must be greater than 0 and no more "
            f"than {MAX_NOTIONAL_MULTIPLE}. It is a notional-to-equity cap, "
            "not an exchange leverage setting."
        )
    return multiple
