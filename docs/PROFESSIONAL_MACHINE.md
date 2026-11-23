# KuCoin Futures Signal Machine Architecture

## Design boundary

The repository is a signal and research system. It deliberately contains no authenticated broker adapter and no order-submission path. This separation prevents an unvalidated signal configuration from silently becoming an execution system.

## Data flow

```text
KuCoin Futures public market metadata / OHLCV
                |
                +--> liquid-universe filters
                |
        UTC normalization and validation
                |
        closed-candle / staleness gates
                |
       technical feature construction
                |
  multi-timeframe regime and consensus model
                |
   breakout / pullback confirmation and risk plan
                |
      signal journal / scanner / alerts / API
                |
 historical backtest and robustness validation
```

## Core reliability controls

- All timestamps are normalized to UTC.
- Duplicate candles are removed before analysis.
- Invalid OHLC relationships, missing values and non-finite values fail closed.
- Live analysis drops the currently forming candle unless explicitly requested for research.
- Every requested higher timeframe must be usable; missing context produces `NO_TRADE` rather than a guess.
- The history downloader enforces monotonic pagination and a maximum-request safety cap.
- Historical files are written atomically and accompanied by SHA-256 metadata.
- SQLite uses WAL mode, foreign keys and a busy timeout for local concurrency.
- One market-data failure does not discard an entire scan.
- Alerts use a persistent cooldown ledger so restarts do not generate duplicate floods.

## Research controls

- Backtests use the same decision engine as live analysis.
- Decisions are formed only after a candle closes.
- Entries occur on the next bar.
- Context candles are visible only after their own close time.
- Fees and slippage are included.
- An ambiguous candle that touches a stop and target resolves stop-first.
- A gap through a stop receives the adverse opening fill.
- Every user-facing route enforces `kucoinfutures`, `swap`, and USDT settlement.
- Chronological train, validation and test segments are evaluated independently.
- Cost stress re-runs the engine with higher fees and slippage.
- Monte Carlo bootstraps trade R-multiples to estimate sequence risk.

## Futures-specific boundary

The current simulator uses futures OHLCV and permits long and short signals, but it does not yet apply historical funding settlements, KuCoin contract-lot rounding, maintenance-margin tiers, margin-mode behavior, or liquidation-price mechanics. A validation pass is therefore a research milestone, not authorization for live futures deployment.

Exchange eligibility, derivatives availability, taxes, legal requirements, production hosting, external authentication, and any future execution adapter remain outside the repository's signal boundary.
