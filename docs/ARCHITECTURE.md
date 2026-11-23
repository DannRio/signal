# Architecture

## Data flow

```text
CCXT public OHLCV or caller-supplied frames
                |
        schema and candle validation
                |
     closed-candle and staleness controls
                |
 EMA / ADX-DMI / RSI / MACD / ATR / volume / structure
                |
       per-timeframe regime scoring
                |
       weighted timeframe consensus
                |
       breakout or pullback trigger
                |
    risk-defined entry, stop, targets and size
                |
           CLI / API / backtester
```

## Design boundaries

The deterministic engine has no exchange dependency. It accepts pandas OHLCV frames and returns immutable domain models. CCXT access exists only in `service.py`, making the decision logic testable without a network connection. The operator boundary is intentionally narrower: CLI, API, dashboard, history, and scanner inputs are restricted to KuCoin USDT-margined perpetual swaps.

The project is signal-only. There is no authenticated exchange adapter, order manager, or live execution route. That boundary prevents an unvalidated research signal from silently becoming an executable order.

## Time semantics

OHLCV timestamps are treated as candle-open times in UTC. A signal's `generated_at` is the execution candle's close time. During backtesting, higher-timeframe candles are visible only after their close time is less than or equal to the decision time.

## Signal states

- `LONG` or `SHORT`: an entry trigger and valid risk plan are confirmed.
- `WATCH_LONG` or `WATCH_SHORT`: directional regime exists, but the trigger or risk plan is incomplete.
- `NO_TRADE`: range, transition, timeframe conflict, unsafe volatility, or insufficient/untrusted data.

## Extensibility

Future components should depend on the public models and never import private backtester state. Alerting, persistence, market-universe selection, and paper execution should be separate adapters around the engine.
