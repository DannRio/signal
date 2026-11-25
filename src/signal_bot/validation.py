from __future__ import annotations

import re
from datetime import UTC, datetime

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
_TIMEFRAME_PATTERN = re.compile(
    r"^(?P<count>[1-9][0-9]*)(?P<unit>[mhdw])$"
)


class DataValidationError(ValueError):
    """Raised when OHLCV data is unsafe to analyze."""

    def __init__(self, message: str, *, code: str = "invalid_ohlcv") -> None:
        super().__init__(message)
        self.code = code


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta:
    match = _TIMEFRAME_PATTERN.fullmatch(timeframe.strip().lower())
    if not match:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r}. Use values such as "
            "15m, 1h, 4h, 1d, or 1w."
        )
    count = int(match.group("count"))
    unit = match.group("unit")
    mapping = {
        "m": "minutes",
        "h": "hours",
        "d": "days",
        "w": "weeks",
    }
    return pd.Timedelta(**{mapping[unit]: count})


def prepare_ohlcv(
    data: pd.DataFrame,
    *,
    minimum_bars: int | None = None,
) -> pd.DataFrame:
    if not isinstance(data, pd.DataFrame):
        raise DataValidationError(
            "OHLCV input must be a pandas DataFrame."
        )
    missing = [
        column
        for column in REQUIRED_COLUMNS
        if column not in data.columns
    ]
    if missing:
        raise DataValidationError(
            f"Missing required OHLCV columns: {missing}"
        )

    frame = data.loc[:, REQUIRED_COLUMNS].copy()
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"], utc=True, errors="coerce"
    )
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if frame.isna().any().any():
        bad_columns = frame.columns[frame.isna().any()].tolist()
        raise DataValidationError(
            "OHLCV data contains missing or invalid values in "
            f"{bad_columns}."
        )

    frame = (
        frame.sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )

    numeric = frame[
        ["open", "high", "low", "close", "volume"]
    ].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise DataValidationError(
            "OHLCV data contains non-finite numeric values."
        )
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise DataValidationError(
            "OHLC prices must be strictly positive."
        )
    if (frame["volume"] < 0).any():
        raise DataValidationError("Volume cannot be negative.")

    invalid_high = frame["high"] < frame[
        ["open", "close", "low"]
    ].max(axis=1)
    invalid_low = frame["low"] > frame[
        ["open", "close", "high"]
    ].min(axis=1)
    if invalid_high.any() or invalid_low.any():
        raise DataValidationError(
            "OHLC price relationships are invalid for one or more candles."
        )

    if minimum_bars is not None and len(frame) < minimum_bars:
        raise DataValidationError(
            f"Need at least {minimum_bars} closed candles; "
            f"received {len(frame)}."
        )
    return frame


def normalize_ccxt_ohlcv(candles: list[list[float]]) -> pd.DataFrame:
    """Normalize CCXT's six-field OHLCV rows into validated columns."""
    if not candles:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    try:
        frame = pd.DataFrame(candles, columns=REQUIRED_COLUMNS)
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"], unit="ms", utc=True, errors="coerce"
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataValidationError(
            "Exchange returned malformed OHLCV rows.", code="malformed_candle"
        ) from exc
    return prepare_ohlcv(frame)


def drop_incomplete_candle(
    data: pd.DataFrame,
    timeframe: str,
    *,
    now: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    if data.empty:
        return data.copy()
    frame = prepare_ohlcv(data)
    current_time = pd.Timestamp(now or datetime.now(UTC))
    if current_time.tzinfo is None:
        current_time = current_time.tz_localize("UTC")
    else:
        current_time = current_time.tz_convert("UTC")

    duration = timeframe_to_timedelta(timeframe)
    # More than one row can be newer than a scan's frozen cutoff. Removing
    # only the last row would allow future/open candles into that snapshot.
    return frame.loc[
        frame["timestamp"] + duration <= current_time
    ].reset_index(drop=True)


def validate_live_window(
    data: pd.DataFrame,
    timeframe: str,
    *,
    now: datetime | pd.Timestamp,
    minimum_bars: int,
    maximum_staleness_bars: int,
    closed_candles_only: bool = True,
) -> None:
    """Fail closed on broken live windows; never fabricate missing bars.

    Price/volume validation is performed by ``normalize_ccxt_ohlcv`` before
    this temporal check. Weekly exchange bars are anchored to Monday UTC.
    """
    if data.empty:
        raise DataValidationError(
            f"No usable {'closed ' if closed_candles_only else ''}{timeframe} candles.",
            code="empty_window",
        )
    if len(data) < minimum_bars:
        raise DataValidationError(
            f"Only {len(data)} usable {timeframe} candles; at least "
            f"{minimum_bars} are required.",
            code="insufficient_history",
        )
    current_time = pd.Timestamp(now)
    current_time = (
        current_time.tz_localize("UTC")
        if current_time.tzinfo is None
        else current_time.tz_convert("UTC")
    )
    if pd.isna(current_time):
        raise ValueError("The live-data cutoff must be a valid timestamp.")
    duration = timeframe_to_timedelta(timeframe)
    timestamps = data["timestamp"]
    if not timestamps.is_unique or not timestamps.is_monotonic_increasing:
        raise DataValidationError(
            f"{timeframe} candles must have unique chronological timestamps.",
            code="duplicate_candle",
        )
    origin_ns = (
        pd.Timestamp("1970-01-05T00:00:00Z").value
        if timeframe.strip().lower().endswith("w")
        else 0
    )
    if any((timestamp.value - origin_ns) % duration.value for timestamp in timestamps):
        raise DataValidationError(
            f"{timeframe} candle timestamps are not aligned to timeframe boundaries.",
            code="misaligned_candle",
        )
    gaps = timestamps.diff().iloc[1:] != duration
    if gaps.any():
        raise DataValidationError(
            f"{timeframe} window has {int(gaps.sum())} timestamp gap(s); "
            "missing candles are not synthesized.",
            code="candle_gap",
        )
    latest_open = timestamps.iloc[-1]
    latest_close = latest_open + duration
    if latest_open > current_time or (closed_candles_only and latest_close > current_time):
        raise DataValidationError(
            f"{timeframe} window contains a candle beyond its analysis cutoff.",
            code="future_candle",
        )
    if maximum_staleness_bars and current_time - latest_close > duration * maximum_staleness_bars:
        raise DataValidationError(
            f"Latest {timeframe} candle is stale by more than "
            f"{maximum_staleness_bars} bars.",
            code="stale_candles",
        )


def ensure_unique_timeframes(frames: dict[str, pd.DataFrame]) -> None:
    if not frames:
        raise DataValidationError(
            "At least one timeframe is required."
        )
    normalized: set[str] = set()
    for timeframe in frames:
        value = timeframe.strip().lower()
        timeframe_to_timedelta(value)
        if value in normalized:
            raise DataValidationError(
                f"Duplicate timeframe: {value}"
            )
        normalized.add(value)
