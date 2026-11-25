# KuCoin Futures Signal Machine

A standalone KuCoin Futures research platform for USDT-margined perpetuals. It classifies markets, produces conditional long/short trade plans, scans liquid contracts, downloads multi-year history, backtests the same decision engine, and records evidence in a persistent journal. The internal market-data adapter remains modular, but every user-facing interface rejects non-KuCoin and spot requests.

It is deliberately **signal-only**. It does not submit orders, store exchange credentials, or claim that a backtest proves a profitable edge.

## Fixed venue scope

- CCXT exchange id: `kucoinfutures`
- Market type: `swap`
- Supported settlement: `USDT`
- Symbol format: `BTC/USDT:USDT` (the simpler `BTC/USDT` is normalized automatically)
- KuCoin raw IDs such as `XBTUSDTM` are intentionally rejected at the user boundary
- Public market data requires no API key; this project still does not place orders

KuCoin's futures Kline endpoint returns at most 500 candles per request, and a live CCXT request may return fewer rows than requested. The live adapter therefore paginates forward from a bounded timestamp, removes overlapping candles, and retains the latest requested closed-candle window. A public KuCoin Futures verification assembled 500 unique, ordered candles for each of 15m, 1h, and 4h and produced all three timeframe states.

Default backtests retain a conservative 10 bps fee estimate per side and 5 bps slippage per side; adjust the configuration only after documenting your actual fee tier and observed fills. Historical funding settlements are not yet deducted from trade P&L, so no validation result should be treated as complete evidence for live futures deployment until funding is modeled. The `maximum_notional_multiple` setting is only a modeled notional-to-equity cap; it does not change leverage on KuCoin, estimate liquidation. The scanner now produces a separate contract-rounded plan; historical backtests and the standalone Live Signal model retain base-quantity sizing.

## Major capabilities

- Multi-timeframe regimes: strong bullish, bullish, range, transition, bearish, and strong bearish
- Decisions: `LONG`, `SHORT`, `WATCH_LONG`, `WATCH_SHORT`, and `NO_TRADE`
- Breakout and pullback-continuation confirmation
- Entry zone, structural/ATR stop, TP1/TP2/TP3, break-even and trailing rules
- Risk-based sizing with fee and slippage estimates
- Uniform eligibility/liquidity screening for manual and automatic symbol lists
- Fresh BBO quotes, continuous closed-candle checks and expiring scan qualifications
- KuCoin contract/tick rounding with preserved risk and notional budgets
- Ranked multi-symbol scanning with rejection reasons and partial-failure diagnostics
- Paginated, resumable CCXT historical downloads with checksums and gap detection
- Closed-candle, next-bar, no-look-ahead backtesting
- Chronological train/validation/test evaluation
- Cost-stress and Monte Carlo robustness analysis
- A research gate that distinguishes working software from evidence suitable for paper validation
- Persistent SQLite journal for signals, scans, datasets, alerts, and backtests
- Console, JSONL, and generic webhook alerts with persistent event/sink deduplication and cooldowns
- Local browser dashboard with price chart, signal explanation, scanner, backtest charts, trade ledger, and journal
- FastAPI endpoints, Docker support, and automated Windows/Ubuntu CI on Python 3.11 and 3.12

## Windows PowerShell setup

```powershell
cd C:\Trading\Signal\signal

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Start the real dashboard

```powershell
signal-machine dashboard
```

Open:

```text
http://127.0.0.1:8081
```

The dashboard has four sections:

1. **Live Signal** — regime, decision, rule alignment, timeframe alignment, price chart, entry, stop and targets.
2. **Market Scanner** — qualified/blocked/failed outcomes, uniform universe filters, contract quantities, funding context, data diagnostics, and expiry across explicit symbols or a liquid universe.
3. **Backtest & Validation** — automatic historical download, equity curve, yearly results, trade ledger, OOS testing, cost stress and Monte Carlo analysis.
4. **Journal** — persistent signals, datasets and research runs.

The developer API documentation remains available at:

```text
http://127.0.0.1:8081/docs
```

## Live signal from PowerShell

```powershell
signal-bot analyze `
  --exchange kucoinfutures `
  --market-type swap `
  --symbol BTC/USDT:USDT `
  --execution-timeframe 15m `
  --context-timeframes 1h,4h `
  --equity 10000 `
  --risk-percent 0.5
```

Only closed candles are analyzed by default. A directional regime does not force an entry. Confirmed `LONG` or `SHORT` requires alignment, a valid trigger, volatility controls, and a valid risk plan.

## Scan markets

Explicit symbols:

```powershell
signal-machine scan `
  --exchange kucoinfutures `
  --market-type swap `
  --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT `
  --execution-timeframe 15m `
  --context-timeframes 1h,4h
```

Auto-discover a liquid universe:

```powershell
signal-machine scan `
  --exchange kucoinfutures `
  --market-type swap `
  --auto-universe `
  --quote-currencies USDT `
  --max-markets 20 `
  --minimum-quote-volume 5000000
```

Both modes enforce the same safety checks. Defaults include a 30-second quote age limit, 25-bps spread limit and 180-second scan budget. You can set `--maximum-quote-age-seconds`, `--maximum-spread-bps` and `--scan-timeout-seconds`. A qualified setup expires when its quote TTL or decision candle expires. The monitor alerts only currently qualified setups. Degraded/failed scans return CLI exit code 2.

Ticker spread is a quality screen, not a fill model. See [the scanner review and implemented safeguards](docs/SCANNER_REVIEW.md) for the audit, defaults and remaining live-futures limitations.

## Download historical data

```powershell
signal-machine download `
  --exchange kucoinfutures `
  --market-type swap `
  --symbol BTC/USDT:USDT `
  --timeframes 15m,1h,4h `
  --start 2023-01-01 `
  --end 2026-11-01
```

Data is stored under `data/history/`, deduplicated, checksummed, and registered in SQLite. Downloads resume from the latest stored candle.

## Run a backtest directly from the exchange

```powershell
signal-machine backtest-live `
  --exchange kucoinfutures `
  --market-type swap `
  --symbol BTC/USDT:USDT `
  --execution-timeframe 15m `
  --context-timeframes 1h,4h `
  --start 2023-01-01 `
  --end 2026-11-01 `
  --equity 10000 `
  --risk-percent 0.5
```

This project evaluates both long and short setups on KuCoin USDT-margined perpetual swaps.

Backtest safeguards include:

- signals after candle close only
- next-bar entries and regime exits
- higher-timeframe candles unavailable until fully closed
- fees and slippage
- conservative stop-first handling for ambiguous bars
- adverse open fills when price gaps through a stop
- partial exits, break-even, ATR trailing stops and time exits

## Full robustness validation

```powershell
signal-machine validate `
  --exchange kucoinfutures `
  --market-type swap `
  --symbol BTC/USDT:USDT `
  --execution-timeframe 15m `
  --context-timeframes 1h,4h `
  --start 2021-01-01 `
  --end 2026-11-01 `
  --monte-carlo 5000
```

The gate checks trade count, expectancy, profit factor, drawdown, untouched chronological test performance, maximum-cost stress, and Monte Carlo loss probability. A pass means **eligible for disciplined paper validation**, not permission to trade live.

## Continuous monitoring and alerts

```powershell
signal-machine monitor `
  --exchange kucoinfutures `
  --market-type swap `
  --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT `
  --execution-timeframe 15m `
  --context-timeframes 1h,4h `
  --interval-seconds 60 `
  --alert-sinks console,jsonl
```

Webhook delivery:

```powershell
$env:SIGNAL_ALERT_WEBHOOK_URL = "https://your-endpoint.example/signal"
signal-machine monitor --symbols BTC/USDT:USDT --alert-sinks console,webhook
```

## Optional local API key

The server binds to `127.0.0.1` by default. To require an API key for `/v1/*` endpoints:

```powershell
$env:SIGNAL_API_KEY = "use-a-long-random-secret"
signal-machine dashboard
```

Enter the same key in the dashboard header. Internet exposure additionally requires TLS, reverse-proxy authentication, rate limiting, request-size limits, logging, and process supervision.

## Project layout

```text
src/signal_bot/
  engine.py       market regime, setup and trade-plan logic
  indicators.py   EMA, RSI, ATR, ADX/DMI, MACD, volume and structure
  service.py      live public CCXT market data
  history.py      paginated, resumable historical data
  scanner.py      liquid-universe discovery and ranked scanning
  backtest.py     no-look-ahead simulator
  research.py     diagnostics, OOS, stress, Monte Carlo and validation gate
  storage.py      SQLite research journal
  alerts.py       persistent cooldown and alert sinks
  api.py          API and dashboard server
  machine_cli.py  operational command-line interface
  web/            dependency-free browser dashboard
```

See [Professional Architecture](docs/PROFESSIONAL_MACHINE.md), [Operations](docs/OPERATIONS.md), and [Validation Standard](docs/VALIDATION.md).
