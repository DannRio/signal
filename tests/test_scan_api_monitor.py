from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_scanner import NOW, FakeService, FakeUniverse

from signal_bot import machine_cli
from signal_bot.api import create_app
from signal_bot.scanner import MarketScanner, ScanInProgressError, ScanRequest
from signal_bot.storage import SignalStore


def scanner(store):
    return MarketScanner(store=store, universe_provider=FakeUniverse(),
                         service_factory=lambda _engine: FakeService(), now_factory=lambda: NOW)


def test_scan_response_is_json_safe_and_status_is_persisted():
    store = SignalStore(":memory:")
    client = TestClient(create_app(store=store, scanner=scanner(store)))
    response = client.post("/v1/scan", json={"symbols": ["AAA/USDT", "BBB/USDT"]})
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "complete"
    assert report["items"][0]["contract_plan"]["contracts"] > 0
    assert len(report["provenance"]["config_sha256"]) == 64
    status = client.get("/v1/scanner/status").json()
    assert status["last_scan"]["run_id"] == report["run_id"]
    assert status["last_scan"]["actionable_at_scan_time"] == report["actionable_markets"]


def test_overlapping_scan_returns_conflict():
    class Busy:
        def scan(self, request):
            raise ScanInProgressError("Scan is already running.")
    client = TestClient(create_app(store=SignalStore(":memory:"), scanner=Busy()))
    response = client.post("/v1/scan", json={"symbols": ["BTC/USDT"]})
    assert response.status_code == 409


def test_api_validates_quote_freshness_before_network():
    client = TestClient(create_app(store=SignalStore(":memory:")))
    response = client.post("/v1/scan", json={"symbols": ["BTC/USDT"], "maximum_quote_age_seconds": 0})
    assert response.status_code == 422
    assert "maximum_quote_age_seconds" in response.json()["detail"]


def test_monitor_surfaces_sink_failures_without_sending_notifications(monkeypatch, capsys):
    store = SignalStore(":memory:")
    report = scanner(store).scan(ScanRequest(exchange_id="kucoinfutures", symbols=("AAA/USDT", "BBB/USDT")))
    qualified = next(item for item in report.items if item.actionable)
    monitored_item = SimpleNamespace(signal=qualified.signal, candidate=qualified.candidate,
                                     contract_plan=qualified.contract_plan, valid_until=qualified.valid_until,
                                     is_actionable=lambda: True)
    monitor_report = SimpleNamespace(items=(monitored_item,), run_id="test", provenance=report.provenance)
    class CaptureDispatcher:
        def __init__(self, *args, **kwargs):
            pass
        def dispatch(self, *args, **kwargs):
            assert kwargs["context"]["contract_plan"] is qualified.contract_plan
            return SimpleNamespace(failures=("webhook: temporary outage",))
    monkeypatch.setattr(machine_cli, "MarketScanner", lambda *args, **kwargs: SimpleNamespace(scan=lambda request: monitor_report))
    monkeypatch.setattr(machine_cli, "AlertDispatcher", CaptureDispatcher)
    monkeypatch.setattr(machine_cli, "_print_scan", lambda report: None)
    result = machine_cli.main(["--db", ":memory:", "monitor", "--symbols", "BBB/USDT", "--iterations", "1", "--alert-sinks", "console"])
    assert result == 0
    assert "alert delivery failed for BBB/USDT:USDT: webhook: temporary outage" in capsys.readouterr().err
