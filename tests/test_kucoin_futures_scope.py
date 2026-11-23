from __future__ import annotations

import pytest

from signal_bot.machine_cli import _scan_request, build_parser
from signal_bot.venue import (
    DEFAULT_EXCHANGE_ID,
    DEFAULT_MARKET_TYPE,
    VenueConfigurationError,
    enforce_venue,
    normalize_symbol,
    validate_kline_limit,
    validate_notional_multiple,
)


def test_symbol_normalization_for_usdt_perpetuals() -> None:
    assert normalize_symbol("btc/usdt") == "BTC/USDT:USDT"
    assert normalize_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"


def test_non_usdt_or_raw_contract_symbols_are_rejected() -> None:
    with pytest.raises(VenueConfigurationError):
        normalize_symbol("BTC/USD:BTC")
    with pytest.raises(VenueConfigurationError):
        normalize_symbol("XBTUSDTM")


def test_user_facing_venue_is_fixed() -> None:
    assert enforce_venue("kucoin-futures", "perpetual") == (
        DEFAULT_EXCHANGE_ID,
        DEFAULT_MARKET_TYPE,
    )
    with pytest.raises(VenueConfigurationError):
        enforce_venue("kraken", "spot")


def test_machine_scan_defaults_to_kucoin_futures_and_normalizes_symbols() -> None:
    args = build_parser().parse_args(["scan", "--symbols", "BTC/USDT,ETH/USDT"])
    request = _scan_request(args)
    assert request.exchange_id == DEFAULT_EXCHANGE_ID
    assert request.market_type == DEFAULT_MARKET_TYPE
    assert request.symbols == ("BTC/USDT:USDT", "ETH/USDT:USDT")
    assert request.quote_currencies == ("USDT",)
    assert request.minimum_quote_volume == 1_000_000.0
    assert request.maximum_spread_bps == 25.0


def test_kucoin_request_and_notional_caps() -> None:
    assert validate_kline_limit(500) == 500
    assert validate_notional_multiple(5) == 5.0
    with pytest.raises(VenueConfigurationError):
        validate_kline_limit(501)
    with pytest.raises(VenueConfigurationError):
        validate_notional_multiple(5.01)
