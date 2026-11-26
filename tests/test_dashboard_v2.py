from __future__ import annotations

from fastapi.testclient import TestClient

from signal_bot.api import create_app
from signal_bot.storage import SignalStore


def test_dashboard_and_assets_are_served() -> None:
    client = TestClient(create_app(store=SignalStore(":memory:")))
    page = client.get("/")
    assert page.status_code == 200
    assert "Signal Machine" in page.text
    assert "Backtest & Validation" in page.text
    assert client.get("/assets/styles.css").status_code == 200
    assert client.get("/assets/app.js").status_code == 200


def test_health_reports_signal_only_boundary() -> None:
    client = TestClient(create_app(store=SignalStore(":memory:")))
    payload = client.get("/health").json()
    assert payload["version"] == "2.1.1"
    assert payload["execution_enabled"] is False
