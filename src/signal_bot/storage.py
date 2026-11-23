from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import MarketSignal, to_dict


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}.")


def _dump(value: Any) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"))


class SignalStore:
    """Durable SQLite journal for signals, datasets, scans, backtests, and alerts.

    File-backed stores open a short-lived connection per operation. ``:memory:``
    uses one guarded connection so tests and embedded applications retain state.
    """

    def __init__(self, path: str | Path = "data/signal_machine.db") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if self.path != ":memory:":
            resolved = Path(self.path)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(resolved)
        else:
            self._memory_connection = self._new_connection()
        self._initialize()

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._memory_connection is not None:
                yield self._memory_connection
                return
            connection = self._new_connection()
            try:
                yield connection
            finally:
                connection.close()

    def _initialize(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL,
                exchange_id TEXT NOT NULL,
                market_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                execution_timeframe TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                action TEXT NOT NULL,
                regime TEXT NOT NULL,
                score REAL NOT NULL,
                confidence REAL NOT NULL,
                consensus REAL NOT NULL,
                setup_type TEXT NOT NULL,
                trigger_price REAL,
                payload_json TEXT NOT NULL,
                UNIQUE(fingerprint, exchange_id, market_type)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_signals_symbol_time
            ON signals(symbol, generated_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS datasets (
                dataset_key TEXT PRIMARY KEY,
                exchange_id TEXT NOT NULL,
                market_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                path TEXT NOT NULL,
                first_timestamp TEXT,
                last_timestamp TEXT,
                rows INTEGER NOT NULL,
                gaps INTEGER NOT NULL,
                duplicates_removed INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS backtests (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                exchange_id TEXT,
                market_type TEXT,
                symbol TEXT NOT NULL,
                execution_timeframe TEXT NOT NULL,
                start_time TEXT,
                end_time TEXT,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_backtests_symbol_time
            ON backtests(symbol, created_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS scans (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                exchange_id TEXT NOT NULL,
                market_type TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS alert_ledger (
                alert_key TEXT PRIMARY KEY,
                last_sent_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """,
        )
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                for statement in statements:
                    connection.execute(statement)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def record_signal(
        self,
        signal: MarketSignal,
        *,
        exchange_id: str,
        market_type: str,
        observed_at: datetime | None = None,
    ) -> bool:
        timestamp = observed_at or _utc_now()
        payload = to_dict(signal)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO signals (
                    fingerprint, exchange_id, market_type, symbol,
                    execution_timeframe, generated_at, observed_at,
                    action, regime, score, confidence, consensus,
                    setup_type, trigger_price, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.fingerprint,
                    exchange_id,
                    market_type,
                    signal.symbol,
                    signal.execution_timeframe,
                    signal.generated_at.isoformat(),
                    timestamp.isoformat(),
                    signal.action.value,
                    signal.regime.value,
                    signal.score,
                    signal.confidence,
                    signal.consensus,
                    signal.setup_type.value,
                    signal.trigger_price,
                    _dump(payload),
                ),
            )
            return cursor.rowcount == 1

    def recent_signals(
        self,
        *,
        limit: int = 100,
        symbol: str | None = None,
        actions: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 5000:
            raise ValueError("limit must be between 1 and 5000.")
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if actions:
            placeholders = ",".join("?" for _ in actions)
            clauses.append(f"action IN ({placeholders})")
            params.extend(actions)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json, exchange_id, market_type, observed_at
                FROM signals
                {where}
                ORDER BY generated_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["exchange_id"] = row["exchange_id"]
            payload["market_type"] = row["market_type"]
            payload["observed_at"] = row["observed_at"]
            result.append(payload)
        return result

    def upsert_dataset(self, metadata: dict[str, Any]) -> None:
        required = {
            "dataset_key",
            "exchange_id",
            "market_type",
            "symbol",
            "timeframe",
            "path",
            "rows",
            "gaps",
            "duplicates_removed",
            "sha256",
        }
        missing = required.difference(metadata)
        if missing:
            raise ValueError(f"Dataset metadata missing: {sorted(missing)}")
        updated_at = str(metadata.get("updated_at") or _utc_now().isoformat())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO datasets (
                    dataset_key, exchange_id, market_type, symbol, timeframe,
                    path, first_timestamp, last_timestamp, rows, gaps,
                    duplicates_removed, sha256, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_key) DO UPDATE SET
                    path = excluded.path,
                    first_timestamp = excluded.first_timestamp,
                    last_timestamp = excluded.last_timestamp,
                    rows = excluded.rows,
                    gaps = excluded.gaps,
                    duplicates_removed = excluded.duplicates_removed,
                    sha256 = excluded.sha256,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    metadata["dataset_key"],
                    metadata["exchange_id"],
                    metadata["market_type"],
                    metadata["symbol"],
                    metadata["timeframe"],
                    metadata["path"],
                    metadata.get("first_timestamp"),
                    metadata.get("last_timestamp"),
                    int(metadata["rows"]),
                    int(metadata["gaps"]),
                    int(metadata["duplicates_removed"]),
                    metadata["sha256"],
                    _dump(metadata),
                    updated_at,
                ),
            )

    def list_datasets(self, *, limit: int = 500) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 5000:
            raise ValueError("limit must be between 1 and 5000.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT metadata_json FROM datasets
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["metadata_json"]) for row in rows]

    def record_backtest(
        self,
        *,
        request: dict[str, Any],
        result: dict[str, Any],
    ) -> str:
        run_id = uuid4().hex
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO backtests (
                    run_id, created_at, exchange_id, market_type, symbol,
                    execution_timeframe, start_time, end_time,
                    request_json, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _utc_now().isoformat(),
                    request.get("exchange_id"),
                    request.get("market_type"),
                    str(request["symbol"]),
                    str(request["execution_timeframe"]),
                    request.get("start"),
                    request.get("end"),
                    _dump(request),
                    _dump(result),
                ),
            )
        return run_id

    def recent_backtests(
        self,
        *,
        limit: int = 50,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        params: list[Any] = []
        where = ""
        if symbol:
            where = "WHERE symbol = ?"
            params.append(symbol)
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, created_at, request_json, result_json
                FROM backtests {where}
                ORDER BY created_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "request": json.loads(row["request_json"]),
                "result": json.loads(row["result_json"]),
            }
            for row in rows
        ]

    def record_scan(
        self,
        *,
        exchange_id: str,
        market_type: str,
        request: dict[str, Any],
        result: dict[str, Any],
    ) -> str:
        run_id = uuid4().hex
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO scans (
                    run_id, created_at, exchange_id, market_type,
                    request_json, result_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _utc_now().isoformat(),
                    exchange_id,
                    market_type,
                    _dump(request),
                    _dump(result),
                ),
            )
        return run_id

    def recent_scans(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT run_id, created_at, request_json, result_json
                FROM scans ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "request": json.loads(row["request_json"]),
                "result": json.loads(row["result_json"]),
            }
            for row in rows
        ]

    def alert_due(
        self,
        alert_key: str,
        *,
        cooldown: timedelta,
        now: datetime | None = None,
    ) -> bool:
        timestamp = now or _utc_now()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT last_sent_at FROM alert_ledger WHERE alert_key = ?",
                (alert_key,),
            ).fetchone()
        if row is None:
            return True
        last = datetime.fromisoformat(row["last_sent_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return timestamp - last >= cooldown

    def mark_alert(
        self,
        alert_key: str,
        payload: dict[str, Any],
        *,
        sent_at: datetime | None = None,
    ) -> None:
        timestamp = sent_at or _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO alert_ledger (alert_key, last_sent_at, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(alert_key) DO UPDATE SET
                    last_sent_at = excluded.last_sent_at,
                    payload_json = excluded.payload_json
                """,
                (alert_key, timestamp.isoformat(), _dump(payload)),
            )

    def close(self) -> None:
        with self._lock:
            if self._memory_connection is not None:
                self._memory_connection.close()
                self._memory_connection = None
