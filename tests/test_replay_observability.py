from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from test_regime_bot import make_trend

from signal_bot.engine import RegimeSignalEngine
from signal_bot.futures import ContractSpec
from signal_bot.models import to_dict
from signal_bot.observability import next_monitor_delay, summarize_scans
from signal_bot.replay import (
    ReplayStore,
    candidate_record,
    qualification_record,
    replay_snapshot,
)
from signal_bot.scanner import MarketCandidate, MarketScanner, ScanItem, ScanRequest
from signal_bot.storage import SignalStore


def capture(archive, *, offset=0):
    frame = make_trend("15min", bars=300)
    as_of = frame.iloc[-1].timestamp.to_pydatetime() + timedelta(minutes=15, seconds=1)
    qualified_at = as_of + timedelta(seconds=offset)
    request = ScanRequest("kucoinfutures", symbols=("BTC/USDT:USDT",), context_timeframes=(), limit=300)
    engine = RegimeSignalEngine()
    signal = engine.analyze(symbol=request.symbols[0], frames={"15m": frame}, execution_timeframe="15m")
    price = float(frame.iloc[-1].close)
    candidate = MarketCandidate(request.symbols[0], "BTC", "USDT", 2_000_000, 1, price,
                                bid=price-.01, ask=price+.01, quote_timestamp=qualified_at.isoformat(),
                                volume_source="fixture", contract_spec=ContractSpec(request.symbols[0], "0.001", "1", "0.01"))
    item = MarketScanner._qualify(ScanItem(0, candidate, signal, None), request, qualified_at, None)
    return archive.capture(frames={"15m": frame}, request=asdict(request), as_of=as_of,
                           engine_config=asdict(engine.config), candidate=candidate_record(candidate),
                           signal=to_dict(signal), qualification=qualification_record(item),
                           qualified_at=qualified_at, provenance={"versions": {"fixture": "1"}})


def test_replay_is_offline_and_survives_restart_with_exact_floats(tmp_path, monkeypatch):
    path = tmp_path / "evidence.db"
    archive = ReplayStore(SignalStore(path))
    snapshot_id = capture(archive)
    restarted = ReplayStore(SignalStore(path))
    monkeypatch.setattr("signal_bot.scanner.CCXTMarketDataClient", lambda *_: pytest.fail("Replay attempted network access"))
    result = restarted.replay(snapshot_id)
    assert result["passed"]
    assert result["signal_matches"] and result["qualification_matches"]
    assert result["configuration_matches"]
    assert not result["execution_enabled"]
    exported = restarted.get(snapshot_id)
    assert len(exported["frame_data"]["15m"]) == 300
    assert exported["frame_data"]["15m"][-1]["close"] == make_trend("15min", bars=300).iloc[-1].close


def test_replay_detects_input_tampering_and_behavior_regression():
    archive = ReplayStore(SignalStore(":memory:"))
    original = archive.get(capture(archive))
    tampered = deepcopy(original)
    tampered["frame_data"]["15m"][-1]["close"] += .1
    with pytest.raises(ValueError, match="checksum"):
        replay_snapshot(tampered)
    class ChangedEngine(RegimeSignalEngine):
        def analyze(self, **kwargs):
            return replace(super().analyze(**kwargs), score=99.9)
    result = replay_snapshot(original, engine=ChangedEngine())
    assert not result["passed"]
    assert any("signal.score" in item for item in result["differences"])


def test_replay_retention_deduplicates_frames_without_breaking_retained_snapshots():
    store = SignalStore(":memory:")
    archive = ReplayStore(store, max_snapshots=1)
    first, second = capture(archive), capture(archive, offset=5)
    with store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM replay_frames").fetchone()[0] == 1
    assert archive.prune() == 1
    with pytest.raises(KeyError):
        archive.get(first)
    assert archive.replay(second)["passed"]


def test_metrics_group_cache_modes_and_different_workloads_separately():
    rows = []
    for symbol, cache, duration in (("BTC", False, 3), ("BTC", True, 1), ("BTC", True, 2), ("ETH", True, .5)):
        rows.append({"request": {"symbols": [symbol], "use_candle_cache": cache}, "result": {
            "status": "complete", "duration_seconds": duration, "requested_markets": 1, "analyzed_markets": 1,
            "items": [{"candidate": {"symbol": symbol}, "metrics": {"quote_age_seconds": 1, "decision_age_seconds": 3}}],
            "metrics": {"cache_enabled": cache, "counters": {"cache_hits": 3 if cache else 0}, "timings_seconds": {"analysis": duration/2}},
        }})
    report = summarize_scans(rows)
    assert report["sampled_runs"] == 4
    assert len(report["groups"]) == 3
    warm = next(group for group in report["groups"] if group["runs"] == 2)
    assert warm["duration_seconds"]["p50"] == 1.5
    assert warm["duration_seconds"]["p95"] == 1.95
    assert warm["coverage_percent"] == 100
    assert warm["quote_age_seconds_p95"] == 1


@pytest.mark.parametrize(("timestamp", "expected"), [
    ("2026-01-01T11:59:59+00:00", 3),
    ("2026-01-01T12:00:01+00:00", 1),
    ("2026-01-01T12:00:02+00:00", 15),
])
def test_scheduler_uses_candle_publication_grace_and_quote_heartbeat(timestamp, expected):
    assert next_monitor_delay(now=datetime.fromisoformat(timestamp), timeframes=("15m", "1h", "4h"), heartbeat_seconds=15) == expected


def test_weekly_scheduler_uses_monday_utc_and_rejects_naive_time():
    assert next_monitor_delay(now=datetime(2026, 1, 4, 23, 59, 59, tzinfo=UTC), timeframes=("1w",), heartbeat_seconds=15) == 3
    with pytest.raises(ValueError, match="timezone"):
        next_monitor_delay(now=datetime(2026, 1, 1), timeframes=("15m",), heartbeat_seconds=15)


def test_scanner_captures_replay_with_cached_service_and_records_stage_metrics(tmp_path):
    from test_data_integrity import NOW, WindowClient, make_rows

    from signal_bot.service import RegimeBotService

    class Client(WindowClient):
        def load_markets(self):
            return {"BTC/USDT:USDT": {"active": True}}
    now = NOW.to_pydatetime()
    candidate = MarketCandidate("BTC/USDT:USDT", "BTC", "USDT", 2e6, 1, 600,
                                bid=600, ask=600.01, quote_timestamp=now.isoformat(), volume_source="fixture")
    class Provider:
        def discover(self, request):
            return [candidate]
    store = SignalStore(tmp_path / "scan.db")
    client = Client(make_rows())
    service = RegimeBotService(client_factory=lambda *_: client, now_factory=lambda: pd.Timestamp(now))
    scanner = MarketScanner(store=store, universe_provider=Provider(), service_factory=lambda _: service, now_factory=lambda: now)
    report = scanner.scan(ScanRequest("kucoinfutures", symbols=(candidate.symbol,), context_timeframes=(), limit=300))
    assert report.analyzed_markets == 1
    assert report.items[0].replay_id
    assert ReplayStore(store).replay(report.items[0].replay_id)["passed"]
    assert "snapshot_write" in report.metrics["timings_seconds"]
    assert "scan_journal_write" in store.recent_scans()[0]["result"]["metrics"]["timings_seconds"]
