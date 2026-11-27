"""Closed-candle storage in the existing SignalStore SQLite database.

This is a recoverable data cache, separate from research dataset files. A
conflicting closed row invalidates its cached series and fails the current
request. A later request must bootstrap that series again explicitly.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from .runtime import current_operation
from .validation import (
    REQUIRED_COLUMNS,
    DataValidationError,
    normalize_ccxt_ohlcv,
    prepare_ohlcv,
    timeframe_to_timedelta,
    validate_live_window,
)


@dataclass(frozen=True)
class CandleKey:
    exchange_id: str
    market_type: str
    symbol: str
    timeframe: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in self.values):
            raise ValueError("Candle cache key fields must be nonempty strings.")
        timeframe_to_timedelta(self.timeframe)

    @property
    def values(self) -> tuple[str, str, str, str]:
        return (self.exchange_id, self.market_type, self.symbol, self.timeframe)


@dataclass(frozen=True)
class CacheSnapshot:
    frame: pd.DataFrame
    requested_limit: int = 0
    invalidation_reason: str | None = None
    validated_at_ms: int | None = None
    validated_rows: int = 0


class CandleCache:
    def __init__(self, store: Any, *, retention_bars: int = 2_000) -> None:
        if isinstance(retention_bars, bool) or not isinstance(retention_bars, int) or retention_bars < 1:
            raise ValueError("retention_bars must be a positive integer.")
        self.store = store
        self.retention_bars = retention_bars
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS candle_cache (
                    exchange_id TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    open_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(exchange_id, market_type, symbol, timeframe, open_ms)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS candle_cache_state (
                    exchange_id TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    requested_limit INTEGER NOT NULL,
                    retention_bars INTEGER NOT NULL,
                    validated_at_ms INTEGER,
                    validated_rows INTEGER NOT NULL DEFAULT 0,
                    invalidation_reason TEXT,
                    PRIMARY KEY(exchange_id, market_type, symbol, timeframe)
                ) WITHOUT ROWID;
            """)
            # Small additive migration for caches created by earlier builds.
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(candle_cache_state)")}
                if "validated_rows" not in columns:
                    connection.execute("ALTER TABLE candle_cache_state ADD COLUMN validated_rows INTEGER NOT NULL DEFAULT 0")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        deadline, _ = current_operation()
        if deadline is not None:
            deadline.check("candle cache access")
        # SignalStore owns connection lifecycle and its process-local lock.
        with self.store._connection() as connection:
            previous_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            if deadline is not None:
                deadline.check("candle cache access")
                remaining_ms = max(1, int(deadline.remaining() * 1000))
                connection.execute(f"PRAGMA busy_timeout = {min(previous_timeout, remaining_ms)}")
            try:
                yield connection
            finally:
                if deadline is not None:
                    connection.execute(f"PRAGMA busy_timeout = {previous_timeout}")
        if deadline is not None:
            deadline.check("candle cache access")

    def read(
        self, key: CandleKey, *, as_of: datetime | pd.Timestamp, limit: int
    ) -> CacheSnapshot:
        _validate_limit(limit)
        duration_ms = int(timeframe_to_timedelta(key.timeframe).total_seconds() * 1000)
        cutoff_ms = int(pd.Timestamp(as_of).timestamp() * 1000)
        with self._connection() as connection:
            # The rows and their validated coverage marker must belong to the
            # same SQLite snapshot, including with another process writing.
            connection.execute("BEGIN")
            try:
                rows = connection.execute(
                    """SELECT open_ms, open, high, low, close, volume FROM candle_cache
                       WHERE exchange_id=? AND market_type=? AND symbol=? AND timeframe=?
                         AND open_ms <= ? ORDER BY open_ms DESC LIMIT ?""",
                    (*key.values, cutoff_ms - duration_ms, limit),
                ).fetchall()
                state = connection.execute(
                    """SELECT requested_limit, invalidation_reason, validated_at_ms, validated_rows
                       FROM candle_cache_state
                       WHERE exchange_id=? AND market_type=? AND symbol=? AND timeframe=?""",
                    key.values,
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        frame = normalize_ccxt_ohlcv([list(row) for row in reversed(rows)])
        return CacheSnapshot(
            frame=frame,
            requested_limit=int(state["requested_limit"]) if state else 0,
            invalidation_reason=state["invalidation_reason"] if state else None,
            validated_at_ms=state["validated_at_ms"] if state else None,
            validated_rows=int(state["validated_rows"]) if state else 0,
        )

    def invalidate(self, key: CandleKey, reason: str) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._invalidate(connection, key, reason)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def _invalidate(self, connection: Any, key: CandleKey, reason: str) -> None:
        connection.execute(
            "DELETE FROM candle_cache WHERE exchange_id=? AND market_type=? AND symbol=? AND timeframe=?",
            key.values,
        )
        connection.execute(
            """INSERT INTO candle_cache_state
                   (exchange_id, market_type, symbol, timeframe, requested_limit,
                    retention_bars, validated_at_ms, validated_rows, invalidation_reason)
               VALUES (?, ?, ?, ?, 0, ?, NULL, 0, ?)
               ON CONFLICT(exchange_id, market_type, symbol, timeframe) DO UPDATE SET
                   requested_limit=0, validated_at_ms=NULL, validated_rows=0, invalidation_reason=excluded.invalidation_reason""",
            (*key.values, self.retention_bars, reason),
        )

    def write_validated(
        self,
        key: CandleKey,
        frame: pd.DataFrame,
        *,
        as_of: datetime | pd.Timestamp,
        requested_limit: int,
    ) -> dict[str, int]:
        _validate_limit(requested_limit)
        clean = prepare_ohlcv(frame)
        if len(clean) != len(frame):
            raise DataValidationError("Duplicate candles cannot be written to the cache.", code="duplicate_candle")
        validate_live_window(
            clean, key.timeframe, now=as_of, minimum_bars=1,
            maximum_staleness_bars=0, closed_candles_only=True,
        )
        observed_ms = int(pd.Timestamp(as_of).timestamp() * 1000)
        rows = [
            (int(row.timestamp.timestamp() * 1000), float(row.open), float(row.high),
             float(row.low), float(row.close), float(row.volume))
            for row in clean.itertuples(index=False)
        ]
        conflict = False
        inserted = 0
        pruned = 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous = connection.execute(
                    """SELECT open_ms, open, high, low, close, volume FROM candle_cache
                       WHERE exchange_id=? AND market_type=? AND symbol=? AND timeframe=?
                         AND open_ms BETWEEN ? AND ?""",
                    (*key.values, rows[0][0], rows[-1][0]),
                ).fetchall()
                old_rows = {int(row[0]): tuple(row) for row in previous}
                conflict = any(row[0] in old_rows and row != old_rows[row[0]] for row in rows)
                if conflict:
                    self._invalidate(connection, key, "conflicting_closed_candle")
                else:
                    state = connection.execute(
                        """SELECT requested_limit, retention_bars FROM candle_cache_state
                           WHERE exchange_id=? AND market_type=? AND symbol=? AND timeframe=?""",
                        key.values,
                    ).fetchone()
                    retained = max(self.retention_bars, requested_limit, int(state["retention_bars"]) if state else 0)
                    covered_limit = max(requested_limit, int(state["requested_limit"]) if state else 0)
                    inserted = sum(row[0] not in old_rows for row in rows)
                    connection.executemany(
                        """INSERT INTO candle_cache
                               (exchange_id, market_type, symbol, timeframe, open_ms,
                                open, high, low, close, volume, observed_at_ms)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(exchange_id, market_type, symbol, timeframe, open_ms)
                           DO UPDATE SET observed_at_ms=excluded.observed_at_ms""",
                        [(*key.values, *row, observed_ms) for row in rows],
                    )
                    connection.execute(
                        """INSERT INTO candle_cache_state
                               (exchange_id, market_type, symbol, timeframe, requested_limit,
                                retention_bars, validated_at_ms, validated_rows, invalidation_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                           ON CONFLICT(exchange_id, market_type, symbol, timeframe) DO UPDATE SET
                               requested_limit=excluded.requested_limit,
                               retention_bars=excluded.retention_bars,
                               validated_at_ms=excluded.validated_at_ms,
                               validated_rows=excluded.validated_rows, invalidation_reason=NULL""",
                        (*key.values, covered_limit, retained, observed_ms, len(clean)),
                    )
                    cursor = connection.execute(
                        """DELETE FROM candle_cache
                           WHERE exchange_id=? AND market_type=? AND symbol=? AND timeframe=?
                             AND open_ms NOT IN (
                                 SELECT open_ms FROM candle_cache
                                 WHERE exchange_id=? AND market_type=? AND symbol=? AND timeframe=?
                                 ORDER BY open_ms DESC LIMIT ?
                             )""",
                        (*key.values, *key.values, retained),
                    )
                    pruned = cursor.rowcount
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        if conflict:
            raise DataValidationError(
                "Closed exchange candles conflict with cached history; the series "
                "was invalidated and requires a fresh bootstrap.", code="cache_conflict",
            )
        return {"inserted_rows": inserted, "pruned_rows": pruned}

    def stats(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM candle_cache").fetchone()[0]
            series = connection.execute("SELECT COUNT(*) FROM candle_cache_state").fetchone()[0]
        return {"rows": int(rows), "series": int(series), "retention_bars": self.retention_bars}

    @staticmethod
    def empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)


def _validate_limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Candle cache limits must be positive integers.")
