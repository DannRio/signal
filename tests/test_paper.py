from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from signal_bot.futures import ContractSpec
from signal_bot.paper import PaperPolicy, PaperTracker
from signal_bot.storage import SignalStore

NOW = datetime(2026, 11, 26, tzinfo=UTC)
SYMBOL = "BTC/USDT:USDT"


def snapshot(*, symbol=SYMBOL, side="long", now=NOW, contracts=10, **changes):
    spec = ContractSpec(symbol=symbol, contract_size=Decimal("0.01"), lot_size=Decimal(1),
                        tick_size=Decimal("0.01"), min_contracts=Decimal(1), max_contracts=Decimal(1000))
    plan = {"side": side, "contracts": contracts, "base_quantity": contracts / 100,
            "entry_low": 99.9, "entry_high": 100.1,
            "stop_loss": 98 if side == "long" else 102,
            "take_profit_1": 102 if side == "long" else 98,
            "take_profit_2": 104 if side == "long" else 96,
            "take_profit_3": 106 if side == "long" else 94,
            "risk_budget_amount": 25, "notional_budget_amount": 1000}
    item = {"candidate": {"symbol": symbol, "quote": "USDT", "bid": 100, "ask": 100,
                          "quote_timestamp": now.isoformat(), "contract_spec": spec.to_dict()},
            "signal": {"symbol": symbol, "action": side, "execution_timeframe": "15m",
                       "generated_at": datetime.fromtimestamp(now.timestamp() // 900 * 900, UTC).isoformat(),
                       "fingerprint": f"{symbol}-{side}-{now.isoformat()}"},
            "status": "analyzed", "actionable": True, "valid_until": (now + timedelta(seconds=30)).isoformat(),
            "contract_plan": plan}
    item.update(changes)
    return {"exchange_id": "kucoinfutures", "market_type": "swap", "execution_timeframe": "15m",
            "run_id": f"scan-{now.isoformat()}", "generated_at": now.isoformat(), "items": [item], "rejections": []}


def tracker(tmp_path, *, policy=None, now=NOW, name="paper.db"):
    return PaperTracker(SignalStore(tmp_path / name), policy=policy or PaperPolicy(fee_bps=0, slippage_bps=0), now_factory=lambda: now)


def opened(paper, report=None, **kwargs):
    response = paper.process_scan(report or snapshot(), **kwargs)
    assert response["opened"] == 1, response
    return response["events"][0]["position_id"]


def quote(paper, price, seconds=15, **kwargs):
    return paper.observe_quote(SYMBOL, bid=price, ask=price, quote_timestamp=NOW + timedelta(seconds=seconds),
                               observed_at=NOW + timedelta(seconds=seconds), **kwargs)


def test_durable_entry_and_restart_preserve_immutable_policy_and_snapshot(tmp_path):
    paper = tracker(tmp_path)
    event = snapshot()
    position_id = opened(paper, event)
    event["items"][0]["contract_plan"]["stop_loss"] = 1
    restored = tracker(tmp_path, policy=PaperPolicy(fee_bps=20, slippage_bps=10, target=2))
    position = restored.positions()[0]
    assert position["position_id"] == position_id
    assert position["policy"]["fee_bps"] == 0
    assert position["exit_target"] == 1
    assert position["stop_loss"] == 98
    assert position["entry_snapshot"]["item"]["contract_plan"]["stop_loss"] == 98
    closed = quote(restored, 102)[0]
    assert closed["status"] == "closed"
    assert closed["pnl_before_funding"] == pytest.approx(.2)
    assert restored.summary()["closed_positions"] == 1


def test_skipped_history_stays_immutable_when_same_candle_later_qualifies(tmp_path):
    paper = tracker(tmp_path)
    first = snapshot(actionable=False, status="blocked", error_code="outside_entry_zone")
    assert paper.process_scan(first)["events"][0]["decision"] == "skipped"
    replacement = snapshot()
    replacement["run_id"] = "new-run"
    replacement["items"][0]["signal"]["fingerprint"] = "changed-score-same-candle"
    second = tracker(tmp_path, policy=PaperPolicy(fee_bps=0, slippage_bps=0, target=2))
    outcome = second.process_scan(replacement)
    assert outcome["opened"] == 1
    assert len(second.events()) == 2
    skipped = next(event for event in second.events() if event["decision"] == "skipped")
    admitted = next(event for event in second.events() if event["decision"] == "opened")
    assert skipped["reason"] == "outside_entry_zone"
    assert skipped["policy"]["target"] == 1
    assert admitted["policy"]["target"] == 2
    assert skipped["input_snapshot"]["opportunity_id"] == admitted["input_snapshot"]["opportunity_id"]
    third = tracker(tmp_path)
    response = third.process_scan(snapshot())
    assert response["opened"] == 0
    assert response["events"][0]["reason"] == "opportunity_already_entered"
    assert len(third.positions()) == 1


def test_closed_signal_does_not_reopen_after_restart(tmp_path):
    paper = tracker(tmp_path)
    opened(paper)
    quote(paper, 102)
    response = tracker(tmp_path).process_scan(snapshot())
    assert response["duplicate"] == 1
    assert response["opened"] == 0
    assert len(paper.positions()) == 1


def test_cross_process_style_connections_serialize_same_event(tmp_path):
    a, b = tracker(tmp_path), tracker(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda paper: paper.process_scan(snapshot()), (a, b)))
    assert sum(result["opened"] for result in results) == 1
    assert sum(result["duplicate"] for result in results) == 1
    assert len(a.positions()) == 1


@pytest.mark.parametrize(("change", "decision", "reason"), [
    ({"valid_until": NOW.isoformat()}, "expired", "signal_expired"),
    ({"actionable": False}, "skipped", "not_qualified"),
    ({"status": "failed", "signal": None, "error_code": "candle_gap"}, "rejected", "candle_gap"),
    ({"contract_plan": None}, "rejected", "missing_contract_plan"),
])
def test_rejected_skipped_and_expired_decisions_are_journaled(tmp_path, change, decision, reason):
    paper = tracker(tmp_path)
    result = paper.process_scan(snapshot(**change))
    assert result["events"][0]["decision"] == decision
    assert result["events"][0]["reason"] == reason
    assert paper.positions() == []
    assert paper.events()[0]["decision"] == decision


def test_universe_rejection_is_audited_once(tmp_path):
    paper = tracker(tmp_path)
    report = snapshot()
    report["items"] = []
    report["rejections"] = [{"symbol": SYMBOL, "code": "stale_quote", "reason": "Stale."}]
    assert paper.process_scan(report)["events"][0]["decision"] == "rejected"
    assert paper.process_scan(report)["duplicate"] == 1
    assert paper.events()[0]["input_snapshot"]["rejection"]["code"] == "stale_quote"


@pytest.mark.parametrize("age", [-1, 31])
def test_entry_rejects_future_or_stale_quotes(tmp_path, age):
    paper = tracker(tmp_path)
    report = snapshot()
    report["items"][0]["candidate"]["quote_timestamp"] = (NOW - timedelta(seconds=age)).isoformat()
    event = paper.process_scan(report)["events"][0]
    assert event["decision"] == "rejected"
    assert event["reason"] == "invalid_snapshot"
    assert paper.positions() == []


def test_entry_uses_executable_side_and_adverse_tick_rounded_slippage(tmp_path):
    policy = PaperPolicy(fee_bps=6, slippage_bps=2)
    paper = tracker(tmp_path, policy=policy)
    report = snapshot()
    report["items"][0]["candidate"]["bid"] = 99.98
    opened(paper, report)
    position = paper.positions()[0]
    assert position["entry_price"] == pytest.approx(100.02)
    assert position["entry_fee"] == pytest.approx(.0060012)
    outcome = quote(paper, 102)[0]
    assert outcome["exit_price"] == pytest.approx(101.97)
    assert outcome["gross_pnl"] == pytest.approx(.195)
    assert outcome["exit_fee"] == pytest.approx(.0061182)
    assert outcome["pnl_before_funding"] == pytest.approx(.1828806)
    assert outcome["funding_complete"] is False


def test_slippage_does_not_chase_outside_original_zone(tmp_path):
    paper = tracker(tmp_path, policy=PaperPolicy(slippage_bps=20))
    result = paper.process_scan(snapshot())
    assert result["events"][0]["reason"] == "slippage_outside_entry_zone"


@pytest.mark.parametrize("change", [
    {"contracts": 1.5, "base_quantity": .015},
    {"base_quantity": 999},
    {"stop_loss": 98.001},
    {"side": "short"},
])
def test_invalid_contract_quantities_and_increments_cannot_open(tmp_path, change):
    paper = tracker(tmp_path)
    report = snapshot()
    report["items"][0]["contract_plan"].update(change)
    assert paper.process_scan(report)["events"][0]["decision"] == "rejected"
    assert paper.positions() == []


def test_target_two_policy_exits_entire_position_only_at_target_two(tmp_path):
    paper = tracker(tmp_path, policy=PaperPolicy(fee_bps=0, slippage_bps=0, target=2))
    opened(paper)
    position = quote(paper, 102)[0]
    assert position["status"] == "open"
    assert position["contracts"] == 10
    position = quote(paper, 104, seconds=30)[0]
    assert position["status"] == "closed"
    assert position["contracts"] == 10
    assert position["pnl_before_funding"] == pytest.approx(.4)


def test_short_uses_ask_to_exit_and_long_uses_bid(tmp_path):
    long = tracker(tmp_path, name="long.db")
    opened(long)
    observed = NOW + timedelta(seconds=15)
    assert long.observe_quote(SYMBOL, bid=101.99, ask=102.01, quote_timestamp=observed, observed_at=observed)[0]["status"] == "open"
    short = tracker(tmp_path, name="short.db")
    opened(short, snapshot(side="short"))
    assert short.observe_quote(SYMBOL, bid=97.99, ask=98.01, quote_timestamp=observed, observed_at=observed)[0]["status"] == "open"
    assert quote(short, 98, seconds=30)[0]["pnl_before_funding"] == pytest.approx(.2)


def test_observed_stop_gap_fills_at_worse_observed_quote(tmp_path):
    paper = tracker(tmp_path)
    opened(paper)
    position = quote(paper, 97)[0]
    assert position["exit_price"] == 97
    assert position["pnl_before_funding"] == pytest.approx(-.3)
    assert position["exit_reason"] == "observed_stop"


def test_mae_mfe_and_quote_idempotence(tmp_path):
    paper = tracker(tmp_path)
    position_id = opened(paper)
    quote(paper, 99)
    assert quote(paper, 99) == []
    quote(paper, 101, seconds=30)
    position = paper.positions()[0]
    assert position["mae"] == pytest.approx(-.1)
    assert position["mfe"] == pytest.approx(.1)
    assert len(paper.observations(position_id)) == 3


@pytest.mark.parametrize("bad", [
    {"bid": float("nan"), "ask": 100},
    {"bid": 101, "ask": 100},
    {"bid": True, "ask": 100},
])
def test_invalid_exit_quotes_leave_position_unchanged(tmp_path, bad):
    paper = tracker(tmp_path)
    opened(paper)
    initial = paper.positions()[0]
    with pytest.raises(ValueError):
        paper.observe_quote(SYMBOL, quote_timestamp=NOW + timedelta(seconds=15), observed_at=NOW + timedelta(seconds=15), **bad)
    assert paper.positions()[0] == initial


def test_future_and_stale_exit_quotes_are_rejected(tmp_path):
    paper = tracker(tmp_path)
    opened(paper)
    with pytest.raises(ValueError, match="future-dated or stale"):
        paper.observe_quote(SYMBOL, bid=102, ask=102, quote_timestamp=NOW + timedelta(seconds=20), observed_at=NOW + timedelta(seconds=15))
    with pytest.raises(ValueError, match="future-dated or stale"):
        paper.observe_quote(SYMBOL, bid=102, ask=102, quote_timestamp=NOW + timedelta(seconds=1), observed_at=NOW + timedelta(seconds=32))
    assert paper.positions()[0]["status"] == "open"


def test_gap_is_unresolved_instead_of_fabricating_profitable_exit(tmp_path):
    paper = tracker(tmp_path)
    opened(paper)
    position = quote(paper, 105, seconds=61)[0]
    assert position["status"] == "unresolved"
    assert position["pnl_before_funding"] is None
    assert position["exit_price"] is None
    summary = paper.summary()
    assert summary["unresolved_positions"] == 1
    assert summary["reserved_risk"] > 0
    assert summary["gross_notional_reserved"] > 0
    assert summary["closed_pnl_before_funding"] == 0
    assert quote(paper, 106, seconds=75) == []


def test_old_quote_cannot_retroactively_exit_before_entry(tmp_path):
    paper = tracker(tmp_path)
    opened(paper)
    assert paper.observe_quote(SYMBOL, bid=105, ask=105, quote_timestamp=NOW, observed_at=NOW + timedelta(seconds=10)) == []
    assert paper.positions()[0]["status"] == "open"


def test_overdue_positions_fail_closed_before_admitting_more_signals(tmp_path):
    paper = tracker(tmp_path)
    opened(paper)
    assert paper.summary(observed_at=NOW + timedelta(seconds=61))["overdue_observations"] == 1
    report = snapshot(symbol="ETH/USDT:USDT", now=NOW + timedelta(seconds=61))
    response = paper.process_scan(report, observed_at=NOW + timedelta(seconds=61))
    assert response["opened"] == 0
    assert response["events"][0]["reason"] == "unresolved_portfolio_exposure"
    assert paper.positions(status="unresolved")[0]["unresolved_reason"] == "quote_monitoring_gap"


def test_explicit_monitor_failure_persists_gap(tmp_path):
    paper = tracker(tmp_path)
    position_id = opened(paper)
    result = paper.mark_monitoring_gap(reason="exchange_unavailable", observed_at=NOW + timedelta(seconds=10))
    assert result[0]["status"] == "unresolved"
    assert tracker(tmp_path).positions()[0]["unresolved_reason"] == "exchange_unavailable"
    assert paper.observations(position_id)[0]["kind"] == "gap"


def test_open_symbol_and_position_count_limits(tmp_path):
    paper = tracker(tmp_path, policy=PaperPolicy(fee_bps=0, slippage_bps=0, max_open_positions=1, maximum_observation_gap_seconds=1800))
    opened(paper)
    now = NOW + timedelta(minutes=15)
    same = paper.process_scan(snapshot(now=now), observed_at=now)
    assert same["events"][0]["reason"] == "symbol_already_open"
    another = paper.process_scan(snapshot(symbol="ETH/USDT:USDT", now=now), observed_at=now)
    assert another["events"][0]["reason"] == "maximum_open_positions"


def test_risk_budget_is_aggregate_and_contract_lots_are_rounded_down(tmp_path):
    policy = PaperPolicy(assumed_equity=100, fee_bps=0, slippage_bps=0, maximum_open_risk_percent=.5)
    paper = tracker(tmp_path, policy=policy)
    report = snapshot(contracts=20)
    report["items"].append(snapshot(symbol="ETH/USDT:USDT", contracts=20)["items"][0])
    assert paper.process_scan(report)["opened"] == 2
    positions = paper.positions()
    assert sorted(position["contracts"] for position in positions) == [5, 20]
    assert all(position["contracts"] % 1 == 0 for position in positions)
    summary = paper.summary()
    assert summary["reserved_risk"] == pytest.approx(.5)
    assert summary["reserved_risk"] <= summary["conservative_budget_equity"] * .005


def test_notional_budget_accounts_for_entry_fees_and_spread(tmp_path):
    policy = PaperPolicy(assumed_equity=10, fee_bps=6, slippage_bps=2, maximum_open_risk_percent=100, maximum_gross_notional_multiple=1)
    paper = tracker(tmp_path, policy=policy)
    opened(paper, snapshot(contracts=100))
    summary = paper.summary()
    position = paper.positions()[0]
    assert position["contracts"] == 9
    assert summary["gross_notional_reserved"] <= summary["conservative_budget_equity"]
    assert summary["conservative_budget_equity"] < 10


def test_realized_loss_reduces_available_account_equity(tmp_path):
    paper = tracker(tmp_path)
    opened(paper)
    quote(paper, 97)
    summary = paper.summary()
    assert summary["conservative_budget_equity"] == pytest.approx(9999.7)
    assert summary["reserved_risk"] == 0
    assert summary["gross_notional_reserved"] == 0


def test_starting_equity_cannot_silently_reset_on_restart(tmp_path):
    tracker(tmp_path)
    with pytest.raises(ValueError, match="immutable assumed equity"):
        tracker(tmp_path, policy=PaperPolicy(assumed_equity=20000))
    separate = PaperTracker(SignalStore(tmp_path / "paper.db"), account_id="experiment-2", policy=PaperPolicy(assumed_equity=20000))
    assert separate.summary()["assumed_equity"] == 20000


def test_open_existing_does_not_create_a_default_account(tmp_path):
    store = SignalStore(tmp_path / "read.db")
    assert PaperTracker.open_existing(store) is None
    with store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_accounts").fetchone()[0] == 0
    paper = PaperTracker(store, policy=PaperPolicy(assumed_equity=5000, target=2), now_factory=lambda: NOW)
    opened(paper)
    restored = PaperTracker.open_existing(store, now_factory=lambda: NOW)
    assert restored is not None
    assert restored.policy.assumed_equity == 5000
    assert restored.policy.target == 2
    assert restored.summary()["assumed_equity"] == 5000


def test_open_existing_restores_last_intake_policy_without_mutating_old_position(tmp_path):
    paper = tracker(tmp_path)
    opened(paper)
    changed = tracker(tmp_path, policy=PaperPolicy(target=2))
    changed.process_scan(snapshot(symbol="ETH/USDT:USDT"))
    restored = PaperTracker.open_existing(paper.store)
    assert restored.policy.target == 2
    first = next(position for position in restored.positions() if position["symbol"] == SYMBOL)
    assert first["policy"]["target"] == 1


def bar(paper, *, start=NOW, open=100, high=101, low=99, close=100):
    return paper.observe_closed_bar(SYMBOL, timeframe="15m", open_time=start,
                                    open=open, high=high, low=low, close=close,
                                    observed_at=start + timedelta(minutes=15))


def test_closed_bars_never_use_pre_entry_movements(tmp_path):
    paper = tracker(tmp_path, policy=PaperPolicy(fee_bps=0, slippage_bps=0, observation_mode="closed_bars"))
    opened(paper)
    assert bar(paper, start=NOW - timedelta(minutes=15), high=110, low=90) == []
    assert paper.positions()[0]["status"] == "open"
    assert bar(paper)[0]["status"] == "open"
    assert bar(paper) == []
    assert bar(paper, start=NOW + timedelta(minutes=15), high=103)[0]["exit_reason"] == "bar_target"


def test_closed_bar_with_both_levels_uses_stop_and_does_not_invent_mfe(tmp_path):
    paper = tracker(tmp_path, policy=PaperPolicy(fee_bps=0, slippage_bps=0, observation_mode="closed_bars"))
    opened(paper)
    position = bar(paper, high=110, low=90)[0]
    assert position["exit_reason"] == "ambiguous_bar_stop"
    assert position["exit_price"] == 98
    assert position["pnl_before_funding"] == pytest.approx(-.2)
    assert position["mfe"] == 0
    assert position["exit_bar_excursions_unresolved"] is True


def test_bar_gap_stop_uses_worse_open(tmp_path):
    paper = tracker(tmp_path, policy=PaperPolicy(fee_bps=0, slippage_bps=0, observation_mode="closed_bars"))
    opened(paper)
    position = bar(paper, open=96, high=99, low=95, close=97)[0]
    assert position["exit_price"] == 96
    assert position["pnl_before_funding"] == pytest.approx(-.4)


def test_missing_future_bar_is_unresolved(tmp_path):
    paper = tracker(tmp_path, policy=PaperPolicy(fee_bps=0, slippage_bps=0, observation_mode="closed_bars"))
    opened(paper)
    result = bar(paper, start=NOW + timedelta(minutes=15), high=104)[0]
    assert result["status"] == "unresolved"
    assert result["unresolved_reason"] == "closed_bar_data_gap"
    assert result["pnl_before_funding"] is None


def test_bar_mode_requires_known_entry_boundary(tmp_path):
    paper = tracker(tmp_path, policy=PaperPolicy(observation_mode="closed_bars"))
    now = NOW + timedelta(seconds=1)
    result = paper.process_scan(snapshot(now=now), observed_at=now)
    assert result["events"][0]["reason"] == "bar_mode_requires_boundary_entry"


def test_quote_and_bar_modes_cannot_silently_mix(tmp_path):
    paper = tracker(tmp_path)
    opened(paper)
    assert bar(paper, high=110, low=90) == []
    assert paper.positions()[0]["status"] == "open"


def test_open_or_invalid_bar_is_rejected_without_an_outcome(tmp_path):
    paper = tracker(tmp_path, policy=PaperPolicy(observation_mode="closed_bars"))
    opened(paper)
    with pytest.raises(ValueError, match="closed and aligned"):
        paper.observe_closed_bar(SYMBOL, timeframe="15m", open_time=NOW, open=100, high=101, low=99, close=100, observed_at=NOW + timedelta(minutes=10))
    with pytest.raises(ValueError, match="Invalid OHLC"):
        bar(paper, high=95)
    assert paper.positions()[0]["status"] == "open"


@pytest.mark.parametrize(("side", "expected"), [("long", -.01), ("short", .01)])
def test_verified_funding_payment_is_idempotent_but_not_complete_coverage(tmp_path, side, expected):
    paper = tracker(tmp_path)
    position_id = opened(paper, snapshot(side=side))
    quote(paper, 100, seconds=10)
    settlement = {"timestamp": NOW + timedelta(seconds=5), "rate": Decimal("0.001"), "mark_price": 100, "source": "verified-fixture-settlement"}
    applied = paper.apply_funding(position_id, settlement, observed_at=NOW + timedelta(seconds=10))
    assert applied["cashflow"] == pytest.approx(expected)
    assert paper.apply_funding(position_id, settlement, observed_at=NOW + timedelta(seconds=10))["duplicate"] is True
    position = quote(paper, 102 if side == "long" else 98)[0]
    assert position["pnl_before_funding"] == pytest.approx(.2)
    assert position["pnl_including_verified_funding"] == pytest.approx(.2 + expected)
    assert position["funding_complete"] is False
    assert paper.summary()["verified_funding_cashflow"] == pytest.approx(expected)


def test_funding_needs_source_mark_and_known_holding_interval(tmp_path):
    paper = tracker(tmp_path)
    position_id = opened(paper)
    quote(paper, 100, seconds=10)
    valid = {"timestamp": NOW + timedelta(seconds=5), "rate": .001, "mark_price": 100, "source": "fixture"}
    for changes in ({"source": ""}, {"mark_price": None}, {"timestamp": NOW}, {"timestamp": NOW + timedelta(days=1)}):
        with pytest.raises(ValueError):
            paper.apply_funding(position_id, {**valid, **changes}, observed_at=NOW + timedelta(seconds=10))
    paper.apply_funding(position_id, valid, observed_at=NOW + timedelta(seconds=10))
    with pytest.raises(ValueError, match="Conflicting funding"):
        paper.apply_funding(position_id, {**valid, "rate": .002}, observed_at=NOW + timedelta(seconds=10))


def test_unresolved_and_intrabar_exit_funding_ownership_is_not_assumed(tmp_path):
    paper = tracker(tmp_path)
    position_id = opened(paper)
    paper.mark_monitoring_gap(observed_at=NOW + timedelta(seconds=5))
    settlement = {"timestamp": NOW + timedelta(seconds=2), "rate": .001, "mark_price": 100, "source": "fixture"}
    with pytest.raises(ValueError, match="Unresolved exposure"):
        paper.apply_funding(position_id, settlement, observed_at=NOW + timedelta(seconds=10))
    bars = tracker(tmp_path, name="bars.db", policy=PaperPolicy(observation_mode="closed_bars"))
    position_id = opened(bars)
    bar(bars, high=103)
    with pytest.raises(ValueError, match="Intrabar exit timing"):
        bars.apply_funding(position_id, settlement, observed_at=NOW + timedelta(minutes=15))


@pytest.mark.parametrize("rate", ("0.001", "-0.001"))
def test_funding_after_restart_cannot_credit_or_debit_unobserved_exposure(tmp_path, rate):
    paper = tracker(tmp_path)
    position_id = opened(paper)
    before = paper.summary()
    restarted = PaperTracker.open_existing(paper.store, now_factory=lambda: NOW + timedelta(hours=1))
    assert restarted.summary()["overdue_observations"] == 1
    settlement = {"timestamp": NOW + timedelta(minutes=30), "rate": rate, "mark_price": 100, "source": "settlement fixture"}
    with pytest.raises(ValueError, match="Unresolved exposure"):
        restarted.apply_funding(position_id, settlement)
    after = restarted.summary()
    assert after["unresolved_positions"] == 1
    assert after["open_positions"] == 0
    assert after["verified_funding_cashflow"] == 0
    assert after["cash_equity_including_verified_funding"] == before["cash_equity_including_verified_funding"]
    assert after["reserved_risk"] == before["reserved_risk"]
    assert restarted.positions()[0]["unresolved_reason"] == "quote_monitoring_gap"
    assert restarted.observations(position_id)[0]["kind"] == "gap"
    with paper.store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM paper_funding").fetchone()[0] == 0


def test_funding_ownership_uses_quote_timestamp_not_receipt_time_or_gap_tolerance(tmp_path):
    paper = tracker(tmp_path)
    position_id = opened(paper)
    paper.observe_quote(SYMBOL, bid=100, ask=100, quote_timestamp=NOW + timedelta(seconds=5), observed_at=NOW + timedelta(seconds=10))
    settlement = {"timestamp": NOW + timedelta(seconds=6), "rate": ".001", "mark_price": 100, "source": "settlement fixture"}
    with pytest.raises(ValueError, match="exceeds observed ownership coverage"):
        paper.apply_funding(position_id, settlement, observed_at=NOW + timedelta(seconds=10))
    assert paper.positions()[0]["status"] == "open"
    assert paper.positions()[0]["verified_funding_cashflow"] == 0
    quote(paper, 100, seconds=15)
    applied = paper.apply_funding(position_id, settlement, observed_at=NOW + timedelta(seconds=15))
    assert applied["cashflow"] == pytest.approx(-.01)


@pytest.mark.parametrize("gap_already_marked", (False, True))
def test_later_monitoring_gap_preserves_earlier_observed_funding_ownership(tmp_path, gap_already_marked):
    paper = tracker(tmp_path)
    position_id = opened(paper)
    quote(paper, 100, seconds=20)
    later = NOW + timedelta(hours=1)
    if gap_already_marked:
        paper.mark_monitoring_gap(observed_at=later)
    settlement = {"timestamp": NOW + timedelta(seconds=20), "rate": ".001", "mark_price": 100, "source": "settlement fixture"}
    applied = paper.apply_funding(position_id, settlement, observed_at=later)
    assert applied["cashflow"] == pytest.approx(-.01)
    assert paper.positions()[0]["status"] == "unresolved"
    assert paper.apply_funding(position_id, settlement, observed_at=later)["duplicate"] is True
    with pytest.raises(ValueError, match="Unresolved exposure"):
        paper.apply_funding(position_id, {**settlement, "timestamp": NOW + timedelta(seconds=21)}, observed_at=later)
    assert paper.positions()[0]["verified_funding_cashflow"] == pytest.approx(-.01)


def test_quote_exit_boundary_is_observed_but_later_funding_is_not_owned(tmp_path):
    paper = tracker(tmp_path)
    position_id = opened(paper)
    quote(paper, 102, seconds=15)
    settlement = {"timestamp": NOW + timedelta(seconds=15), "rate": ".001", "mark_price": 100, "source": "settlement fixture"}
    paper.apply_funding(position_id, settlement, observed_at=NOW + timedelta(seconds=20))
    position = paper.positions()[0]
    assert position["status"] == "closed"
    assert position["pnl_including_verified_funding"] == pytest.approx(.19)
    with pytest.raises(ValueError, match="outside the known holding interval"):
        paper.apply_funding(position_id, {**settlement, "timestamp": NOW + timedelta(seconds=16)}, observed_at=NOW + timedelta(seconds=20))


def test_closed_bar_funding_requires_contiguous_coverage_and_preserves_known_boundary(tmp_path):
    paper = tracker(tmp_path, policy=PaperPolicy(fee_bps=0, slippage_bps=0, observation_mode="closed_bars"))
    position_id = opened(paper)
    settlement = {"timestamp": NOW + timedelta(minutes=15), "rate": ".001", "mark_price": 100, "source": "settlement fixture"}
    with pytest.raises(ValueError, match="exceeds observed ownership coverage"):
        paper.apply_funding(position_id, settlement, observed_at=NOW + timedelta(minutes=15))
    assert paper.positions()[0]["status"] == "open"
    bar(paper)
    bar(paper, start=NOW + timedelta(minutes=15), high=103)
    applied = paper.apply_funding(position_id, settlement, observed_at=NOW + timedelta(minutes=30))
    assert applied["cashflow"] == pytest.approx(-.01)
    with pytest.raises(ValueError, match="Intrabar exit timing"):
        paper.apply_funding(position_id, {**settlement, "timestamp": NOW + timedelta(minutes=15, seconds=1)}, observed_at=NOW + timedelta(minutes=30))


def test_funding_cannot_use_an_observation_received_after_declared_application_time(tmp_path):
    paper = tracker(tmp_path)
    position_id = opened(paper)
    quote(paper, 100, seconds=15)
    settlement = {"timestamp": NOW + timedelta(seconds=5), "rate": ".001", "mark_price": 100, "source": "settlement fixture"}
    with pytest.raises(ValueError, match="precedes the last observation"):
        paper.apply_funding(position_id, settlement, observed_at=NOW + timedelta(seconds=10))
    assert paper.positions()[0]["verified_funding_cashflow"] == 0


def _paper_replay_archive(store):
    with store._connection() as connection:
        connection.execute("CREATE TABLE decision_snapshots (snapshot_id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE replay_pins (snapshot_id TEXT NOT NULL, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, pinned_at TEXT NOT NULL, PRIMARY KEY(snapshot_id,owner_type,owner_id), FOREIGN KEY(snapshot_id) REFERENCES decision_snapshots(snapshot_id))")
        connection.execute("INSERT INTO decision_snapshots VALUES ('retained-entry')")


@pytest.mark.parametrize("archive_available", (False, True))
def test_missing_entry_replay_is_explicit_without_blocking_standalone_paper(tmp_path, archive_available):
    paper = tracker(tmp_path)
    if archive_available:
        _paper_replay_archive(paper.store)
    opened(paper, snapshot(replay_id="missing-entry"))
    assert paper.positions()[0]["replay_evidence_available"] is False


def test_paper_entry_pins_existing_replay_and_keeps_it_after_close(tmp_path):
    paper = tracker(tmp_path)
    _paper_replay_archive(paper.store)
    position_id = opened(paper, snapshot(replay_id="retained-entry"))
    assert paper.positions()[0]["replay_evidence_available"] is True
    quote(paper, 102)
    with paper.store._connection() as connection:
        pins = connection.execute("SELECT snapshot_id,owner_type,owner_id FROM replay_pins").fetchall()
    assert [tuple(row) for row in pins] == [("retained-entry", "paper_position", position_id)]


def test_failed_paper_admission_rolls_back_replay_pin_with_position(tmp_path):
    paper = tracker(tmp_path)
    _paper_replay_archive(paper.store)
    with paper.store._connection() as connection:
        connection.execute("CREATE TRIGGER abort_paper_insert BEFORE INSERT ON paper_positions BEGIN SELECT RAISE(ABORT, 'injected admission failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected admission failure"):
        paper.process_scan(snapshot(replay_id="retained-entry"))
    with paper.store._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM replay_pins").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    {"assumed_equity": -1}, {"fee_bps": float("nan")}, {"slippage_bps": 1000},
    {"target": 1.0}, {"target": True}, {"max_open_positions": 1.5},
    {"maximum_open_risk_percent": 101}, {"maximum_gross_notional_multiple": 6},
    {"maximum_quote_age_seconds": 301}, {"maximum_observation_gap_seconds": 0},
    {"observation_mode": "guessed_fills"},
])
def test_policy_rejects_invalid_assumptions(change):
    with pytest.raises(ValueError):
        replace(PaperPolicy(), **change)


def test_memory_store_and_account_isolation():
    store = SignalStore(":memory:")
    a = PaperTracker(store, account_id="a", now_factory=lambda: NOW)
    b = PaperTracker(store, account_id="b", now_factory=lambda: NOW)
    position_id = opened(a)
    assert b.positions() == []
    assert b.events() == []
    assert b.observations(position_id) == []
    assert b.summary()["open_positions"] == 0


def test_report_accepts_to_dict_object_and_keeps_original_untouched(tmp_path):
    paper = tracker(tmp_path)
    report = snapshot()
    copy = deepcopy(report)

    class Report:
        def to_dict(self):
            return report

    opened(paper, Report())
    assert report == copy


def test_old_decision_candle_cannot_be_revived_by_extending_valid_until(tmp_path):
    paper = tracker(tmp_path)
    report = snapshot()
    report["items"][0]["signal"]["generated_at"] = (NOW - timedelta(minutes=15)).isoformat()
    assert paper.process_scan(report)["events"][0]["reason"] == "decision_candle_expired"


def test_independent_connections_cannot_spend_same_portfolio_slot(tmp_path):
    policy = PaperPolicy(fee_bps=0, slippage_bps=0, max_open_positions=1)
    a, b = tracker(tmp_path, policy=policy), tracker(tmp_path, policy=policy)
    reports = (snapshot(), snapshot(symbol="ETH/USDT:USDT"))
    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = [workers.submit(paper.process_scan, report) for paper, report in zip((a, b), reports, strict=True)]
        results = [job.result() for job in jobs]
    assert sum(result["opened"] for result in results) == 1
    assert len(a.positions()) == 1
    assert {event["decision"] for event in a.events()} == {"opened", "skipped"}
