from __future__ import annotations

import hashlib
import json
import math
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.request import Request, urlopen

from .models import MarketSignal, SignalAction, to_dict
from .storage import SignalStore


class AlertSink(Protocol):
    name: str

    def send(self, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class AlertPolicy:
    actions: tuple[SignalAction, ...] = (
        SignalAction.LONG,
        SignalAction.SHORT,
    )
    minimum_confidence: float = 0.0
    minimum_consensus: float = 0.0
    cooldown_minutes: int = 60

    def validate(self) -> None:
        if not self.actions:
            raise ValueError("Alert policy must include at least one action.")
        if not 0.0 <= self.minimum_confidence <= 100.0:
            raise ValueError("minimum_confidence must be between 0 and 100.")
        if not 0.0 <= self.minimum_consensus <= 1.0:
            raise ValueError("minimum_consensus must be between 0 and 1.")
        if (
            isinstance(self.cooldown_minutes, bool)
            or not isinstance(self.cooldown_minutes, int)
            or self.cooldown_minutes < 0
        ):
            raise ValueError("cooldown_minutes must be a nonnegative integer.")


class ConsoleAlertSink:
    name = "console"

    def send(self, payload: dict[str, Any]) -> None:
        signal = payload["signal"]
        plan = signal.get("entry_plan")
        context = payload.get("context", {})
        contract_plan = context.get("contract_plan")
        line = (
            f"ALERT {signal['symbol']} {signal['action'].upper()} | "
            f"{signal['regime'].upper()} | rule alignment "
            f"{signal['confidence']:.1f}/100"
        )
        if contract_plan:
            line += (
                f" | ref {contract_plan['reference_entry']}"
                f" | entry {contract_plan['entry_low']}-{contract_plan['entry_high']}"
                f" | stop {contract_plan['stop_loss']}"
                f" | TP1/2/3 {contract_plan['take_profit_1']}/"
                f"{contract_plan['take_profit_2']}/{contract_plan['take_profit_3']}"
                f" | contracts {contract_plan['contracts']}"
                f" | planned risk {contract_plan['actual_risk_amount']} USDT"
            )
        elif plan:
            line += (
                f" | entry {plan['entry_low']:.8g}-{plan['entry_high']:.8g}"
                f" | stop {plan['stop_loss']:.8g}"
                f" | TP2 {plan['take_profit_2']:.8g}"
            )
        if context.get("valid_until"):
            line += f" | valid until {context['valid_until']}"
        print(line, file=sys.stdout, flush=True)


class JsonlAlertSink:
    name = "jsonl"

    def __init__(self, path: str | Path = "data/alerts/signals.jsonl") -> None:
        self.path = Path(path)
        self.delivery_key = "jsonl:" + hashlib.sha256(
            str(self.path.resolve()).encode("utf-8")
        ).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


class WebhookAlertSink:
    name = "webhook"

    def __init__(self, url: str, *, timeout_seconds: float = 10.0) -> None:
        if not url.startswith(("https://", "http://")):
            raise ValueError("Webhook URL must use HTTP or HTTPS.")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite.")
        self.url = url
        self.delivery_key = "webhook:" + hashlib.sha256(url.encode("utf-8")).hexdigest()
        self.timeout_seconds = timeout_seconds

    def send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "market-signal-bot/2",
        }
        if payload.get("event_id"):
            headers["Idempotency-Key"] = str(payload["event_id"])
        request = Request(
            self.url,
            data=body,
            method="POST",
            headers=headers,
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    f"Webhook returned HTTP {response.status}."
                )


@dataclass(frozen=True)
class AlertDispatchResult:
    eligible: bool
    sent_to: tuple[str, ...]
    suppressed_reason: str | None
    failures: tuple[str, ...]
    already_delivered_to: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "sent_to": list(self.sent_to),
            "suppressed_reason": self.suppressed_reason,
            "failures": list(self.failures),
            "already_delivered_to": list(self.already_delivered_to),
        }


class AlertDispatcher:
    """Best-effort notifications with durable successful-delivery deduplication.

    Run one dispatcher per monitor. Calls on one instance are serialized, but
    separate processes do not share this lock. A crash after remote acceptance
    and before the receipt commit can repeat delivery; receivers should honor
    the event_id / Idempotency-Key when they support idempotency.
    """

    def __init__(
        self,
        store: SignalStore,
        *,
        policy: AlertPolicy | None = None,
        sinks: tuple[AlertSink, ...] = (),
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or AlertPolicy()
        self.policy.validate()
        self.sinks = sinks
        self._now_factory = now_factory or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        identities = [self._sink_id(sink) for sink in sinks]
        if len(identities) != len(set(identities)):
            raise ValueError("Each alert sink must have a unique name or delivery_key.")

    @staticmethod
    def _sink_id(sink: AlertSink) -> str:
        identity = str(getattr(sink, "delivery_key", sink.name)).strip()
        if not identity:
            raise ValueError("An alert sink needs a nonempty identity.")
        return identity

    def dispatch(
        self,
        signal: MarketSignal,
        *,
        exchange_id: str,
        market_type: str,
        context: Mapping[str, Any] | None = None,
    ) -> AlertDispatchResult:
        with self._lock:
            return self._dispatch(
                signal, exchange_id=exchange_id, market_type=market_type, context=context
            )

    def _dispatch(
        self,
        signal: MarketSignal,
        *,
        exchange_id: str,
        market_type: str,
        context: Mapping[str, Any] | None,
    ) -> AlertDispatchResult:
        if signal.action not in self.policy.actions:
            return AlertDispatchResult(False, (), f"Action {signal.action.value} is not alertable.", ())
        if not math.isfinite(signal.confidence) or not 0 <= signal.confidence <= 100:
            return AlertDispatchResult(False, (), "Signal confidence is invalid.", ())
        if not math.isfinite(signal.consensus) or not 0 <= signal.consensus <= 1:
            return AlertDispatchResult(False, (), "Timeframe consensus is invalid.", ())
        if signal.confidence < self.policy.minimum_confidence:
            return AlertDispatchResult(False, (), "Signal confidence is below the alert threshold.", ())
        if signal.consensus < self.policy.minimum_consensus:
            return AlertDispatchResult(False, (), "Timeframe consensus is below the alert threshold.", ())
        if not self.sinks:
            return AlertDispatchResult(False, (), "No alert sinks are configured.", ())

        exchange_id = exchange_id.strip().lower()
        market_type = market_type.strip().lower()
        if not exchange_id or not market_type or not signal.fingerprint:
            raise ValueError("Alert venue and signal fingerprint cannot be empty.")
        key = "|".join((
            exchange_id, market_type, signal.symbol, signal.execution_timeframe,
            signal.action.value, signal.setup_type.value,
        ))
        event_key = hashlib.sha256(
            "|".join((exchange_id, market_type, signal.fingerprint)).encode("utf-8")
        ).hexdigest()
        receipts = self.store.alert_deliveries(event_key)
        delivered = receipts["sink_ids"]
        already_sent = tuple(sink.name for sink in self.sinks if self._sink_id(sink) in delivered)
        remaining = [sink for sink in self.sinks if self._sink_id(sink) not in delivered]
        if not remaining:
            return AlertDispatchResult(
                False, (), "Signal already delivered to all configured sinks.", (), already_sent
            )
        now = self._now_factory()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if not delivered and not self.store.alert_due(
            key, cooldown=timedelta(minutes=self.policy.cooldown_minutes), now=now
        ):
            return AlertDispatchResult(False, (), "Alert cooldown is active.", ())

        # Use the original event snapshot for retries: a later scan may carry a
        # different sizing assumption or run_id despite an identical signal.
        payload = receipts["payload"] or {
            "event": "market_signal",
            "event_id": event_key,
            "exchange_id": exchange_id,
            "market_type": market_type,
            "signal": to_dict(signal),
            **({"context": dict(context)} if context is not None else {}),
        }
        expiry = payload.get("context", {}).get("valid_until")
        if expiry is not None:
            try:
                valid_until = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            except (ValueError, TypeError) as exc:
                raise ValueError("Alert valid_until must be an ISO-8601 timestamp.") from exc
            if valid_until.tzinfo is None:
                raise ValueError("Alert valid_until must include a timezone.")
            if now >= valid_until:
                return AlertDispatchResult(False, (), "Signal validity window has expired.", (), already_sent)
        # Validate once before any sink sees the event. Invalid/non-finite
        # context must not generate a partial notification followed by failure.
        json.dumps(payload, allow_nan=False)
        sent: list[str] = []
        failures: list[str] = []
        for sink in remaining:
            try:
                sink.send(payload)
                sent.append(sink.name)
                self.store.mark_alert_delivery(
                    event_key, self._sink_id(sink), key, payload,
                    sent_at=self._now_factory(),
                )
            except Exception as exc:  # isolate independent notification sinks.
                failures.append(f"{sink.name}: {exc}")
        return AlertDispatchResult(
            eligible=True, sent_to=tuple(sent), suppressed_reason=None,
            failures=tuple(failures), already_delivered_to=already_sent,
        )
