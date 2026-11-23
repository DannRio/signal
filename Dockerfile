FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SIGNAL_DB_PATH=/app/data/signal_machine.db

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir ".[web]" \
    && useradd --create-home --uid 10001 signal \
    && mkdir -p /app/data \
    && chown -R signal:signal /app

USER signal
VOLUME ["/app/data"]
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3).read()"
ENTRYPOINT ["signal-api"]
CMD ["--host", "0.0.0.0", "--port", "8081"]
