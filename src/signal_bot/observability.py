"""Comparable scan measurements and candle/quote scheduling helpers."""
from __future__ import annotations

import math
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from .replay import canonical_json
from .validation import timeframe_to_timedelta


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    point = (len(ordered) - 1) * fraction
    lower = math.floor(point)
    return round(ordered[lower] + (ordered[math.ceil(point)] - ordered[lower]) * (point - lower), 6)


def summarize_scans(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {}
    for row in rows:
        report = row["result"]
        request = dict(row["request"])
        request.pop("use_candle_cache", None)
        request.pop("capture_replay", None)
        symbols = sorted(item["candidate"]["symbol"] for item in report.get("items", []))
        workload = {"request": request, "symbols": symbols, "engine_config": report.get("provenance", {}).get("engine_config")}
        workload_id = sha256(canonical_json(workload).encode()).hexdigest()
        counters = report.get("metrics", {}).get("counters", {})
        cache_enabled = report.get("metrics", {}).get("cache_enabled", False)
        cache_state = "disabled" if not cache_enabled else "warm" if counters.get("cache_hits", 0) and not counters.get("cache_misses", 0) else "cold_or_incremental"
        key = (workload_id, cache_state)
        group = groups.setdefault(key, {"workload_id": workload_id, "workload": workload, "cache_state": cache_state,
                                       "runs": 0, "complete_runs": 0, "failed_markets": 0, "analyzed_markets": 0,
                                       "requested_markets": 0, "durations": [], "counters": {}, "stage_seconds": {},
                                       "decision_ages": [], "quote_ages": []})
        group["runs"] += 1
        group["complete_runs"] += report.get("status") == "complete"
        for name in ("failed_markets", "analyzed_markets", "requested_markets"):
            group[name] += report.get(name, 0)
        duration = report.get("duration_seconds")
        if isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0:
            group["durations"].append(duration)
        for name, value in counters.items():
            group["counters"][name] = group["counters"].get(name, 0) + value
        for name, value in report.get("metrics", {}).get("timings_seconds", {}).items():
            group["stage_seconds"].setdefault(name, []).append(value)
        for item in report.get("items", []):
            for field, target in (("decision_age_seconds", "decision_ages"), ("quote_age_seconds", "quote_ages")):
                value = item.get("metrics", {}).get(field)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    group[target].append(value)
    results = []
    for group in groups.values():
        durations = group.pop("durations")
        group["duration_seconds"] = {"p50": _percentile(durations, .50), "p95": _percentile(durations, .95), "max": max(durations) if durations else None}
        group["coverage_percent"] = round(100 * group["analyzed_markets"] / group["requested_markets"], 2) if group["requested_markets"] else None
        group["stage_seconds"] = {name: {"p50": _percentile(values, .5), "p95": _percentile(values, .95)} for name, values in group["stage_seconds"].items()}
        group["decision_age_seconds_p95"] = _percentile(group.pop("decision_ages"), .95)
        group["quote_age_seconds_p95"] = _percentile(group.pop("quote_ages"), .95)
        results.append(group)
    return {"sampled_runs": len(rows), "groups": results, "analysis_workers": 1,
            "notes": ["Workloads and cache states are grouped separately; observations do not establish maximum capacity.",
                      "Stage timers can nest; do not add them to estimate total duration.",
                      "Network concurrency is not enabled; retain the coordinated exchange limiter."]}


def next_monitor_delay(*, now: datetime, timeframes: tuple[str, ...], heartbeat_seconds: float,
                       publication_grace_seconds: float = 2.0) -> float:
    """Wake at a quote heartbeat or just after the next candle close, whichever is sooner.

    The grace is a polling delay, not a claim that the exchange published a bar.
    The data service must still validate that the expected closed candle exists.
    """
    if now.tzinfo is None:
        raise ValueError("Scheduler time must include a timezone.")
    if not math.isfinite(heartbeat_seconds) or not 1 <= heartbeat_seconds <= 60:
        raise ValueError("heartbeat_seconds must be between 1 and 60.")
    if not math.isfinite(publication_grace_seconds) or not 0 <= publication_grace_seconds <= 30:
        raise ValueError("publication_grace_seconds must be between 0 and 30.")
    seconds = now.astimezone(UTC).timestamp()
    delay = heartbeat_seconds
    for timeframe in timeframes:
        period = timeframe_to_timedelta(timeframe).total_seconds()
        origin = datetime(1970, 1, 5, tzinfo=UTC).timestamp() if timeframe.endswith("w") else 0
        previous = math.floor((seconds - origin) / period) * period + origin
        next_wake = previous + publication_grace_seconds
        if next_wake <= seconds:
            next_wake += period
        delay = min(delay, next_wake - seconds)
    return max(.05, delay)
