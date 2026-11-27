# Operations Guide

## Local start

```bat
cd /d C:\Trading\Signal\signal
.venv\Scripts\python.exe -m signal_bot.launcher dashboard
```

Open `http://127.0.0.1:8081`.

## Chart display

Version 2.1.1 fixes price and equity charts that appeared squeezed against an edge,
stretched vertically, or clipped after analysis or resizing. Results are now made
visible before measuring the canvas. CSS controls a stable chart height while the
canvas bitmap scales separately for the display's pixel density. Both charts redraw
when their container changes size, their tab reopens, or display density changes.

After upgrading, restart the dashboard and refresh the browser with `Ctrl+F5`.
The health badge should report `v2.2.0` after the current upgrade. The price chart stays 430 CSS pixels high
and the equity chart 330; changing Windows display scaling should not increase
those layout heights. Browser regression checks exercise repeated analyses,
backtests, resizing, and tab changes at 100%, 125%, and 200% pixel density.

## Fixed venue scope

- Exchange id: `kucoinfutures`
- Market type: `swap`
- Settlement: `USDT`
- Unified symbol format: `BTC/USDT:USDT`
- Public Kline request limit: 500 candles; the downloader paginates longer ranges

The service rejects spot and non-KuCoin venue requests before market-data retrieval.

## Runtime files

- SQLite journal: `data/signal_machine.db`
- Historical candles: `data/history/`
- JSONL alerts: `data/alerts/signals.jsonl`

These paths are ignored by Git. Back them up separately if the journal matters.

## Backup

Stop the service or use SQLite's backup command before copying the database:

```powershell
sqlite3 data\signal_machine.db ".backup 'data\signal_machine.backup.db'"
```

Historical CSVs can be copied normally after downloads finish.

## Recovery

1. Stop the dashboard or monitor process.
2. Replace `data/signal_machine.db` with the backup.
3. Verify with `signal-machine dashboard` and the Journal tab.
4. Re-download any historical dataset whose checksum or gap count is suspect.

## Configuration and secrets

- `SIGNAL_DB_PATH` changes the journal location.
- `SIGNAL_API_KEY` protects `/v1/*` endpoints.
- `SIGNAL_ALERT_WEBHOOK_URL` supplies the generic alert endpoint.

Never commit `.env` files or exchange credentials. The current system does not require private API keys.

## Production deployment minimums

- Bind the application behind a reverse proxy.
- Terminate TLS at the proxy.
- Require authentication in addition to the optional API key.
- Add rate limits and request-size limits.
- Run as a non-root user.
- Persist `/app/data` on durable storage.
- Capture stdout/stderr in a log system.
- Add process supervision and restart limits.
- Restrict outbound webhook destinations.

## Upgrade and rollback

Before upgrading:

```powershell
git status
git pull origin main
python -m pip install -e ".[dev]"
python -m pytest -q
```

To roll back, switch to the previous known-good commit, reinstall the editable package, and rerun tests. Runtime data is backward-independent from source control, but always back up SQLite before schema-affecting upgrades.

## Scanner run health

`GET /v1/scanner/status` reports idle or the most recent persisted scan outcome. `GET /v1/journal/scans` includes full run diagnostics. The public `/health` endpoint reports the API process, not live exchange availability.

A scan is `complete`, `degraded`, `failed`, or `empty`. Per-market records distinguish analyzed, blocked and failed results. Inspect `errors` and `error_code` before treating an empty opportunity list as a market conclusion. `actionable_at_scan_time` is historical; check each scan item's `valid_until` before relying on it.

The scan-wide candle cutoff is frozen. Quotes are refreshed before qualification. One scanner instance admits one run at a time; concurrent API requests receive 409. Time budgets propagate through requests, retries and pagination, with shrinking transport timeouts. Synchronous third-party waits cannot be interrupted mid-call. Avoid running multiple independent monitors against the same IP without a shared request budget.

After this upgrade, alert receipt storage is added automatically and existing journal tables are retained. Stop the application and back up the database before updating. To roll back code, check out the prior commit and reinstall; do not delete the additive receipt table or the database.

See [SCANNER_REVIEW.md](SCANNER_REVIEW.md) for defaults and exact qualification/expiry semantics.

## Measurements, caching and paper outcomes

See [SCALING_AND_PAPER.md](SCALING_AND_PAPER.md) for the v2.2 cache/replay behavior, measured benchmarks, candle-aware monitoring, paper accounts, futures evidence input, and rollback instructions. New tables are additive; paper entry snapshots are retained beyond the rolling diagnostic limit. No private API key is needed.
