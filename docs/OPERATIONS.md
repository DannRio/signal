# Operations Guide

## Local start

```powershell
cd C:\Trading\Signal
.\.venv\Scripts\Activate.ps1
signal-machine dashboard
```

Open `http://127.0.0.1:8081`.

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
