# Professional Market Signal Machine

A standalone, exchange-agnostic research platform that classifies crypto markets, produces conditional long/short trade plans, scans liquid markets, downloads multi-year history, backtests the same decision engine, and records evidence in a persistent journal.

It is deliberately **signal-only**. It does not submit orders, store exchange credentials, or claim that a backtest proves a profitable edge.

## Major capabilities

- Multi-timeframe regimes: strong bullish, bullish, range, transition, bearish, and strong bearish
- Decisions: `LONG`, `SHORT`, `WATCH_LONG`, `WATCH_SHORT`, and `NO_TRADE`
- Breakout and pullback-continuation confirmation
- Entry zone, structural/ATR stop, TP1/TP2/TP3, break-even and trailing rules
- Risk-based sizing with fee and slippage estimates
- Liquid-market discovery and ranked multi-symbol scanning
- Paginated, resumable CCXT historical downloads with checksums and gap detection
- Closed-candle, next-bar, no-look-ahead backtesting
- Chronological train/validation/test evaluation
- Cost-stress and Monte Carlo robustness analysis
- A research gate that distinguishes working software from evidence suitable for paper validation
- Persistent SQLite journal for signals, scans, datasets, alerts, and backtests
- Console, JSONL, and generic webhook alerts with persistent cooldowns
- Local browser dashboard with price chart, signal explanation, scanner, backtest charts, trade ledger, and journal
- FastAPI endpoints, Docker support, and automated CI on Python 3.11 and 3.12

## Windows PowerShell setup

```powershell
cd C:\Trading\Signal

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

1. **Live Signal** — regime, decision, confidence, timeframe alignment, price chart, entry, stop and targets.
2. **Market Scanner** — ranked confirmed and forming setups across explicit symbols or an auto-discovered liquid universe.
3. **Backtest & Validation** — automatic historical download, equity curve, yearly results, trade ledger, OOS testing, cost stress and Monte Carlo analysis.
4. **Journal** — persistent signals, datasets and research runs.

The developer API documentation remains available at:

```text
http://127.0.0.1:8081/docs
```

## Live signal from PowerShell

```powershell
signal-bot analyze `
  --exchange kraken `
  --market-type spot `
  --symbol BTC/USDT `
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
  --exchange kraken `
  --market-type spot `
  --symbols BTC/USDT,ETH/USDT,SOL/USDT `
  --execution-timeframe 15m `
  --context-timeframes 1h,4h
```

Auto-discover a liquid universe:

```powershell
signal-machine scan `
  --exchange kraken `
  --market-type spot `
  --auto-universe `
  --quote-currencies USD,USDT `
  --max-markets 20 `
  --minimum-quote-volume 5000000
```

Ticker spread is only a universe-quality screen. It is not an execution fill model.

## Download historical data

```powershell
signal-machine download `
  --exchange kraken `
  --market-type spot `
  --symbol BTC/USDT `
  --timeframes 15m,1h,4h `
  --start 2023-01-01 `
  --end 2026-11-01
```

Data is stored under `data/history/`, deduplicated, checksummed, and registered in SQLite. Downloads resume from the latest stored candle.

## Run a backtest directly from the exchange

```powershell
signal-machine backtest-live `
  --exchange kraken `
  --market-type spot `
  --symbol BTC/USDT `
  --execution-timeframe 15m `
  --context-timeframes 1h,4h `
  --start 2023-01-01 `
  --end 2026-11-01 `
  --equity 10000 `
  --risk-percent 0.5
```

Spot research is long-only. Swap/futures research can evaluate both long and short setups.

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
  --exchange kraken `
  --market-type spot `
  --symbol BTC/USDT `
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
  --exchange kraken `
  --market-type spot `
  --symbols BTC/USDT,ETH/USDT,SOL/USDT `
  --execution-timeframe 15m `
  --context-timeframes 1h,4h `
  --interval-seconds 60 `
  --alert-sinks console,jsonl
```

Webhook delivery:

```powershell
$env:SIGNAL_ALERT_WEBHOOK_URL = "https://your-endpoint.example/signal"
signal-machine monitor --symbols BTC/USDT --alert-sinks console,webhook
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
