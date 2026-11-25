from __future__ import annotations

import argparse
import os
from dataclasses import replace
from hmac import compare_digest
from importlib.resources import files
from pathlib import Path
from typing import Any

import pandas as pd

from .config import RegimeBotConfig
from .engine import RegimeSignalEngine
from .history import HistoryRequest, HistoryService
from .models import SignalAction, to_dict
from .research import ResearchService
from .scanner import MarketScanner, ScanInProgressError, ScanRequest
from .service import LiveMarketRequest, RegimeBotService, parse_timeframes
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


def create_app(
    config: RegimeBotConfig | None = None,
    *,
    db_path: str | Path | None = None,
    store: SignalStore | None = None,
    service: RegimeBotService | None = None,
    history_service: HistoryService | None = None,
    scanner: MarketScanner | None = None,
):
    try:
        from fastapi import (
            APIRouter,
            Depends,
            FastAPI,
            Header,
            HTTPException,
            Query,
        )
        from fastapi.responses import HTMLResponse, PlainTextResponse
    except ImportError as exc:  # pragma: no cover - packaging boundary.
        raise RuntimeError(
            'Install the web extra: python -m pip install -e ".[web]"'
        ) from exc

    active_config = config or RegimeBotConfig()
    active_config.validate()
    engine = RegimeSignalEngine(active_config)
    active_store = store or SignalStore(
        db_path or os.getenv("SIGNAL_DB_PATH", "data/signal_machine.db")
    )
    active_service = service or RegimeBotService(engine)
    active_history = history_service or HistoryService(store=active_store)
    active_scanner = scanner or MarketScanner(engine, store=active_store)
    research = ResearchService(active_config)
    configured_api_key = os.getenv("SIGNAL_API_KEY", "").strip()

    app = FastAPI(
        title="KuCoin Futures Signal Machine",
        version="2.1.0",
        description=(
            "KuCoin USDT-perpetual, signal-only market-regime analysis, "
            "scanning, historical research, and validation platform. Confidence is a "
            "rule-alignment score, not a probability of profit."
        ),
    )

    def require_api_key(
        x_api_key: str | None = Header(default=None),
    ) -> None:
        if configured_api_key and not (
            x_api_key is not None
            and compare_digest(x_api_key, configured_api_key)
        ):
            raise HTTPException(status_code=401, detail="Invalid API key.")

    router = APIRouter(
        prefix="/v1",
        dependencies=[Depends(require_api_key)],
    )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        return _asset("index.html")

    @app.get(
        "/assets/styles.css",
        response_class=PlainTextResponse,
        include_in_schema=False,
    )
    def dashboard_styles() -> PlainTextResponse:
        return PlainTextResponse(
            _asset("styles.css"), media_type="text/css; charset=utf-8"
        )

    @app.get(
        "/assets/app.js",
        response_class=PlainTextResponse,
        include_in_schema=False,
    )
    def dashboard_script() -> PlainTextResponse:
        return PlainTextResponse(
            _asset("app.js"),
            media_type="application/javascript; charset=utf-8",
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "engine": "signal_bot",
            "version": "2.1.0",
            "minimum_bars": engine.config.minimum_bars,
            "database": active_store.path,
            "api_key_required": bool(configured_api_key),
            "execution_enabled": False,
            "exchange_id": DEFAULT_EXCHANGE_ID,
            "market_type": DEFAULT_MARKET_TYPE,
            "settlement_currency": "USDT",
        }

    @router.get("/live-signal")
    def live_signal(
        symbol: str = Query(...),
        exchange: str = Query(DEFAULT_EXCHANGE_ID),
        market_type: str = Query(DEFAULT_MARKET_TYPE),
        execution_timeframe: str = Query("15m"),
        context_timeframes: str = Query("1h,4h"),
        limit: int = Query(KUCOIN_MAX_KLINE_LIMIT, ge=1, le=KUCOIN_MAX_KLINE_LIMIT),
        maximum_staleness_bars: int = Query(3, ge=0, le=100),
        equity: float = Query(10_000.0, gt=0),
        risk_percent: float | None = Query(None, gt=0),
        maximum_notional_multiple: float | None = Query(None, gt=0, le=5.0),
        include_open_candle: bool = Query(False),
    ) -> dict[str, Any]:
        try:
            request = _live_request(
                {
                    "exchange_id": exchange,
                    "market_type": market_type,
                    "symbol": symbol,
                    "execution_timeframe": execution_timeframe,
                    "context_timeframes": context_timeframes,
                    "limit": limit,
                    "maximum_staleness_bars": maximum_staleness_bars,
                    "include_open_candle": include_open_candle,
                }
            )
            signal = active_service.analyze_live(
                request,
                equity=equity,
                risk_percent=risk_percent,
                maximum_notional_multiple=maximum_notional_multiple,
            )
            active_store.record_signal(
                signal,
                exchange_id=request.exchange_id,
                market_type=request.market_type,
            )
            return to_dict(signal)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/market-snapshot")
    def market_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = _live_request(payload)
            frames = active_service.fetch_frames(request)
            signal = engine.analyze(
                symbol=request.symbol,
                frames=frames,
                execution_timeframe=request.execution_timeframe,
                equity=float(payload.get("equity", 10_000.0)),
                risk_percent=_optional_float(payload, "risk_percent"),
                maximum_notional_multiple=validate_notional_multiple(
                    payload.get("maximum_notional_multiple")
                ),
            )
            if request.market_type.lower() == "spot" and signal.action in {
                SignalAction.SHORT,
                SignalAction.WATCH_SHORT,
            }:
                signal = replace(
                    signal,
                    warnings=signal.warnings
                    + (
                        "Short signals are informational on spot markets; "
                        "confirm venue eligibility before any execution.",
                    ),
                )
            active_store.record_signal(
                signal,
                exchange_id=request.exchange_id,
                market_type=request.market_type,
            )
            prepared = engine.prepare_feature_frames(
                frames,
                execution_timeframe=request.execution_timeframe,
            )[request.execution_timeframe]
            chart_bars = int(payload.get("chart_bars", 240))
            chart_bars = max(50, min(chart_bars, KUCOIN_MAX_KLINE_LIMIT))
            return {
                "signal": to_dict(signal),
                "candles": _frame_records(prepared.tail(chart_bars)),
                "exchange_id": request.exchange_id,
                "market_type": request.market_type,
            }
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/analyze")
    def analyze(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            symbol = normalize_symbol(str(payload["symbol"]))
            execution_timeframe = str(payload["execution_timeframe"])
            raw_frames = payload["frames"]
            if not isinstance(raw_frames, dict) or not raw_frames:
                raise ValueError(
                    "frames must be a non-empty timeframe-to-candles object."
                )
            frames = {
                str(timeframe).strip().lower(): pd.DataFrame(candles)
                for timeframe, candles in raw_frames.items()
            }
            signal = engine.analyze(
                symbol=symbol,
                frames=frames,
                execution_timeframe=execution_timeframe,
                equity=float(payload.get("equity", 10_000.0)),
                risk_percent=_optional_float(payload, "risk_percent"),
                maximum_notional_multiple=validate_notional_multiple(
                    payload.get("maximum_notional_multiple")
                ),
            )
            return to_dict(signal)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/scan")
    def scan(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            exchange_id, market_type = enforce_venue(
                str(payload.get("exchange_id", DEFAULT_EXCHANGE_ID)),
                str(payload.get("market_type", DEFAULT_MARKET_TYPE)),
            )
            symbols = normalize_symbols(_string_tuple(payload.get("symbols")))
            contexts = _string_tuple(
                payload.get("context_timeframes", ("1h", "4h"))
            )
            report = active_scanner.scan(
                ScanRequest(
                    exchange_id=exchange_id,
                    market_type=market_type,
                    execution_timeframe=str(
                        payload.get("execution_timeframe", "15m")
                    ).lower(),
                    context_timeframes=contexts,
                    symbols=symbols,
                    auto_universe=bool(payload.get("auto_universe", not symbols)),
                    quote_currencies=normalize_quote_currencies(
                        _string_tuple(
                            payload.get(
                                "quote_currencies", DEFAULT_QUOTE_CURRENCIES
                            )
                        )
                    ),
                    max_markets=int(payload.get("max_markets", 20)),
                    minimum_quote_volume=float(
                        payload.get("minimum_quote_volume", 1_000_000.0)
                    ),
                    maximum_spread_bps=_optional_float(
                        payload, "maximum_spread_bps", default=25.0
                    ),
                    maximum_quote_age_seconds=float(payload.get("maximum_quote_age_seconds", 30.0)),
                    maximum_staleness_bars=int(payload.get("maximum_staleness_bars", 1)),
                    scan_timeout_seconds=float(payload.get("scan_timeout_seconds", 180.0)),
                    limit=validate_kline_limit(
                        payload.get("limit", KUCOIN_MAX_KLINE_LIMIT)
                    ),
                    equity=float(payload.get("equity", 10_000.0)),
                    risk_percent=_optional_float(payload, "risk_percent"),
                    maximum_notional_multiple=validate_notional_multiple(
                        payload.get("maximum_notional_multiple")
                    ),
                )
            )
            return report.to_dict()
        except ScanInProgressError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/history/download")
    def download_history(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            exchange_id, market_type = enforce_venue(
                str(payload.get("exchange_id", DEFAULT_EXCHANGE_ID)),
                str(payload.get("market_type", DEFAULT_MARKET_TYPE)),
            )
            timeframe = str(payload["timeframe"]).lower()
            _, result = active_history.download(
                HistoryRequest(
                    exchange_id=exchange_id,
                    market_type=market_type,
                    symbol=normalize_symbol(str(payload["symbol"])),
                    timeframe=timeframe,
                    start=payload["start"],
                    end=payload["end"],
                    limit=validate_kline_limit(
                        payload.get("limit", KUCOIN_MAX_KLINE_LIMIT)
                    ),
                    output_dir=str(
                        payload.get("output_dir", "data/history")
                    ),
                    resume=bool(payload.get("resume", True)),
                    strict_gaps=bool(payload.get("strict_gaps", False)),
                )
            )
            return result.to_dict()
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/backtest/live")
    def backtest_live(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            frames, datasets = _download_frames(active_history, payload)
            execution = str(payload.get("execution_timeframe", "15m")).lower()
            result = research.backtest(
                symbol=normalize_symbol(str(payload["symbol"])),
                frames=frames,
                execution_timeframe=execution,
                starting_equity=float(payload.get("equity", 10_000.0)),
                risk_percent=_optional_float(payload, "risk_percent"),
                maximum_notional_multiple=validate_notional_multiple(
                    payload.get("maximum_notional_multiple")
                ),
                allow_short=True,
            )
            diagnostics = research.diagnostics(result)
            request_payload = {
                **payload,
                "execution_timeframe": execution,
                "exchange_id": DEFAULT_EXCHANGE_ID,
                "market_type": DEFAULT_MARKET_TYPE,
                "symbol": normalize_symbol(str(payload["symbol"])),
            }
            run_id = active_store.record_backtest(
                request=request_payload,
                result=diagnostics,
            )
            return {
                "run_id": run_id,
                "datasets": {
                    timeframe: item.to_dict()
                    for timeframe, item in datasets.items()
                },
                "result": to_dict(result),
                "diagnostics": diagnostics,
            }
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/research/validate")
    def validate_strategy(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            frames, datasets = _download_frames(active_history, payload)
            execution = str(payload.get("execution_timeframe", "15m")).lower()
            validation = research.full_validation(
                symbol=normalize_symbol(str(payload["symbol"])),
                frames=frames,
                execution_timeframe=execution,
                starting_equity=float(payload.get("equity", 10_000.0)),
                risk_percent=_optional_float(payload, "risk_percent"),
                maximum_notional_multiple=validate_notional_multiple(
                    payload.get("maximum_notional_multiple")
                ),
                monte_carlo_simulations=int(
                    payload.get("monte_carlo_simulations", 2000)
                ),
                allow_short=True,
            )
            request_payload = {
                **payload,
                "execution_timeframe": execution,
                "exchange_id": DEFAULT_EXCHANGE_ID,
                "market_type": DEFAULT_MARKET_TYPE,
                "symbol": normalize_symbol(str(payload["symbol"])),
            }
            run_id = active_store.record_backtest(
                request=request_payload,
                result=validation,
            )
            return {
                "run_id": run_id,
                "datasets": {
                    timeframe: item.to_dict()
                    for timeframe, item in datasets.items()
                },
                "validation": validation,
            }
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/scanner/status")
    def scanner_status() -> dict[str, Any]:
        rows = active_store.recent_scans(limit=1)
        if not rows:
            return {"status": "idle", "last_scan": None}
        latest = rows[0]
        result = latest["result"]
        return {
            "status": result.get("status", "unknown"),
            "last_scan": {
                "run_id": latest["run_id"],
                "generated_at": result.get("generated_at"),
                "duration_seconds": result.get("duration_seconds"),
                "analyzed_markets": result.get("analyzed_markets"),
                "failed_markets": result.get("failed_markets"),
                "blocked_markets": result.get("blocked_markets"),
                "actionable_at_scan_time": result.get("actionable_markets"),
                "errors": result.get("errors", []),
            },
        }

    @router.get("/journal/signals")
    def journal_signals(
        limit: int = Query(100, ge=1, le=5000),
        symbol: str | None = Query(None),
    ) -> dict[str, Any]:
        return {
            "items": active_store.recent_signals(limit=limit, symbol=symbol)
        }

    @router.get("/journal/backtests")
    def journal_backtests(
        limit: int = Query(50, ge=1, le=1000),
        symbol: str | None = Query(None),
    ) -> dict[str, Any]:
        return {
            "items": active_store.recent_backtests(
                limit=limit, symbol=symbol
            )
        }

    @router.get("/journal/scans")
    def journal_scans(
        limit: int = Query(20, ge=1, le=500),
    ) -> dict[str, Any]:
        return {"items": active_store.recent_scans(limit=limit)}

    @router.get("/datasets")
    def datasets(limit: int = Query(500, ge=1, le=5000)) -> dict[str, Any]:
        return {"items": active_store.list_datasets(limit=limit)}

    app.include_router(router)
    return app


def _asset(name: str) -> str:
    resource = files("signal_bot").joinpath("web", name)
    return resource.read_text(encoding="utf-8")


def _live_request(payload: dict[str, Any]) -> LiveMarketRequest:
    exchange_id, market_type = enforce_venue(
        str(payload.get("exchange_id", DEFAULT_EXCHANGE_ID)),
        str(payload.get("market_type", DEFAULT_MARKET_TYPE)),
    )
    execution = str(payload.get("execution_timeframe", "15m")).strip().lower()
    contexts = tuple(
        value
        for value in parse_timeframes(
            payload.get("context_timeframes", "1h,4h")
        )
        if value != execution
    )
    return LiveMarketRequest(
        exchange_id=exchange_id,
        market_type=market_type,
        symbol=normalize_symbol(str(payload["symbol"])),
        execution_timeframe=execution,
        context_timeframes=contexts,
        limit=validate_kline_limit(
            payload.get("limit", KUCOIN_MAX_KLINE_LIMIT)
        ),
        closed_candles_only=not bool(payload.get("include_open_candle", False)),
        maximum_staleness_bars=int(
            payload.get("maximum_staleness_bars", 3)
        ),
    )


def _download_frames(
    history: HistoryService,
    payload: dict[str, Any],
):
    exchange_id, market_type = enforce_venue(
        str(payload.get("exchange_id", DEFAULT_EXCHANGE_ID)),
        str(payload.get("market_type", DEFAULT_MARKET_TYPE)),
    )
    execution = str(payload.get("execution_timeframe", "15m")).lower()
    contexts = _string_tuple(
        payload.get("context_timeframes", ("1h", "4h"))
    )
    timeframes = tuple(dict.fromkeys((execution, *contexts)))
    return history.download_bundle(
        exchange_id=exchange_id,
        market_type=market_type,
        symbol=normalize_symbol(str(payload["symbol"])),
        timeframes=timeframes,
        start=payload["start"],
        end=payload["end"],
        limit=validate_kline_limit(
            payload.get("download_limit", KUCOIN_MAX_KLINE_LIMIT)
        ),
        output_dir=str(payload.get("output_dir", "data/history")),
        resume=bool(payload.get("resume", True)),
        strict_gaps=bool(payload.get("strict_gaps", False)),
    )


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    columns = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "ema_fast",
        "ema_mid",
        "ema_slow",
    ]
    available = [column for column in columns if column in frame.columns]
    records: list[dict[str, Any]] = []
    for _, row in frame[available].iterrows():
        record: dict[str, Any] = {}
        for column in available:
            value = row[column]
            if column == "timestamp":
                record[column] = pd.Timestamp(value).isoformat()
            elif pd.isna(value):
                record[column] = None
            else:
                record[column] = float(value)
        records.append(record)
    return records


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        raise ValueError("Expected a comma-separated string or list.")
    return tuple(
        dict.fromkeys(
            str(item).strip()
            for item in values
            if str(item).strip()
        )
    )


def _optional_float(
    payload: dict[str, Any],
    key: str,
    *,
    default: float | None = None,
) -> float | None:
    value = payload.get(key, default)
    if value is None or value == "":
        return None
    return float(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signal-api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--config", default="")
    parser.add_argument("--db", default="")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - packaging boundary.
        raise RuntimeError(
            'Install the web extra: python -m pip install -e ".[web]"'
        ) from exc

    config = (
        RegimeBotConfig.from_json(args.config)
        if args.config
        else RegimeBotConfig()
    )
    uvicorn.run(
        create_app(config, db_path=args.db or None),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
