from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_alerts_v2 import CaptureSink, confirmed_signal

from signal_bot.alerts import (
    AlertDispatcher,
    AlertPolicy,
    ConsoleAlertSink,
    JsonlAlertSink,
    WebhookAlertSink,
)
from signal_bot.storage import SignalStore


class FlakySink(CaptureSink):
    def __init__(self, name="flaky", failures=1):
        super().__init__()
        self.name = name
        self.remaining_failures = failures
        self.calls = 0

    def send(self, payload):
        self.calls += 1
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise RuntimeError("Temporary delivery failure")
        super().send(payload)


def dispatch(dispatcher, signal=None, **kwargs):
    return dispatcher.dispatch(
        signal or confirmed_signal(), exchange_id="kucoinfutures",
        market_type="swap", **kwargs,
    )


def test_event_is_deduplicated_after_cooldown_and_restart(tmp_path):
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    sink = CaptureSink()
    path = tmp_path / "alerts.db"
    store = SignalStore(path)
    first = AlertDispatcher(store, sinks=(sink,), now_factory=lambda: clock[0])
    assert dispatch(first).sent_to == ("capture",)
    store.close()
    clock[0] += timedelta(days=1)
    restarted = AlertDispatcher(SignalStore(path), sinks=(sink,), now_factory=lambda: clock[0])
    result = dispatch(restarted)
    assert result.sent_to == ()
    assert result.already_delivered_to == ("capture",)
    assert len(sink.payloads) == 1


def test_partial_success_retries_only_failed_sink_and_keeps_original_snapshot(tmp_path):
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    successful = CaptureSink()
    flaky = FlakySink()
    path = tmp_path / "alerts.db"
    store = SignalStore(path)
    first = AlertDispatcher(store, sinks=(successful, flaky), now_factory=lambda: clock[0])
    result = dispatch(first, context={"run_id": "original", "contract_plan": {"contracts": 3}})
    assert result.sent_to == ("capture",)
    assert len(result.failures) == 1
    store.close()
    clock[0] += timedelta(minutes=1)
    restarted = AlertDispatcher(SignalStore(path), sinks=(successful, flaky), now_factory=lambda: clock[0])
    retried = dispatch(restarted, context={"run_id": "retry", "contract_plan": {"contracts": 4}})
    assert retried.sent_to == ("flaky",)
    assert retried.already_delivered_to == ("capture",)
    assert not retried.failures
    assert len(successful.payloads) == 1
    assert flaky.payloads[0]["context"] == {"run_id": "original", "contract_plan": {"contracts": 3}}
    assert flaky.payloads[0]["event_id"] == successful.payloads[0]["event_id"]
    assert dispatch(restarted).sent_to == ()


def test_all_failed_sinks_can_retry_without_starting_cooldown():
    sink = FlakySink()
    dispatcher = AlertDispatcher(SignalStore(":memory:"), sinks=(sink,))
    first = dispatch(dispatcher)
    assert first.sent_to == ()
    assert first.failures
    assert dispatch(dispatcher).sent_to == ("flaky",)
    assert sink.calls == 2


def test_new_event_respects_cooldown_and_retry_does_not_extend_it():
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    successful = CaptureSink()
    flaky = FlakySink()
    dispatcher = AlertDispatcher(
        SignalStore(":memory:"), sinks=(successful, flaky), now_factory=lambda: clock[0]
    )
    dispatch(dispatcher)
    new_event = replace(confirmed_signal(), fingerprint="next-candle")
    clock[0] += timedelta(minutes=30)
    suppressed = dispatch(dispatcher, new_event)
    assert suppressed.suppressed_reason == "Alert cooldown is active."
    assert dispatch(dispatcher).sent_to == ("flaky",)
    clock[0] += timedelta(minutes=31)
    assert dispatch(dispatcher, new_event).sent_to == ("capture", "flaky")
    assert len(successful.payloads) == 2


def test_dispatch_is_serialized_for_one_monitor_instance():
    sink = CaptureSink()
    dispatcher = AlertDispatcher(
        SignalStore(":memory:"), policy=AlertPolicy(cooldown_minutes=0), sinks=(sink,)
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: dispatch(dispatcher), range(12)))
    assert sum(bool(result.sent_to) for result in results) == 1
    assert len(sink.payloads) == 1


def test_event_identity_includes_venue():
    sink = CaptureSink()
    dispatcher = AlertDispatcher(SignalStore(":memory:"), sinks=(sink,))
    dispatch(dispatcher)
    other = dispatcher.dispatch(confirmed_signal(), exchange_id="another", market_type="swap")
    assert other.sent_to == ("capture",)
    assert sink.payloads[0]["event_id"] != sink.payloads[1]["event_id"]


def test_same_named_sinks_need_distinct_persistent_identities(tmp_path):
    with pytest.raises(ValueError, match="unique"):
        AlertDispatcher(SignalStore(":memory:"), sinks=(CaptureSink(), CaptureSink()))
    first = JsonlAlertSink(tmp_path / "a.jsonl")
    second = JsonlAlertSink(tmp_path / "b.jsonl")
    assert first.delivery_key != second.delivery_key
    assert first.delivery_key == JsonlAlertSink(first.path).delivery_key
    assert WebhookAlertSink("https://example.test/a").delivery_key != WebhookAlertSink("https://example.test/b").delivery_key


def test_expired_event_is_not_delivered_or_retried():
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    first = CaptureSink()
    flaky = FlakySink()
    dispatcher = AlertDispatcher(SignalStore(":memory:"), sinks=(first, flaky), now_factory=lambda: clock[0])
    expiry = (clock[0] + timedelta(minutes=1)).isoformat()
    dispatch(dispatcher, context={"valid_until": expiry})
    clock[0] += timedelta(minutes=2)
    result = dispatch(dispatcher, context={"valid_until": (clock[0] + timedelta(hours=1)).isoformat()})
    assert result.suppressed_reason == "Signal validity window has expired."
    assert flaky.calls == 1


@pytest.mark.parametrize("context", [
    {"valid_until": "invalid"}, {"valid_until": "2026-01-01T00:00:00"},
    {"contract_plan": {"contracts": float("nan")}},
])
def test_invalid_context_is_rejected_before_any_sink_receives_it(context):
    sink = CaptureSink()
    dispatcher = AlertDispatcher(SignalStore(":memory:"), sinks=(sink,))
    with pytest.raises(ValueError):
        dispatch(dispatcher, context=context)
    assert not sink.payloads


def test_additive_storage_migration_keeps_existing_cooldown(tmp_path):
    path = tmp_path / "legacy.db"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alert_ledger (alert_key TEXT PRIMARY KEY, last_sent_at TEXT NOT NULL, payload_json TEXT NOT NULL)")
        connection.execute("INSERT INTO alert_ledger VALUES (?, ?, ?)", ("existing", now.isoformat(), "{}"))
    store = SignalStore(path)
    assert not store.alert_due("existing", cooldown=timedelta(hours=1), now=now)
    assert store.alert_deliveries("new") == {"sink_ids": set(), "payload": None}


def test_webhook_carries_idempotency_header_without_network(monkeypatch):
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(request, timeout):
        calls.append(request)
        return Response()

    monkeypatch.setattr("signal_bot.alerts.urlopen", fake_urlopen)
    WebhookAlertSink("https://example.test/alerts").send({"event_id": "event123", "signal": {}})
    assert calls[0].get_header("Idempotency-key") == "event123"


def test_console_uses_rounded_scanner_plan_and_rule_alignment(capsys):
    payload = {
        "signal": {
            "symbol": "BTC/USDT:USDT", "action": "long", "regime": "bullish",
            "confidence": 90.0,
            "entry_plan": {"entry_low": 999, "entry_high": 1000, "stop_loss": 998, "take_profit_2": 1002},
        },
        "context": {"contract_plan": {
            "reference_entry": 100.1, "entry_low": 100.0, "entry_high": 100.2,
            "stop_loss": 99.0, "take_profit_1": 101.2,
            "take_profit_2": 102.3, "take_profit_3": 103.4,
            "contracts": 3, "actual_risk_amount": 4.25,
        }, "valid_until": "2026-01-01T00:15:00+00:00"},
    }
    ConsoleAlertSink().send(payload)
    text = capsys.readouterr().out
    assert "rule alignment 90.0/100" in text
    assert "confidence" not in text
    assert "ref 100.1" in text
    assert "entry 100.0-100.2" in text
    assert "stop 99.0" in text
    assert "TP1/2/3 101.2/102.3/103.4" in text
    assert "contracts 3" in text
    assert "planned risk 4.25 USDT" in text
    assert "999" not in text
