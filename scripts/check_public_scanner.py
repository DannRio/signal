"""Read-only live KuCoin integration check; never creates orders or alerts."""
from __future__ import annotations

import json

from signal_bot.scanner import MarketScanner, ScanRequest
from signal_bot.storage import SignalStore


def main() -> int:
    report = MarketScanner(store=SignalStore(":memory:")).scan(
        ScanRequest(exchange_id="kucoinfutures", symbols=("BTC/USDT:USDT",),
                    limit=500, scan_timeout_seconds=180)
    )
    summary = {
        "status": report.status,
        "generated_at": report.generated_at,
        "duration_seconds": report.duration_seconds,
        "analyzed_markets": report.analyzed_markets,
        "errors": report.errors,
        "rejections": [value.to_dict() for value in report.rejections],
        "items": [{"symbol": item.candidate.symbol, "action": item.signal.action.value if item.signal else None,
                   "status": item.status, "error": item.error, "error_code": item.error_code,
                   "blocked_reasons": item.blocked_reasons,
                   "quote_timestamp": item.candidate.quote_timestamp,
                   "quote_volume": item.candidate.quote_volume, "spread_bps": item.candidate.spread_bps,
                   "data_quality": item.data_quality} for item in report.items],
    }
    print(json.dumps(summary, indent=2, allow_nan=False))
    if report.status not in {"complete", "empty"} or report.analyzed_markets != 1:
        return 1
    item = report.items[0]
    if set(item.data_quality) != {"15m", "1h", "4h"}:
        return 1
    if any(frame["rows"] < 220 for frame in item.data_quality.values()):
        return 1
    # NO_TRADE or price outside a confirmed entry zone is a normal market result.
    # Data/quote failures are not a successful integration check.
    return 0 if item.error_code in {None, "outside_entry_zone", "below_minimum_contracts", "below_minimum_notional", "nonpositive_net_reward"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
