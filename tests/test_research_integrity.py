from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from test_research import sample_result

from signal_bot.research import (
    ResearchService,
    ValidationThresholds,
    _chronological_windows,
    _compact_result,
    _research_provenance,
)
from signal_bot.validation import DataValidationError


def evidence():
    held_out = _compact_result(sample_result(trades=60))
    return {
        "base_result": sample_result(),
        "oos": {
            "validation": {"result": held_out.copy()},
            "test": {"result": held_out.copy()},
        },
        "cost_stress": [
            {"scope": "test", "cost_multiplier": 3.0, "result": held_out.copy()}
        ],
        "monte_carlo": {
            "scope": "test", "simulations": 200, "trades_per_path": 60,
            "probability_ending_below_start": 0.1,
        },
    }


def check_by_name(gate, name):
    return next(check for check in gate["checks"] if check["name"] == name)


@pytest.mark.parametrize("profit_factor", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_or_zero_profit_factor_cannot_pass(profit_factor):
    inputs = evidence()
    inputs["base_result"] = replace(inputs["base_result"], profit_factor=profit_factor)
    gate = ResearchService().validation_gate(**inputs)
    assert gate["status"] == "FAIL"
    assert not check_by_name(gate, "base_profit_factor")["passed"]
    json.dumps(gate, allow_nan=False)


def test_none_profit_factor_requires_actual_profitable_no_loss_summary():
    inputs = evidence()
    inputs["base_result"] = replace(inputs["base_result"], profit_factor=None)
    gate = ResearchService().validation_gate(**inputs)
    assert gate["status"] == "FAIL"
    assert not check_by_name(gate, "base_profit_factor")["passed"]
    details = tuple(
        replace(trade, net_pnl=abs(trade.net_pnl), r_multiple=abs(trade.r_multiple))
        for trade in inputs["base_result"].trades_detail
    )
    inputs["base_result"] = replace(inputs["base_result"], wins=250, losses=0, trades_detail=details)
    assert ResearchService().validation_gate(**inputs)["status"] == "PASS_FOR_PAPER_VALIDATION"


@pytest.mark.parametrize("missing", ["oos", "cost_stress", "monte_carlo"])
def test_every_evidence_layer_is_mandatory(missing):
    inputs = evidence()
    inputs[missing] = None
    gate = ResearchService().validation_gate(**inputs)
    assert gate["status"] == "INSUFFICIENT_EVIDENCE"
    assert gate["missing_evidence"]
    assert gate["live_ready"] is False


@pytest.mark.parametrize("label", ["validation", "test"])
def test_held_out_sample_size_is_required(label):
    inputs = evidence()
    inputs["oos"][label]["result"]["trades"] = 1
    gate = ResearchService().validation_gate(**inputs)
    assert gate["status"] == "INSUFFICIENT_EVIDENCE"
    assert f"held_out_{label}_minimum_trades" in gate["insufficient_evidence"]


@pytest.mark.parametrize("label", ["validation", "test"])
@pytest.mark.parametrize("metric,value", [
    ("expectancy_r", -0.1), ("profit_factor", 0),
    ("max_drawdown_percent", 50.0), ("total_return_percent", float("nan")),
])
def test_both_holdout_periods_must_pass_metrics(label, metric, value):
    inputs = evidence()
    inputs["oos"][label]["result"][metric] = value
    gate = ResearchService().validation_gate(**inputs)
    assert gate["status"] == "FAIL"
    json.dumps(gate, allow_nan=False)


def test_stress_order_does_not_hide_a_losing_scenario():
    inputs = evidence()
    bad = {**inputs["cost_stress"][0], "result": {
        **inputs["cost_stress"][0]["result"], "expectancy_r": -0.2,
    }}
    good = {**inputs["cost_stress"][0], "cost_multiplier": 1.0}
    inputs["cost_stress"] = [bad, good]
    first = ResearchService().validation_gate(**inputs)
    inputs["cost_stress"].reverse()
    second = ResearchService().validation_gate(**inputs)
    assert first == second
    assert first["status"] == "FAIL"


@pytest.mark.parametrize("metric,check_name", [
    ("total_return_percent", "cost_stress_positive_return"),
    ("net_pnl", "cost_stress_positive_net_pnl"),
])
@pytest.mark.parametrize("value", [-1.0, 0.0, float("nan"), float("inf"), float("-inf")])
def test_positive_stressed_expectancy_cannot_hide_unprofitable_or_invalid_realized_results(metric, check_name, value):
    inputs = evidence()
    inputs["cost_stress"][0]["result"][metric] = value
    assert inputs["cost_stress"][0]["result"]["expectancy_r"] > 0
    gate = ResearchService().validation_gate(**inputs)
    assert gate["status"] == "FAIL"
    assert not check_by_name(gate, check_name)["passed"]
    json.dumps(gate, allow_nan=False)


@pytest.mark.parametrize("metric,check_name", [
    ("total_return_percent", "cost_stress_positive_return"),
    ("net_pnl", "cost_stress_positive_net_pnl"),
])
def test_stressed_realized_results_are_mandatory(metric, check_name):
    inputs = evidence()
    del inputs["cost_stress"][0]["result"][metric]
    gate = ResearchService().validation_gate(**inputs)
    assert gate["status"] == "INSUFFICIENT_EVIDENCE"
    assert check_name in gate["missing_evidence"]


def test_variable_size_losing_stress_scenario_cannot_pass_in_any_row_order():
    inputs = evidence()
    # Thirty +1R trades at 10 risk and thirty -0.5R trades at 100 risk:
    # average R is +0.25, while realized P&L is -1200.
    losing = {**inputs["cost_stress"][0], "result": {
        **inputs["cost_stress"][0]["result"],
        "expectancy_r": 0.25, "net_pnl": -1200.0, "total_return_percent": -12.0,
    }}
    passing = {**inputs["cost_stress"][0], "cost_multiplier": 1.0}
    inputs["cost_stress"] = [losing, passing]
    first = ResearchService().validation_gate(**inputs)
    inputs["cost_stress"].reverse()
    second = ResearchService().validation_gate(**inputs)
    assert first == second
    assert first["status"] == "FAIL"
    assert check_by_name(first, "cost_stress_positive_expectancy")["passed"]
    assert not check_by_name(first, "cost_stress_positive_return")["passed"]
    assert not check_by_name(first, "cost_stress_positive_net_pnl")["passed"]


def test_stress_must_include_a_higher_cost_scenario():
    inputs = evidence()
    inputs["cost_stress"][0]["cost_multiplier"] = 1.0
    gate = ResearchService().validation_gate(**inputs)
    assert gate["status"] == "INSUFFICIENT_EVIDENCE"
    assert "cost_stress_coverage" in gate["insufficient_evidence"]


def test_stress_drawdown_is_not_hidden_by_positive_expectancy():
    inputs = evidence()
    inputs["cost_stress"][0]["result"]["max_drawdown_percent"] = 60
    gate = ResearchService().validation_gate(**inputs)
    assert gate["status"] == "FAIL"
    assert not check_by_name(gate, "cost_stress_drawdown")["passed"]


def test_in_sample_stress_and_bootstrap_do_not_satisfy_oos_evidence():
    inputs = evidence()
    inputs["cost_stress"][0]["scope"] = "full_history"
    inputs["monte_carlo"]["scope"] = "full_history"
    gate = ResearchService().validation_gate(**inputs)
    assert gate["status"] == "INSUFFICIENT_EVIDENCE"
    assert "cost_stress_test_scope" in gate["insufficient_evidence"]
    assert "monte_carlo_test_scope" in gate["insufficient_evidence"]


@pytest.mark.parametrize("kwargs", [
    {"starting_equity": 0}, {"starting_equity": float("inf")},
    {"risk_percent": 0}, {"risk_percent": -1}, {"risk_percent": float("nan")},
    {"simulations": 200.5}, {"simulations": True},
])
def test_monte_carlo_does_not_silently_replace_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        ResearchService().monte_carlo(sample_result(), **kwargs)


def test_monte_carlo_rejects_nonfinite_trade_results():
    result = sample_result(trades=1)
    result = replace(result, trades_detail=(replace(result.trades_detail[0], r_multiple=float("nan")),))
    with pytest.raises(ValueError, match="finite"):
        ResearchService().monte_carlo(result)


def frame(count=1500):
    prices = 100 + np.arange(count) * 0.01
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=count, freq="15min", tz="UTC"),
        "open": prices, "high": prices + 0.2, "low": prices - 0.2,
        "close": prices + 0.01, "volume": np.full(count, 1000.0),
    })


def test_partitions_keep_causal_warmup_without_future_rows():
    candles = frame()
    windows = _chronological_windows({"15m": candles}, "15m", 220, 0.6, 0.2)
    assert [window.bars for window in windows] == [900, 300, 300]
    assert windows[1].start == candles.iloc[900]["timestamp"]
    assert len(windows[1].frames["15m"]) == 1200
    assert windows[1].frames["15m"].iloc[0]["timestamp"] < windows[1].start
    assert all(window.frames["15m"]["timestamp"].max() < window.end for window in windows)


def test_warmup_cannot_open_positions_before_evaluation_start():
    # Real engine and backtester exercise the entry boundary, rather than
    # checking only a slicing helper. This synthetic fixture generates trades.
    from test_regime_backtest import make_repeated_breakouts

    candles = make_repeated_breakouts(cycles=5)
    start = candles.iloc[400]["timestamp"]
    service = ResearchService()
    unbounded = service.backtest(symbol="TEST/USDT", frames={"15m": candles}, execution_timeframe="15m")
    held_out = service.backtest(
        symbol="TEST/USDT", frames={"15m": candles}, execution_timeframe="15m",
        evaluation_start=start,
    )
    assert unbounded.trades > held_out.trades > 0
    assert all(trade.signal_time > start for trade in held_out.trades_detail)
    assert all(trade.entry_time > start for trade in held_out.trades_detail)
    assert all(timestamp >= start for timestamp, _ in held_out.equity_curve)


def test_full_validation_uses_test_trades_for_cost_and_bootstrap(monkeypatch):
    service = ResearchService()
    calls = []

    def fake_backtest(**kwargs):
        calls.append(kwargs)
        start = kwargs.get("evaluation_start")
        # Enough trades for gating, with distinct counts identifying each split.
        count = 250 if start is None else 60 + int(start.day)
        return sample_result(trades=count)

    monkeypatch.setattr(service, "backtest", fake_backtest)
    validation = service.full_validation(
        symbol="TEST/USDT", frames={"15m": frame()}, execution_timeframe="15m",
        monte_carlo_simulations=100,
    )
    test = validation["oos"]["test"]
    assert validation["monte_carlo"]["scope"] == "test"
    assert validation["monte_carlo"]["trades_per_path"] == test["result"]["trades"]
    assert all(row["scope"] == "test" for row in validation["cost_stress"])
    assert all(call["evaluation_start"] == pd.Timestamp(test["start"]) for call in calls[-4:])
    assert validation["provenance"]["configuration_sha256"]
    assert validation["gate"]["live_ready"] is False


def test_provenance_changes_when_data_or_configuration_changes():
    service = ResearchService()
    candles = frame()
    first = _research_provenance({"15m": candles}, service.config, service.thresholds, {})
    assert first == _research_provenance({"15m": candles.copy()}, service.config, service.thresholds, {})
    changed = candles.copy()
    changed.loc[0, "volume"] += 1
    second = _research_provenance({"15m": changed}, service.config, service.thresholds, {})
    assert first["datasets"]["15m"]["sha256"] != second["datasets"]["15m"]["sha256"]
    config = replace(service.config, risk=replace(service.config.risk, fee_bps=12.0))
    third = _research_provenance({"15m": candles}, config, service.thresholds, {})
    assert first["configuration_sha256"] != third["configuration_sha256"]


@pytest.mark.parametrize("thresholds", [
    ValidationThresholds(minimum_oos_trades=0),
    ValidationThresholds(maximum_drawdown_percent=float("nan")),
    ValidationThresholds(minimum_cost_stress_multiplier=1.0),
])
def test_invalid_thresholds_rejected(thresholds):
    with pytest.raises(ValueError):
        ResearchService(thresholds=thresholds)


@pytest.mark.parametrize("timeframe", ["15m", "1h"])
@pytest.mark.parametrize("defect,code", [
    ("gap", "candle_gap"),
    ("misalignment", "misaligned_candle"),
    ("conflicting_duplicate", "conflicting_candle"),
])
def test_full_validation_rejects_unsafe_history_before_any_backtest(monkeypatch, timeframe, defect, code):
    frames = {"15m": frame()}
    context = frame(count=400)
    context["timestamp"] = pd.date_range("2024-01-01", periods=400, freq="1h", tz="UTC")
    frames["1h"] = context
    damaged = frames[timeframe].copy()
    if defect == "gap":
        damaged = damaged.drop(index=len(damaged) // 2).reset_index(drop=True)
    elif defect == "misalignment":
        damaged["timestamp"] += pd.Timedelta(minutes=1)
    else:
        duplicate = damaged.iloc[[100]].copy()
        duplicate["volume"] += 1
        damaged = pd.concat([damaged, duplicate], ignore_index=True)
    frames[timeframe] = damaged
    service = ResearchService()

    def forbidden_backtest(**kwargs):
        raise AssertionError("Unsafe data reached the backtester")

    monkeypatch.setattr(service, "backtest", forbidden_backtest)
    with pytest.raises(DataValidationError) as error:
        service.full_validation(
            symbol="TEST/USDT", frames=frames, execution_timeframe="15m",
            monte_carlo_simulations=100,
        )
    assert error.value.code == code


def test_full_validation_accepts_old_aligned_history_and_discloses_identical_overlap(monkeypatch):
    candles = frame()
    overlapping = pd.concat([candles, candles.iloc[[100, 500]]], ignore_index=True)
    service = ResearchService()
    counts = []

    def fake_backtest(**kwargs):
        counts.append(len(kwargs["frames"]["15m"]))
        return sample_result()

    monkeypatch.setattr(service, "backtest", fake_backtest)
    validation = service.full_validation(
        symbol="TEST/USDT", frames={"15m": overlapping}, execution_timeframe="15m",
        monte_carlo_simulations=100,
    )
    quality = validation["data_quality"]
    assert quality["status"] == "passed"
    assert quality["wall_clock_freshness_required"] is False
    assert quality["frames"]["15m"] == {
        "rows": 1500, "identical_duplicates_removed": 2, "gaps": 0, "aligned": True,
    }
    assert counts[0] == 1500
    assert validation["provenance"]["datasets"]["15m"]["rows"] == 1500
