# Validation Standard

This software should not be described as profitable until it survives all of the following with frozen rules and realistic costs.

## Minimum research gates

1. Multiple years covering bull, bear, range, crash, and high-volatility regimes.
2. Chronological train, validation, and untouched test periods.
3. Walk-forward evaluation with parameters selected only from each training window.
4. Fee, spread, slippage, and—where applicable—funding stress tests.
5. Results separated by asset, year, direction, setup type, timeframe, and regime.
6. A large enough out-of-sample trade count to estimate uncertainty; a few attractive trades are not evidence.
7. Bootstrap or Monte Carlo analysis of trade-order risk and drawdown.
8. Continuous paper trading using the same signal and cost assumptions.

## Metrics that matter

- expectancy in R after all modeled costs
- profit factor
- maximum drawdown and drawdown duration
- trade count and exposure
- stability across assets and periods
- sensitivity to reasonable parameter changes
- degradation from training to validation to test

## Promotion rule

No parameter set should be promoted because it wins a broad grid search. Prefer broad stable regions over isolated peaks. Any rule changed after viewing test results invalidates that test period and requires a new untouched period.

## Current status

The repository contains a deterministic signal engine, a no-lookahead backtester, and automated tests. It does not yet contain evidence that the strategy has positive expectancy in live trading.
