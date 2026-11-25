from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from .alerts import (
    AlertDispatcher,
    AlertPolicy,
    ConsoleAlertSink,
    JsonlAlertSink,
    WebhookAlertSink,
)
from .api import create_app
from .config import RegimeBotConfig
from .engine import RegimeSignalEngine
from .history import HistoryService
from .models import SignalAction, to_dict
from .research import ResearchService
from .scanner import MarketScanner, ScanRequest
from .storage import SignalStore
from .venue import (
    DEFAULT_EXCHANGE_ID,
    DEFAULT_MARKET_TYPE,
    DEFAULT_QUOTE_CURRENCIES,
    KUCOIN_MAX_KLINE_LIMIT,
    enforce_venue,
    normalize_quote_currencies,
    normalize_symbol,
    normalize_symbols,
    validate_kline_limit,
    validate_notional_multiple,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signal-machine",
        description=(
            "Professional signal research machine: dashboard, scanner, history, "
            "backtests, robustness validation, and persistent monitoring."
        ),
    )
    parser.add_argument("--config", default="")
    parser.add_argument("--db", default="data/signal_machine.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dashboard = subparsers.add_parser("dashboard", help="Run the local dashboard.")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8081)

    download = subparsers.add_parser("download", help="Download paginated OHLCV history.")
    _add_market_arguments(download)
    download.add_argument("--timeframes", default="15m,1h,4h")
    download.add_argument("--start", required=True)
    download.add_argument("--end", required=True)
    download.add_argument("--output-dir", default="data/history")
    download.add_argument("--strict-gaps", action="store_true")
    download.add_argument("--json", action="store_true")

    scan = subparsers.add_parser("scan", help="Scan and rank multiple markets.")
    _add_scan_arguments(scan)
    scan.add_argument("--json", action="store_true")

    backtest = subparsers.add_parser(
        "backtest-live",
        help="Download historical data and run a cost-aware backtest.",
    )
    _add_research_arguments(backtest)
    backtest.add_argument("--json", action="store_true")

    validate = subparsers.add_parser(
        "validate",
        help="Run chronological OOS, cost stress, and Monte Carlo validation.",
    )
    _add_research_arguments(validate)
    validate.add_argument("--monte-carlo", type=int, default=2000)
    validate.add_argument("--json", action="store_true")

    monitor = subparsers.add_parser(
        "monitor",
        help="Continuously scan, journal, and alert on confirmed signals.",
    )
    _add_scan_arguments(monitor)
    monitor.add_argument("--interval-seconds", type=int, default=60)
    monitor.add_argument("--iterations", type=int, default=0)
    monitor.add_argument(
        "--alert-sinks",
        default="console,jsonl",
        help="Comma-separated: console,jsonl,webhook.",
    )
    monitor.add_argument("--alert-cooldown-minutes", type=int, default=60)
    monitor.add_argument("--minimum-alert-confidence", type=float, default=0.0)
    monitor.add_argument("--minimum-alert-consensus", type=float, default=0.0)
    return parser


def _add_market_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--exchange",
        default=DEFAULT_EXCHANGE_ID,
        choices=(DEFAULT_EXCHANGE_ID,),
    )
    parser.add_argument(
        "--market-type",
        default=DEFAULT_MARKET_TYPE,
        choices=(DEFAULT_MARKET_TYPE,),
    )
    parser.add_argument(
        "--symbol",
        required=True,
        type=normalize_symbol,
        help="KuCoin USDT perpetual in CCXT notation, e.g. BTC/USDT:USDT.",
    )


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--exchange",
        default=DEFAULT_EXCHANGE_ID,
        choices=(DEFAULT_EXCHANGE_ID,),
    )
    parser.add_argument(
        "--market-type",
        default=DEFAULT_MARKET_TYPE,
        choices=(DEFAULT_MARKET_TYPE,),
    )
    parser.add_argument("--symbols", default="")
    parser.add_argument("--auto-universe", action="store_true")
    parser.add_argument(
        "--quote-currencies",
        default=",".join(DEFAULT_QUOTE_CURRENCIES),
    )
    parser.add_argument("--max-markets", type=int, default=20)
    parser.add_argument(
        "--minimum-quote-volume", type=float, default=1_000_000.0
    )
    parser.add_argument("--maximum-spread-bps", type=float, default=25.0)
    parser.add_argument("--maximum-quote-age-seconds", type=float, default=30.0)
    parser.add_argument("--maximum-staleness-bars", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument("--scan-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--execution-timeframe", default="15m")
    parser.add_argument("--context-timeframes", default="1h,4h")
    parser.add_argument(
        "--limit",
        type=validate_kline_limit,
        default=KUCOIN_MAX_KLINE_LIMIT,
    )
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--risk-percent", type=float, default=None)
    parser.add_argument(
        "--maximum-notional-multiple",
        type=validate_notional_multiple,
        default=None,
        help="Position-notional cap as a multiple of equity; does not set KuCoin leverage.",
    )


def _add_research_arguments(parser: argparse.ArgumentParser) -> None:
    _add_market_arguments(parser)
    parser.add_argument("--execution-timeframe", default="15m")
    parser.add_argument("--context-timeframes", default="1h,4h")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--risk-percent", type=float, default=None)
    parser.add_argument(
        "--maximum-notional-multiple",
        type=validate_notional_multiple,
        default=None,
        help="Position-notional cap as a multiple of equity; does not set KuCoin leverage.",
    )
    parser.add_argument("--output-dir", default="data/history")
    parser.add_argument("--strict-gaps", action="store_true")


def _config(path: str) -> RegimeBotConfig:
    return RegimeBotConfig.from_json(path) if path else RegimeBotConfig()


def _items(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _scan_request(args: argparse.Namespace) -> ScanRequest:
    exchange_id, market_type = enforce_venue(args.exchange, args.market_type)
    symbols = normalize_symbols(_items(args.symbols))
    auto = bool(args.auto_universe)
    if not symbols and not auto:
        auto = True
    return ScanRequest(
        exchange_id=exchange_id,
        market_type=market_type,
        execution_timeframe=args.execution_timeframe,
        context_timeframes=_items(args.context_timeframes),
        symbols=symbols,
        auto_universe=auto,
        quote_currencies=normalize_quote_currencies(
            _items(args.quote_currencies)
        ),
        max_markets=args.max_markets,
        minimum_quote_volume=args.minimum_quote_volume,
        maximum_spread_bps=args.maximum_spread_bps,
        maximum_quote_age_seconds=args.maximum_quote_age_seconds,
        maximum_staleness_bars=args.maximum_staleness_bars,
        scan_timeout_seconds=args.scan_timeout_seconds,
        limit=args.limit,
        equity=args.equity,
        risk_percent=args.risk_percent,
        maximum_notional_multiple=args.maximum_notional_multiple,
    )


def _research_frames(
    history: HistoryService,
    args: argparse.Namespace,
):
    execution = args.execution_timeframe.strip().lower()
    timeframes = tuple(
        dict.fromkeys((execution, *_items(args.context_timeframes)))
    )
    return history.download_bundle(
        exchange_id=args.exchange,
        market_type=args.market_type,
        symbol=args.symbol,
        timeframes=timeframes,
        start=args.start,
        end=args.end,
        output_dir=args.output_dir,
        strict_gaps=args.strict_gaps,
        limit=KUCOIN_MAX_KLINE_LIMIT,
    )


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _print_scan(report) -> None:
    print(
        f"{report.exchange_id} {report.market_type} | "
        f"{report.status.upper()} | {report.analyzed_markets} analyzed | "
        f"{report.actionable_markets} qualified | {report.blocked_markets} blocked | "
        f"{report.rejected_markets} rejected | {report.failed_markets} failed | "
        f"{report.duration_seconds:.1f}s"
    )
    for item in report.items:
        if item.signal is None:
            print(f"{item.rank:>2}. {item.candidate.symbol:<18} ERROR {item.error}")
            continue
        signal = item.signal
        if item.status == "blocked":
            print(f"{item.rank:>2}. {item.candidate.symbol:<18} BLOCKED [{item.error_code}]: " + "; ".join(item.blocked_reasons))
            continue
        print(
            f"{item.rank:>2}. {item.candidate.symbol:<18} "
            f"{signal.action.value.upper():<12} "
            f"{signal.regime.value:<16} alignment {signal.confidence:>5.1f}% "
            f"consensus {signal.consensus * 100:>3.0f}% score {signal.score:>6.1f}"
        )

        if item.contract_plan is not None:
            plan = item.contract_plan
            print(f"    {plan['contracts']:g} contracts | risk {plan['actual_risk_amount']:.2f} USDT | quote valid until {item.valid_until}")
    for rejection in report.rejections:
        print(f"  REJECT {rejection.symbol}: [{rejection.code}] {rejection.reason}")
    for error in report.errors:
        print(f"  ERROR [{error['code']}]: {error['message']}")


def _alert_sinks(value: str):
    sinks = []
    for name in _items(value.lower()):
        if name == "console":
            sinks.append(ConsoleAlertSink())
        elif name == "jsonl":
            sinks.append(JsonlAlertSink())
        elif name == "webhook":
            url = os.getenv("SIGNAL_ALERT_WEBHOOK_URL", "").strip()
            if not url:
                raise ValueError(
                    "SIGNAL_ALERT_WEBHOOK_URL is required for webhook alerts."
                )
            sinks.append(WebhookAlertSink(url))
        else:
            raise ValueError(f"Unknown alert sink: {name}")
    return tuple(sinks)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _config(args.config)
        store = SignalStore(args.db)

        if args.command == "dashboard":
            try:
                import uvicorn
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    'Install web support: python -m pip install -e ".[web]"'
                ) from exc
            uvicorn.run(
                create_app(config, store=store),
                host=args.host,
                port=args.port,
            )
            return 0

        history = HistoryService(store=store)
        if args.command == "download":
            frames, results = history.download_bundle(
                exchange_id=args.exchange,
                market_type=args.market_type,
                symbol=args.symbol,
                timeframes=_items(args.timeframes),
                start=args.start,
                end=args.end,
                output_dir=args.output_dir,
                strict_gaps=args.strict_gaps,
                limit=KUCOIN_MAX_KLINE_LIMIT,
            )
            payload = {
                "datasets": {
                    timeframe: result.to_dict()
                    for timeframe, result in results.items()
                },
                "loaded_rows": {
                    timeframe: len(frame) for timeframe, frame in frames.items()
                },
            }
            if args.json:
                _print_json(payload)
            else:
                for timeframe, result in results.items():
                    print(
                        f"{args.symbol} {timeframe}: {result.rows} rows | "
                        f"{result.gaps} gaps | {result.path}"
                    )
            return 0

        if args.command in {"scan", "monitor"}:
            scanner = MarketScanner(RegimeSignalEngine(config), store=store)
            request = _scan_request(args)
            if args.command == "scan":
                report = scanner.scan(request)
                _print_json(report.to_dict()) if args.json else _print_scan(report)
                return 2 if report.status in {"failed", "degraded"} else 0

            if args.interval_seconds < 60:
                raise ValueError("interval-seconds must be at least 60.")
            if args.iterations < 0:
                raise ValueError("iterations cannot be negative.")
            dispatcher = AlertDispatcher(
                store,
                policy=AlertPolicy(
                    actions=(SignalAction.LONG, SignalAction.SHORT),
                    minimum_confidence=args.minimum_alert_confidence,
                    minimum_consensus=args.minimum_alert_consensus,
                    cooldown_minutes=args.alert_cooldown_minutes,
                ),
                sinks=_alert_sinks(args.alert_sinks),
            )
            iteration = 0
            while args.iterations == 0 or iteration < args.iterations:
                try:
                    report = scanner.scan(request)
                    _print_scan(report)
                    for item in report.items:
                        if item.signal is not None and item.is_actionable():
                            delivery = dispatcher.dispatch(
                                item.signal,
                                exchange_id=request.exchange_id,
                                market_type=request.market_type,
                                context={"scan_run_id": report.run_id, "valid_until": item.valid_until,
                                         "contract_plan": item.contract_plan, "config_sha256": report.provenance["config_sha256"]},
                            )
                            for failure in delivery.failures:
                                print(f"alert delivery failed for {item.candidate.symbol}: {failure}", file=sys.stderr)
                except Exception as exc:
                    print(f"monitor cycle failed: {exc}", file=sys.stderr)
                iteration += 1
                if args.iterations and iteration >= args.iterations:
                    break
                time.sleep(args.interval_seconds)
            return 0

        frames, datasets = _research_frames(history, args)
        research = ResearchService(config)
        if args.command == "backtest-live":
            result = research.backtest(
                symbol=args.symbol,
                frames=frames,
                execution_timeframe=args.execution_timeframe,
                starting_equity=args.equity,
                risk_percent=args.risk_percent,
                maximum_notional_multiple=args.maximum_notional_multiple,
                allow_short=True,
            )
            diagnostics = research.diagnostics(result)
            run_id = store.record_backtest(
                request={
                    "exchange_id": args.exchange,
                    "market_type": args.market_type,
                    "symbol": args.symbol,
                    "execution_timeframe": args.execution_timeframe,
                    "start": args.start,
                    "end": args.end,
                },
                result=diagnostics,
            )
            payload = {
                "run_id": run_id,
                "datasets": {
                    timeframe: item.to_dict()
                    for timeframe, item in datasets.items()
                },
                "result": to_dict(result),
                "diagnostics": diagnostics,
            }
            if args.json:
                _print_json(payload)
            else:
                print(
                    f"{args.symbol} | {result.trades} trades | "
                    f"return {result.total_return_percent:.2f}% | "
                    f"expectancy {result.expectancy_r:.3f}R | "
                    f"max DD {result.max_drawdown_percent:.2f}%"
                )
                print(f"Research run: {run_id}")
            return 0

        validation = research.full_validation(
            symbol=args.symbol,
            frames=frames,
            execution_timeframe=args.execution_timeframe,
            starting_equity=args.equity,
            risk_percent=args.risk_percent,
            maximum_notional_multiple=args.maximum_notional_multiple,
            monte_carlo_simulations=args.monte_carlo,
            allow_short=True,
        )
        run_id = store.record_backtest(
            request={
                "exchange_id": args.exchange,
                "market_type": args.market_type,
                "symbol": args.symbol,
                "execution_timeframe": args.execution_timeframe,
                "start": args.start,
                "end": args.end,
                "mode": "full_validation",
            },
            result=validation,
        )
        if args.json:
            _print_json({"run_id": run_id, "validation": validation})
        else:
            gate = validation["gate"]
            base = validation["base"]["summary"]
            print(
                f"{args.symbol} validation: {gate['status']} | "
                f"{gate['passed']} passed, {gate['failed']} failed"
            )
            print(
                f"Base: {base['trades']} trades | "
                f"{base['expectancy_r']:.3f}R expectancy | "
                f"{base['max_drawdown_percent']:.2f}% max DD"
            )
            for check in gate["checks"]:
                marker = "PASS" if check["passed"] else "FAIL"
                print(
                    f"  {marker:<4} {check['name']}: {check['value']} "
                    f"(requires {check['requirement']})"
                )
            print(f"Research run: {run_id}")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
