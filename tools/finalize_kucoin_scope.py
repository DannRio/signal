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
    '''                    maximum_notional_multiple=_optional_float(
                        payload, "maximum_notional_multiple"
                    ),
''',
    '''                    maximum_notional_multiple=validate_notional_multiple(
                        payload.get("maximum_notional_multiple")
                    ),
''',
)
replace_exact(
    "src/signal_bot/api.py",
    '            symbol = str(payload["symbol"])\n',
    '            symbol = normalize_symbol(str(payload["symbol"]))\n',
)
replace_exact(
    "src/signal_bot/api.py",
    '''                "execution_timeframe": execution,
                "exchange_id": DEFAULT_EXCHANGE_ID,
                "market_type": DEFAULT_MARKET_TYPE,
''',
    '''                "execution_timeframe": execution,
                "exchange_id": DEFAULT_EXCHANGE_ID,
                "market_type": DEFAULT_MARKET_TYPE,
                "symbol": normalize_symbol(str(payload["symbol"])),
''',
    expected=2,
)

replace_exact(
    "docs/PROFESSIONAL_MACHINE.md",
    "# Professional Signal Machine Architecture",
    "# KuCoin Futures Signal Machine Architecture",
)
replace_exact(
    "docs/PROFESSIONAL_MACHINE.md",
    "CCXT public market metadata / OHLCV",
    "KuCoin Futures public market metadata / OHLCV",
)
replace_exact(
    "docs/PROFESSIONAL_MACHINE.md",
    "- Spot research suppresses short entries.\n",
    "- Every user-facing route enforces `kucoinfutures`, `swap`, and USDT settlement.\n",
)
replace_exact(
    "docs/PROFESSIONAL_MACHINE.md",
    "## What remains a deployment decision\n\nExchange eligibility, derivatives availability, taxes, legal requirements, production hosting, external authentication, and any future execution adapter are deliberately outside this repository's signal boundary.\n",
    "## Futures-specific boundary\n\nThe current simulator uses futures OHLCV and permits long and short signals, but it does not yet apply historical funding settlements, KuCoin contract-lot rounding, maintenance-margin tiers, margin-mode behavior, or liquidation-price mechanics. A validation pass is therefore a research milestone, not authorization for live futures deployment.\n\nExchange eligibility, derivatives availability, taxes, legal requirements, production hosting, external authentication, and any future execution adapter remain outside the repository's signal boundary.\n",
)

replace_exact(
    "docs/VALIDATION.md",
    "4. Fee, spread, slippage, and—where applicable—funding stress tests.",
    "4. KuCoin fees, spread, slippage, and historical funding stress tests.",
)
replace_exact(
    "docs/VALIDATION.md",
    "## Current status\n\nThe repository contains a deterministic signal engine, a no-lookahead backtester, and automated tests. It does not yet contain evidence that the strategy has positive expectancy in live trading.\n",
    "## Current status\n\nThe repository contains a deterministic KuCoin Futures signal engine, a no-lookahead backtester, venue guardrails, and automated tests. It does not yet contain evidence that the strategy has positive expectancy in live trading. Historical funding, contract-lot rounding, maintenance margin, margin mode, and liquidation mechanics are not yet included in simulated P&L, so the current validation gate cannot by itself qualify a strategy for real futures capital.\n",
)

replace_exact(
    "docs/OPERATIONS.md",
    "Open `http://127.0.0.1:8081`.\n",
    "Open `http://127.0.0.1:8081`.\n\n## Fixed venue scope\n\n- Exchange id: `kucoinfutures`\n- Market type: `swap`\n- Settlement: `USDT`\n- Unified symbol format: `BTC/USDT:USDT`\n- Public Kline request limit: 500 candles; the downloader paginates longer ranges\n\nThe service rejects spot and non-KuCoin venue requests before market-data retrieval.\n",
)

replace_exact(
    "docs/ARCHITECTURE.md",
    "The deterministic engine has no exchange dependency. It accepts pandas OHLCV frames and returns immutable domain models. CCXT access exists only in `service.py`, making the decision logic testable without a network connection.\n",
    "The deterministic engine has no exchange dependency. It accepts pandas OHLCV frames and returns immutable domain models. CCXT access exists only in `service.py`, making the decision logic testable without a network connection. The operator boundary is intentionally narrower: CLI, API, dashboard, history, and scanner inputs are restricted to KuCoin USDT-margined perpetual swaps.\n",
)

path = Path("tests/test_kucoin_futures_scope.py")
source = path.read_text(encoding="utf-8")
addition = '''


def test_scan_api_rejects_excessive_notional_cap_before_network() -> None:
    client = TestClient(create_app(store=SignalStore(":memory:")))
    response = client.post(
        "/v1/scan",
        json={
            "exchange_id": DEFAULT_EXCHANGE_ID,
            "market_type": DEFAULT_MARKET_TYPE,
            "symbols": ["BTC/USDT:USDT"],
            "maximum_notional_multiple": 5.01,
        },
    )
    assert response.status_code == 422
    assert "no more than 5.0" in response.json()["detail"]


def test_frame_analysis_rejects_non_usdt_futures_symbol() -> None:
    client = TestClient(create_app(store=SignalStore(":memory:")))
    response = client.post(
        "/v1/analyze",
        json={
            "symbol": "BTC/USD:BTC",
            "execution_timeframe": "15m",
            "frames": {"15m": []},
        },
    )
    assert response.status_code == 422
    assert "USDT-margined perpetual" in response.json()["detail"]
'''
if "test_scan_api_rejects_excessive_notional_cap_before_network" in source:
    raise RuntimeError("Final KuCoin API tests already present")
path.write_text(source.rstrip() + addition + "\n", encoding="utf-8")
