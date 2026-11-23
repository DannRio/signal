from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from datetime import timedelta
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
        if self.cooldown_minutes < 0:
            raise ValueError("cooldown_minutes cannot be negative.")


class ConsoleAlertSink:
    name = "console"

    def send(self, payload: dict[str, Any]) -> None:
        signal = payload["signal"]
        plan = signal.get("entry_plan")
        line = (
            f"ALERT {signal['symbol']} {signal['action'].upper()} | "
            f"{signal['regime'].upper()} | confidence "
            f"{signal['confidence']:.1f}%"
        )
        if plan:
            line += (
                f" | entry {plan['entry_low']:.8g}-{plan['entry_high']:.8g}"
                f" | stop {plan['stop_loss']:.8g}"
                f" | TP2 {plan['take_profit_2']:.8g}"
            )
        print(line, file=sys.stdout, flush=True)


class JsonlAlertSink:
    name = "jsonl"

    def __init__(self, path: str | Path = "data/alerts/signals.jsonl") -> None:
        self.path = Path(path)
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
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self.url = url
        self.timeout_seconds = timeout_seconds

    def send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "market-signal-bot/2",
            },
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "sent_to": list(self.sent_to),
            "suppressed_reason": self.suppressed_reason,
            "failures": list(self.failures),
        }


class AlertDispatcher:
    def __init__(
        self,
        store: SignalStore,
        *,
        policy: AlertPolicy | None = None,
        sinks: tuple[AlertSink, ...] = (),
    ) -> None:
        self.store = store
        self.policy = policy or AlertPolicy()
        self.policy.validate()
        self.sinks = sinks

    def dispatch(
        self,
        signal: MarketSignal,
        *,
        exchange_id: str,
        market_type: str,
    ) -> AlertDispatchResult:
        if signal.action not in self.policy.actions:
            return AlertDispatchResult(
                False,
                (),
                f"Action {signal.action.value} is not alertable.",
                (),
            )
        if signal.confidence < self.policy.minimum_confidence:
            return AlertDispatchResult(
                False,
                (),
                "Signal confidence is below the alert threshold.",
                (),
            )
        if signal.consensus < self.policy.minimum_consensus:
            return AlertDispatchResult(
                False,
                (),
                "Timeframe consensus is below the alert threshold.",
                (),
            )
        key = "|".join(
            (
                exchange_id,
                market_type,
                signal.symbol,
                signal.execution_timeframe,
                signal.action.value,
                signal.setup_type.value,
            )
        )
        cooldown = timedelta(minutes=self.policy.cooldown_minutes)
        if not self.store.alert_due(key, cooldown=cooldown):
            return AlertDispatchResult(
                False,
                (),
                "Alert cooldown is active.",
                (),
            )

        payload = {
            "event": "market_signal",
            "exchange_id": exchange_id,
            "market_type": market_type,
            "signal": to_dict(signal),
        }
        sent: list[str] = []
        failures: list[str] = []
        for sink in self.sinks:
            try:
                sink.send(payload)
                sent.append(sink.name)
            except Exception as exc:  # isolate independent notification sinks.
                failures.append(f"{sink.name}: {exc}")
        if sent:
            self.store.mark_alert(key, payload)
        return AlertDispatchResult(
            eligible=True,
            sent_to=tuple(sent),
            suppressed_reason=None,
            failures=tuple(failures),
        )
