"""Content-addressed, bounded decision evidence and offline deterministic replay."""
from __future__ import annotations

import json
import math
import zlib
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

import pandas as pd

from .config import RegimeBotConfig
from .engine import RegimeSignalEngine
from .models import to_dict
from .storage import SignalStore
from .validation import validate_live_window

_QUALIFICATION_KEYS = ("status", "error_code", "actionable", "blocked_reasons", "valid_until", "contract_plan")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


def frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    normalized = frame.loc[:, columns].copy()
    normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], utc=True).map(lambda value: value.isoformat())
    return [
        {"timestamp": row.timestamp, "open": float(row.open), "high": float(row.high),
         "low": float(row.low), "close": float(row.close), "volume": float(row.volume)}
        for row in normalized.itertuples(index=False)
    ]


def candidate_record(candidate: Any) -> dict[str, Any]:
    result = candidate.to_dict()
    if candidate.contract_spec is not None:
        result["contract_spec"] = {key: str(value) if isinstance(value, Decimal) else value
                                   for key, value in asdict(candidate.contract_spec).items()}
    return result


def qualification_record(item: Any) -> dict[str, Any]:
    payload = item.to_dict() if hasattr(item, "to_dict") else item
    return {key: payload.get(key) for key in _QUALIFICATION_KEYS}


class ReplayStore:
    """Full input frames are deduplicated; recent decision/quote snapshots are retained.

    This is a rolling diagnostic archive, not an unlimited historical dataset.
    Export a snapshot to retain evidence beyond ``max_snapshots``.
    """

    def __init__(self, store: SignalStore, *, max_snapshots: int = 5000) -> None:
        if isinstance(max_snapshots, bool) or not isinstance(max_snapshots, int) or max_snapshots < 1:
            raise ValueError("max_snapshots must be a positive integer.")
        self.store, self.max_snapshots = store, max_snapshots
        with store._connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS replay_frames (sha256 TEXT PRIMARY KEY, payload_z BLOB NOT NULL)")
            connection.execute("""CREATE TABLE IF NOT EXISTS decision_snapshots (
                snapshot_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, symbol TEXT NOT NULL,
                action TEXT NOT NULL, payload_json TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_replay_time ON decision_snapshots(captured_at DESC)")
            connection.execute("""CREATE TABLE IF NOT EXISTS replay_pins (
                snapshot_id TEXT NOT NULL, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL,
                pinned_at TEXT NOT NULL, PRIMARY KEY(snapshot_id,owner_type,owner_id),
                FOREIGN KEY(snapshot_id) REFERENCES decision_snapshots(snapshot_id))""")

    def capture(self, *, frames: dict[str, pd.DataFrame], request: dict[str, Any],
                as_of: datetime, engine_config: dict[str, Any], candidate: dict[str, Any],
                signal: dict[str, Any], qualification: dict[str, Any], qualified_at: datetime,
                provenance: dict[str, Any], refresh_error: str | None = None) -> str:
        encoded = {}
        references = {}
        for timeframe, frame in sorted(frames.items()):
            records = frame_records(frame)
            digest = _digest(records)
            references[timeframe] = digest
            encoded[digest] = zlib.compress(canonical_json(records).encode())
        payload = {"schema_version": 1, "request": request, "as_of": as_of.isoformat(),
                   "engine_config": engine_config, "frames": references,
                   "candidate": candidate, "expected_signal": signal,
                   "expected_qualification": qualification, "qualified_at": qualified_at.isoformat(),
                   "refresh_error": refresh_error, "provenance": provenance}
        snapshot_id = _digest(payload)
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for digest, compressed in encoded.items():
                    connection.execute("INSERT OR IGNORE INTO replay_frames VALUES (?, ?)", (digest, compressed))
                connection.execute("INSERT OR IGNORE INTO decision_snapshots VALUES (?, ?, ?, ?, ?)",
                                   (snapshot_id, qualified_at.isoformat(), signal["symbol"], signal["action"], canonical_json(payload)))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return snapshot_id

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        with self.store._connection() as connection:
            rows = connection.execute("SELECT snapshot_id,captured_at,symbol,action FROM decision_snapshots ORDER BY captured_at DESC,snapshot_id LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get(self, snapshot_id: str) -> dict[str, Any]:
        with self.store._connection() as connection:
            row = connection.execute("SELECT payload_json FROM decision_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            if row is None:
                raise KeyError("Snapshot is unavailable or has passed the rolling retention limit.")
            payload = json.loads(row["payload_json"])
            if _digest(payload) != snapshot_id:
                raise ValueError("Decision snapshot checksum mismatch.")
            frame_data = {}
            for timeframe, digest in payload["frames"].items():
                stored = connection.execute("SELECT payload_z FROM replay_frames WHERE sha256=?", (digest,)).fetchone()
                if stored is None:
                    raise ValueError("Snapshot is missing a required candle window.")
                records = json.loads(zlib.decompress(stored["payload_z"]))
                if _digest(records) != digest:
                    raise ValueError("Candle snapshot checksum mismatch.")
                frame_data[timeframe] = records
        return {"snapshot_id": snapshot_id, **payload, "frame_data": frame_data}

    def prune(self) -> int:
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute("""DELETE FROM decision_snapshots WHERE snapshot_id NOT IN
                    (SELECT snapshot_id FROM decision_snapshots ORDER BY captured_at DESC,snapshot_id LIMIT ?)
                    AND NOT EXISTS (SELECT 1 FROM replay_pins WHERE replay_pins.snapshot_id=decision_snapshots.snapshot_id)""", (self.max_snapshots,))
                removed = cursor.rowcount
                if removed:
                    used = set()
                    for row in connection.execute("SELECT payload_json FROM decision_snapshots"):
                        used.update(json.loads(row[0])["frames"].values())
                    for row in connection.execute("SELECT sha256 FROM replay_frames").fetchall():
                        if row[0] not in used:
                            connection.execute("DELETE FROM replay_frames WHERE sha256=?", (row[0],))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return removed

    def replay(self, snapshot_id: str) -> dict[str, Any]:
        return replay_snapshot(self.get(snapshot_id))


def replay_snapshot(snapshot: dict[str, Any], *, engine: RegimeSignalEngine | None = None) -> dict[str, Any]:
    """Replay frozen inputs without network access, orders, alerts, or wall-clock qualification."""
    from .futures import ContractSpec
    from .scanner import MarketCandidate, MarketScanner, ScanItem, ScanRequest

    if snapshot.get("schema_version") != 1:
        raise ValueError("Unsupported replay schema version.")
    payload = {key: value for key, value in snapshot.items() if key not in {"snapshot_id", "frame_data"}}
    if _digest(payload) != snapshot.get("snapshot_id"):
        raise ValueError("Decision snapshot checksum mismatch.")
    config = RegimeBotConfig.from_dict(snapshot["engine_config"])
    replay_engine = engine or RegimeSignalEngine(config)
    request = ScanRequest(**snapshot["request"]).normalized()
    request.validate()
    frames = {}
    for timeframe, digest in snapshot["frames"].items():
        records = snapshot["frame_data"].get(timeframe)
        if records is None or _digest(records) != digest:
            raise ValueError("Candle snapshot checksum mismatch.")
        frame = pd.DataFrame(records)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        validate_live_window(frame, timeframe, now=pd.Timestamp(snapshot["as_of"]),
                             minimum_bars=config.minimum_bars,
                             maximum_staleness_bars=request.maximum_staleness_bars,
                             closed_candles_only=True)
        if len(frame) > request.limit:
            raise ValueError("Replay candle window exceeds the original analysis limit.")
        frames[timeframe] = frame
    expected_frames = {request.execution_timeframe, *request.context_timeframes}
    if set(frames) != expected_frames:
        raise ValueError("Replay is missing a requested timeframe.")
    signal = replay_engine.analyze(symbol=snapshot["candidate"]["symbol"], frames=frames,
                                   execution_timeframe=request.execution_timeframe, equity=request.equity,
                                   risk_percent=request.risk_percent, maximum_notional_multiple=request.maximum_notional_multiple)
    candidate_data = dict(snapshot["candidate"])
    spec = candidate_data.pop("contract_spec", None)
    candidate = MarketCandidate(**candidate_data, contract_spec=ContractSpec(**spec) if spec else None)
    qualified = MarketScanner._qualify(ScanItem(0, candidate, signal, None), request,
                                       datetime.fromisoformat(snapshot["qualified_at"]), snapshot.get("refresh_error"))
    signal_diff = _differences(snapshot["expected_signal"], to_dict(signal), "signal")
    qualification_diff = _differences(snapshot["expected_qualification"], qualification_record(qualified), "qualification")
    return {"snapshot_id": snapshot["snapshot_id"], "passed": not signal_diff and not qualification_diff,
            "signal_matches": not signal_diff, "qualification_matches": not qualification_diff,
            "differences": (signal_diff + qualification_diff)[:100],
            "original_as_of": snapshot["as_of"], "original_qualified_at": snapshot["qualified_at"],
            "original_versions": snapshot["provenance"].get("versions", {}),
            "configuration_matches": asdict(replay_engine.config) == snapshot["engine_config"],
            "replayed_at": datetime.now(UTC).isoformat(), "execution_enabled": False}


def _differences(expected: Any, actual: Any, path: str) -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        result = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                result.append(f"{path}.{key}: field missing")
            else:
                result.extend(_differences(expected[key], actual[key], f"{path}.{key}"))
        return result
    if isinstance(expected, (tuple, list)) and isinstance(actual, (tuple, list)):
        if len(expected) != len(actual):
            return [f"{path}: length differs"]
        return [item for index, (left, right) in enumerate(zip(expected, actual, strict=True))
                for item in _differences(left, right, f"{path}[{index}]")]
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)) and not isinstance(expected, bool) and not isinstance(actual, bool):
        equal = expected == actual if path.startswith("qualification.contract_plan") else math.isclose(expected, actual, rel_tol=1e-12, abs_tol=0)
    else:
        equal = type(expected) is type(actual) and expected == actual
    return [] if equal else [f"{path}: expected {expected!r}, got {actual!r}"]
