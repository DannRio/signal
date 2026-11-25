from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_scanner import (
    AAA,
    BBB,
    DECISION_CLOSE,
    NOW,
    FakeService,
    FakeUniverse,
    entry_plan,
    make_candidate,
    make_signal,
)

from signal_bot import scanner as scanner_module
from signal_bot.models import SignalAction
from signal_bot.scanner import (
    CCXTUniverseProvider,
    MarketRejection,
    MarketScanner,
    ScanInProgressError,
    ScanRequest,
    UniverseRequest,
    UniverseSnapshot,
    candidate_rejection,
)
from signal_bot.service import MarketDataUnavailableError
from signal_bot.storage import SignalStore
from signal_bot.validation import DataValidationError


def market(symbol=AAA, **changes):
    result = {
        "symbol": symbol, "base": symbol.split("/")[0], "quote": "USDT", "settle": "USDT",
        "active": True, "swap": True, "future": False, "expiry": None,
        "linear": True, "inverse": False, "contractSize": 0.001,
        "info": {
            "status": "Open", "marketStage": "NORMAL", "turnoverOf24h": "2000000",
            "multiplier": "0.001", "lotSize": "1", "tickSize": "0.01",
            "fundingFeeRate": "0.0001", "fundingRateGranularity": 28_800_000,
            "nextFundingRateDateTime": int((NOW + timedelta(hours=7)).timestamp() * 1000),
        },
        "limits": {"amount": {"min": 1, "max": 100_000}},
    }
    info = changes.pop("info", None)
    result.update(changes)
    if info is not None:
        result["info"].update(info)
    return result


def ticker(**changes):
    result = {"bid": 99.99, "ask": 100.01, "last": 100.0, "timestamp": int(NOW.timestamp() * 1000), "info": {}}
    result.update(changes)
    return result


class FakePublicClient:
    def __init__(self, markets, tickers, *, bulk_bbo=True):
        self.markets = markets
        self.tickers = tickers
        self.loads = []
        self.calls = []
        self.exchange = SimpleNamespace(
            has={"fetchBidsAsks": bulk_bbo, "fetchTickers": True},
            fetch_bids_asks=self.fetch_bids_asks, fetch_tickers=self.fetch_tickers,
        )

    def load_markets(self, reload=False):
        self.loads.append(reload)
        return deepcopy(self.markets)

    def fetch_bids_asks(self):
        self.calls.append(("bbo", {}))
        return deepcopy(self.tickers)

    def fetch_tickers(self, **kwargs):
        self.calls.append(("tickers", kwargs))
        return deepcopy(self.tickers)

    def call_with_retries(self, operation, *args, **kwargs):
        return operation(*args, **kwargs)


def provider(markets=None, tickers=None, **kwargs):
    client = FakePublicClient(markets or {AAA: market()}, tickers or {AAA: ticker()}, **kwargs)
    return CCXTUniverseProvider(client=client, now_factory=lambda: NOW), client


def scan_request(**changes):
    values = {"exchange_id": "kucoinfutures", "symbols": (AAA, BBB)}
    values.update(changes)
    return ScanRequest(**values)


def scanner_for(candidates=None, *, service=None, universe=None, store=None, now=NOW, monotonic=None):
    chosen_service = service or FakeService()
    chosen_universe = universe if universe is not None else FakeUniverse(candidates)
    return MarketScanner(
        universe_provider=chosen_universe, service_factory=lambda _: chosen_service,
        store=store, now_factory=lambda: now, monotonic_factory=monotonic,
    )


def test_explicit_symbols_use_the_same_filters_as_auto_universe():
    universe, client = provider(
        {AAA: market(info={"turnoverOf24h": 10}), BBB: market(BBB)},
        {AAA: ticker(), BBB: ticker()},
    )
    auto = universe.discover_report(UniverseRequest("kucoinfutures"))
    explicit = universe.discover_report(UniverseRequest("kucoinfutures", symbols=(AAA, BBB)))
    assert [item.symbol for item in auto.candidates] == [BBB]
    assert explicit.candidates == auto.candidates
    assert explicit.rejections == auto.rejections
    assert explicit.rejections[0].code == "low_volume"
    assert client.loads == [True, True]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"bid": None}, "invalid_quote"),
        ({"ask": None}, "invalid_quote"),
        ({"bid": float("nan")}, "invalid_quote"),
        ({"bid": True}, "invalid_quote"),
        ({"ask": 99.0}, "invalid_quote"),
        ({"bid": 99.0, "ask": 101.0}, "wide_spread"),
        ({"timestamp": None}, "missing_quote_timestamp"),
        ({"timestamp": int((NOW - timedelta(seconds=31)).timestamp() * 1000)}, "stale_quote"),
        ({"timestamp": int((NOW + timedelta(seconds=6)).timestamp() * 1000)}, "future_quote"),
    ],
)
def test_bad_quotes_never_enter_the_universe(explicit, change, code):
    universe, _ = provider(tickers={AAA: ticker(**change)})
    report = universe.discover_report(UniverseRequest("kucoinfutures", symbols=(AAA,) if explicit else ()))
    assert report.candidates == ()
    assert report.rejections[0].code == code


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"active": False}, "inactive_contract"),
        ({"active": None}, "inactive_contract"),
        ({"info": {"status": "Closed"}}, "inactive_contract"),
        ({"swap": False}, "not_perpetual"),
        ({"future": True}, "not_perpetual"),
        ({"expiry": 1}, "not_perpetual"),
        ({"linear": False}, "not_linear"),
        ({"inverse": True}, "not_linear"),
        ({"settle": "BTC"}, "wrong_settlement"),
        ({"quote": "USD"}, "wrong_settlement"),
        ({"info": {"marketStage": "PRE_MARKET"}}, "nonstandard_market_stage"),
    ],
)
def test_contract_eligibility_is_checked_for_explicit_symbols(change, code):
    universe, _ = provider(markets={AAA: market(**change)})
    report = universe.discover_report(UniverseRequest("kucoinfutures", symbols=(AAA,)))
    assert report.candidates == ()
    assert report.rejections[0].code == code


def test_jup_is_not_misclassified_as_a_leveraged_token_by_its_name():
    symbol = "JUP/USDT:USDT"
    universe, _ = provider({symbol: market(symbol)}, {symbol: ticker()})
    assert universe.discover_report(UniverseRequest("kucoinfutures")).candidates[0].symbol == symbol


def test_quote_turnover_comes_from_contract_metadata_and_never_from_base_volume():
    universe, _ = provider(tickers={AAA: ticker(quoteVolume=None, baseVolume=1_000_000)})
    candidate = universe.discover_report(UniverseRequest("kucoinfutures")).candidates[0]
    assert candidate.quote_volume == 2_000_000
    assert candidate.volume_source == "contracts/active.turnoverOf24h"
    assert candidate.metadata_observed_at == NOW.isoformat()
    assert candidate.funding_rate == 0.0001
    assert candidate.funding_interval_hours == 8

    missing = market()
    missing["info"].pop("turnoverOf24h")
    universe, _ = provider({AAA: missing}, {AAA: ticker(quoteVolume=3_000_000)})
    candidate = universe.discover_report(UniverseRequest("kucoinfutures")).candidates[0]
    assert candidate.quote_volume == 3_000_000
    assert candidate.volume_source == "ticker.quoteVolume"

    universe, _ = provider({AAA: missing}, {AAA: ticker(quoteVolume=None, baseVolume=1_000_000)})
    report = universe.discover_report(UniverseRequest("kucoinfutures"))
    assert report.candidates == ()
    assert report.rejections[0].code == "missing_volume"


def test_nanosecond_quote_timestamp_and_bulk_endpoint_fallback():
    universe, client = provider(
        tickers={AAA: ticker(timestamp=None, info={"ts": str(int(NOW.timestamp()) * 1_000_000_000)})},
        bulk_bbo=False,
    )
    candidate = universe.discover_report(UniverseRequest("kucoinfutures")).candidates[0]
    assert candidate.quote_timestamp == NOW.isoformat()
    assert client.calls == [("tickers", {"params": {"method": "futuresPublicGetAllTickers"}})]


def test_invalid_metadata_is_a_rejection_and_does_not_hide_valid_markets():
    invalid = market()
    invalid["info"] = ["not an object"]
    universe, _ = provider({AAA: invalid, BBB: market(BBB)}, {AAA: ticker(), BBB: ticker()})
    report = universe.discover_report(UniverseRequest("kucoinfutures"))
    assert [item.symbol for item in report.candidates] == [BBB]
    assert report.rejections[0].symbol == AAA


def test_unknown_explicit_symbol_is_reported_as_a_rejection():
    universe, _ = provider()
    report = universe.discover_report(UniverseRequest("kucoinfutures", symbols=(AAA, BBB)))
    assert report.candidates[0].symbol == AAA
    assert report.rejections[0].symbol == BBB
    assert report.rejections[0].code == "unknown_symbol"


def test_universe_limit_is_distinct_from_quality_rejection_and_ties_are_stable():
    universe, _ = provider({BBB: market(BBB), AAA: market(AAA)}, {BBB: ticker(), AAA: ticker()})
    report = universe.discover_report(UniverseRequest("kucoinfutures", max_markets=1))
    assert report.candidates[0].symbol == AAA
    assert report.excluded_by_limit == 1
    assert report.considered_markets == 2
    assert report.rejections == ()


@pytest.mark.parametrize("timestamp", ["not-a-date", "2026-01-01T00:01:00"])
def test_invalid_or_naive_quote_timestamps_fail_closed(timestamp):
    rejection = candidate_rejection(make_candidate(quote_timestamp=timestamp), UniverseRequest("kucoinfutures"), now=NOW)
    assert rejection.code == "invalid_quote_timestamp"


def test_scan_normalizes_symbols_and_passes_one_frozen_cutoff_to_all_symbols():
    service = FakeService()
    universe = FakeUniverse()
    scanner = scanner_for(service=service, universe=universe)
    report = scanner.scan(scan_request(symbols=("aaa/usdt", "bbb/usdt", "AAA/USDT:USDT")))
    assert universe.requests[0].symbols == (AAA, BBB)
    assert [kwargs["as_of"] for _, kwargs in service.requests] == [NOW, NOW]
    assert all(request.maximum_staleness_bars == 1 for request, _ in service.requests)
    assert report.started_at == NOW.isoformat()
    assert report.items[0].data_quality["15m"]["sha256"] == "a" * 64


def test_per_market_failure_remains_distinct_from_no_trade():
    service = FakeService(errors={AAA: DataValidationError("Missing 15m candle.", code="candle_gap")})
    report = scanner_for(service=service).scan(scan_request())
    assert report.status == "degraded"
    assert (report.requested_markets, report.analyzed_markets, report.failed_markets) == (2, 1, 1)
    assert report.items[0].candidate.symbol == BBB
    failed = report.items[-1]
    assert failed.signal is None
    assert failed.status == "failed"
    assert failed.error_code == "candle_gap"
    assert failed.data_quality == {}


def test_zero_eligible_markets_is_an_audited_empty_scan():
    class EmptyUniverse:
        def discover_report(self, request):
            return UniverseSnapshot((), (MarketRejection(AAA, "low_volume", "Too little turnover."),), considered_markets=1)

    store = SignalStore(":memory:")
    report = scanner_for(universe=EmptyUniverse(), store=store).scan(scan_request())
    assert report.status == "empty"
    assert report.rejected_markets == 1
    assert report.failed_markets == 0
    assert report.items == ()
    assert report.run_id
    assert store.recent_scans()[0]["result"]["status"] == "empty"


def test_whole_universe_outage_is_failed_and_persisted():
    class BrokenUniverse:
        def discover(self, request):
            raise MarketDataUnavailableError("Exchange unavailable.", attempts=3)

    store = SignalStore(":memory:")
    report = scanner_for(universe=BrokenUniverse(), store=store).scan(scan_request())
    assert report.status == "failed"
    assert report.errors[0]["code"] == "market_data_unavailable"
    assert report.analyzed_markets == 0
    assert report.run_id
    assert store.recent_scans()[0]["result"]["status"] == "failed"


@pytest.mark.parametrize(
    "changes",
    [
        {"exchange_id": "binance"}, {"equity": float("nan")}, {"minimum_quote_volume": float("inf")},
        {"maximum_spread_bps": float("nan")}, {"maximum_quote_age_seconds": 0},
        {"scan_timeout_seconds": float("inf")}, {"maximum_staleness_bars": 0},
        {"max_markets": True}, {"limit": 1}, {"risk_percent": 2.01},
        {"maximum_notional_multiple": 5.01}, {"context_timeframes": ("1m",)},
        {"auto_universe": True},
    ],
)
def test_invalid_scan_requests_fail_before_market_access(changes):
    universe = FakeUniverse()
    service = FakeService()
    scanner = scanner_for(universe=universe, service=service)
    with pytest.raises(ValueError):
        scanner.scan(scan_request(**changes))
    assert universe.requests == []
    assert service.requests == []


def test_actionable_expiry_respects_both_quote_ttl_and_decision_close():
    service = FakeService(signals={AAA: make_signal(AAA)})
    report = scanner_for([make_candidate()], service=service).scan(scan_request(symbols=(AAA,)))
    item = report.items[0]
    assert item.actionable
    assert item.valid_until == (NOW + timedelta(seconds=30)).isoformat()
    assert item.is_actionable(NOW + timedelta(seconds=29))
    assert not item.is_actionable(NOW + timedelta(seconds=30))

    late_now = DECISION_CLOSE + timedelta(minutes=14, seconds=50)
    candidate = make_candidate(quote_timestamp=late_now.isoformat())
    report = scanner_for([candidate], service=service, now=late_now).scan(scan_request(symbols=(AAA,)))
    assert report.items[0].valid_until == (DECISION_CLOSE + timedelta(minutes=15)).isoformat()

    expired_now = DECISION_CLOSE + timedelta(minutes=15)
    candidate = make_candidate(quote_timestamp=expired_now.isoformat())
    report = scanner_for([candidate], service=service, now=expired_now).scan(scan_request(symbols=(AAA,)))
    assert not report.items[0].actionable
    assert report.items[0].status == "blocked"
    assert report.items[0].error_code == "signal_expired"


def test_entry_outside_zone_is_blocked_and_not_persisted_as_an_executable_signal():
    service = FakeService(signals={AAA: make_signal(AAA)})
    store = SignalStore(":memory:")
    candidate = make_candidate(bid=101.0, ask=101.01)
    report = scanner_for([candidate], service=service, store=store).scan(scan_request(symbols=(AAA,)))
    assert report.items[0].error_code == "outside_entry_zone"
    assert report.items[0].status == "blocked"
    assert report.blocked_markets == 1
    assert report.actionable_markets == 0
    assert store.recent_signals() == []
    assert store.recent_scans()[0]["result"]["items"][0]["blocked_reasons"]


def test_confirmed_signal_requires_a_valid_lot_rounded_contract_plan():
    signal = make_signal(AAA)
    service = FakeService(signals={AAA: signal})
    report = scanner_for([make_candidate()], service=service).scan(scan_request(symbols=(AAA,)))
    plan = report.items[0].contract_plan
    assert plan["contracts"] == int(plan["contracts"])
    assert Decimal(str(plan["base_quantity"])) == Decimal(str(plan["contracts"])) * Decimal("0.001")
    assert plan["actual_risk_amount"] <= entry_plan().actual_risk_amount
    assert plan["notional_value"] <= entry_plan().notional_value
    assert plan["net_reward_to_risk_1"] > 0

    missing = replace(signal, entry_plan=None)
    service = FakeService(signals={AAA: missing})
    report = scanner_for([make_candidate()], service=service).scan(scan_request(symbols=(AAA,)))
    assert report.items[0].error_code == "missing_entry_plan"
    assert not report.items[0].actionable


def test_quote_refresh_failure_blocks_every_confirmed_signal():
    class RefreshFails(FakeUniverse):
        def refresh_quotes(self, candidates):
            raise RuntimeError("Rate limited.")

    service = FakeService(signals={AAA: make_signal(AAA), BBB: make_signal(BBB)})
    report = scanner_for(universe=RefreshFails(), service=service).scan(scan_request())
    assert report.status == "degraded"
    assert report.actionable_markets == 0
    assert report.blocked_markets == 2
    assert all(item.error_code == "quote_refresh_failed" for item in report.items)
    assert report.errors[0]["code"] == "quote_refresh_failed"


def test_ranking_is_deterministic_and_rule_score_cannot_outrank_actionability():
    signals = {AAA: make_signal(AAA), BBB: make_signal(BBB, action=SignalAction.WATCH_LONG, confidence=100)}
    service = FakeService(signals=signals)
    candidates = [make_candidate(BBB), make_candidate(AAA)]
    report = scanner_for(candidates, service=service).scan(scan_request())
    assert report.items[0].candidate.symbol == AAA
    assert report.items[0].actionable

    service = FakeService(signals={AAA: make_signal(AAA), BBB: make_signal(BBB)})
    first = scanner_for(candidates, service=service).scan(scan_request())
    second = scanner_for(list(reversed(candidates)), service=service).scan(scan_request())
    assert [item.candidate.symbol for item in first.items] == [AAA, BBB]
    assert [item.candidate.symbol for item in second.items] == [AAA, BBB]


def test_scan_lock_rejects_overlap_and_releases_after_errors():
    scanner = scanner_for()
    scanner._lock.acquire()
    try:
        with pytest.raises(ScanInProgressError):
            scanner.scan(scan_request())
    finally:
        scanner._lock.release()
    assert scanner.scan(scan_request()).status == "complete"
    broken_service = FakeService(errors={AAA: RuntimeError("Unavailable."), BBB: RuntimeError("Unavailable.")})
    scanner = scanner_for(service=broken_service)
    assert scanner.scan(scan_request()).status == "failed"
    assert not scanner._lock.locked()
    assert scanner.scan(scan_request()).status == "failed"


def test_default_scanner_reuses_one_client_for_universe_quotes_and_candles(monkeypatch):
    client = FakePublicClient({AAA: market()}, {AAA: ticker()})
    created = []
    candle_clients = []

    def create_client(exchange_id, market_type):
        created.append((exchange_id, market_type))
        return client

    class SharedService(FakeService):
        def __init__(self, engine, *, client_factory):
            super().__init__()
            candle_clients.append(client_factory("kucoinfutures", "swap"))

    monkeypatch.setattr(scanner_module, "CCXTMarketDataClient", create_client)
    monkeypatch.setattr(scanner_module, "RegimeBotService", SharedService)
    scanner = MarketScanner(now_factory=lambda: NOW)
    assert scanner.scan(scan_request(symbols=(AAA,))).status == "complete"
    assert scanner.scan(scan_request(symbols=(AAA,))).status == "complete"
    assert created == [("kucoinfutures", "swap")]
    assert candle_clients == [client, client]
    assert client.loads == [True, True]
    assert len(client.calls) == 4  # Discovery and final quote verification per cycle.


def test_scan_budget_marks_unstarted_symbols_as_failures():
    ticks = iter([0.0, 11.0, 11.0, 11.0])
    service = FakeService()
    report = scanner_for(service=service, monotonic=lambda: next(ticks)).scan(scan_request(scan_timeout_seconds=10))
    assert report.status == "failed"
    assert report.failed_markets == 2
    assert service.requests == []
    assert all(item.error_code == "scan_budget_exceeded" for item in report.items)


def test_provenance_is_stable_json_safe_and_changes_with_settings():
    scanner = scanner_for()
    first = scanner.scan(scan_request())
    second = scanner.scan(scan_request())
    changed = scanner.scan(scan_request(minimum_quote_volume=1_500_000))
    assert first.provenance["config_sha256"] == second.provenance["config_sha256"]
    assert first.provenance["config_sha256"] != changed.provenance["config_sha256"]
    assert len(first.provenance["config_sha256"]) == 64
    assert first.provenance["confidence_is_probability"] is False
    assert first.provenance["execution_enabled"] is False
    assert "engine_config" in first.provenance
    assert "ccxt" in first.provenance["versions"]
    assert json.loads(json.dumps(first.to_dict(), allow_nan=False))["status"] == "complete"
