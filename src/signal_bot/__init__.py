"""Professional, exchange-agnostic market signal and research engine."""

from .backtest import RegimeBacktester
from .config import (
    IndicatorConfig,
    RegimeBotConfig,
    RiskConfig,
    SignalConfig,
)
from .engine import RegimeSignalEngine
from .history import HistoryRequest, HistoryResult, HistoryService
from .models import (
    BacktestResult,
    BacktestTrade,
    EntryPlan,
    MarketRegime,
    MarketSignal,
    PositionSide,
    SetupType,
    SignalAction,
    TimeframeState,
    to_dict,
)
from .research import ResearchService, ValidationThresholds
from .scanner import MarketScanner, ScanReport, ScanRequest
from .storage import SignalStore

__version__ = "2.0.0"

__all__ = [
    "BacktestResult",
    "BacktestTrade",
    "EntryPlan",
    "HistoryRequest",
    "HistoryResult",
    "HistoryService",
    "IndicatorConfig",
    "MarketRegime",
    "MarketScanner",
    "MarketSignal",
    "PositionSide",
    "RegimeBacktester",
    "RegimeBotConfig",
    "RegimeSignalEngine",
    "ResearchService",
    "RiskConfig",
    "ScanReport",
    "ScanRequest",
    "SetupType",
    "SignalAction",
    "SignalConfig",
    "SignalStore",
    "TimeframeState",
    "ValidationThresholds",
    "to_dict",
]
