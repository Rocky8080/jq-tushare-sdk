"""Public continuous daily execution contract for dependent applications.

The caller owns persistence, scheduling, worker isolation and the UI. ``run_day``
returns an uncommitted calculation and a trusted internal checkpoint. Historical
results must never be recomputed implicitly when this package is upgraded.
"""

from jq_tushare_sdk.runtime.daily.runner import CHECKPOINT_VERSION, RUNTIME_CONTRACT, run_day
from jq_tushare_sdk.runtime.daily.daily_portal import DailyDataUnavailable
from jq_tushare_sdk.runtime.daily.preflight import analyze_source, inspect_readiness
from jq_tushare_sdk.data.daily_sync import DailyDataSync, DataSyncError

__all__ = [
    "CHECKPOINT_VERSION", "RUNTIME_CONTRACT", "run_day", "DailyDataUnavailable",
    "analyze_source", "inspect_readiness", "DailyDataSync", "DataSyncError",
]
