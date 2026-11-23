# Security Policy

## Scope

This project consumes public market data and is signal-only. It must not be modified to accept exchange secrets or submit orders without a separate security and execution review.

## Supported version

Security fixes target the current `main` branch.

## Reporting

Report vulnerabilities privately to the repository owner. Do not publish API keys, webhook secrets, private datasets, or exploit details in a public issue.

## Safe operation

- Keep the server bound to `127.0.0.1` for local use.
- Set `SIGNAL_API_KEY` when another local process can reach the API.
- For network exposure, place the service behind TLS, authentication, rate limiting, request-size controls, and process supervision.
- Restrict webhook destinations and never embed credentials in source code.
- Treat downloaded market data as untrusted input; the application validates timestamps and OHLC relationships before analysis.
- Back up the SQLite journal before upgrades.
