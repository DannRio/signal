"""Conservative, signal-only conversion of base-asset plans to linear contracts.

The engine's EntryPlan remains unchanged.  This module applies the instrument's
current lot and price increments and reduces quantity when rounding or the entry
zone would otherwise exceed the engine's risk or notional budgets.  These are
planning estimates, not order execution, margin, or liquidation calculations.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from typing import Any

from .models import EntryPlan, PositionSide


class ContractPlanError(ValueError):
    """An instrument or plan cannot produce a valid, bounded contract plan."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _number(
    value: Any,
    field: str,
    *,
    code: str,
    allow_zero: bool = False,
) -> Decimal:
    if value is None:
        raise ContractPlanError(code, f"{field} is missing.")
    try:
        if isinstance(value, bool):
            raise ValueError("Boolean is not a numeric instrument value.")
        result = Decimal(str(value))
        if not result.is_finite() or not math.isfinite(float(result)):
            raise ValueError("Non-finite value.")
        if result != 0 and float(result) == 0:
            raise ValueError("Value is too small to represent in API output.")
        if result < 0 or (result == 0 and not allow_zero):
            raise ValueError("Non-positive value.")
    except (InvalidOperation, ValueError, OverflowError) as exc:
        requirement = "nonnegative" if allow_zero else "positive"
        raise ContractPlanError(
            code, f"{field} must be finite and {requirement}."
        ) from exc
    return result


def _metadata_number(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    return _number(
        value,
        field,
        code="missing_contract_metadata" if value is None else "invalid_contract_metadata",
        allow_zero=allow_zero,
    )


def _limits(values: list[tuple[str, Any]], *, minimum: bool) -> Decimal | None:
    supplied = [
        _metadata_number(value, name, allow_zero=minimum)
        for name, value in values
        if value is not None
    ]
    if not supplied:
        return None
    return max(supplied) if minimum else min(supplied)


def _json_values(value: Any) -> dict[str, Any]:
    result = {}
    for key, item in asdict(value).items():
        if isinstance(item, Decimal):
            item = float(item)
            if not math.isfinite(item):
                raise ContractPlanError("invalid_entry_plan", f"{key} cannot be represented as a finite number.")
        result[key] = item
    return result


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """Contract quantities are counts; contract_size is base asset per count."""

    symbol: str
    contract_size: Decimal
    lot_size: Decimal
    tick_size: Decimal
    min_contracts: Decimal | None = None
    max_contracts: Decimal | None = None
    min_notional: Decimal | None = None
    max_notional: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ContractPlanError("missing_contract_metadata", "Contract symbol is missing.")
        for field in ("contract_size", "lot_size", "tick_size"):
            object.__setattr__(self, field, _metadata_number(getattr(self, field), field))
        for field in ("min_contracts", "max_contracts", "min_notional", "max_notional"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(
                    self,
                    field,
                    _metadata_number(value, field, allow_zero=field.startswith("min_")),
                )
        object.__setattr__(self, "min_contracts", max(self.min_contracts or Decimal(0), self.lot_size))
        if self.max_contracts is not None and self.max_contracts < self.min_contracts:
            raise ContractPlanError(
                "invalid_contract_metadata", "Maximum contract count is below the minimum."
            )
        if (
            self.min_notional is not None
            and self.max_notional is not None
            and self.max_notional < self.min_notional
        ):
            raise ContractPlanError(
                "invalid_contract_metadata", "Maximum notional is below the minimum."
            )

    @classmethod
    def from_market(cls, market: Mapping[str, Any]) -> ContractSpec:
        if not isinstance(market, Mapping):
            raise ContractPlanError("missing_contract_metadata", "Contract metadata is missing.")
        if market.get("inverse") is True or market.get("linear") is False:
            raise ContractPlanError("invalid_contract_metadata", "Only linear contracts are supported.")
        info = market.get("info")
        info = info if isinstance(info, Mapping) else {}
        limits = market.get("limits")
        limits = limits if isinstance(limits, Mapping) else {}
        amount = limits.get("amount")
        amount = amount if isinstance(amount, Mapping) else {}
        cost = limits.get("cost")
        cost = cost if isinstance(cost, Mapping) else {}
        size = market.get("contractSize")
        if size is None:
            size = info.get("multiplier")
        contract_size = _metadata_number(size, "contractSize/multiplier")
        if market.get("contractSize") is not None and info.get("multiplier") is not None:
            multiplier = _metadata_number(info["multiplier"], "multiplier")
            if multiplier != contract_size:
                raise ContractPlanError(
                    "invalid_contract_metadata", "contractSize and multiplier disagree."
                )
        # CCXT precision may mean decimal digits or a step depending on the
        # exchange. KuCoin's explicit increments avoid guessing that mode.
        lot_size = _metadata_number(info.get("lotSize"), "lotSize")
        tick_size = _metadata_number(info.get("tickSize"), "tickSize")
        return cls(
            symbol=market.get("symbol") or market.get("id") or "",
            contract_size=contract_size,
            lot_size=lot_size,
            tick_size=tick_size,
            min_contracts=_limits(
                [("limits.amount.min", amount.get("min")), ("minOrderQty", info.get("minOrderQty"))],
                minimum=True,
            ),
            max_contracts=_limits(
                [
                    ("limits.amount.max", amount.get("max")),
                    ("maxOrderQty", info.get("maxOrderQty")),
                    ("marketMaxOrderQty", info.get("marketMaxOrderQty")),
                ],
                minimum=False,
            ),
            min_notional=_limits([("limits.cost.min", cost.get("min"))], minimum=True),
            max_notional=_limits([("limits.cost.max", cost.get("max"))], minimum=False),
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_values(self)


@dataclass(frozen=True, slots=True)
class ContractPlan:
    side: str
    contracts: Decimal
    base_quantity: Decimal
    contract_size: Decimal
    lot_size: Decimal
    tick_size: Decimal
    trigger_price: Decimal
    entry_low: Decimal
    entry_high: Decimal
    reference_entry: Decimal
    sizing_entry: Decimal
    stop_loss: Decimal
    take_profit_1: Decimal
    take_profit_2: Decimal
    take_profit_3: Decimal
    notional_entry_price: Decimal
    notional_value: Decimal
    notional_budget_amount: Decimal
    target_risk_amount: Decimal
    risk_budget_amount: Decimal
    price_risk_amount: Decimal
    estimated_round_trip_cost: Decimal
    actual_risk_amount: Decimal
    estimated_cost_rate_per_side: Decimal
    net_reward_to_risk_1: Decimal
    net_reward_to_risk_2: Decimal
    net_reward_to_risk_3: Decimal

    def to_dict(self) -> dict[str, Any]:
        return _json_values(self)


def _round_step(value: Decimal, step: Decimal, *, up: bool) -> Decimal:
    mode = ROUND_CEILING if up else ROUND_FLOOR
    return (value / step).to_integral_value(rounding=mode) * step


def build_contract_plan(plan: EntryPlan, spec: ContractSpec) -> ContractPlan:
    """Round levels and reduce size without increasing any original budget.

    Risk uses the worst entry-zone price and a conservative round-trip estimate
    inferred from the engine's fee/slippage costs. Notional uses the highest
    entry-zone price for either side. Net R ratios each assume the entire
    position exits at that target; they are not blended partial-exit returns.
    Funding, stop gaps, order-book fills, margin and liquidation are not modeled.
    """
    with localcontext() as context:
        context.prec = 50
        return _build_contract_plan(plan, spec)


def _build_contract_plan(plan: EntryPlan, spec: ContractSpec) -> ContractPlan:
    try:
        side = PositionSide(plan.side)
    except (ValueError, TypeError) as exc:
        raise ContractPlanError("invalid_entry_plan", "Entry side must be long or short.") from exc
    names = (
        "trigger_price", "entry_low", "entry_high", "reference_entry", "stop_loss",
        "take_profit_1", "take_profit_2", "take_profit_3", "target_risk_amount",
        "actual_risk_amount", "position_size", "notional_value", "estimated_round_trip_cost",
    )
    values = {
        name: _number(
            getattr(plan, name), name, code="invalid_entry_plan",
            allow_zero=name == "estimated_round_trip_cost",
        )
        for name in names
    }
    if not values["entry_low"] <= values["reference_entry"] <= values["entry_high"]:
        raise ContractPlanError("invalid_entry_plan", "Reference entry must be inside its entry zone.")
    long = side == PositionSide.LONG
    levels = {
        name: _round_step(values[name], spec.tick_size, up=long)
        for name in ("trigger_price", "entry_low", "entry_high", "reference_entry")
    }
    levels.update({
        name: _round_step(values[name], spec.tick_size, up=not long)
        for name in ("stop_loss", "take_profit_1", "take_profit_2", "take_profit_3")
    })
    low, high, stop = levels["entry_low"], levels["entry_high"], levels["stop_loss"]
    targets = [levels[f"take_profit_{index}"] for index in (1, 2, 3)]
    valid_order = (
        stop < low <= high < targets[0] < targets[1] < targets[2]
        if long else targets[2] < targets[1] < targets[0] < low <= high < stop
    )
    if not valid_order or any(value <= 0 for value in levels.values()):
        raise ContractPlanError(
            "invalid_rounded_levels",
            "Rounded stop, entry zone and targets must remain positive, ordered and distinct.",
        )
    quantity_cap = values["position_size"]
    original_distance = abs(values["reference_entry"] - values["stop_loss"])
    if original_distance == 0:
        raise ContractPlanError("invalid_entry_plan", "Original stop distance must be positive.")
    original_price_risk = quantity_cap * original_distance
    inferred_cost = max(
        values["estimated_round_trip_cost"],
        values["actual_risk_amount"] - original_price_risk,
    )
    cost_rate = inferred_cost / (
        quantity_cap * (values["reference_entry"] + values["stop_loss"])
    )
    sizing_entry = high if long else low
    distance = abs(sizing_entry - stop)
    # For shorts, the lowest entry is worst for price risk, but the highest
    # entry costs more. Combining both bounds cannot understate the estimate.
    cost_per_unit = (high + stop) * cost_rate
    risk_per_unit = distance + cost_per_unit
    risk_budget = min(values["target_risk_amount"], values["actual_risk_amount"])
    notional_budget = values["notional_value"]
    if spec.max_notional is not None:
        notional_budget = min(notional_budget, spec.max_notional)
    quantity_cap = min(quantity_cap, risk_budget / risk_per_unit, notional_budget / high)
    contracts_cap = quantity_cap / spec.contract_size
    if spec.max_contracts is not None:
        contracts_cap = min(contracts_cap, spec.max_contracts)
    contracts = _round_step(contracts_cap, spec.lot_size, up=False)
    quantity = contracts * spec.contract_size
    # Decimal division can round a ratio at its precision boundary. Enforce
    # the original budgets once more after lot rounding, never with a tolerance.
    if (
        quantity * risk_per_unit > risk_budget
        or quantity * high > notional_budget
        or quantity > values["position_size"]
    ):
        contracts -= spec.lot_size
        quantity = contracts * spec.contract_size
    if contracts <= 0 or contracts < spec.min_contracts:
        raise ContractPlanError(
            "below_minimum_contracts", "Risk/notional budget cannot fund the minimum contract lot."
        )
    # Minimum order value must be satisfied throughout the offered entry zone.
    if spec.min_notional is not None and quantity * low < spec.min_notional:
        raise ContractPlanError(
            "below_minimum_notional", "Rounded quantity is below the minimum order value."
        )
    price_risk = quantity * distance
    cost = quantity * cost_per_unit
    actual_risk = price_risk + cost
    direction = Decimal(1) if long else Decimal(-1)
    net_ratios = [
        (direction * (target - sizing_entry) - (high + target) * cost_rate) / risk_per_unit
        for target in targets
    ]
    result = ContractPlan(
        side=side.value,
        contracts=contracts,
        base_quantity=quantity,
        contract_size=spec.contract_size,
        lot_size=spec.lot_size,
        tick_size=spec.tick_size,
        **levels,
        sizing_entry=sizing_entry,
        notional_entry_price=high,
        notional_value=quantity * high,
        notional_budget_amount=notional_budget,
        target_risk_amount=values["target_risk_amount"],
        risk_budget_amount=risk_budget,
        price_risk_amount=price_risk,
        estimated_round_trip_cost=cost,
        actual_risk_amount=actual_risk,
        estimated_cost_rate_per_side=cost_rate,
        net_reward_to_risk_1=net_ratios[0],
        net_reward_to_risk_2=net_ratios[1],
        net_reward_to_risk_3=net_ratios[2],
    )
    if actual_risk > risk_budget or result.notional_value > notional_budget:
        raise ContractPlanError("invalid_entry_plan", "Precision is insufficient to preserve plan budgets.")
    result.to_dict()  # Refuse values that cannot be serialized as finite JSON numbers.
    return result
