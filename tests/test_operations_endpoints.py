from __future__ import annotations

import json
import socket
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_futures_research import candles, evidence
from test_paper import NOW, SYMBOL, snapshot
from test_replay_observability import capture

from signal_bot import machine_cli
from signal_bot.api import create_app
from signal_bot.paper import PaperPolicy, PaperTracker
from signal_bot.paper_monitor import observe_paper_scan
from signal_bot.replay import ReplayStore
from signal_bot.storage import SignalStore


@pytest.fixture(autouse=True)
def offline_operations(monkeypatch):
    monkeypatch.delenv("SIGNAL_API_KEY", raising=False)
    original_connect = socket.socket.connect

    def offline_connect(connection, address):
        # Windows asyncio emulates socketpair with a loopback TCP connection.
        # Permit only its exact loopback literals, never venue hosts or a
        # broader private-network range.
        host = address[0] if isinstance(address, tuple) and address else None
        if (connection.family == socket.AF_INET and host == "127.0.0.1") or (
            connection.family == socket.AF_INET6 and host == "::1"
        ):
            return original_connect(connection, address)
        pytest.fail(
            "An offline operations test attempted an external network connection."
        )

    monkeypatch.setattr(socket.socket, "connect", offline_connect)


@pytest.mark.parametrize(
    ("family", "host"),
    [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")],
)
def test_offline_guard_allows_event_loop_loopback_connections(family, host):
    listener = None
    try:
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.bind((host, 0))
    except OSError:
        if listener is not None:
            listener.close()
        if family == socket.AF_INET6:
            pytest.skip("IPv6 loopback is unavailable on this host.")
        raise
    with listener:
        listener.settimeout(1.0)
        listener.listen(1)
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect(listener.getsockname())
            accepted, address = listener.accept()
            with accepted:
                assert address[0] == host


@pytest.mark.parametrize("host", ["198.51.100.10", "api-futures.kucoin.com"])
def test_offline_guard_still_blocks_external_and_venue_connections(host):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        with pytest.raises(pytest.fail.Exception, match="external network connection"):
            connection.connect((host, 443))


class RecordingScanner:
    def __init__(self):
        self.requests = []

    def scan(self, request):
        self.requests.append(request)
        raise AssertionError("Invalid input reached the scanner.")


class RecordingHistory:
    def __init__(self):
        self.requests = []

    def download_bundle(self, **kwargs):
        self.requests.append(kwargs)
        raise AssertionError("Invalid futures evidence reached history download.")


def account_rows(store):
    with store._connection() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT account_id,assumed_equity,policy_json FROM paper_accounts ORDER BY account_id"
            ).fetchall()
        ]


def test_replay_api_get_and_verification_reproduce_archived_inputs(tmp_path):
    store = SignalStore(tmp_path / "replay.db")
    archive = ReplayStore(store)
    snapshot_id = capture(archive)
    client = TestClient(create_app(store=store))

    listing = client.get("/v1/scanner/replays")
    assert listing.status_code == 200
    assert any(item["snapshot_id"] == snapshot_id for item in listing.json()["items"])
    response = client.get(f"/v1/scanner/replays/{snapshot_id}")
    assert response.status_code == 200
    assert response.json() == archive.get(snapshot_id)
    assert len(response.json()["frame_data"]["15m"]) == 300

    verified = client.post(f"/v1/scanner/replays/{snapshot_id}/verify")
    assert verified.status_code == 200
    assert verified.json()["passed"] is True
    assert verified.json()["signal_matches"] is True
    assert verified.json()["qualification_matches"] is True
    assert verified.json()["execution_enabled"] is False
    assert client.get("/v1/scanner/replays/missing").status_code == 404
    assert client.post("/v1/scanner/replays/missing/verify").status_code == 404


def test_replay_cli_exports_complete_inputs_and_replays_without_original_database(
    tmp_path, capsys
):
    database = tmp_path / "replay.db"
    archive = ReplayStore(SignalStore(database))
    snapshot_id = capture(archive)
    destination = tmp_path / "exports" / "decision.json"

    assert (
        machine_cli.main(
            ["--db", str(database), "replay", snapshot_id, "--export", str(destination)]
        )
        == 0
    )
    exported_result = json.loads(capsys.readouterr().out)
    assert exported_result["passed"] is True
    assert exported_result["exported_to"] == str(destination)
    assert json.loads(destination.read_text(encoding="utf-8")) == archive.get(
        snapshot_id
    )

    assert (
        machine_cli.main(["--db", ":memory:", "replay", "--file", str(destination)])
        == 0
    )
    offline_result = json.loads(capsys.readouterr().out)
    assert offline_result["passed"] is True
    assert offline_result["configuration_matches"] is True
    assert offline_result["execution_enabled"] is False


def test_metrics_api_and_cli_preserve_workload_and_cache_grouping(tmp_path, capsys):
    database = tmp_path / "metrics.db"
    store = SignalStore(database)
    for symbol, cached, duration in (
        (SYMBOL, False, 3.0),
        (SYMBOL, True, 1.0),
        (SYMBOL, True, 2.0),
        ("ETH/USDT:USDT", True, 0.5),
    ):
        store.record_scan(
            exchange_id="kucoinfutures",
            market_type="swap",
            request={"symbols": [symbol], "use_candle_cache": cached},
            result={
                "status": "complete",
                "duration_seconds": duration,
                "requested_markets": 1,
                "analyzed_markets": 1,
                "items": [
                    {
                        "candidate": {"symbol": symbol},
                        "metrics": {
                            "quote_age_seconds": 2.0,
                            "decision_age_seconds": 4.0,
                        },
                    }
                ],
                "metrics": {
                    "cache_enabled": cached,
                    "counters": {"cache_hits": 3 if cached else 0},
                    "timings_seconds": {"analysis": duration / 2},
                },
            },
        )
    response = TestClient(create_app(store=store)).get("/v1/scanner/metrics?limit=4")
    assert response.status_code == 200
    report = response.json()
    assert report["sampled_runs"] == 4
    assert len(report["groups"]) == 3
    warm = next(group for group in report["groups"] if group["runs"] == 2)
    assert warm["cache_state"] == "warm"
    assert warm["duration_seconds"] == {"p50": 1.5, "p95": 1.95, "max": 2.0}
    assert warm["coverage_percent"] == 100.0
    assert warm["quote_age_seconds_p95"] == 2.0
    assert machine_cli.main(["--db", str(database), "metrics", "--limit", "4"]) == 0
    assert json.loads(capsys.readouterr().out) == report


def test_paper_read_endpoints_and_cli_do_not_create_absent_accounts(tmp_path, capsys):
    database = tmp_path / "paper.db"
    store = SignalStore(database)
    client = TestClient(create_app(store=store))
    status = client.get("/v1/paper/status?account_id=not-created")
    assert status.status_code == 200
    assert status.json()["status"] == "not_started"
    assert status.json()["order_execution"] is False
    for endpoint in ("positions", "events"):
        response = client.get(f"/v1/paper/{endpoint}?account_id=not-created")
        assert response.status_code == 200
        assert response.json()["items"] == []
    assert (
        machine_cli.main(
            ["--db", str(database), "paper-status", "--account", "not-created"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "not_started"
    assert account_rows(store) == []


def test_paper_status_restores_nondefault_equity_and_saved_policy_after_restart(
    tmp_path, capsys
):
    database = tmp_path / "paper.db"
    original = SignalStore(database)
    policy = PaperPolicy(assumed_equity=2500.0, fee_bps=7.0, target=2)
    PaperTracker(original, account_id="experiment", policy=policy)
    before = account_rows(original)

    client = TestClient(create_app(store=SignalStore(database)))
    response = client.get("/v1/paper/status?account_id=experiment")
    assert response.status_code == 200
    status = response.json()
    assert status["status"] == "observed"
    assert status["assumed_equity"] == 2500.0
    assert status["cash_equity_including_verified_funding"] == 2500.0
    assert status["policy"] == asdict(policy)
    assert (
        machine_cli.main(
            ["--db", str(database), "paper-status", "--account", "experiment"]
        )
        == 0
    )
    saved = json.loads(capsys.readouterr().out)
    assert saved["summary"]["assumed_equity"] == 2500.0
    assert saved["summary"]["policy"] == asdict(policy)
    assert account_rows(original) == before


@pytest.mark.parametrize(
    ("paper_policy", "detail"),
    [
        ([], "object"),
        ({"target": 4}, "target"),
        ({"max_open_positions": 0}, "max_open_positions"),
        ({"observation_mode": "closed_bars"}, "quotes"),
        ({"assumed_equity": 20000.0}, "equity"),
    ],
)
def test_scan_api_rejects_invalid_paper_policy_before_scanner_work(
    paper_policy, detail
):
    scanner = RecordingScanner()
    client = TestClient(create_app(store=SignalStore(":memory:"), scanner=scanner))
    response = client.post(
        "/v1/scan",
        json={
            "symbols": [SYMBOL],
            "track_paper": True,
            "equity": 10000.0,
            "paper_policy": paper_policy,
        },
    )
    assert response.status_code == 422
    assert detail in response.json()["detail"]
    assert scanner.requests == []


def test_scan_api_rejects_existing_paper_account_equity_mismatch_before_scanner_work():
    store = SignalStore(":memory:")
    PaperTracker(
        store, account_id="experiment", policy=PaperPolicy(assumed_equity=2500.0)
    )
    scanner = RecordingScanner()
    client = TestClient(create_app(store=store, scanner=scanner))
    response = client.post(
        "/v1/scan",
        json={
            "symbols": [SYMBOL],
            "track_paper": True,
            "paper_account": "experiment",
            "equity": 10000.0,
        },
    )
    assert response.status_code == 422
    assert "equity" in response.json()["detail"]
    assert scanner.requests == []


@pytest.mark.parametrize(
    "option", ["--paper-risk-budget-percent", "--paper-max-positions"]
)
def test_monitor_cli_rejects_invalid_paper_policy_before_scanner_work(
    monkeypatch, capsys, option
):
    scanner = RecordingScanner()
    monkeypatch.setattr(machine_cli, "MarketScanner", lambda *args, **kwargs: scanner)
    result = machine_cli.main(
        [
            "--db",
            ":memory:",
            "monitor",
            "--symbols",
            SYMBOL,
            "--paper",
            "--iterations",
            "1",
            option,
            "0",
        ]
    )
    assert result == 2
    assert "error:" in capsys.readouterr().err
    assert scanner.requests == []


def test_benchmark_freezes_auto_universe_before_one_uncached_and_one_cached_measurement(
    monkeypatch, capsys
):
    events = []
    requests = []
    frozen_symbols = ("ETH/USDT:USDT", SYMBOL)

    class BenchmarkScanner:
        def discover_universe(self, request):
            events.append("discover")
            assert request.auto_universe is True
            return SimpleNamespace(
                candidates=tuple(
                    SimpleNamespace(symbol=symbol) for symbol in frozen_symbols
                ),
                rejections=(),
            )

        def scan(self, request):
            events.append("cached" if request.use_candle_cache else "uncached")
            requests.append(request)
            payload = {
                "status": "complete",
                "duration_seconds": 1.0,
                "requested_markets": 2,
                "analyzed_markets": 2,
                "items": [
                    {"candidate": {"symbol": symbol}} for symbol in request.symbols
                ],
                "metrics": {
                    "cache_enabled": request.use_candle_cache,
                    "counters": {"cache_hits": 2 if request.use_candle_cache else 0},
                },
            }
            return SimpleNamespace(to_dict=lambda: payload)

    monkeypatch.setattr(
        machine_cli, "MarketScanner", lambda *args, **kwargs: BenchmarkScanner()
    )
    monkeypatch.setattr(
        machine_cli,
        "AlertDispatcher",
        lambda *args, **kwargs: pytest.fail(
            "A benchmark must not initialize notifications."
        ),
    )
    result = machine_cli.main(
        ["--db", ":memory:", "benchmark", "--auto-universe", "--repetitions", "1"]
    )
    assert result == 0
    assert events == ["discover", "uncached", "cached"]
    assert all(
        request.symbols == frozen_symbols and request.auto_universe is False
        for request in requests
    )
    first, second = map(asdict, requests)
    assert first.pop("use_candle_cache") is False
    assert second.pop("use_candle_cache") is True
    assert first == second
    output = json.loads(capsys.readouterr().out)
    assert len(output["measurements"]) == 2
    assert {group["cache_state"] for group in output["summary"]["groups"]} == {
        "disabled",
        "warm",
    }
    assert len({group["workload_id"] for group in output["summary"]["groups"]}) == 1


def test_paper_monitor_fetches_outside_universe_quotes_and_exits_before_admission(
    tmp_path,
):
    now = [NOW]
    paper = PaperTracker(
        SignalStore(tmp_path / "paper.db"),
        policy=PaperPolicy(
            fee_bps=0.0,
            slippage_bps=0.0,
            max_open_positions=1,
        ),
        now_factory=lambda: now[0],
    )
    assert paper.process_scan(snapshot())["opened"] == 1
    now[0] += timedelta(seconds=15)
    report = snapshot(symbol="ETH/USDT:USDT", now=now[0])
    requested_quotes = []

    def paper_quotes(symbols):
        requested_quotes.append(symbols)
        return {
            SYMBOL: {
                "symbol": SYMBOL,
                "bid": 102.0,
                "ask": 102.0,
                "quote_timestamp": now[0].isoformat(),
            }
        }

    outcome = observe_paper_scan(
        SimpleNamespace(paper_quotes=paper_quotes), paper, report
    )
    assert requested_quotes == [(SYMBOL,)]
    assert outcome["errors"] == []
    assert outcome["admissions"]["opened"] == 1
    positions = {position["symbol"]: position for position in paper.positions()}
    assert positions[SYMBOL]["status"] == "closed"
    assert positions["ETH/USDT:USDT"]["status"] == "open"
    assert outcome["summary"]["open_positions"] == 1


def test_paper_monitor_missing_outside_quote_retains_unresolved_risk_and_blocks_admission(
    tmp_path,
):
    now = [NOW]
    paper = PaperTracker(
        SignalStore(tmp_path / "paper.db"),
        policy=PaperPolicy(
            fee_bps=0.0,
            slippage_bps=0.0,
            max_open_positions=1,
        ),
        now_factory=lambda: now[0],
    )
    assert paper.process_scan(snapshot())["opened"] == 1
    now[0] += timedelta(seconds=15)
    report = snapshot(symbol="ETH/USDT:USDT", now=now[0])

    def unavailable_quote(symbols):
        assert symbols == (SYMBOL,)
        raise RuntimeError("Simulated public quote outage")

    outcome = observe_paper_scan(
        SimpleNamespace(paper_quotes=unavailable_quote), paper, report
    )
    assert {error["code"] for error in outcome["errors"]} == {
        "paper_quote_fetch_failed",
        "paper_observation_gap",
    }
    assert outcome["admissions"]["opened"] == 0
    assert outcome["summary"]["unresolved_positions"] == 1
    assert outcome["summary"]["reserved_risk"] > 0
    assert len(paper.positions()) == 1
    assert paper.positions()[0]["symbol"] == SYMBOL


def test_monitor_cli_connects_paper_processing_without_sending_notifications(
    monkeypatch, capsys
):
    report = SimpleNamespace(items=())
    scanner = SimpleNamespace(scan=lambda request: report)
    tracker = SimpleNamespace(
        mark_monitoring_gap=lambda **kwargs: pytest.fail(
            "Successful observation was marked missing."
        )
    )
    observed = []

    def observe(actual_scanner, actual_tracker, actual_report):
        observed.append((actual_scanner, actual_tracker, actual_report))
        return {"summary": {"open_positions": 0}, "admissions": {}, "errors": []}

    monkeypatch.setattr(machine_cli, "MarketScanner", lambda *args, **kwargs: scanner)
    monkeypatch.setattr(machine_cli, "PaperTracker", lambda *args, **kwargs: tracker)
    monkeypatch.setattr(machine_cli, "observe_paper_scan", observe)
    monkeypatch.setattr(machine_cli, "_print_scan", lambda value: None)
    monkeypatch.setattr(machine_cli, "_alert_sinks", lambda value: ())
    monkeypatch.setattr(
        machine_cli,
        "AlertDispatcher",
        lambda *args, **kwargs: SimpleNamespace(
            dispatch=lambda *args, **kwargs: pytest.fail(
                "This monitor fixture contains no actionable signal."
            ),
        ),
    )
    assert (
        machine_cli.main(
            [
                "--db",
                ":memory:",
                "monitor",
                "--symbols",
                SYMBOL,
                "--paper",
                "--iterations",
                "1",
            ]
        )
        == 0
    )
    assert observed == [(scanner, tracker, report)]
    assert "Paper:" in capsys.readouterr().out


def invalid_futures_evidence(kind):
    if kind == "missing_fields":
        return {}
    frame = candles()
    payload = evidence(frame, [(frame.iloc[221]["timestamp"], "0.001", "100")])
    payload["funding"]["events"] = []
    return payload


@pytest.mark.parametrize("route", ["/v1/backtest/live", "/v1/research/validate"])
@pytest.mark.parametrize("kind", ["missing_fields", "incomplete_funding"])
def test_research_api_prevalidates_futures_inputs_before_history_download(route, kind):
    history = RecordingHistory()
    client = TestClient(
        create_app(store=SignalStore(":memory:"), history_service=history)
    )
    response = client.post(
        route,
        json={
            "symbol": SYMBOL,
            "start": "2024-01-01",
            "end": "2024-02-01",
            "execution_timeframe": "15m",
            "context_timeframes": [],
            "futures_inputs": invalid_futures_evidence(kind),
        },
    )
    assert response.status_code == 422
    assert history.requests == []
    assert (
        "funding" in response.json()["detail"].lower()
        or "futures" in response.json()["detail"].lower()
    )


@pytest.mark.parametrize("command", ["backtest-live", "validate"])
@pytest.mark.parametrize(
    "kind", ["malformed_json", "missing_fields", "incomplete_funding"]
)
def test_research_cli_prevalidates_futures_file_before_history_download(
    tmp_path, monkeypatch, capsys, command, kind
):
    history = RecordingHistory()
    monkeypatch.setattr(machine_cli, "HistoryService", lambda **kwargs: history)
    destination = tmp_path / "funding.json"
    destination.write_text(
        "{broken JSON"
        if kind == "malformed_json"
        else json.dumps(invalid_futures_evidence(kind)),
        encoding="utf-8",
    )
    result = machine_cli.main(
        [
            "--db",
            ":memory:",
            command,
            "--symbol",
            SYMBOL,
            "--start",
            "2024-01-01",
            "--end",
            "2024-02-01",
            "--futures-inputs",
            str(destination),
            "--json",
        ]
    )
    assert result == 2
    assert history.requests == []
    assert "error:" in capsys.readouterr().err
