from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class BacktestConfig:
    strategy_path: str
    start_date: str
    end_date: str
    initial_cash: float
    cache_db: str
    output_dir: str = "backtest_runs"
    strategy_name: Optional[str] = None
    strategy_version: Optional[str] = None
    strategy_source: Optional[str] = None
    strategy_hash: Optional[str] = None
    project_strategy_path: Optional[str] = None
    project_strategy_version: Optional[str] = None
    project_strategy_hash: Optional[str] = None
    project_strategy_is_newer: Optional[bool] = None
    git_commit: Optional[str] = None
    benchmark: str = "399006.XSHE"
    cache_mode: str = "strict_local"
    optimize_data: bool = True

    def to_json_dict(self) -> dict:
        payload = asdict(self)
        payload["strategy_path"] = str(self.strategy_path)
        payload["cache_db"] = str(self.cache_db)
        payload["output_dir"] = str(self.output_dir)
        return payload


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    run_dir: Path
    logs_dir: Path
    trades_dir: Path
    reports_dir: Path
    signals_dir: Path
    artifacts_dir: Path
