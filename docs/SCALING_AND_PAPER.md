# Scanner measurements, replay, caching and paper outcomes (v2.2)

This release preserves the strategy thresholds. It changes data reuse, observability,
reproducibility and research accounting. A functioning scanner or passing replay is
not evidence of a profitable strategy.

## What runs by default

Dashboard/CLI scans with a SQLite store now use a persistent closed-candle cache and
capture complete replay inputs. The same shared CCXT client performs serial public
requests. A bounded in-memory analysis cache reuses a decision only when its exact
candle hashes, configuration, symbol/timeframes and economic inputs match.

The cache keeps each symbol/timeframe separate, preserves the requested analysis
window, and refreshes only timeframes whose expected closed candle has advanced.
It does not supply an open candle to the scanner. A delayed exchange publication
causes another fetch on the next cycle; a clock boundary is not proof of publication.

Every cache read is revalidated. Incremental downloads overlap recent closed bars.
Conflicting old values invalidate the series and fail that analysis explicitly; the
next cycle bootstraps a clean window. Missing/corrupt/short windows trigger repair or
an explicit validation failure. No synthetic candles are created. Default retention
is 2,000 bars per series, at least the requested window; historical CSV datasets are
separate.

`--no-candle-cache` disables both persistent candle reuse and the scanner's in-memory
analysis reuse for a full-fetch baseline. `--no-replay-capture` disables snapshot
capture for that request. The API equivalents are `use_candle_cache: false` and
`capture_replay: false`.

## Measurements

The scanner response and journal contain `metrics` with:

- Actual HTTP transport requests when the adapter supports instrumentation.
- CCXT operation attempts separately from HTTP requests; cached CCXT operations are
  not incorrectly counted as HTTP calls.
- OHLCV pages, downloaded rows, retries and cache/analysis hits.
- Retrieval, validation, calculation, rate-limit waiting, quote refresh and
  persistence timing.
- Per-market duration, decision age at qualification, quote age and coverage.

Some timers nest. Do not sum all stage timings. Report duration includes the primary
scan-journal write; its small final measurement-update write is not included.
`/v1/scanner/status` also exposes progress for the active scanner instance. This is
not a distributed job queue.

Use matching workloads when comparing measurements:

```text
signal-machine metrics --limit 100
signal-machine benchmark --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT --repetitions 2
signal-machine benchmark --auto-universe --max-markets 20 --repetitions 2
```

The benchmark freezes an automatic universe before its measured runs. It performs
uncached trials followed by cached trials, emits the reports as JSON, and sends no
alerts or orders. Existing warm caches are retained, so a cached trial need not be a
cold bootstrap. Use a separate `--db` path for an isolated cold-cache experiment.
Live quotes and candle boundaries can change between trials; use frozen replay for
exact behavior comparisons. Percentiles from a few runs are descriptive, not a
capacity guarantee. Failed or degraded runs return exit code 2.

`GET /v1/scanner/metrics?limit=100` groups recent measurements by exact selected
markets, scan/engine configuration and cache state. Concurrency remains **one**.
Increasing worker count has not been justified by a representative deployment
benchmark; multiple independent exchange clients must not be used to evade the
shared rate limit.

## Replay evidence

Each captured decision includes the exact candle window, configuration, risk inputs,
contract specifications, quote, original analysis cutoff and original qualification
time. Decimal contract metadata is retained without conversion to floating point.
Candle windows are compressed and deduplicated by content. Checksums are verified
before replay.

```text
signal-machine snapshots --limit 20
signal-machine replay SNAPSHOT_ID
signal-machine replay SNAPSHOT_ID --export data/evidence/decision.json
signal-machine replay --file data/evidence/decision.json
```

Replay makes no network calls and does not dispatch alerts or orders. It reports
underlying signal and quote-qualification differences independently. Numerical
indicator comparisons allow a 1e-12 relative tolerance; rounded contract plans must
match exactly. Strategy actions, reasons, statuses and expiry timestamps must match.

The Scanner diagnostics and Journal offer **Verify replay** and **Export inputs**.
API routes:

```text
GET  /v1/scanner/replays
GET  /v1/scanner/replays/{snapshot_id}
POST /v1/scanner/replays/{snapshot_id}/verify
```

The rolling diagnostic archive retains 5,000 recent snapshots. Entry snapshots
referenced by paper positions are pinned transactionally and remain after positions
close. Consequently total retained snapshots can exceed 5,000. Include the database
in backups; long-running paper experiments intentionally accumulate evidence.

## Candle-aware monitoring

```text
signal-machine monitor --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT --interval-seconds 15 --alert-sinks console,jsonl
```

The monitor targets a quote-check interval, capped at half the requested quote TTL
and at 60 seconds. It wakes near candle closes with a two-second polling grace,
while the cache independently decides which timeframes need data. Repeated analysis
of identical inputs is skipped, but quotes and actionability are checked again.

The interval is a target, not a guarantee: a slow/full scan can overrun it. Request
budgets propagate through requests, pagination and retry waits; transport timeouts
shrink to the remaining budget. Synchronous third-party waits or native computations
cannot be forcibly interrupted. After an overrun, further request work is refused
and fresh-quote qualification fails conservatively. A large timeout does not establish
that a large universe remains timely.

Use one coordinating monitor per deployment. Separate processes do not share a CCXT
rate limiter. A dashboard remains useful for reading the same SQLite journal, but
additional active scans also consume the venue's request budget.

## Forward paper observations

Paper tracking is opt-in and never accesses an account or places exchange orders.
Start with a small fixed universe:

```text
signal-machine monitor --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT --paper --paper-account default --equity 10000 --interval-seconds 15 --alert-sinks console,jsonl
signal-machine paper-status --account default
```

Default scanner-integrated policy:

- Starting hypothetical equity comes from `--equity`; it cannot silently reset for
  the same account name. Use a different account name for an independent experiment.
- Fees/slippage come from the strategy configuration, applied adversely.
- The entire position exits at target 1 or its stop. This is a disclosed forward
  observation policy, not a reproduction of the backtester's partial/trailing exits.
- At most five positions; at most 2% combined modeled stop risk; a combined notional
  cap equal to the configured notional multiple.
- Sizing respects contract lots, ticks, instrument limits, original plan budgets,
  realized losses, entry fees and adverse open-position mark-to-market.
- Entry requires a fresh executable quote still inside the entry zone after
  slippage. A later qualified observation may follow an earlier skipped observation.
- One entry per venue/symbol/timeframe/decision candle, including across restarts
  and changed signal fingerprints.

`--paper-risk-budget-percent` and `--paper-max-positions` adjust the shared budgets.
Scanner UI paper tracking observes one scan; it does **not** start a background
monitor. Existing positions outside the ranked universe still receive quote checks
when the monitor runs.

Missing observations, stale quotes, or a gap beyond the position's policy produce
**unresolved** exposure. Its budget remains reserved and new admissions pause. The
program never guesses a stop/target outcome during a missing interval. Restarts do
not erase uncertainty. To run a fresh independent experiment, choose a new account
name and preserve the old record for review.

Quote outcomes describe sampled observations, not the continuous price path or
actual exchange fills. The Python interface additionally supports conservative
closed-bar observations, which require entry on a bar boundary and contiguous future
bars; scanner/monitor integration intentionally accepts only quote mode.

Dashboard **Paper outcomes** and these endpoints expose the lifecycle:

```text
GET /v1/paper/status?account_id=default
GET /v1/paper/positions?account_id=default
GET /v1/paper/events?account_id=default
```

Paper results label P&L **before funding**. Sourced historical funding payments may
be applied using the Python interface only within established observed ownership;
a payment alone does not prove complete settlement coverage. Portfolio correlation,
depth, maintenance margin and liquidation remain outside this model.

## Historical futures accounting

An optional `futures_inputs` object enables contract-valid sizing/fills/partial exits
and realized historical funding. It propagates through ordinary backtests,
chronological validation/test windows, walk-forward evaluation and cost stress.
Funding-adjusted trade returns then feed the existing Monte Carlo diagnostics.

```text
signal-machine backtest-live --symbol BTC/USDT:USDT --start 2025-01-01 --end 2025-02-01 --futures-inputs data/evidence/btc-futures.json --json
signal-machine validate --symbol BTC/USDT:USDT --start 2025-01-01 --end 2025-02-01 --futures-inputs data/evidence/btc-futures.json --json
```

Upload the same JSON file in the Backtest panel, or supply its object as
`futures_inputs` to `/v1/backtest/live` or `/v1/research/validate`.

Required structure (the values below illustrate the schema, **not historical market
evidence**; replace the complete schedule, marks, rates, coverage and specifications):

```json
{
  "schema_version": 1,
  "contract_spec": {
    "symbol": "BTC/USDT:USDT",
    "contract_size": "0.001",
    "lot_size": "1",
    "tick_size": "0.1",
    "min_contracts": "1"
  },
  "contract_spec_source": "Path and provenance of verified contract metadata",
  "contract_spec_assumption": "fixed_over_backtest_window",
  "funding": {
    "coverage_start": "2025-01-01T00:00:00+00:00",
    "coverage_end": "2025-01-01T08:00:00+00:00",
    "source": "Historical funding rates plus settlement mark-price dataset",
    "schedule_source": "Verified historical settlement schedule for this contract",
    "expected_settlement_timestamps": ["2025-01-01T08:00:00+00:00"],
    "events": [{
      "timestamp": "2025-01-01T08:00:00+00:00",
      "rate": "0.0001",
      "mark_price": "65000",
      "source": "Source record identifying the actual rate and settlement mark"
    }]
  }
}
```

Every expected settlement, including zero-rate events, must be present. Coverage
must span the evaluated trading interval. The parser checks consistency; it cannot
authenticate an invented source description or an incomplete schedule supplied as
complete. Do not infer a constant eight-hour interval or backfill old payments using
today's funding rate. KuCoin's public historical-rate endpoint alone does not include
the historical mark valuation required by this model.

Positive funding charges a carried long and credits a carried short, based on actual
remaining quantity and the supplied settlement mark. Rates are never exposed to the
signal engine before settlement. Settlements at bar boundaries apply to carried
positions before same-time orders; terminal/time-stop closes follow this convention.
Intrabar settlement times are rejected because OHLC data cannot establish precise
position ownership then.

Contract specifications are explicitly assumed fixed over the study window.
Historical specification changes require appropriately segmented evidence; a current
metadata response must not be presented as proof of past rules. Invalid fractional
partial exits are skipped, and stop changes dependent on those exits do not occur.

Without evidence, the legacy research mode still runs and explicitly reports funding
as unmodeled (`funding_pnl: null`). Neither mode models maintenance margin,
mark-price liquidation, ADL or depth. Validation continues to report
`PASS_FOR_PAPER_VALIDATION` at best and `live_ready: false`.

Primary references: [KuCoin funding mechanism](https://www.kucoin.com/support/26695933047705),
[public funding history](https://www.kucoin.com/docs-new/rest/futures-trading/funding-fees/get-public-funding-history),
[KuCoin candles](https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-klines),
and [CCXT rate-limit and OHLCV documentation](https://docs.ccxt.com/docs/manual).

## Upgrade, backup and rollback

All new database tables are additive. Stop the monitor/dashboard and create a SQLite
backup before upgrading. Avoid copying an active WAL database file by itself.
The existing Python installation can perform the backup without installing sqlite3:

```text
python -c "import sqlite3; source=sqlite3.connect('data/signal_machine.db'); target=sqlite3.connect('data/signal_machine.before-v22.db'); source.backup(target); target.close(); source.close()"
```

Install the updated package and restart. On Windows, invoking the virtual
environment's Python directly avoids requiring PowerShell activation scripts:

```bat
cd /d C:\Trading\Signal\signal
git pull origin main
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m signal_bot.launcher dashboard
```

Refresh the browser with Ctrl+F5; health should show v2.2.0. Code rollback is a
checkout/reinstall of the previous known-good version. Keep the additive tables and
backup; earlier versions ignore them. Do not discard paper evidence to make an
unresolved outcome appear resolved.
