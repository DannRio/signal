"""Read-only live verification of cache, restart and replay on a small universe.

Results describe this observation, not a capacity/latency guarantee. No keys,
orders, paper entries, or alerts are used. Deterministic tests cover bar rollovers
and missing/corrupt data separately.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from signal_bot.replay import ReplayStore
from signal_bot.scanner import MarketScanner, ScanRequest
from signal_bot.storage import SignalStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT")
    parser.add_argument("--output", default="artifacts/public-scaling.json")
    args = parser.parse_args()
    symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
    if not 1 <= len(symbols) <= 20:
        parser.error("Choose one through twenty symbols for this bounded public check.")
    rows, replays = [], []
    with tempfile.TemporaryDirectory(prefix="signal-public-") as directory:
        store = SignalStore(Path(directory) / "verification.db")
        scanner = MarketScanner(store=store)
        request = ScanRequest("kucoinfutures", symbols=symbols, max_markets=len(symbols), scan_timeout_seconds=180)
        for label, cached in (("uncached", False), ("cache_bootstrap", True), ("cache_reuse", True), ("after_restart", True)):
            if label == "after_restart":
                scanner = MarketScanner(store=SignalStore(store.path))
            report = scanner.scan(replace(request, use_candle_cache=cached))
            rows.append({"phase": label, "report": report.to_dict()})
            archive = ReplayStore(store)
            for item in report.items:
                if item.replay_id:
                    replays.append({"phase": label, "symbol": item.candidate.symbol, **archive.replay(item.replay_id)})
        payload = {"symbols": symbols, "runs": rows, "replays": replays,
                   "execution_enabled": False, "alerts_sent": False,
                   "notes": ["Observational public-data check, not a maximum-capacity benchmark.",
                             "Candle boundaries can change between phases; replay freezes each individual decision."]}
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
        summary = {"runs": [{"phase": row["phase"], "status": row["report"]["status"],
                             "analyzed_markets": row["report"]["analyzed_markets"],
                             "duration_seconds": row["report"]["duration_seconds"],
                             "metrics": row["report"]["metrics"],
                             "errors": row["report"]["errors"]} for row in rows],
                   "replays": len(replays), "replays_passed": sum(item["passed"] for item in replays),
                   "output": str(destination)}
        print(json.dumps(summary, indent=2, allow_nan=False))
        good_runs = all(row["report"]["status"] == "complete" and row["report"]["analyzed_markets"] == len(symbols) for row in rows)
        good_replays = len(replays) == len(symbols) * len(rows) and all(item["passed"] for item in replays)
        return 0 if good_runs and good_replays else 1


if __name__ == "__main__":
    raise SystemExit(main())
