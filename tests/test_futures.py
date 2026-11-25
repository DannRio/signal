from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from signal_bot.futures import ContractPlanError, ContractSpec, build_contract_plan
from signal_bot.models import EntryPlan, PositionSide, SetupType


def market(**changes):
    value = {
        "symbol": "BTC/USDT:USDT",
        "linear": True,
        "inverse": False,
        "contractSize": 0.001,
        "info": {"multiplier": "0.001", "lotSize": "1", "tickSize": "0.1"},
        "limits": {"amount": {"min": 1, "max": 100000}, "cost": {"min": None, "max": None}},
    }
    value.update(changes)
    return value


def entry(side=PositionSide.LONG, **changes):
    long = side == PositionSide.LONG
    reference = 100.04
    stop = 95.03 if long else 105.07
    quantity = 10.0
    cost = quantity * (reference + stop) * 0.0015
    distance = abs(reference - stop)
    direction = 1 if long else -1
    plan = EntryPlan(
        side=side,
        setup_type=SetupType.BREAKOUT,
        trigger_price=100.01,
        entry_low=99.77,
        entry_high=100.26,
        reference_entry=reference,
        stop_loss=stop,
        take_profit_1=reference + direction * distance,
        take_profit_2=reference + direction * distance * 2,
        take_profit_3=reference + direction * distance * 3,
        risk_per_unit=distance,
        target_risk_amount=60.0,
        actual_risk_amount=quantity * distance + cost,
        position_size=quantity,
        notional_value=quantity * reference,
        estimated_round_trip_cost=cost,
        reward_to_risk_1=1,
        reward_to_risk_2=2,
        reward_to_risk_3=3,
        move_stop_to_break_even_after=1,
        trailing_stop_after=2,
        trailing_atr_multiple=2,
        max_holding_bars=96,
    )
    return replace(plan, **changes)


def test_spec_uses_strictest_market_limits_and_exact_increments():
    spec = ContractSpec.from_market(market(
        info={"multiplier": "0.001", "lotSize": "0.25", "tickSize": "0.05", "maxOrderQty": 800, "marketMaxOrderQty": 600, "minOrderQty": 3},
        limits={"amount": {"min": 2, "max": 1000}, "cost": {"min": 5, "max": 500}},
    ))
    assert spec.contract_size == Decimal("0.001")
    assert spec.lot_size == Decimal("0.25")
    assert spec.tick_size == Decimal("0.05")
    assert spec.min_contracts == 3
    assert spec.max_contracts == 600
    assert spec.min_notional == 5
    assert spec.max_notional == 500
    with pytest.raises(FrozenInstanceError):
        spec.lot_size = Decimal("1")
    assert json.loads(json.dumps(spec.to_dict()))["contract_size"] == 0.001


def test_multiplier_fallback_preserves_contract_units():
    spec = ContractSpec.from_market(market(contractSize=None))
    result = build_contract_plan(entry(), spec)
    assert result.base_quantity == result.contracts * Decimal("0.001")
    assert result.contracts > result.base_quantity


@pytest.mark.parametrize("side", [PositionSide.LONG, PositionSide.SHORT])
def test_rounding_cannot_increase_risk_or_notional_at_any_entry_price(side):
    plan = entry(side)
    original = replace(plan)
    spec = ContractSpec.from_market(market())
    result = build_contract_plan(plan, spec)
    assert plan == original
    assert result.contracts % spec.lot_size == 0
    assert result.base_quantity <= Decimal(str(plan.position_size))
    assert result.actual_risk_amount <= Decimal(str(plan.actual_risk_amount))
    assert result.actual_risk_amount <= Decimal(str(plan.target_risk_amount))
    assert result.notional_value <= Decimal(str(plan.notional_value))
    with localcontext() as context:
        context.prec = 50
        for price in (result.entry_low, result.reference_entry, result.entry_high):
            assert price % spec.tick_size == 0
            risk = result.base_quantity * (
                abs(price - result.stop_loss)
                + (price + result.stop_loss) * result.estimated_cost_rate_per_side
            )
            assert risk <= result.actual_risk_amount
            assert result.base_quantity * price <= result.notional_value
        assert result.actual_risk_amount == result.price_risk_amount + result.estimated_round_trip_cost
    assert 0 < result.net_reward_to_risk_1 < 1
    assert result.net_reward_to_risk_1 < result.net_reward_to_risk_2 < result.net_reward_to_risk_3
    assert json.loads(json.dumps(result.to_dict()))["side"] == side.value


def test_prices_round_adversely_for_long_and_short():
    spec = ContractSpec.from_market(market())
    long = build_contract_plan(entry(), spec)
    short = build_contract_plan(entry(PositionSide.SHORT), spec)
    assert (long.reference_entry, long.stop_loss, long.take_profit_1) == (
        Decimal("100.1"), Decimal("95.0"), Decimal("105.0"),
    )
    assert (short.reference_entry, short.stop_loss, short.take_profit_1) == (
        Decimal("100.0"), Decimal("105.1"), Decimal("95.1"),
    )
    assert long.sizing_entry == long.entry_high
    assert short.sizing_entry == short.entry_low
    assert short.notional_entry_price == short.entry_high


def test_coarse_tick_that_collapses_target_levels_is_rejected():
    spec = ContractSpec.from_market(market(info={"lotSize": 1, "tickSize": 10}))
    with pytest.raises(ContractPlanError) as error:
        build_contract_plan(entry(), spec)
    assert error.value.code == "invalid_rounded_levels"


@pytest.mark.parametrize("side", [PositionSide.LONG, PositionSide.SHORT])
def test_fractional_lots_and_non_power_of_ten_price_ticks(side):
    spec = ContractSpec.from_market(market(
        contractSize="0.3",
        info={"lotSize": "0.25", "tickSize": "0.05"},
        limits={},
    ))
    result = build_contract_plan(entry(side), spec)
    assert result.contracts % Decimal("0.25") == 0
    assert result.base_quantity == result.contracts * Decimal("0.3")
    assert result.stop_loss % Decimal("0.05") == 0
    assert result.actual_risk_amount <= Decimal(str(entry(side).actual_risk_amount))


def test_budget_below_minimum_lot_never_rounds_up():
    spec = ContractSpec.from_market(market(
        contractSize=20,
        info={"lotSize": 1, "tickSize": "0.1"},
    ))
    with pytest.raises(ContractPlanError) as error:
        build_contract_plan(entry(), spec)
    assert error.value.code == "below_minimum_contracts"


def test_minimum_cost_is_checked_at_lowest_entry_zone_price():
    spec = ContractSpec.from_market(market(limits={"cost": {"min": 1000}}))
    with pytest.raises(ContractPlanError) as error:
        build_contract_plan(entry(), spec)
    assert error.value.code == "below_minimum_notional"


def test_contract_count_and_notional_market_maxima_reduce_size():
    spec = ContractSpec.from_market(market(
        info={"lotSize": 1, "tickSize": "0.1", "marketMaxOrderQty": 500},
        limits={"cost": {"max": 40}},
    ))
    result = build_contract_plan(entry(), spec)
    assert result.contracts <= 500
    assert result.notional_value <= 40


def test_market_order_maximum_and_reduced_target_risk_are_respected():
    spec = ContractSpec.from_market(market(
        info={"lotSize": 1, "tickSize": "0.1", "marketMaxOrderQty": 500},
    ))
    result = build_contract_plan(entry(), spec)
    assert result.contracts == 500
    smaller = build_contract_plan(entry(target_risk_amount=1), spec)
    assert smaller.actual_risk_amount <= 1
    assert smaller.contracts < result.contracts


@pytest.mark.parametrize("field", ["contractSize", "lotSize", "tickSize"])
@pytest.mark.parametrize("bad", [None, 0, -1, float("nan"), float("inf"), True])
def test_invalid_or_missing_required_metadata_fails_closed(field, bad):
    raw = market()
    raw["info"].pop("multiplier")
    if field == "contractSize":
        raw[field] = bad
    else:
        raw["info"][field] = bad
    with pytest.raises(ContractPlanError) as error:
        ContractSpec.from_market(raw)
    assert error.value.code == ("missing_contract_metadata" if bad is None else "invalid_contract_metadata")


def test_conflicting_contract_metadata_is_rejected():
    with pytest.raises(ContractPlanError, match="disagree"):
        ContractSpec.from_market(market(contractSize=0.01))
    with pytest.raises(ContractPlanError, match="below the minimum"):
        ContractSpec.from_market(market(limits={"amount": {"min": 3, "max": 2}}))


@pytest.mark.parametrize("field", ["reference_entry", "stop_loss", "position_size", "actual_risk_amount", "target_risk_amount", "estimated_round_trip_cost"])
def test_non_finite_plan_values_are_rejected(field):
    with pytest.raises(ContractPlanError) as error:
        build_contract_plan(entry(**{field: float("nan")}), ContractSpec.from_market(market()))
    assert error.value.code == "invalid_entry_plan"


def test_zero_cost_plan_retains_zero_estimated_cost():
    plan = entry(estimated_round_trip_cost=0, actual_risk_amount=50.1)
    result = build_contract_plan(plan, ContractSpec.from_market(market()))
    assert result.estimated_round_trip_cost == 0
    assert result.actual_risk_amount <= Decimal("50.1")
