"""Connect public scanner observations to the explicitly hypothetical account."""
from __future__ import annotations

from typing import Any

from .paper import PaperTracker


def observe_paper_scan(scanner: Any, tracker: PaperTracker, report: Any) -> dict[str, Any]:
    """Observe existing exposure before admitting additional opportunities.

    Positions outside the current ranked universe still need quotes. A missing
    observation is explicit; it is never silently treated as an unchanged price.
    """
    payload = report.to_dict() if hasattr(report, "to_dict") else report
    candidates = {item["candidate"]["symbol"]: item["candidate"] for item in payload.get("items", [])}
    open_symbols = {position["symbol"] for position in tracker.positions(status="open", limit=100)}
    missing = open_symbols.difference(candidates)
    errors = []
    if missing:
        try:
            extra = scanner.paper_quotes(tuple(sorted(missing)))
            candidates.update({symbol: value.to_dict() if hasattr(value, "to_dict") else value for symbol, value in extra.items()})
        except Exception as exc:
            errors.append({"code": "paper_quote_fetch_failed", "message": str(exc)})
    for symbol in sorted(open_symbols):
        quote = candidates.get(symbol)
        try:
            if quote is None:
                raise ValueError("The open paper position has no verified quote in this observation.")
            tracker.observe_quote(symbol, bid=quote.get("bid"), ask=quote.get("ask"),
                                  quote_timestamp=quote.get("quote_timestamp"))
        except (ValueError, TypeError) as exc:
            tracker.mark_monitoring_gap(symbol=symbol, reason="quote_observation_unavailable")
            errors.append({"code": "paper_observation_gap", "symbol": symbol, "message": str(exc)})
    admissions = tracker.process_scan(report)
    return {"admissions": admissions, "summary": tracker.summary(), "errors": errors}
