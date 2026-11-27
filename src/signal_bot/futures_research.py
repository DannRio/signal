"""Explicit inputs for linear USDT futures research, never live execution.

Funding evidence includes a supplied historical settlement schedule and a mark
valuation at every settlement. A rate-only download is intentionally insufficient.
The fixed contract specification is an acknowledged assumption, not a claim that
today's instrument rules held throughout the historical period.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from typing import Any

import pandas as pd

from .futures import ContractSpec
from .models import PositionSide
from .validation import (
    REQUIRED_COLUMNS,
    DataValidationError,
    prepare_ohlcv,
    timeframe_to_timedelta,
    validate_live_window,
)
from .venue import normalize_symbol


class FuturesResearchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def decimal_value(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError("Not a number")
        number = Decimal(str(value))
        if (
            not number.is_finite()
            or not math.isfinite(float(number))
            or number != 0
            and float(number) == 0
        ):
            raise ValueError("Not finite")
        if positive and number <= 0:
            raise ValueError("Not positive")
    except (ValueError, InvalidOperation, OverflowError) as exc:
        raise FuturesResearchError(
            "invalid_futures_evidence",
            f"{name} must be finite" + (" and positive." if positive else "."),
        ) from exc
    return number


def utc_timestamp(value: Any, name: str) -> datetime:
    try:
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, str):
            result = pd.Timestamp(value)
            if result.nanosecond:
                raise ValueError("Sub-microsecond precision is unsupported")
            result = result.to_pydatetime()
        else:
            raise ValueError("Use an aware datetime or ISO timestamp")
        if result.tzinfo is None or pd.isna(result):
            raise ValueError("Timezone is required")
        return result.astimezone(UTC)
    except (ValueError, TypeError, OverflowError) as exc:
        raise FuturesResearchError(
            "invalid_futures_evidence",
            f"{name} must be a timezone-aware ISO timestamp.",
        ) from exc


def _source(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuturesResearchError(
            "missing_futures_evidence",
            f"{name} must identify the supplied evidence source.",
        )
    return value.strip()


def _fields(
    payload: Mapping[str, Any], required: set[str], optional: set[str] | None = None
) -> None:
    if not isinstance(payload, Mapping):
        raise FuturesResearchError(
            "invalid_futures_evidence", "Futures evidence must be a JSON object."
        )
    missing = required - set(payload)
    unknown = set(payload) - required - (optional or set())
    if missing or unknown:
        raise FuturesResearchError(
            "invalid_futures_evidence",
            f"Futures evidence fields: missing={sorted(missing)}, unknown={sorted(unknown)}.",
        )


@dataclass(frozen=True)
class FundingSettlement:
    timestamp: datetime
    rate: Decimal
    mark_price: Decimal
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "timestamp", utc_timestamp(self.timestamp, "funding timestamp")
        )
        object.__setattr__(self, "rate", decimal_value(self.rate, "funding rate"))
        object.__setattr__(
            self,
            "mark_price",
            decimal_value(self.mark_price, "historical mark_price", positive=True),
        )
        object.__setattr__(self, "source", _source(self.source, "funding event source"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FundingSettlement:
        _fields(payload, {"timestamp", "rate", "mark_price", "source"})
        return cls(**dict(payload))

    def cashflow(self, side: PositionSide | str, base_quantity: Any) -> Decimal:
        quantity = decimal_value(base_quantity, "remaining base quantity")
        if quantity < 0:
            raise FuturesResearchError(
                "invalid_futures_evidence",
                "Remaining base quantity cannot be negative.",
            )
        direction = (
            Decimal(1) if PositionSide(side) == PositionSide.LONG else Decimal(-1)
        )
        with localcontext() as context:
            context.prec = 48
            payment = -direction * quantity * self.mark_price * self.rate
        decimal_value(payment, "funding cashflow")
        return payment

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "rate": str(self.rate),
            "mark_price": str(self.mark_price),
            "source": self.source,
        }


@dataclass(frozen=True)
class FundingHistory:
    coverage_start: datetime
    coverage_end: datetime
    expected_settlement_timestamps: tuple[datetime, ...]
    events: tuple[FundingSettlement, ...]
    source: str
    schedule_source: str

    def __post_init__(self) -> None:
        start = utc_timestamp(self.coverage_start, "coverage_start")
        end = utc_timestamp(self.coverage_end, "coverage_end")
        if end <= start:
            raise FuturesResearchError(
                "invalid_funding_coverage",
                "Funding coverage_end must follow coverage_start.",
            )
        expected = tuple(
            utc_timestamp(value, "expected settlement timestamp")
            for value in self.expected_settlement_timestamps
        )
        if len(expected) != len(set(expected)):
            raise FuturesResearchError(
                "duplicate_funding_settlement",
                "Expected settlement schedule contains duplicates.",
            )
        if any(not start <= value <= end for value in expected):
            raise FuturesResearchError(
                "invalid_funding_coverage",
                "Expected settlement lies outside the declared coverage.",
            )
        events = tuple(self.events)
        if any(not isinstance(event, FundingSettlement) for event in events):
            raise FuturesResearchError(
                "invalid_futures_evidence",
                "Funding events must be validated FundingSettlement values.",
            )
        actual = tuple(event.timestamp for event in events)
        if len(actual) != len(set(actual)):
            raise FuturesResearchError(
                "duplicate_funding_settlement",
                "Funding events contain duplicate settlement timestamps.",
            )
        if set(actual) != set(expected):
            raise FuturesResearchError(
                "incomplete_funding_coverage",
                "Actual funding events must match every expected settlement, including zero-rate events; missing or unexpected events cannot be treated as zero funding.",
            )
        object.__setattr__(self, "coverage_start", start)
        object.__setattr__(self, "coverage_end", end)
        object.__setattr__(
            self, "expected_settlement_timestamps", tuple(sorted(expected))
        )
        object.__setattr__(
            self, "events", tuple(sorted(events, key=lambda event: event.timestamp))
        )
        object.__setattr__(self, "source", _source(self.source, "funding source"))
        object.__setattr__(
            self,
            "schedule_source",
            _source(self.schedule_source, "historical settlement schedule source"),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FundingHistory:
        _fields(
            payload,
            {
                "coverage_start",
                "coverage_end",
                "expected_settlement_timestamps",
                "events",
                "source",
                "schedule_source",
            },
        )
        for name in ("expected_settlement_timestamps", "events"):
            if not isinstance(payload[name], Sequence) or isinstance(
                payload[name], (str, bytes)
            ):
                raise FuturesResearchError(
                    "invalid_futures_evidence", f"{name} must be an explicit array."
                )
        return cls(
            coverage_start=payload["coverage_start"],
            coverage_end=payload["coverage_end"],
            expected_settlement_timestamps=tuple(
                payload["expected_settlement_timestamps"]
            ),
            events=tuple(
                FundingSettlement.from_dict(event) for event in payload["events"]
            ),
            source=payload["source"],
            schedule_source=payload["schedule_source"],
        )

    def events_for_window(
        self, start: datetime, end: datetime, bar_opens: Sequence[datetime]
    ) -> dict[datetime, FundingSettlement]:
        start = utc_timestamp(start, "research start")
        end = utc_timestamp(end, "research end")
        if self.coverage_start > start or self.coverage_end < end:
            raise FuturesResearchError(
                "incomplete_funding_coverage",
                "Funding evidence must cover the entire research window, including its terminal close.",
            )
        allowed_times = {utc_timestamp(value, "bar open") for value in bar_opens} | {
            end
        }
        events = {
            event.timestamp: event
            for event in self.events
            if start <= event.timestamp <= end
        }
        if any(timestamp not in allowed_times for timestamp in events):
            raise FuturesResearchError(
                "unaligned_funding_settlement",
                "A funding settlement falls inside an execution candle. Supply finer execution candles; OHLC bars cannot establish the remaining position at that exact intrabar instant.",
            )
        return events

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "expected_settlement_timestamps": [
                value.isoformat() for value in self.expected_settlement_timestamps
            ],
            "events": [event.to_dict() for event in self.events],
            "source": self.source,
            "schedule_source": self.schedule_source,
        }


@dataclass(frozen=True)
class FuturesResearchInputs:
    contract_spec: ContractSpec
    contract_spec_source: str
    contract_spec_assumption: str
    funding: FundingHistory

    def __post_init__(self) -> None:
        if not isinstance(self.contract_spec, ContractSpec) or not isinstance(
            self.funding, FundingHistory
        ):
            raise FuturesResearchError(
                "invalid_futures_evidence",
                "A validated contract spec and funding history are required together.",
            )
        if normalize_symbol(self.contract_spec.symbol) != self.contract_spec.symbol:
            raise FuturesResearchError(
                "invalid_futures_evidence",
                "Contract spec must use canonical linear USDT perpetual notation.",
            )
        object.__setattr__(
            self,
            "contract_spec_source",
            _source(self.contract_spec_source, "contract_spec_source"),
        )
        if self.contract_spec_assumption != "fixed_over_backtest_window":
            raise FuturesResearchError(
                "unacknowledged_contract_assumption",
                "Explicitly acknowledge contract_spec_assumption='fixed_over_backtest_window'; historical contract-rule changes are not reconstructed.",
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FuturesResearchInputs:
        _fields(
            payload,
            {
                "contract_spec",
                "contract_spec_source",
                "contract_spec_assumption",
                "funding",
            },
            {"schema_version"},
        )
        if (
            isinstance(payload.get("schema_version", 1), bool)
            or payload.get("schema_version", 1) != 1
        ):
            raise FuturesResearchError(
                "invalid_futures_evidence",
                "Unsupported futures evidence schema_version.",
            )
        spec = payload["contract_spec"]
        _fields(
            spec,
            {"symbol", "contract_size", "lot_size", "tick_size"},
            {"min_contracts", "max_contracts", "min_notional", "max_notional"},
        )
        return cls(
            contract_spec=ContractSpec(**dict(spec)),
            contract_spec_source=payload["contract_spec_source"],
            contract_spec_assumption=payload["contract_spec_assumption"],
            funding=FundingHistory.from_dict(payload["funding"]),
        )

    def to_dict(self) -> dict[str, Any]:
        spec = self.contract_spec
        return {
            "schema_version": 1,
            "contract_spec": {
                name: str(getattr(spec, name))
                if isinstance(getattr(spec, name), Decimal)
                else getattr(spec, name)
                for name in (
                    "symbol",
                    "contract_size",
                    "lot_size",
                    "tick_size",
                    "min_contracts",
                    "max_contracts",
                    "min_notional",
                    "max_notional",
                )
            },
            "contract_spec_source": self.contract_spec_source,
            "contract_spec_assumption": self.contract_spec_assumption,
            "funding": self.funding.to_dict(),
        }

    def metadata(self, *, settlements_in_window: int) -> dict[str, Any]:
        payload = self.to_dict()
        return {
            "mode": "linear_futures_with_supplied_funding",
            "funding_modeled": True,
            "contract_rounding_modeled": True,
            "funding_coverage_basis": "caller_supplied_expected_schedule_matched_to_actual_events",
            "funding_coverage_start": self.funding.coverage_start.isoformat(),
            "funding_coverage_end": self.funding.coverage_end.isoformat(),
            "funding_source": self.funding.source,
            "funding_schedule_source": self.funding.schedule_source,
            "settlements_in_window": settlements_in_window,
            "contract_spec": payload["contract_spec"],
            "contract_spec_source": self.contract_spec_source,
            "contract_spec_assumption": self.contract_spec_assumption,
            "inputs_sha256": hashlib.sha256(
                json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
            ).hexdigest(),
            "funding_boundary_policy": "carried_position_settles_before_same_timestamp_orders_including_terminal_close",
            "partial_exit_policy": "round_down_to_lots_skip_unplaceable_partial_without_moving_stop_split_large_closes",
            "margin_and_liquidation_modeled": False,
            "limitations": [
                "Contract rules are assumed fixed for this window; historical specification changes are not reconstructed.",
                "Funding completeness relies on supplied historical schedule evidence; source authenticity is not independently certified.",
                "Same-timestamp funding precedes orders on carried positions; exact exchange settlement/execution sequencing is not reconstructed.",
                "Intrabar ownership at funding times is unsupported and rejected; use finer execution bars.",
                "Maintenance margin, leverage/margin mode, liquidation, ADL and order-book capacity remain unmodeled.",
                "Risk sizing includes modeled stop execution costs but cannot cap gap losses or unknown future funding.",
            ],
            "live_ready": False,
        }


def parse_futures_inputs(
    value: FuturesResearchInputs | Mapping[str, Any] | None,
) -> FuturesResearchInputs | None:
    return (
        value
        if value is None or isinstance(value, FuturesResearchInputs)
        else FuturesResearchInputs.from_dict(value)
    )


def round_step(value: Decimal, step: Decimal, *, up: bool) -> Decimal:
    with localcontext() as context:
        context.prec = 48
        return (value / step).to_integral_value(
            rounding=ROUND_CEILING if up else ROUND_FLOOR
        ) * step


def rounded_fill(
    price: Any,
    side: PositionSide,
    *,
    is_entry: bool,
    slippage_rate: Any,
    spec: ContractSpec,
) -> Decimal:
    value = decimal_value(price, "fill price", positive=True)
    slippage = decimal_value(slippage_rate, "slippage rate")
    if not 0 <= slippage < 1:
        raise FuturesResearchError(
            "invalid_futures_evidence", "Slippage rate must be in [0, 1)."
        )
    buying = (side == PositionSide.LONG) == is_entry
    with localcontext() as context:
        context.prec = 48
        result = round_step(
            value * (Decimal(1) + slippage if buying else Decimal(1) - slippage),
            spec.tick_size,
            up=buying,
        )
    return decimal_value(result, "rounded fill", positive=True)


def contract_bounds(
    spec: ContractSpec, price: Decimal
) -> tuple[Decimal, Decimal | None]:
    minimum = spec.min_contracts or spec.lot_size
    maximum = spec.max_contracts
    with localcontext() as context:
        context.prec = 48
        if spec.min_notional is not None:
            minimum = max(minimum, spec.min_notional / (price * spec.contract_size))
        if spec.max_notional is not None:
            notional_maximum = spec.max_notional / (price * spec.contract_size)
            maximum = (
                min(maximum, notional_maximum)
                if maximum is not None
                else notional_maximum
            )
        return round_step(minimum, spec.lot_size, up=True), round_step(
            maximum, spec.lot_size, up=False
        ) if maximum is not None else None


def entry_contracts(
    spec: ContractSpec,
    *,
    entry: Decimal,
    stop_fill: Decimal,
    equity: Any,
    risk_percent: Any,
    notional_multiple: Any,
    fee_rate: Any,
) -> tuple[Decimal, Decimal]:
    with localcontext() as context:
        context.prec = 48
        equity_value = decimal_value(equity, "equity", positive=True)
        fee = decimal_value(fee_rate, "fee rate")
        risk_per_unit = abs(entry - stop_fill) + (entry + stop_fill) * fee
        if risk_per_unit <= 0:
            raise FuturesResearchError(
                "invalid_contract_entry",
                "Rounded entry has no positive modeled stop risk.",
            )
        risk_budget = (
            equity_value
            * decimal_value(risk_percent, "risk percent", positive=True)
            / 100
        )
        notional_budget = equity_value * decimal_value(
            notional_multiple, "notional multiple", positive=True
        )
        maximum_quantity = min(risk_budget / risk_per_unit, notional_budget / entry)
        minimum, maximum = contract_bounds(spec, entry)
        count = round_step(
            maximum_quantity / spec.contract_size, spec.lot_size, up=False
        )
        if maximum is not None:
            count = min(count, maximum)
        if count < minimum:
            return Decimal(0), risk_per_unit
        # Decimal division can round at a precision boundary. Never allow that
        # last rounding bit to exceed either declared sizing budget.
        if (
            count * spec.contract_size * risk_per_unit > risk_budget
            or count * spec.contract_size * entry > notional_budget
        ):
            count -= spec.lot_size
        if count < minimum:
            return Decimal(0), risk_per_unit
        if (
            count * spec.contract_size * risk_per_unit > risk_budget
            or count * spec.contract_size * entry > notional_budget
        ):
            raise FuturesResearchError(
                "invalid_contract_entry",
                "Cannot preserve sizing budgets at the supplied precision.",
            )
        return count, risk_per_unit


def closing_chunks(
    spec: ContractSpec, contracts: Decimal, price: Decimal
) -> tuple[Decimal, ...]:
    """Split an exact close into valid orders, preserving a valid final remainder."""
    minimum, maximum = contract_bounds(spec, price)
    if contracts <= 0 or contracts % spec.lot_size != 0 or contracts < minimum:
        raise FuturesResearchError(
            "unplaceable_contract_exit",
            "Close quantity is below the supplied minimum or is not a lot multiple.",
        )
    maximum = min(maximum, contracts) if maximum is not None else contracts
    if maximum < minimum:
        raise FuturesResearchError(
            "unplaceable_contract_exit",
            "No close quantity satisfies the supplied minimum and maximum limits at this price.",
        )
    chunks: list[Decimal] = []
    remaining = contracts
    while remaining > maximum:
        if len(chunks) >= 10_000:
            raise FuturesResearchError(
                "unplaceable_contract_exit",
                "Close would require more than 10000 child fills under the supplied limits.",
            )
        chunk = maximum
        if remaining - chunk < minimum:
            chunk = round_step(remaining - minimum, spec.lot_size, up=False)
        if chunk < minimum:
            raise FuturesResearchError(
                "unplaceable_contract_exit",
                "The supplied order limits cannot close this position without an invalid remainder.",
            )
        chunks.append(chunk)
        remaining -= chunk
    if remaining < minimum:
        raise FuturesResearchError(
            "unplaceable_contract_exit",
            "The final close quantity is below the supplied minimum.",
        )
    return tuple([*chunks, remaining])


def prepare_futures_frame(
    raw: pd.DataFrame, timeframe: str, *, minimum_bars: int
) -> pd.DataFrame:
    if not isinstance(raw, pd.DataFrame) or any(
        column not in raw.columns for column in REQUIRED_COLUMNS
    ):
        return prepare_ohlcv(raw, minimum_bars=minimum_bars)
    canonical = raw.loc[:, REQUIRED_COLUMNS].copy()
    canonical["timestamp"] = pd.to_datetime(
        canonical["timestamp"], utc=True, errors="coerce"
    )
    for name in REQUIRED_COLUMNS[1:]:
        canonical[name] = pd.to_numeric(canonical[name], errors="coerce")
    duplicate = canonical.loc[canonical["timestamp"].duplicated(keep=False)]
    if (
        not duplicate.empty
        and (
            duplicate.groupby("timestamp", dropna=False)[
                list(REQUIRED_COLUMNS[1:])
            ].nunique(dropna=False)
            > 1
        )
        .any()
        .any()
    ):
        raise DataValidationError(
            f"{timeframe} history contains conflicting duplicate values.",
            code="conflicting_candle",
        )
    prepared = prepare_ohlcv(canonical, minimum_bars=minimum_bars)
    validate_live_window(
        prepared,
        timeframe,
        now=prepared["timestamp"].max() + timeframe_to_timedelta(timeframe),
        minimum_bars=minimum_bars,
        maximum_staleness_bars=0,
    )
    return prepared
