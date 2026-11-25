# Operations Guide

## Local start

```powershell
cd C:\Trading\Signal\signal
.\.venv\Scripts\Activate.ps1
signal-machine dashboard
```

Open `http://127.0.0.1:8081`.

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

The scan-wide candle cutoff is frozen. Quotes are refreshed before qualification. One scanner instance admits one run at a time; concurrent API requests receive 409. Time budgets are cooperative between markets, with bounded network attempts. Avoid running multiple independent monitors against the same IP without a shared request budget.

After this upgrade, alert receipt storage is added automatically and existing journal tables are retained. Stop the application and back up the database before updating. To roll back code, check out the prior commit and reinstall; do not delete the additive receipt table or the database.

See [SCANNER_REVIEW.md](SCANNER_REVIEW.md) for defaults and exact qualification/expiry semantics.
