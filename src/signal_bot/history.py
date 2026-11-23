from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pandas as pd

from .storage import SignalStore
from .validation import (
    DataValidationError,
    drop_incomplete_candle,
    normalize_ccxt_ohlcv,
    prepare_ohlcv,
    timeframe_to_timedelta,
)


class HistoryClient(Protocol):
    @property
    def timeframes(self) -> set[str]: ...

    def load_markets(self) -> dict: ...

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int,
    ) -> list[list[float]]: ...


class CCXTHistoryClient:
    """Rate-limited public CCXT history client."""

    def __init__(self, exchange_id: str, market_type: str) -> None:
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover - packaging boundary.
            raise RuntimeError("CCXT is required for historical downloads.") from exc

        exchange_name = exchange_id.strip().lower()
        if not exchange_name or not hasattr(ccxt, exchange_name):
            raise ValueError(f"Unsupported CCXT exchange: {exchange_id!r}.")
        market_name = market_type.strip().lower()
        if not market_name:
            raise ValueError("market_type cannot be empty.")
        exchange_class = getattr(ccxt, exchange_name)
        self.exchange = exchange_class(
            {
                "enableRateLimit": True,
                "options": {"defaultType": market_name},
            }
        )

    @property
    def timeframes(self) -> set[str]:
        return set(getattr(self.exchange, "timeframes", None) or {})

    def load_markets(self) -> dict:
        return self.exchange.load_markets()

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int,
    ) -> list[list[float]]:
        if not self.exchange.has.get("fetchOHLCV"):
            raise NotImplementedError(
                f"{self.exchange.id} does not support OHLCV history."
            )
        return self.exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            since=since,
            limit=limit,
        )


@dataclass(frozen=True)
class HistoryRequest:
    exchange_id: str
    market_type: str
    symbol: str
    timeframe: str
    start: datetime | str
    end: datetime | str
    limit: int = 1000
    output_dir: str | Path = "data/history"
    resume: bool = True
    strict_gaps: bool = False
    maximum_requests: int = 100_000

    def normalized(self) -> HistoryRequest:
        start = _timestamp(self.start)
        end = _timestamp(self.end)
        exchange_id = self.exchange_id.strip().lower()
        market_type = self.market_type.strip().lower()
        symbol = self.symbol.strip()
        timeframe = self.timeframe.strip().lower()
        timeframe_to_timedelta(timeframe)
        if not exchange_id:
            raise ValueError("exchange_id cannot be empty.")
        if not market_type:
            raise ValueError("market_type cannot be empty.")
        if "/" not in symbol:
            raise ValueError("symbol must use CCXT notation, for example BTC/USDT.")
        if end <= start:
            raise ValueError("end must be after start.")
        if self.limit <= 0 or self.limit > 5000:
            raise ValueError("limit must be between 1 and 5000.")
        if self.maximum_requests <= 0:
            raise ValueError("maximum_requests must be positive.")
        return HistoryRequest(
            exchange_id=exchange_id,
            market_type=market_type,
            symbol=symbol,
            timeframe=timeframe,
            start=start.to_pydatetime(),
            end=end.to_pydatetime(),
            limit=self.limit,
            output_dir=self.output_dir,
            resume=self.resume,
            strict_gaps=self.strict_gaps,
            maximum_requests=self.maximum_requests,
        )


@dataclass(frozen=True)
class HistoryResult:
    dataset_key: str
    exchange_id: str
    market_type: str
    symbol: str
    timeframe: str
    path: str
    metadata_path: str
    rows: int
    first_timestamp: str
    last_timestamp: str
    requests: int
    gaps: int
    missing_bars_estimate: int
    duplicates_removed: int
    resumed_rows: int
    sha256: str
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


class HistoryService:
    def __init__(
        self,
        *,
        client_factory=CCXTHistoryClient,
        store: SignalStore | None = None,
    ) -> None:
        self.client_factory = client_factory
        self.store = store

    def download(
        self,
        request: HistoryRequest,
        *,
        client: HistoryClient | None = None,
    ) -> tuple[pd.DataFrame, HistoryResult]:
        normalized = request.normalized()
        start = _timestamp(normalized.start)
        end = _timestamp(normalized.end)
        duration = timeframe_to_timedelta(normalized.timeframe)
        duration_ms = int(duration.total_seconds() * 1000)
        if duration_ms <= 0:
            raise ValueError("timeframe duration must be positive.")

        data_client = client or self.client_factory(
            normalized.exchange_id,
            normalized.market_type,
        )
        markets = data_client.load_markets()
        if normalized.symbol not in markets:
            raise ValueError(
                f"{normalized.symbol!r} is unavailable on "
                f"{normalized.exchange_id} {normalized.market_type}."
            )
        advertised = data_client.timeframes
        if advertised and normalized.timeframe not in advertised:
            raise ValueError(
                f"{normalized.exchange_id} does not advertise "
                f"{normalized.timeframe!r}."
            )

        csv_path, metadata_path, dataset_key = _dataset_paths(normalized)
        existing = pd.DataFrame()
        resumed_rows = 0
        if normalized.resume and csv_path.exists():
            existing = prepare_ohlcv(pd.read_csv(csv_path))
            existing = existing.loc[
                (existing["timestamp"] >= start)
                & (existing["timestamp"] < end)
            ].reset_index(drop=True)
            resumed_rows = len(existing)

        cursor = start
        if not existing.empty:
            cursor = max(
                cursor,
                existing.iloc[-1]["timestamp"] + duration,
            )

        chunks: list[pd.DataFrame] = []
        request_count = 0
        last_cursor_ms = -1
        while cursor < end:
            if request_count >= normalized.maximum_requests:
                raise RuntimeError(
                    "Historical download exceeded maximum_requests; "
                    "narrow the date range or inspect exchange pagination."
                )
            cursor_ms = int(cursor.timestamp() * 1000)
            if cursor_ms <= last_cursor_ms:
                raise RuntimeError("Historical pagination stopped making progress.")
            last_cursor_ms = cursor_ms
            raw = data_client.fetch_ohlcv(
                normalized.symbol,
                normalized.timeframe,
                cursor_ms,
                normalized.limit,
            )
            request_count += 1
            if not raw:
                break
            chunk = normalize_ccxt_ohlcv(raw)
            chunk = chunk.loc[
                (chunk["timestamp"] >= start)
                & (chunk["timestamp"] < end)
            ].reset_index(drop=True)
            if not chunk.empty:
                chunks.append(chunk)
            raw_last_ms = int(raw[-1][0])
            next_ms = raw_last_ms + duration_ms
            if next_ms <= cursor_ms:
                raise RuntimeError(
                    "Exchange returned non-progressing OHLCV pagination."
                )
            cursor = pd.Timestamp(next_ms, unit="ms", tz="UTC")
            if raw_last_ms >= int(end.timestamp() * 1000) - duration_ms:
                break

        frames = [frame for frame in (existing, *chunks) if not frame.empty]
        if not frames:
            raise DataValidationError(
                "No historical candles were returned for the requested window."
            )
        combined_raw = pd.concat(frames, ignore_index=True)
        before = len(combined_raw)
        combined = prepare_ohlcv(combined_raw)
        combined = combined.loc[
            (combined["timestamp"] >= start)
            & (combined["timestamp"] < end)
        ].reset_index(drop=True)
        now = pd.Timestamp(datetime.now(UTC))
        if not combined.empty and combined.iloc[-1]["timestamp"] + duration > now:
            combined = drop_incomplete_candle(
                combined,
                normalized.timeframe,
                now=now,
            )
        if combined.empty:
            raise DataValidationError(
                "The requested window contains no complete candles."
            )

        duplicates_removed = before - len(combined)
        gaps, missing_estimate = _gap_metrics(combined, duration)
        if normalized.strict_gaps and gaps:
            raise DataValidationError(
                f"Dataset contains {gaps} timestamp gap(s), approximately "
                f"{missing_estimate} missing candle(s)."
            )

        csv_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = csv_path.with_suffix(csv_path.suffix + ".tmp")
        export = combined.copy()
        export["timestamp"] = export["timestamp"].dt.strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        export.to_csv(temp_path, index=False)
        os.replace(temp_path, csv_path)
        digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        updated_at = datetime.now(UTC).isoformat()
        result = HistoryResult(
            dataset_key=dataset_key,
            exchange_id=normalized.exchange_id,
            market_type=normalized.market_type,
            symbol=normalized.symbol,
            timeframe=normalized.timeframe,
            path=str(csv_path),
            metadata_path=str(metadata_path),
            rows=len(combined),
            first_timestamp=combined.iloc[0]["timestamp"].isoformat(),
            last_timestamp=combined.iloc[-1]["timestamp"].isoformat(),
            requests=request_count,
            gaps=gaps,
            missing_bars_estimate=missing_estimate,
            duplicates_removed=duplicates_removed,
            resumed_rows=resumed_rows,
            sha256=digest,
            updated_at=updated_at,
        )
        metadata_path.write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if self.store is not None:
            self.store.upsert_dataset(result.to_dict())
        return combined, result

    def download_bundle(
        self,
        *,
        exchange_id: str,
        market_type: str,
        symbol: str,
        timeframes: tuple[str, ...],
        start: datetime | str,
        end: datetime | str,
        limit: int = 1000,
        output_dir: str | Path = "data/history",
        resume: bool = True,
        strict_gaps: bool = False,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, HistoryResult]]:
        normalized_timeframes = tuple(
            dict.fromkeys(value.strip().lower() for value in timeframes if value.strip())
        )
        if not normalized_timeframes:
            raise ValueError("At least one timeframe is required.")
        client = self.client_factory(exchange_id, market_type)
        frames: dict[str, pd.DataFrame] = {}
        results: dict[str, HistoryResult] = {}
        for timeframe in normalized_timeframes:
            frame, result = self.download(
                HistoryRequest(
                    exchange_id=exchange_id,
                    market_type=market_type,
                    symbol=symbol,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    limit=limit,
                    output_dir=output_dir,
                    resume=resume,
                    strict_gaps=strict_gaps,
                ),
                client=client,
            )
            frames[timeframe] = frame
            results[timeframe] = result
        return frames, results

    @staticmethod
    def load(path: str | Path) -> pd.DataFrame:
        return prepare_ohlcv(pd.read_csv(Path(path)))


def _timestamp(value: datetime | str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp


def _dataset_paths(request: HistoryRequest) -> tuple[Path, Path, str]:
    safe_symbol = (
        request.symbol.replace("/", "_")
        .replace(":", "-")
        .replace(" ", "")
    )
    root = Path(request.output_dir)
    directory = root / request.exchange_id / request.market_type / safe_symbol
    csv_path = directory / f"{request.timeframe}.csv"
    metadata_path = directory / f"{request.timeframe}.meta.json"
    dataset_key = "|".join(
        (
            request.exchange_id,
            request.market_type,
            request.symbol,
            request.timeframe,
        )
    )
    return csv_path, metadata_path, dataset_key


def _gap_metrics(
    frame: pd.DataFrame,
    duration: pd.Timedelta,
) -> tuple[int, int]:
    if len(frame) < 2:
        return 0, 0
    differences = frame["timestamp"].diff().dropna()
    gap_mask = differences > duration * 1.5
    gaps = int(gap_mask.sum())
    missing = 0
    for difference in differences[gap_mask]:
        missing += max(0, int(round(difference / duration)) - 1)
    return gaps, missing
