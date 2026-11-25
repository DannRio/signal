# Scanner engineering review and safeguards (v2.1)

Review baseline: `c05d22e327f7b2a8a79aa8b9297974a1f31cd96a`.
Scope: signal-only KuCoin USDT perpetual scanning, monitoring, and the evidence gate used to assess scanner strategies. No strategy thresholds were optimized against trading results in this change.

## Findings and implemented corrections

| Priority | Baseline defect | Correction |
| --- | --- | --- |
| Critical | Explicit symbol lists bypassed liquidity and contract-eligibility checks. | Manual and automatic universes use the same active/open, linear, perpetual, USDT settlement, normal-stage, turnover, quote, and contract-metadata gates. |
| Critical | Unknown/missing spread passed the maximum-spread filter. Quote age was not checked. | Finite positive uncrossed bid/ask and exchange timestamp are required. Missing, stale, future, and malformed quotes receive explicit rejection codes. |
| Critical | KuCoin's default CCXT ticker source can provide turnover without bid/ask. | Combine refreshed contract metadata (`turnoverOf24h`) with the BBO endpoint. Do not estimate turnover by multiplying base volume by the last price. |
| Critical | Candle windows could contain gaps; incomplete-candle removal removed only the final row. | Validate OHLCV, continuity, alignment, freshness, and required history. Remove every row unclosed at the shared scan cutoff. Never synthesize missing candles. |
| High | Each symbol could see a different candle cutoff; late results retained old quotes. | One scan-wide `as_of` cutoff, then a final quote refresh. Qualification expires at the earlier of quote TTL and the next execution-candle close. |
| High | Base-asset size was not a KuCoin contract quantity. | Apply current multiplier, lot, tick, amount and notional limits with Decimal arithmetic. Round contracts down and recompute risk/notional across the entry zone. Reject plans below market minima or with collapsed levels. |
| High | A confirmed candle setup could be presented after price left its entry zone. | Require the current executable ask (long) or bid (short) inside the planned zone; otherwise show BLOCKED. |
| High | Network outages and incomplete scans lacked a clear run-level contract. | Shared rate-limited client, bounded transient retries/timeouts, overlap protection, scan budget, explicit complete/degraded/failed/empty statuses and per-market failures. |
| High | Zero profit factor was treated as infinity; missing evidence could still pass research readiness. | Zero remains zero. Mandatory named evidence covers validation/test trade count, PF, expectancy, drawdown, held-out cost stress and held-out bootstrap. Missing evidence cannot pass. |
| High | Split evaluation discarded useful pre-split indicator warmup. | Include only causal preceding candles for warmup while prohibiting trades before each evaluation boundary; windows start flat. |
| High | Alert cooldown alone allowed a repeated candle event later; partial delivery could resend successful channels. | Durable per-event/per-sink success receipts, failed-sink-only retry, context expiry, endpoint-specific identity and a webhook idempotency key. |
| Medium | Reports lacked configuration/input identity and the UI hid screening reasons. | Configuration hash, dependency versions, per-frame hashes and timestamps, funding context, rejection details, rounded plans and expiry displayed/journaled. |
| Medium | Windows users were covered only by Linux CI. | CI runs the suite and CLI checks on Windows and Ubuntu, Python 3.11 and 3.12. |

## What qualified means

A qualified setup is an engine LONG/SHORT whose current scanner checks pass. It is a conditional research signal, not proof of positive expectancy and not an order. The underlying rule-alignment score is not a calibrated win probability. Ranking prioritizes qualification, rule alignment, timeframe agreement and liquidity; it does not estimate expected return.

The scanner preserves the raw engine decision in each scan record alongside `status`, `actionable`, `blocked_reasons`, `error_code`, `valid_until`, and `contract_plan`. Blocked raw decisions are not inserted as fresh standalone signal-journal entries. All blocked/rejected/failed outcomes remain in the complete scan journal.

`actionable` is the result at report creation. Consumers must check `valid_until` again before use; `ScanItem.is_actionable()` does so. The monitor checks it before dispatch. The dashboard marks elapsed qualifications EXPIRED. A valid quote is a point-in-time observation, not a guarantee of the price at a later fill.

## Default operational settings

| Setting | Default | Meaning |
| --- | --- | --- |
| Minimum 24h turnover | 1,000,000 USDT | Measured quote turnover; manual symbols do not bypass it. |
| Maximum spread | 25 bps | Bid/ask quality screen, not a fill model. |
| Maximum quote age | 30 seconds | Uses exchange event time; future tolerance is 5 seconds. |
| Candle freshness | 1 execution/context bar | Applies to each requested timeframe; next execution-candle close also expires the decision. |
| Scan time budget | 180 seconds | Cooperative budget checked between markets; an in-flight bounded request and final quote verification can finish later. |
| Request timeout | 10 seconds | Per CCXT request. |
| Transient attempts | 3 | Network/server/rate-limit errors only; invalid requests are not retried. |
| Pagination limit | 10 pages | Timestamp progress is required; identical overlap is accepted, conflicting closed rows are rejected. |
| Maximum OHLCV request | 500 rows | A response may contain fewer; short pages are not assumed to be the end of history. |
| Trade risk | 0.5% of equity | Existing engine default, capped by configuration and rounded contract sizing. |
| Notional cap | 1x equity | Existing default; user-facing maximum is 5x. This does not set exchange leverage. |

The same scanner instance rejects overlapping runs with HTTP 409. Separate processes have separate CCXT limiters; run one monitor per deployment unless you coordinate them externally. A scan CLI result returns exit code 2 for degraded/failed runs and 0 for complete/empty runs. An empty screened universe is distinct from an exchange failure or an analyzed NO_TRADE decision.

## Contract plan semantics

The original model plan is retained. `contract_plan` is a separate rounded estimate. Entries round adversely, stops outward and targets inward. Quantity is reduced as needed to respect the original quantity, original actual/target risk, original notional, and instrument limits. Risk uses the adverse entry-zone edge. Minimum notional is checked at the lowest entry-zone price. A minimum lot is never met by increasing risk.

Net reward/risk values assume a full-position exit at each target after configured estimated fees/slippage. They are not the blended result of partial exits. Funding is displayed with its metadata observation time, supplied interval and next settlement timestamp when available; absent fields remain unavailable. Funding is not deducted from these plan estimates or historical P&L.

## Research evidence boundary

Default screening requirements include 200 full-sample trades, at least 50 trades in each validation and test window, positive expected R above the configured threshold, PF and drawdown limits, test-window stress of at least 2x configured costs, and a test-trade bootstrap. These are configurable screening rules, not statistically proven optimal cutoffs.

Pre-split candles only warm indicators; evaluated windows start flat. Cost stress and Monte Carlo use the test window. The bootstrap is independent trade resampling and does not preserve serial dependence. Repeatedly selecting configurations after examining the test window consumes its holdout value. A passing status remains `PASS_FOR_PAPER_VALIDATION`, with `live_ready: false`.

## Remaining work before live futures reliance

- Demonstrate positive expectancy on sufficient independent historical data after realistic costs, then forward paper observations. This engineering review supplies no new evidence of profitability.
- Add realized historical funding settlement accounting; validate fee tiers and fill/slippage assumptions using observations.
- Model contract lots in the historical backtester, mark-price liquidation, margin tiers/modes and liquidation costs before treating backtest P&L as a full futures simulation.
- Add depth-based capacity estimates when sizing beyond small positions. Spread and 24h turnover alone cannot establish executable depth.
- Add aggregate exposure/correlation and open-position awareness if multiple signals will be acted on. Each current plan assumes the same independently available equity.
- Validate contract-valid partial-exit quantities before adding any order workflow. The current scanner does not submit orders.
- Establish a supervised uptime record, backups and restore checks in the actual Windows/deployment environment. CI cannot establish operational uptime.

Hashes identify the configuration and normalized inputs used for a scan; the scan journal does not archive every complete live candle window. Historical downloads remain available for explicit dataset retention/research.

Alert receipts prevent routine duplicate delivery across restart/cooldown and isolate failed sinks. They cannot guarantee exactly-once remote delivery if a process dies after the receiver accepts a notification but before its receipt is saved. Receivers that honor the supplied webhook idempotency key can reduce this remaining ambiguity.

## Primary API references

- [KuCoin Futures Klines](https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-klines): 500-row maximum, time pagination and absent no-tick intervals.
- [KuCoin Futures all tickers](https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-all-tickers): BBO and nanosecond event timestamps.
- [KuCoin Futures all symbols](https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-all-symbols): contract status, multiplier, lot/tick, turnover and funding fields.
- [CCXT manual](https://docs.ccxt.com/docs/manual): exchange-instance limiter, contract counts, OHLCV handling.
- [CCXT KuCoin implementation](https://github.com/ccxt/ccxt/blob/master/python/ccxt/kucoin.py): separate contract metadata and BBO ticker methods.
