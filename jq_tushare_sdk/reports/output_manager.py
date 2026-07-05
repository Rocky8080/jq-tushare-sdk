import json
from pathlib import Path
from typing import Callable

from jq_tushare_sdk import __version__
from jq_tushare_sdk.config import BacktestConfig, RunManifest


class OutputManager:
    def __init__(self, clock: Callable[[], str] | None = None):
        self._clock = clock

    def _timestamp(self) -> str:
        if self._clock:
            return self._clock()
        from datetime import datetime

        return datetime.now().strftime("%Y%m%d-%H%M%S")

    def create_run(self, config: BacktestConfig) -> RunManifest:
        root = Path(config.output_dir)
        root.mkdir(parents=True, exist_ok=True)

        base_name = config.strategy_name or Path(config.strategy_path).stem
        date_span = f"{config.start_date.replace('-', '')}_{config.end_date.replace('-', '')}"
        prefix = f"{self._timestamp()}_{base_name}_{date_span}"

        suffix = 1
        while True:
            run_id = prefix if suffix == 1 else f"{prefix}_{suffix}"
            run_dir = root / run_id
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                suffix += 1

        # run_dir has been claimed above with an atomic mkdir.

        logs_dir = run_dir / "logs"
        trades_dir = run_dir / "trades"
        reports_dir = run_dir / "reports"
        signals_dir = run_dir / "signals"
        artifacts_dir = run_dir / "artifacts"
        for path in (logs_dir, trades_dir, reports_dir, signals_dir, artifacts_dir):
            path.mkdir(parents=True, exist_ok=True)

        manifest = RunManifest(
            run_id=run_id,
            run_dir=run_dir,
            logs_dir=logs_dir,
            trades_dir=trades_dir,
            reports_dir=reports_dir,
            signals_dir=signals_dir,
            artifacts_dir=artifacts_dir,
        )

        self._write_json(run_dir / "config.json", config.to_json_dict())
        self._write_json(run_dir / "manifest.json", self._manifest_payload(config, manifest))
        (root / "latest").write_text(run_id + "\n", encoding="utf-8")
        return manifest

    def _manifest_payload(self, config: BacktestConfig, manifest: RunManifest) -> dict:
        return {
            "run_id": manifest.run_id,
            "strategy_path": config.strategy_path,
            "strategy_name": config.strategy_name or Path(config.strategy_path).stem,
            "strategy_version": config.strategy_version,
            "strategy_source": config.strategy_source,
            "strategy_hash": config.strategy_hash,
            "project_strategy_path": config.project_strategy_path,
            "project_strategy_version": config.project_strategy_version,
            "project_strategy_hash": config.project_strategy_hash,
            "project_strategy_is_newer": config.project_strategy_is_newer,
            "start_date": config.start_date,
            "end_date": config.end_date,
            "initial_cash": config.initial_cash,
            "cache_db": config.cache_db,
            "sdk_version": __version__,
            "git_commit": config.git_commit,
            "benchmark": config.benchmark,
            "cache_mode": config.cache_mode,
        }

    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
