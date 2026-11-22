# Security

The project currently uses unauthenticated public market data and should not require exchange API credentials.

Do not commit API keys, secrets, webhooks, or account identifiers. If authenticated features are added later, use environment variables or an external secret manager, restrict keys to the minimum permissions, disable withdrawals, and separate research credentials from any execution environment.

The FastAPI server is a development interface. Bind it to localhost unless it is placed behind authenticated TLS infrastructure with rate limiting and request-size controls.
