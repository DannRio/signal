"""Durable forward paper observations; this module cannot submit exchange orders.

Entries consume a qualified scanner snapshot and its contemporaneous executable
quote. Every entry freezes a full-position stop/target policy (TP1 by default;
TP2/TP3 are explicit alternatives, never inferred partial exits). Quotes are
sampled observations, not proof of the path between observations. Closed-bar
processing is a separate, deliberately conservative model: entry must be on a
bar boundary, bars must be subsequent and contiguous, and stop wins ambiguity.
An unobserved interval makes a position unresolved and keeps its capital/risk
reserved. Funding, depth, margin and liquidation completeness are never implied.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from .futures import ContractSpec
from .storage import SignalStore
from .validation import timeframe_to_timedelta

_ZERO = Decimal(0)
_BPS = Decimal(10000)
_ACTIVE = ("open", "unresolved")
_NUMERIC = frozenset({
    "contracts", "base_quantity", "entry_price", "entry_fee", "stop_loss", "target_price",
    "entry_notional", "reserved_risk", "last_price", "last_notional", "unrealized_gross_pnl",
    "mae", "mfe", "exit_price", "exit_fee", "gross_pnl", "pnl_before_funding",
    "verified_funding_cashflow", "pnl_including_verified_funding",
})


def _utc(value: datetime | str, field: str = "timestamp") -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be an ISO timestamp.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone.")
    return result.astimezone(UTC)


def _decimal(value: Any, field: str, *, zero: bool = False, signed: bool = False) -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError("Missing/boolean numeric value.")
        number = Decimal(str(value))
        if not number.is_finite() or not math.isfinite(float(number)) or (number != 0 and float(number) == 0):
            raise ValueError("Non-finite value.")
        if not signed and (number < 0 or (number == 0 and not zero)):
            raise ValueError("Value is not positive.")
        return number
    except (ValueError, InvalidOperation, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite {'number' if signed else 'nonnegative number' if zero else 'positive number'}.") from exc


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _dump(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return sha256(_dump(value).encode()).hexdigest()


def _rounded(value: Decimal, step: Decimal, *, up: bool) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step


def _public(position: dict[str, Any]) -> dict[str, Any]:
    return {key: float(value) if key in _NUMERIC and value is not None else value for key, value in position.items()}


@dataclass(frozen=True)
class PaperPolicy:
    assumed_equity: float = 10_000.0
    fee_bps: float = 6.0
    slippage_bps: float = 2.0
    target: int = 1
    max_open_positions: int = 5
    maximum_open_risk_percent: float = 2.0
    maximum_gross_notional_multiple: float = 1.0
    maximum_quote_age_seconds: float = 30.0
    maximum_observation_gap_seconds: float = 60.0
    observation_mode: str = "quotes"

    def __post_init__(self) -> None:
        _decimal(self.assumed_equity, "assumed_equity")
        for name in ("fee_bps", "slippage_bps"):
            if _decimal(getattr(self, name), name, zero=True) >= 1000:
                raise ValueError(f"{name} must be below 1000 bps.")
        if isinstance(self.target, bool) or not isinstance(self.target, int) or self.target not in (1, 2, 3):
            raise ValueError("target must be 1, 2 or 3; the entire position exits there.")
        if isinstance(self.max_open_positions, bool) or not isinstance(self.max_open_positions, int) or not 1 <= self.max_open_positions <= 100:
            raise ValueError("max_open_positions must be an integer from 1 through 100.")
        if _decimal(self.maximum_open_risk_percent, "maximum_open_risk_percent") > 100:
            raise ValueError("maximum_open_risk_percent must not exceed 100.")
        if _decimal(self.maximum_gross_notional_multiple, "maximum_gross_notional_multiple") > 5:
            raise ValueError("maximum_gross_notional_multiple must not exceed 5.")
        if _decimal(self.maximum_quote_age_seconds, "maximum_quote_age_seconds") > 300:
            raise ValueError("maximum_quote_age_seconds must not exceed 300.")
        _decimal(self.maximum_observation_gap_seconds, "maximum_observation_gap_seconds")
        if self.observation_mode not in ("quotes", "closed_bars"):
            raise ValueError("observation_mode must be quotes or closed_bars.")
        for name in ("assumed_equity", "fee_bps", "slippage_bps", "maximum_open_risk_percent",
                     "maximum_gross_notional_multiple", "maximum_quote_age_seconds", "maximum_observation_gap_seconds"):
            object.__setattr__(self, name, float(getattr(self, name)))

    @property
    def fingerprint(self) -> str:
        return _hash(asdict(self))


class PaperTracker:
    """One simulated account; all admissions/budgets are serialized in SQLite.

    The account's starting equity is immutable. A different policy may govern
    future events, while existing event and position policy snapshots stay fixed.
    Use a distinct account_id for an independent experiment/equity assumption.
    """

    def __init__(self, store: SignalStore, *, policy: PaperPolicy | None = None,
                 account_id: str = "default", now_factory=None) -> None:
        self.store = store
        self.policy = policy or PaperPolicy()
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 100:
            raise ValueError("account_id must be a nonempty string of at most 100 characters.")
        self.account_id = account_id
        self.now_factory = now_factory or (lambda: datetime.now(UTC))
        self._initialize()

    def _now(self, value: datetime | str | None = None) -> datetime:
        return _utc(value if value is not None else self.now_factory())

    @staticmethod
    def _ensure_schema(store: SignalStore) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS paper_accounts (
                account_id TEXT PRIMARY KEY, assumed_equity TEXT NOT NULL, created_at TEXT NOT NULL,
                policy_json TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS paper_events (
                event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
                decision TEXT NOT NULL, reason TEXT NOT NULL, observed_at TEXT NOT NULL,
                policy_hash TEXT NOT NULL, policy_json TEXT NOT NULL, input_json TEXT NOT NULL,
                position_id TEXT, FOREIGN KEY(account_id) REFERENCES paper_accounts(account_id))""",
            """CREATE TABLE IF NOT EXISTS paper_positions (
                position_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
                status TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES paper_accounts(account_id))""",
            """CREATE INDEX IF NOT EXISTS idx_paper_positions_account_status
                ON paper_positions(account_id,status,symbol)""",
            """CREATE TABLE IF NOT EXISTS paper_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT NOT NULL,
                observation_key TEXT NOT NULL, kind TEXT NOT NULL, observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL, UNIQUE(position_id,observation_key),
                FOREIGN KEY(position_id) REFERENCES paper_positions(position_id))""",
            """CREATE TABLE IF NOT EXISTS paper_funding (
                position_id TEXT NOT NULL, settlement_time TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(position_id,settlement_time), FOREIGN KEY(position_id) REFERENCES paper_positions(position_id))""",
        )
        with store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    connection.execute(statement)
                if "policy_json" not in {row["name"] for row in connection.execute("PRAGMA table_info(paper_accounts)")}:
                    connection.execute("ALTER TABLE paper_accounts ADD COLUMN policy_json TEXT NOT NULL DEFAULT '{}'")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _initialize(self) -> None:
        self._ensure_schema(self.store)
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("INSERT OR IGNORE INTO paper_accounts (account_id,assumed_equity,created_at,policy_json) VALUES (?,?,?,?)",
                                   (self.account_id, str(self.policy.assumed_equity), self._now().isoformat(), _dump(asdict(self.policy))))
                row = connection.execute("SELECT assumed_equity FROM paper_accounts WHERE account_id=?", (self.account_id,)).fetchone()
                if Decimal(row["assumed_equity"]) != _decimal(self.policy.assumed_equity, "assumed_equity"):
                    raise ValueError("This paper account has a different immutable assumed equity; use its original equity or a new account_id.")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @classmethod
    def open_existing(cls, store: SignalStore, *, account_id: str = "default", now_factory=None) -> PaperTracker | None:
        """Read a saved experiment without creating an account with default capital."""
        cls._ensure_schema(store)
        with store._connection() as connection:
            row = connection.execute("SELECT assumed_equity,policy_json FROM paper_accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            return None
        policy = json.loads(row["policy_json"])
        policy["assumed_equity"] = float(Decimal(row["assumed_equity"]))
        return cls(store, policy=PaperPolicy(**policy), account_id=account_id, now_factory=now_factory)

    def process_scan(self, report: Any, *, observed_at: datetime | str | None = None) -> dict[str, Any]:
        """Record each immutable scanner decision and optionally admit a position.

        Exact observation repeats are deduplicated. A later fresh quote can
        qualify a previously skipped candle; its earlier event stays immutable.
        A stable opportunity ID permits only one position for each decision
        candle, even if the signal fingerprint or policy subsequently changes.
        """
        snapshot = _jsonable(report)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("items"), list):
            raise ValueError("report must be a ScanReport or its dictionary representation.")
        now = self._now(observed_at)
        results = []
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._expire_overdue(connection, now)
                for item in snapshot["items"]:
                    results.append(self._process_item(connection, snapshot, item, now))
                for rejection in snapshot.get("rejections", []):
                    identity = {"account": self.account_id, "run": snapshot.get("run_id") or snapshot.get("generated_at"), "rejection": rejection}
                    results.append(self._record_event(connection, _hash(identity), rejection.get("symbol", ""),
                                                       "rejected", rejection.get("code", "universe_rejection"), now,
                                                       {"report_run_id": snapshot.get("run_id"), "rejection": rejection}))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"account_id": self.account_id, "observed_at": now.isoformat(), "events": results,
                "opened": sum(event["decision"] == "opened" and not event["duplicate"] for event in results),
                "duplicate": sum(event["duplicate"] for event in results), "order_execution": False}

    def _record_event(self, connection, event_id, symbol, decision, reason, now, source, position_id=None):
        existing = connection.execute("SELECT * FROM paper_events WHERE event_id=?", (event_id,)).fetchone()
        if existing:
            return {"event_id": event_id, "symbol": existing["symbol"], "decision": existing["decision"],
                    "reason": existing["reason"], "position_id": existing["position_id"], "duplicate": True}
        connection.execute("INSERT INTO paper_events VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (event_id, self.account_id, symbol, decision, reason, now.isoformat(),
                            self.policy.fingerprint, _dump(asdict(self.policy)), _dump(source), position_id))
        connection.execute("UPDATE paper_accounts SET policy_json=? WHERE account_id=?", (_dump(asdict(self.policy)), self.account_id))
        return {"event_id": event_id, "symbol": symbol, "decision": decision, "reason": reason,
                "position_id": position_id, "duplicate": False}

    def _process_item(self, connection, report, item, now):
        candidate = item.get("candidate") or {}
        signal = item.get("signal") or {}
        symbol = candidate.get("symbol", "")
        fingerprint = signal.get("fingerprint")
        try:
            candle = _utc(signal.get("generated_at"), "generated_at").isoformat() if signal else None
        except ValueError:
            candle = None
        identity = {"account": self.account_id, "exchange": report.get("exchange_id"), "market": report.get("market_type"),
                    "decision": {"symbol": symbol, "timeframe": signal.get("execution_timeframe") or report.get("execution_timeframe"), "candle": candle} if candle else fingerprint or {"run": report.get("run_id") or report.get("generated_at"), "symbol": symbol, "error": item.get("error_code")}}
        opportunity_id = _hash(identity)
        event_id = _hash({"opportunity_id": opportunity_id, "policy_hash": self.policy.fingerprint,
                          "signal_fingerprint": fingerprint, "status": item.get("status"),
                          "actionable": item.get("actionable"), "error_code": item.get("error_code"),
                          "valid_until": item.get("valid_until"), "quote_timestamp": candidate.get("quote_timestamp"),
                          "bid": candidate.get("bid"), "ask": candidate.get("ask"), "contract_plan": item.get("contract_plan")})
        existing = connection.execute("SELECT 1 FROM paper_events WHERE event_id=?", (event_id,)).fetchone()
        source = {"opportunity_id": opportunity_id, "report_run_id": report.get("run_id"), "provenance": report.get("provenance", {}), "item": item}
        position_id = f"paper_{opportunity_id[:32]}"

        def record(decision, reason, position_id=None):
            return self._record_event(connection, event_id, symbol, decision, reason, now, source, position_id)

        if existing:
            return record("skipped", "duplicate")
        if connection.execute("SELECT 1 FROM paper_positions WHERE position_id=?", (position_id,)).fetchone():
            return record("skipped", "opportunity_already_entered", position_id)
        if not signal or item.get("status") == "failed":
            return record("rejected", item.get("error_code") or "analysis_failed")
        if not fingerprint or not isinstance(fingerprint, str):
            return record("rejected", "missing_signal_fingerprint")
        if item.get("actionable") is not True or signal.get("action") not in ("long", "short") or item.get("status") != "analyzed":
            return record("skipped", item.get("error_code") or "not_qualified")
        try:
            if _utc(item.get("valid_until"), "valid_until") <= now:
                return record("expired", "signal_expired")
            if _utc(signal.get("generated_at"), "generated_at") > now:
                return record("rejected", "future_signal")
            if not isinstance(report.get("exchange_id"), str) or not report["exchange_id"] or signal.get("symbol") != symbol or report.get("market_type") != "swap" or candidate.get("quote") != "USDT" or not str(symbol).endswith(":USDT"):
                return record("rejected", "inconsistent_market")
            quote_time = _utc(candidate.get("quote_timestamp"), "quote_timestamp")
            bid, ask = self._quote(candidate.get("bid"), candidate.get("ask"), quote_time, now, self.policy)
            plan = item.get("contract_plan")
            if not isinstance(plan, dict) or not isinstance(candidate.get("contract_spec"), dict):
                return record("rejected", "missing_contract_plan")
            spec = ContractSpec(**candidate["contract_spec"])
            if spec.symbol != symbol or plan.get("side") != signal["action"]:
                return record("rejected", "inconsistent_contract_plan")
            side = signal["action"]
            buy = side == "long"
            entry_quote = ask if buy else bid
            entry = self._fill(entry_quote, spec.tick_size, self.policy, buy=buy)
            low, high = _decimal(plan["entry_low"], "entry_low"), _decimal(plan["entry_high"], "entry_high")
            if not low <= entry_quote <= high or not low <= entry <= high:
                return record("skipped", "slippage_outside_entry_zone")
            stop = _decimal(plan["stop_loss"], "stop_loss")
            target = _decimal(plan[f"take_profit_{self.policy.target}"], "target")
            if not (stop < entry < target if buy else target < entry < stop):
                return record("rejected", "invalid_exit_levels")
            if any(price % spec.tick_size != 0 for price in (stop, target)):
                return record("rejected", "invalid_price_increment")
            count = _decimal(plan["contracts"], "contracts")
            if count % spec.lot_size or _decimal(plan["base_quantity"], "base_quantity") != count * spec.contract_size:
                return record("rejected", "invalid_contract_quantity")
            timeframe = signal.get("execution_timeframe") or report.get("execution_timeframe")
            interval = timeframe_to_timedelta(timeframe)
            if now >= _utc(signal["generated_at"]) + interval:
                return record("expired", "decision_candle_expired")
            if self.policy.observation_mode == "closed_bars" and now.timestamp() % interval.total_seconds() != 0:
                return record("skipped", "bar_mode_requires_boundary_entry")
            account = self._account(connection)
            active = account["active"]
            if any(position["status"] == "unresolved" for position in active):
                return record("skipped", "unresolved_portfolio_exposure")
            if any(position["exchange_id"] == report.get("exchange_id") and position["symbol"] == symbol for position in active):
                return record("skipped", "symbol_already_open")
            if len(active) >= self.policy.max_open_positions:
                return record("skipped", "maximum_open_positions")
            risk_budget = min(_decimal(plan["risk_budget_amount"], "risk_budget_amount"),
                              account["budget_equity"] * Decimal(str(self.policy.maximum_open_risk_percent)) / 100 - account["reserved_risk"])
            notional_budget = min(_decimal(plan["notional_budget_amount"], "notional_budget_amount"),
                                  account["budget_equity"] * Decimal(str(self.policy.maximum_gross_notional_multiple)) - account["gross_notional"])
            if risk_budget <= 0 or notional_budget <= 0:
                return record("skipped", "portfolio_budget_exhausted")
            fee = Decimal(str(self.policy.fee_bps)) / _BPS
            stop_fill = self._fill(stop, spec.tick_size, self.policy, buy=not buy)
            risk_per_base = abs(entry - stop_fill) + (entry + stop_fill) * fee
            exit_quote = bid if buy else ask
            # Reserve entry costs/initial spread loss before spending the same
            # assumed equity on a new position. Never count unrealized gains.
            initial_loss = max(_ZERO, (entry - exit_quote) if buy else (exit_quote - entry))
            capital_cost = entry * fee + initial_loss
            portfolio_risk_rate = Decimal(str(self.policy.maximum_open_risk_percent)) / 100
            portfolio_notional_rate = Decimal(str(self.policy.maximum_gross_notional_multiple))
            limits = [count, risk_budget / ((risk_per_base + capital_cost * portfolio_risk_rate) * spec.contract_size),
                      notional_budget / ((max(entry, exit_quote) + capital_cost * portfolio_notional_rate) * spec.contract_size)]
            if spec.max_contracts is not None:
                limits.append(spec.max_contracts)
            if spec.max_notional is not None:
                limits.append(spec.max_notional / (entry * spec.contract_size))
            count = _rounded(min(limits), spec.lot_size, up=False)
            quantity = count * spec.contract_size
            if count < spec.min_contracts or (spec.min_notional is not None and quantity * entry < spec.min_notional):
                return record("skipped", "insufficient_budget_for_minimum_contract")
            target_fill = self._fill(target, spec.tick_size, self.policy, buy=not buy)
            if ((target_fill - entry) if buy else (entry - target_fill)) <= (target_fill + entry) * fee:
                return record("skipped", "target_not_positive_after_costs")
        except (ValueError, KeyError, TypeError) as exc:
            source["validation_error"] = str(exc)
            return record("rejected", "invalid_snapshot")
        position = {
            "position_id": position_id, "event_id": event_id, "opportunity_id": opportunity_id, "account_id": self.account_id,
            "symbol": symbol, "exchange_id": report.get("exchange_id"), "market_type": report.get("market_type"),
            "signal_fingerprint": fingerprint, "scan_run_id": report.get("run_id"),
            "execution_timeframe": timeframe, "side": side, "status": "open", "opened_at": now.isoformat(),
            "last_observed_at": now.isoformat(), "last_quote_timestamp": quote_time.isoformat(),
            "last_bar_close": now.isoformat(), "contracts": str(count), "base_quantity": str(quantity),
            "entry_price": str(entry), "entry_fee": str(entry * quantity * fee),
            "stop_loss": str(stop), "target_price": str(target), "exit_target": self.policy.target,
            "entry_notional": str(entry * quantity), "reserved_risk": str(quantity * risk_per_base),
            "last_price": str(bid if buy else ask), "last_notional": str(entry * quantity),
            "unrealized_gross_pnl": "0", "mae": "0", "mfe": "0",
            "policy": asdict(self.policy), "policy_hash": self.policy.fingerprint, "contract_spec": spec.to_dict(),
            "entry_snapshot": source, "exit_at": None, "exit_price": None, "exit_fee": None,
            "gross_pnl": None, "pnl_before_funding": None, "verified_funding_cashflow": "0",
            "pnl_including_verified_funding": None, "funding_complete": False,
            "funding_status": "missing_settlement_coverage", "outcome_basis": self.policy.observation_mode,
            "limitations": ["Hypothetical fills; order-book depth, margin and liquidation are not modeled.",
                            "Funding is incomplete until verified settlement coverage is supplied.",
                            "Quote observations do not establish the path between samples." if self.policy.observation_mode == "quotes" else "Closed-bar ambiguity uses stop first; spread is not reconstructed from OHLC."],
        }
        self._mark(position, bid if buy else ask)
        self._save_position(connection, position, now, insert=True)
        self._observation(connection, position, "entry", now, {"quote_timestamp": quote_time.isoformat(), "bid": str(bid), "ask": str(ask)})
        return record("opened", "qualified_snapshot", position_id)

    @staticmethod
    def _quote(bid, ask, quote_time, now, policy):
        bid, ask = _decimal(bid, "bid"), _decimal(ask, "ask")
        if bid > ask:
            raise ValueError("Quote is crossed.")
        age = (now - quote_time).total_seconds()
        if age < 0 or age > policy.maximum_quote_age_seconds:
            raise ValueError("Quote is future-dated or stale.")
        return bid, ask

    @staticmethod
    def _fill(price, tick, policy, *, buy):
        factor = Decimal(1) + Decimal(str(policy.slippage_bps)) / _BPS * (1 if buy else -1)
        return _rounded(price * factor, tick, up=buy)

    def _load_positions(self, connection, *, active=False, symbol=None):
        query, params = "SELECT payload_json FROM paper_positions WHERE account_id=?", [self.account_id]
        if active:
            query += " AND status IN ('open','unresolved')"
        if symbol is not None:
            query += " AND symbol=?"
            params.append(symbol)
        return [json.loads(row["payload_json"]) for row in connection.execute(query + " ORDER BY updated_at,position_id", params)]

    def _account(self, connection):
        positions = self._load_positions(connection)
        active = [position for position in positions if position["status"] in _ACTIVE]
        realized = sum((Decimal(position["pnl_before_funding"]) for position in positions if position["status"] == "closed"), _ZERO)
        funding = sum((Decimal(position["verified_funding_cashflow"]) for position in positions), _ZERO)
        fees = sum((Decimal(position["entry_fee"]) for position in active), _ZERO)
        losses = sum((min(_ZERO, Decimal(position["unrealized_gross_pnl"])) for position in active), _ZERO)
        cash_equity = Decimal(str(self.policy.assumed_equity)) + realized + funding - fees
        return {"positions": positions, "active": active, "realized_before_funding": realized, "funding": funding,
                "cash_equity": cash_equity, "budget_equity": max(_ZERO, cash_equity + losses),
                "reserved_risk": sum((Decimal(position["reserved_risk"]) for position in active), _ZERO),
                "gross_notional": sum((max(Decimal(position["entry_notional"]), Decimal(position["last_notional"])) for position in active), _ZERO)}

    def _expire_overdue(self, connection, now):
        for position in self._load_positions(connection, active=True):
            if position["status"] != "open" or position["policy"]["observation_mode"] != "quotes":
                continue
            if (now - _utc(position["last_observed_at"])).total_seconds() > position["policy"]["maximum_observation_gap_seconds"]:
                self._unresolved(position, now, "quote_monitoring_gap")
                self._observation(connection, position, "gap", now, {"reason": "quote_monitoring_gap", "at": now.isoformat()})
                self._save_position(connection, position, now)

    @staticmethod
    def _save_position(connection, position, now, *, insert=False):
        if insert:
            # A paper position can outlive the rolling replay archive. Pin its
            # exact entry evidence in this transaction so archive collection
            # cannot remove it between admission and persistence.
            replay_id = position.get("entry_snapshot", {}).get("item", {}).get("replay_id")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('replay_pins','decision_snapshots')"
            )}
            available = bool(isinstance(replay_id, str) and replay_id and
                             tables == {"replay_pins", "decision_snapshots"} and
                             connection.execute("SELECT 1 FROM decision_snapshots WHERE snapshot_id=?", (replay_id,)).fetchone())
            position["replay_evidence_available"] = available
            if available:
                connection.execute("INSERT OR IGNORE INTO replay_pins (snapshot_id,owner_type,owner_id,pinned_at) VALUES (?,?,?,?)",
                                   (replay_id, "paper_position", position["position_id"], now.isoformat()))
            connection.execute("INSERT INTO paper_positions VALUES (?,?,?,?,?,?)",
                               (position["position_id"], position["account_id"], position["symbol"], position["status"], now.isoformat(), _dump(position)))
        else:
            connection.execute("UPDATE paper_positions SET status=?,updated_at=?,payload_json=? WHERE position_id=?",
                               (position["status"], now.isoformat(), _dump(position), position["position_id"]))

    @staticmethod
    def _observation(connection, position, kind, now, payload):
        connection.execute("INSERT OR IGNORE INTO paper_observations (position_id,observation_key,kind,observed_at,payload_json) VALUES (?,?,?,?,?)",
                           (position["position_id"], _hash({"kind": kind, "payload": payload}), kind, now.isoformat(), _dump(payload)))

    @staticmethod
    def _mark(position, price):
        quantity, entry = Decimal(position["base_quantity"]), Decimal(position["entry_price"])
        pnl = (price - entry) * quantity * (1 if position["side"] == "long" else -1)
        position.update(last_price=str(price), last_notional=str(price * quantity), unrealized_gross_pnl=str(pnl),
                        mae=str(min(Decimal(position["mae"]), pnl)), mfe=str(max(Decimal(position["mfe"]), pnl)))

    def _close(self, position, raw_price, now, reason, *, evidence_time):
        policy = PaperPolicy(**position["policy"])
        price = self._fill(raw_price, Decimal(str(position["contract_spec"]["tick_size"])), policy, buy=position["side"] == "short")
        self._mark(position, price)
        quantity = Decimal(position["base_quantity"])
        gross = Decimal(position["unrealized_gross_pnl"])
        exit_fee = price * quantity * Decimal(str(policy.fee_bps)) / _BPS
        pnl = gross - Decimal(position["entry_fee"]) - exit_fee
        position.update(status="closed", exit_at=evidence_time.isoformat(), exit_observed_at=now.isoformat(),
                        exit_price=str(price), exit_fee=str(exit_fee), gross_pnl=str(gross),
                        pnl_before_funding=str(pnl), pnl_including_verified_funding=str(pnl + Decimal(position["verified_funding_cashflow"])),
                        exit_reason=reason, last_observed_at=now.isoformat())

    @staticmethod
    def _unresolved(position, now, reason):
        position.update(status="unresolved", unresolved_at=now.isoformat(), unresolved_reason=reason)

    def observe_quote(self, symbol: str, *, bid: Any, ask: Any, quote_timestamp: datetime | str,
                      observed_at: datetime | str | None = None) -> list[dict[str, Any]]:
        """Apply an observed executable quote, never a retroactive candle high/low."""
        now, quote_time = self._now(observed_at), _utc(quote_timestamp, "quote_timestamp")
        outcomes = []
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for position in self._load_positions(connection, active=True, symbol=symbol):
                    if position["status"] != "open" or position["policy"]["observation_mode"] != "quotes":
                        continue
                    policy = PaperPolicy(**position["policy"])
                    current_bid, current_ask = self._quote(bid, ask, quote_time, now, policy)
                    if quote_time <= _utc(position["last_quote_timestamp"]) or quote_time <= _utc(position["opened_at"]):
                        continue
                    if now < _utc(position["last_observed_at"]):
                        raise ValueError("Observation time precedes the last observation.")
                    gap = max((now - _utc(position["last_observed_at"])).total_seconds(),
                              (quote_time - _utc(position["last_quote_timestamp"])).total_seconds())
                    evidence = {"quote_timestamp": quote_time.isoformat(), "bid": str(current_bid), "ask": str(current_ask)}
                    if gap > policy.maximum_observation_gap_seconds:
                        self._unresolved(position, now, "quote_monitoring_gap")
                    else:
                        price = current_bid if position["side"] == "long" else current_ask
                        self._mark(position, price)
                        position.update(last_observed_at=now.isoformat(), last_quote_timestamp=quote_time.isoformat())
                        buy = position["side"] == "long"
                        stop, target = Decimal(position["stop_loss"]), Decimal(position["target_price"])
                        if price <= stop if buy else price >= stop:
                            self._close(position, price, now, "observed_stop", evidence_time=quote_time)
                        elif price >= target if buy else price <= target:
                            self._close(position, price, now, "observed_target", evidence_time=quote_time)
                    self._observation(connection, position, "quote", now, evidence)
                    self._save_position(connection, position, now)
                    outcomes.append(_public(position))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return outcomes

    def observe_closed_bar(self, symbol: str, *, timeframe: str, open_time: datetime | str,
                           open: Any, high: Any, low: Any, close: Any,
                           observed_at: datetime | str | None = None) -> list[dict[str, Any]]:
        """Future-only contiguous bars. A missing/partial entry interval is unresolved."""
        now, started = self._now(observed_at), _utc(open_time, "open_time")
        interval = timeframe_to_timedelta(timeframe)
        closed = started + interval
        prices = {name: _decimal(value, name) for name, value in (("open", open), ("high", high), ("low", low), ("close", close))}
        if closed > now or started.timestamp() % interval.total_seconds() != 0:
            raise ValueError("Bar must be closed and aligned to its timeframe.")
        if prices["high"] < max(prices.values()) or prices["low"] > min(prices.values()):
            raise ValueError("Invalid OHLC bounds.")
        outcomes = []
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for position in self._load_positions(connection, active=True, symbol=symbol):
                    if position["status"] != "open" or position["policy"]["observation_mode"] != "closed_bars" or position["execution_timeframe"] != timeframe:
                        continue
                    if started < _utc(position["opened_at"]) or closed <= _utc(position["last_bar_close"]):
                        continue
                    if now < _utc(position["last_observed_at"]):
                        raise ValueError("Observation time precedes the last observation.")
                    if started != _utc(position["last_bar_close"]):
                        self._unresolved(position, now, "closed_bar_data_gap")
                    else:
                        buy = position["side"] == "long"
                        stop, target = Decimal(position["stop_loss"]), Decimal(position["target_price"])
                        stop_hit = prices["low"] <= stop if buy else prices["high"] >= stop
                        target_hit = prices["high"] >= target if buy else prices["low"] <= target
                        self._mark(position, prices["open"])
                        if stop_hit:
                            fill = min(stop, prices["open"]) if buy else max(stop, prices["open"])
                            self._close(position, fill, now, "ambiguous_bar_stop" if target_hit else "bar_stop", evidence_time=closed)
                        elif target_hit:
                            self._close(position, target, now, "bar_target", evidence_time=closed)
                        else:
                            self._mark(position, prices["low"])
                            self._mark(position, prices["high"])
                            self._mark(position, prices["close"])
                        if stop_hit or target_hit:
                            position["exit_time_resolution"] = "bar_close; intrabar hit time unknown"
                            position["exit_bar_excursions_unresolved"] = True
                        position.update(last_bar_close=closed.isoformat(), last_observed_at=now.isoformat())
                    self._observation(connection, position, "closed_bar", now, {"open_time": started.isoformat(), "timeframe": timeframe, **prices})
                    self._save_position(connection, position, now)
                    outcomes.append(_public(position))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return outcomes

    def mark_monitoring_gap(self, *, symbol: str | None = None, reason: str = "monitoring_unavailable",
                            observed_at: datetime | str | None = None) -> list[dict[str, Any]]:
        """Explicitly invalidate an uncovered monitoring interval without inventing an exit."""
        now, outcomes = self._now(observed_at), []
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for position in self._load_positions(connection, active=True, symbol=symbol):
                    if position["status"] != "open":
                        continue
                    if now < _utc(position["last_observed_at"]):
                        raise ValueError("Gap time precedes the last observation.")
                    self._unresolved(position, now, reason)
                    self._observation(connection, position, "gap", now, {"reason": reason, "at": now.isoformat()})
                    self._save_position(connection, position, now)
                    outcomes.append(_public(position))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return outcomes

    def apply_funding(self, position_id: str, settlement: Any, *, observed_at: datetime | str | None = None) -> dict[str, Any]:
        """Apply a sourced settlement once. One payment alone does not prove coverage.

        settlement accepts FundingSettlement or a mapping with timestamp, rate,
        mark_price and source. The caller must supply an actual historical
        settlement, never a current/predicted funding-rate snapshot.

        Ownership ends at the last accepted quote or contiguous closed bar,
        not the time the settlement is submitted. This establishes ownership
        only in the selected paper observation model; sampled quotes do not
        establish a continuous market path. A later monitoring gap does not
        erase an earlier interval whose ownership was already established.
        """
        event, now = _jsonable(settlement), self._now(observed_at)
        if not isinstance(event, dict) or not str(event.get("source", "")).strip():
            raise ValueError("Funding settlement requires a verifiable source.")
        timestamp = _utc(event.get("timestamp"), "funding timestamp")
        rate, mark = _decimal(event.get("rate"), "funding rate", signed=True), _decimal(event.get("mark_price"), "funding mark price")
        if timestamp > now:
            raise ValueError("Future funding settlements cannot be applied.")
        ownership_error = None
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT payload_json FROM paper_positions WHERE position_id=? AND account_id=?", (position_id, self.account_id)).fetchone()
                if row is None:
                    raise ValueError("Unknown paper position.")
                position = json.loads(row["payload_json"])
                if now < _utc(position["last_observed_at"]):
                    raise ValueError("Funding observation time precedes the last observation.")
                if timestamp <= _utc(position["opened_at"]) or (position["exit_at"] and timestamp > _utc(position["exit_at"])):
                    raise ValueError("Funding settlement is outside the known holding interval.")
                payload = {"timestamp": timestamp.isoformat(), "rate": str(rate), "mark_price": str(mark), "source": str(event["source"])}
                existing = connection.execute("SELECT payload_json FROM paper_funding WHERE position_id=? AND settlement_time=?", (position_id, timestamp.isoformat())).fetchone()
                if existing and json.loads(existing["payload_json"]) != payload:
                    raise ValueError("Conflicting funding settlement for the same timestamp.")

                quote_mode = position["policy"]["observation_mode"] == "quotes"
                observed_through = _utc(position["last_quote_timestamp"] if quote_mode else position["last_bar_close"])
                if (position["status"] == "open" and quote_mode and
                        (now - _utc(position["last_observed_at"])).total_seconds() > position["policy"]["maximum_observation_gap_seconds"]):
                    self._unresolved(position, now, "quote_monitoring_gap")
                    self._observation(connection, position, "gap", now, {"reason": "quote_monitoring_gap", "at": now.isoformat()})
                    self._save_position(connection, position, now)

                if position.get("exit_time_resolution") and timestamp > _utc(position["last_bar_close"]) - timeframe_to_timedelta(position["execution_timeframe"]):
                    ownership_error = "Intrabar exit timing cannot establish funding ownership."
                elif timestamp > observed_through:
                    ownership_error = ("Unresolved exposure cannot establish settlement ownership beyond the last observation."
                                       if position["status"] == "unresolved" else
                                       "Funding settlement exceeds observed ownership coverage; observe the holding interval first.")

                if ownership_error is None:
                    if existing:
                        result = {"position_id": position_id, "duplicate": True, "funding_complete": position["funding_complete"]}
                    else:
                        cashflow = -Decimal(1 if position["side"] == "long" else -1) * Decimal(position["base_quantity"]) * mark * rate
                        connection.execute("INSERT INTO paper_funding VALUES (?,?,?)", (position_id, timestamp.isoformat(), _dump(payload)))
                        position["verified_funding_cashflow"] = str(Decimal(position["verified_funding_cashflow"]) + cashflow)
                        position["funding_status"] = "partial_verified_settlements; coverage incomplete"
                        if position["pnl_before_funding"] is not None:
                            position["pnl_including_verified_funding"] = str(Decimal(position["pnl_before_funding"]) + Decimal(position["verified_funding_cashflow"]))
                        self._save_position(connection, position, now)
                        result = {"position_id": position_id, "duplicate": False, "cashflow": float(cashflow), "funding_complete": False}
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if ownership_error is not None:
            # Persist a discovered monitoring gap, but never its unverified
            # funding credit/debit. Raising inside the transaction would undo
            # the unresolved exposure and leave a misleading open position.
            raise ValueError(ownership_error)
        return result

    def positions(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError("limit must be an integer from 1 through 5000.")
        if status not in (None, "open", "closed", "unresolved"):
            raise ValueError("Unknown paper position status.")
        with self.store._connection() as connection:
            positions = self._load_positions(connection)
        return [_public(position) for position in reversed(positions) if status is None or position["status"] == status][:limit]

    def events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError("limit must be an integer from 1 through 5000.")
        with self.store._connection() as connection:
            rows = connection.execute("SELECT * FROM paper_events WHERE account_id=? ORDER BY observed_at DESC,event_id DESC LIMIT ?", (self.account_id, limit)).fetchall()
        return [{"event_id": row["event_id"], "symbol": row["symbol"], "decision": row["decision"], "reason": row["reason"],
                 "observed_at": row["observed_at"], "policy_hash": row["policy_hash"], "policy": json.loads(row["policy_json"]),
                 "position_id": row["position_id"], "input_snapshot": json.loads(row["input_json"])} for row in rows]

    def observations(self, position_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError("limit must be an integer from 1 through 5000.")
        with self.store._connection() as connection:
            rows = connection.execute("SELECT o.* FROM paper_observations o JOIN paper_positions p USING(position_id) WHERE p.account_id=? AND o.position_id=? ORDER BY o.id DESC LIMIT ?", (self.account_id, position_id, limit)).fetchall()
        return [{"kind": row["kind"], "observed_at": row["observed_at"], "payload": json.loads(row["payload_json"])} for row in rows]

    def summary(self, *, observed_at: datetime | str | None = None) -> dict[str, Any]:
        now = self._now(observed_at)
        with self.store._connection() as connection:
            account = self._account(connection)
            counts = {row["decision"]: row["n"] for row in connection.execute("SELECT decision,COUNT(*) AS n FROM paper_events WHERE account_id=? GROUP BY decision", (self.account_id,))}
        positions, active = account["positions"], account["active"]
        closed = [position for position in positions if position["status"] == "closed"]
        overdue = [position for position in active if position["status"] == "open" and position["policy"]["observation_mode"] == "quotes" and (now - _utc(position["last_observed_at"])).total_seconds() > position["policy"]["maximum_observation_gap_seconds"]]
        return {"account_id": self.account_id, "assumed_equity": self.policy.assumed_equity,
                "cash_equity_including_verified_funding": float(account["cash_equity"]),
                "conservative_budget_equity": float(account["budget_equity"]),
                "open_positions": sum(position["status"] == "open" for position in active),
                "unresolved_positions": sum(position["status"] == "unresolved" for position in active),
                "overdue_observations": len(overdue), "closed_positions": len(closed), "event_counts": counts,
                "reserved_risk": float(account["reserved_risk"]), "gross_notional_reserved": float(account["gross_notional"]),
                "closed_pnl_before_funding": float(account["realized_before_funding"]),
                "verified_funding_cashflow": float(account["funding"]),
                "wins_before_funding": sum(Decimal(position["pnl_before_funding"]) > 0 for position in closed),
                "funding_complete": False, "order_execution": False, "live_ready": False,
                "policy": asdict(self.policy), "policy_hash": self.policy.fingerprint,
                "limitations": ["Forward paper observations are hypothetical, not actual exchange fills.",
                                "Missing monitoring periods retain unresolved exposure; no exit is invented.",
                                "PnL before funding excludes unknown settlement costs; supplied funding alone does not prove complete coverage.",
                                "Depth, portfolio correlation, maintenance margin and liquidation are not modeled."]}
