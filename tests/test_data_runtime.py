from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_candle_cache import ClockedCandles, make_service, request

from signal_bot.runtime import (
    RequestDeadline,
    RequestDeadlineExceeded,
    RuntimeMetrics,
    current_operation,
    operation_context,
)
from signal_bot.service import CCXTMarketDataClient


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class NetworkError(Exception):
    pass


class RateLimitError(NetworkError):
    pass


class Transport:
    def __init__(self, clock):
        self.clock = clock
        self.timeout = 10_000
        self.last_response_headers = {}
        self.calls = []
        self.wait = 0.0
        self.transport_duration = 0.0

    def throttle(self):
        self.clock.advance(self.wait)

    def fetch(self, url):
        self.calls.append((url, self.timeout))
        self.clock.advance(self.transport_duration)
        return {"ok": True}


def client_for(monkeypatch, exchange):
    monkeypatch.setitem(sys.modules, "ccxt", SimpleNamespace(
        kucoinfutures=lambda _: None, NetworkError=NetworkError,
        RateLimitExceeded=RateLimitError, DDoSProtection=RateLimitError,
    ))
    return CCXTMarketDataClient("kucoinfutures", "swap", exchange=exchange, sleep=exchange.clock.advance)


def test_metrics_are_thread_safe_and_measure_failed_stages():
    clock = Clock()
    metrics = RuntimeMetrics(clock=clock)
    with pytest.raises(ValueError), metrics.measure("stage"):
        clock.advance(0.25)
        raise ValueError("test")
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: metrics.increment("items"), range(1000)))
    assert metrics.to_dict() == {"counters": {"items": 1000}, "timings_seconds": {"stage": 0.25}}


def test_deadline_context_is_inherited_not_extended_and_resets_after_failure():
    clock = Clock()
    outer = RequestDeadline.from_timeout(1, clock=clock)
    inner = RequestDeadline.from_timeout(10, clock=clock)
    metrics = RuntimeMetrics()
    with pytest.raises(RequestDeadlineExceeded), operation_context(deadline=outer, metrics=metrics):
        with operation_context(deadline=inner):
            active, active_metrics = current_operation()
            assert active is outer
            assert active_metrics is metrics
            clock.advance(2)
            active.check()
    assert current_operation() == (None, None)


def test_expired_deadline_prevents_all_exchange_work(monkeypatch):
    clock = Clock()
    exchange = Transport(clock)
    client = client_for(monkeypatch, exchange)
    deadline = RequestDeadline.from_timeout(1, clock=clock)
    clock.advance(1)
    with pytest.raises(RequestDeadlineExceeded) as error:
        client.call_with_retries(lambda: exchange.fetch("public"), deadline=deadline)
    assert error.value.code == "scan_deadline_exceeded"
    assert exchange.calls == []
    assert exchange.timeout == 10_000


def test_transport_timeout_accounts_for_rate_limit_wait_and_counts_actual_requests(monkeypatch):
    clock = Clock()
    exchange = Transport(clock)
    exchange.wait = 0.75
    exchange.transport_duration = 0.25
    client = client_for(monkeypatch, exchange)
    metrics = RuntimeMetrics(clock=clock)
    deadline = RequestDeadline.from_timeout(2, clock=clock)

    def operation():
        exchange.throttle()
        return exchange.fetch("public")

    with client.operation_context(deadline=deadline, metrics=metrics):
        assert client.call_with_retries(operation) == {"ok": True}
    assert exchange.calls == [("public", 1250)]
    assert exchange.timeout == 10_000
    assert client.metrics_capabilities == {"http_requests": True, "rate_limit_wait": True}
    counters = metrics.to_dict()["counters"]
    timings = metrics.to_dict()["timings_seconds"]
    assert counters["exchange_operation_attempts"] == 1
    assert counters["http_requests"] == 1
    assert timings["rate_limit_wait"] == 0.75
    assert timings["http_transport"] == 0.25


def test_one_ccxt_operation_can_have_multiple_transport_requests(monkeypatch):
    clock = Clock()
    exchange = Transport(clock)
    client = client_for(monkeypatch, exchange)
    metrics = RuntimeMetrics()

    def operation():
        exchange.fetch("first")
        return exchange.fetch("second")

    client.call_with_retries(operation, metrics=metrics)
    assert metrics.to_dict()["counters"]["exchange_operation_attempts"] == 1
    assert metrics.to_dict()["counters"]["http_requests"] == 2


def test_rate_limiter_that_consumes_budget_prevents_transport(monkeypatch):
    clock = Clock()
    exchange = Transport(clock)
    exchange.wait = 2
    client = client_for(monkeypatch, exchange)
    deadline = RequestDeadline.from_timeout(1, clock=clock)

    def operation():
        exchange.throttle()
        return exchange.fetch("must-not-run")

    with pytest.raises(RequestDeadlineExceeded):
        client.call_with_retries(operation, deadline=deadline)
    assert exchange.calls == []
    assert exchange.timeout == 10_000


def test_late_success_cannot_escape_the_deadline(monkeypatch):
    clock = Clock()
    exchange = Transport(clock)
    client = client_for(monkeypatch, exchange)
    deadline = RequestDeadline.from_timeout(1, clock=clock)

    def operation():
        clock.advance(2)
        return "late"

    with pytest.raises(RequestDeadlineExceeded):
        client.call_with_retries(operation, deadline=deadline)
    assert exchange.timeout == 10_000


def test_retry_delay_cannot_exceed_remaining_budget(monkeypatch):
    clock = Clock()
    exchange = Transport(clock)
    exchange.last_response_headers = {"gw-ratelimit-reset": "2000"}
    client = client_for(monkeypatch, exchange)
    metrics = RuntimeMetrics()
    deadline = RequestDeadline.from_timeout(1, clock=clock)
    calls = []

    def operation():
        calls.append(1)
        raise RateLimitError("429")

    with pytest.raises(RequestDeadlineExceeded):
        client.call_with_retries(operation, deadline=deadline, metrics=metrics)
    assert calls == [1]
    assert clock.value == 0
    assert metrics.to_dict()["counters"].get("network_retries", 0) == 0


def test_wrapping_one_exchange_twice_does_not_double_count_transport(monkeypatch):
    clock = Clock()
    exchange = Transport(clock)
    client_for(monkeypatch, exchange)
    client = client_for(monkeypatch, exchange)
    metrics = RuntimeMetrics()
    client.call_with_retries(lambda: exchange.fetch("public"), metrics=metrics)
    assert metrics.to_dict()["counters"]["http_requests"] == 1


def test_fake_adapters_without_transport_hooks_have_explicit_unavailable_metrics(monkeypatch):
    clock = Clock()
    exchange = SimpleNamespace(clock=clock, timeout=10_000, last_response_headers={})
    client = client_for(monkeypatch, exchange)
    assert client.metrics_capabilities == {"http_requests": False, "rate_limit_wait": False}
    metrics = RuntimeMetrics()
    assert client.call_with_retries(lambda: 42, metrics=metrics) == 42
    assert "http_requests" not in metrics.to_dict()["counters"]


def test_service_checks_budget_between_pages_even_for_injected_clients():
    clock = Clock()
    client = ClockedCandles(cap=200)
    original_fetch = client.fetch_ohlcv

    def slow_fetch(*args, **kwargs):
        rows = original_fetch(*args, **kwargs)
        clock.advance(0.6)
        return rows

    client.fetch_ohlcv = slow_fetch
    service = make_service(client)
    deadline = RequestDeadline.from_timeout(1, clock=clock)
    metrics = RuntimeMetrics()
    with pytest.raises(RequestDeadlineExceeded):
        service.fetch_frames_result(request(context_timeframes=()), deadline=deadline, metrics=metrics)
    assert len(client.calls) == 2
    assert service.last_frame_metadata == {}


def test_per_result_metrics_exclude_previous_symbols():
    client = ClockedCandles()
    service = make_service(client)
    metrics = RuntimeMetrics()
    one = service.fetch_frames_result(request(context_timeframes=()), metrics=metrics)
    two = service.fetch_frames_result(request(context_timeframes=()), metrics=metrics)
    assert one.metrics["counters"]["ohlcv_pages"] == 3
    assert two.metrics["counters"]["ohlcv_pages"] == 3
    assert metrics.to_dict()["counters"]["ohlcv_pages"] == 6
    assert one.metadata == two.metadata
    assert all(isinstance(value, (int, float)) for value in one.metrics["timings_seconds"].values())


def test_past_deadline_prevents_even_cached_analysis():
    from signal_bot.candle_cache import CandleCache
    from signal_bot.storage import SignalStore

    client = ClockedCandles()
    service = make_service(client, CandleCache(SignalStore(":memory:")))
    previous = service.analyze_live_result(request(context_timeframes=()))
    clock = Clock()
    deadline = RequestDeadline.from_timeout(1, clock=clock)
    clock.advance(2)
    with pytest.raises(RequestDeadlineExceeded):
        service.analyze_live_result(request(context_timeframes=()), deadline=deadline)
    assert service.last_frame_metadata == {}
    assert previous.metadata  # The explicit successful result remains available.


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), True])
def test_invalid_deadline_parameters(seconds):
    with pytest.raises(ValueError):
        RequestDeadline.from_timeout(seconds)
