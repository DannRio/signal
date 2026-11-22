# Market Signal Bot

A standalone, exchange-agnostic Python engine that classifies a market as bullish, bearish, ranging, or transitional and produces conditional, risk-defined entry and exit plans.

This repository is deliberately separate from any execution bot. It consumes public OHLCV data, generates signals, and backtests the same decision engine. It does **not** place orders.

## What it produces

- `STRONG_BULLISH`, `BULLISH`, `RANGE`, `TRANSITION`, `BEARISH`, or `STRONG_BEARISH`
- `LONG`, `SHORT`, `WATCH_LONG`, `WATCH_SHORT`, or `NO_TRADE`
- an entry trigger and entry zone
- structure/ATR-based stop loss
- TP1, TP2, and TP3
- account-risk-based position size
- break-even, trailing-stop, and maximum-holding-time rules
- explainable per-timeframe scores and warnings

A score or confidence value is **not a probability of profit**. A functioning signal engine is not evidence of a trading edge; the edge must be demonstrated through out-of-sample and paper validation.

## Decision model

The engine combines:

- EMA 20/50/200 structure and slope
- ADX and DMI directional strength
- MACD histogram and RSI momentum
- Donchian market structure
- relative volume
- ATR and volatility-percentile risk gates
- higher-timeframe weighted consensus
- breakout and pullback-continuation entry confirmation

A bullish or bearish regime alone does not force a trade. The engine emits a confirmed `LONG` or `SHORT` only when the regime, multi-timeframe alignment, execution trigger, volatility filters, and risk plan all pass.

## Installation

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Analyze a live market

Public market data is retrieved through CCXT. No API key is required.

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

Machine-readable output:

```powershell
signal-bot analyze --exchange kraken --market-type spot --symbol BTC/USDT --execution-timeframe 15m --context-timeframes 1h,4h --json
```

The bot analyzes only closed candles by default. `--include-open-candle` is available for research, but open-candle signals can change before close and should not be treated as stable.

## Watch for signal changes

```powershell
signal-bot watch `
  --exchange kraken `
  --market-type spot `
  --symbol BTC/USDT `
  --execution-timeframe 15m `
  --context-timeframes 1h,4h `
  --interval-seconds 60
```

Watch mode prints only when the signal fingerprint changes. It never submits an order.

## Backtest local data

CSV files must contain:

```text
timestamp,open,high,low,close,volume
```

Example:

```powershell
signal-bot backtest `
  --symbol BTC/USDT `
  --execution-timeframe 15m `
  --execution-csv data/btc_15m.csv `
  --context-csv 1h=data/btc_1h.csv `
  --context-csv 4h=data/btc_4h.csv `
  --equity 10000 `
  --risk-percent 0.5 `
  --json
```

Backtest safeguards include:

- decisions only after candle close
- next-bar entry and regime-flip exit
- higher-timeframe candles unavailable until fully closed
- fees and slippage
- stop-first resolution when stop and target are touched in one bar
- adverse opening-price fills when price gaps through a stop
- partial profit-taking, break-even, ATR trailing stop, and time exit

## API

```powershell
python -m pip install -e ".[web]"
signal-api --host 127.0.0.1 --port 8081
```

Endpoints:

- `GET /health`
- `GET /v1/live-signal`
- `POST /v1/analyze`

Keep the development server bound to `127.0.0.1`. Internet-facing deployment needs authentication, TLS termination, rate limiting, request-size controls, logging, and process supervision.

## Configuration

Use the supplied validated configuration:

```powershell
signal-bot --config configs/default.json analyze --exchange kraken --market-type spot --symbol ETH/USDT --execution-timeframe 15m --context-timeframes 1h,4h
```

Unknown configuration fields, invalid indicator periods, excessive risk, and unsafe fee/slippage assumptions are rejected.

## Docker

```powershell
docker build -t market-signal-bot .
docker run --rm -p 8081:8081 market-signal-bot
```

## Project structure

```text
src/signal_bot/
  api.py          FastAPI interface
  backtest.py     no-lookahead simulator
  cli.py          analyze/watch/backtest commands
  config.py       immutable validated configuration
  engine.py       regime, signal, entry and exit logic
  indicators.py   technical features
  models.py       typed domain models
  service.py      standalone CCXT market-data service
  validation.py   OHLCV and timeframe validation
```

See [Architecture](docs/ARCHITECTURE.md) and [Validation Standard](docs/VALIDATION.md).
