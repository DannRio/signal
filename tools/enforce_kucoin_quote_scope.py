from __future__ import annotations

from pathlib import Path


def replace_exact(path: str, old: str, new: str, *, expected: int = 1) -> None:
    target = Path(path)
    source = target.read_text(encoding="utf-8")
    count = source.count(old)
    if count != expected:
        raise RuntimeError(
            f"Expected {expected} occurrence(s) in {path}; found {count}: {old!r}"
        )
    target.write_text(source.replace(old, new), encoding="utf-8")


replace_exact(
    "src/signal_bot/api.py",
    "    normalize_symbol,\n    normalize_symbols,\n",
    "    normalize_quote_currencies,\n    normalize_symbol,\n    normalize_symbols,\n",
)
replace_exact(
    "src/signal_bot/api.py",
    '''                    quote_currencies=_string_tuple(
                        payload.get(
                            "quote_currencies", DEFAULT_QUOTE_CURRENCIES
                        )
                    ),
''',
    '''                    quote_currencies=normalize_quote_currencies(
                        _string_tuple(
                            payload.get(
                                "quote_currencies", DEFAULT_QUOTE_CURRENCIES
                            )
                        )
                    ),
''',
)

replace_exact(
    "src/signal_bot/machine_cli.py",
    "    normalize_symbol,\n    normalize_symbols,\n",
    "    normalize_quote_currencies,\n    normalize_symbol,\n    normalize_symbols,\n",
)
replace_exact(
    "src/signal_bot/machine_cli.py",
    "        quote_currencies=_items(args.quote_currencies),\n",
    "        quote_currencies=normalize_quote_currencies(\n            _items(args.quote_currencies)\n        ),\n",
)

replace_exact(
    "src/signal_bot/scanner.py",
    "from .storage import SignalStore\n",
    "from .storage import SignalStore\nfrom .venue import normalize_quote_currencies\n",
)
replace_exact(
    "src/signal_bot/scanner.py",
    '''        if not self.symbols and not self.quote_currencies:
            raise ValueError("quote_currencies cannot be empty in auto-universe mode.")
''',
    '''        if not self.symbols and not self.quote_currencies:
            raise ValueError("quote_currencies cannot be empty in auto-universe mode.")
        normalize_quote_currencies(self.quote_currencies)
''',
)
replace_exact(
    "src/signal_bot/scanner.py",
    '''            if not base or quote.upper() not in quotes:
                continue
''',
    '''            if not base or quote.upper() not in quotes:
                continue
            settle = str(market.get("settle") or quote).upper()
            if settle != "USDT":
                continue
''',
)

path = Path("tests/test_kucoin_futures_scope.py")
source = path.read_text(encoding="utf-8")
source = source.replace(
    "    normalize_symbol,\n",
    "    normalize_quote_currencies,\n    normalize_symbol,\n",
    1,
)
addition = '''


def test_only_usdt_quote_universe_is_accepted() -> None:
    assert normalize_quote_currencies(["usdt"]) == ("USDT",)
    with pytest.raises(VenueConfigurationError):
        normalize_quote_currencies(["USD"])
    with pytest.raises(VenueConfigurationError):
        normalize_quote_currencies(["USDT", "USD"])


def test_scan_api_rejects_non_usdt_universe_before_network() -> None:
    client = TestClient(create_app(store=SignalStore(":memory:")))
    response = client.post(
        "/v1/scan",
        json={
            "exchange_id": DEFAULT_EXCHANGE_ID,
            "market_type": DEFAULT_MARKET_TYPE,
            "auto_universe": True,
            "quote_currencies": ["USD"],
        },
    )
    assert response.status_code == 422
    assert "Only USDT-settled" in response.json()["detail"]
'''
if "test_only_usdt_quote_universe_is_accepted" in source:
    raise RuntimeError("KuCoin quote scope tests already present")
path.write_text(source.rstrip() + addition + "\n", encoding="utf-8")
