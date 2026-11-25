from __future__ import annotations

from datetime import UTC, datetime, timedelta

from signal_bot.models import (
    BacktestResult,
    BacktestTrade,
    PositionSide,
    SetupType,
)
from signal_bot.research import ResearchService, _compact_result


def sample_result(trades=250, expectancy=0.2) -> BacktestResult:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    details = []
    equity = 10_000.0
    curve = []
    for index in range(trades):
        r_value = 1.0 if index % 2 == 0 else -0.6
        net = r_value * 50.0
        equity += net
        entry = start + timedelta(hours=index * 2)
        exit_time = entry + timedelta(hours=1)
        details.append(
            BacktestTrade(
                side=PositionSide.LONG if index % 3 else PositionSide.SHORT,
                setup_type=SetupType.BREAKOUT,
                signal_time=entry - timedelta(minutes=15),
                entry_time=entry,
                exit_time=exit_time,
                entry_price=100.0,
                exit_price=101.0 if r_value > 0 else 99.4,
                initial_stop=99.0,
                quantity=1.0,
                gross_pnl=net + 1.0,
                fees=1.0,
                net_pnl=net,
                initial_risk=50.0,
                r_multiple=r_value,
                exit_reason="take_profit_3" if r_value > 0 else "stop_loss",
                bars_held=4,
            )
        )
        curve.append((exit_time, equity))
    return BacktestResult(
        symbol="TEST/USDT",
        execution_timeframe="15m",
        starting_equity=10_000.0,
        ending_equity=equity,
        net_pnl=equity - 10_000.0,
        total_return_percent=(equity / 10_000.0 - 1.0) * 100.0,
        max_drawdown_percent=3.0,
        trades=trades,
        wins=(trades + 1) // 2,
        losses=trades // 2,
        win_rate_percent=50.0,
        profit_factor=1.6,
        expectancy_r=expectancy,
        average_r=expectancy,
        trades_detail=tuple(details),
        equity_curve=tuple(curve),
    )


def test_diagnostics_and_monte_carlo_are_deterministic() -> None:
    service = ResearchService()
    result = sample_result()
    diagnostics = service.diagnostics(result)
    assert diagnostics["trade_quality"]["payoff_ratio"] > 1
    assert diagnostics["by_side"]
    first = service.monte_carlo(result, simulations=200, seed=7)
    second = service.monte_carlo(result, simulations=200, seed=7)
    assert first == second


def test_validation_gate_requires_multiple_evidence_layers() -> None:
    service = ResearchService()
    result = sample_result()
    held_out = _compact_result(sample_result(trades=60))
    oos = {
        "validation": {"result": held_out},
        "test": {"result": held_out},
    }
    stress = [{"scope": "test", "cost_multiplier": 3.0, "result": held_out}]
    monte = {
        "scope": "test",
        "simulations": 200,
        "trades_per_path": 60,
        "probability_ending_below_start": 0.1,
    }
    gate = service.validation_gate(
        base_result=result,
        oos=oos,
        cost_stress=stress,
        monte_carlo=monte,
    )
    assert gate["status"] == "PASS_FOR_PAPER_VALIDATION"
    assert gate["live_ready"] is False
    assert not gate["missing_evidence"]
