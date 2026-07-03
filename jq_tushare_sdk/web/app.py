from __future__ import annotations

import calendar
import json
import mimetypes
import os
import re
import sqlite3
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from jq_tushare_sdk import __version__
from jq_tushare_sdk.config import BacktestConfig, RunManifest
from jq_tushare_sdk.reports.refresher import refresh_backtest_report


_REQUIRED_LOCAL_APIS = [
    "trade_cal",
    "daily",
    "daily_basic",
    "index_weight",
    "stock_basic",
    "index_daily",
    "income",
]

_EXCLUDED_STRATEGY_DIRS = {
    ".git",
    ".pytest_cache",
    ".superpowers",
    ".jqts_web",
    "__pycache__",
    "backtest_runs",
    "build",
    "data",
    "dist",
    "jq_tushare_sdk",
    "scripts",
    "venv",
    ".venv",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class BacktestRequest:
    strategy_path: str
    start_date: str
    end_date: str
    initial_cash: float
    cache_db: str | None = None
    output_dir: str | None = None
    optimize_data: bool = True

    @classmethod
    def from_payload(cls, payload: dict) -> "BacktestRequest":
        return cls(
            strategy_path=str(payload.get("strategy_path") or "").strip(),
            start_date=str(payload.get("start_date") or "").strip(),
            end_date=str(payload.get("end_date") or "").strip(),
            initial_cash=float(payload.get("initial_cash") or 0),
            cache_db=_optional_text(payload.get("cache_db")),
            output_dir=_optional_text(payload.get("output_dir")),
            optimize_data=bool(payload.get("optimize_data", True)),
        )


class RunStore:
    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)

    def list_runs(self) -> list[dict]:
        if not self.output_dir.exists():
            return []

        runs = []
        for manifest_path in sorted(self.output_dir.glob("*/manifest.json"), reverse=True):
            run_dir = manifest_path.parent
            manifest = _read_json(manifest_path)
            summary = _read_json(run_dir / "reports" / "summary.json")
            run_id = str(manifest.get("run_id") or run_dir.name)
            report_file = run_dir / "reports" / "report.html"
            has_report = report_file.exists()
            initial_cash = _to_float(summary.get("initial_cash"), manifest.get("initial_cash"))
            final_value = _to_float(summary.get("final_value"))
            return_rate = None
            if initial_cash and final_value is not None:
                return_rate = final_value / initial_cash - 1
            profile = summary.get("performance_profile") if isinstance(summary, dict) else {}
            runs.append(
                {
                    "run_id": run_id,
                    "strategy_name": manifest.get("strategy_name") or Path(str(manifest.get("strategy_path", ""))).stem,
                    "strategy_path": manifest.get("strategy_path"),
                    "start_date": manifest.get("start_date"),
                    "end_date": manifest.get("end_date"),
                    "initial_cash": initial_cash,
                    "final_value": final_value,
                    "return_rate": return_rate,
                    "trade_count": summary.get("trade_count"),
                    "sdk_version": manifest.get("sdk_version"),
                    "duration_seconds": _to_float((profile or {}).get("total_seconds")),
                    "status": "completed" if has_report else "incomplete",
                    "report_path": f"{run_id}/reports/report.html" if has_report else None,
                    "run_dir": str(run_dir),
                }
            )
        return runs

    def read_log_tail(self, run_id: str, *, max_lines: int = 300) -> list[str]:
        run_dir = self._safe_run_dir(run_id)
        log_path = run_dir / "logs" / "backtest.log"
        if not log_path.exists():
            return []
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max_lines:]

    def _safe_run_dir(self, run_id: str) -> Path:
        root = self.output_dir.resolve()
        run_dir = (root / Path(run_id).name).resolve()
        if not run_dir.is_relative_to(root):
            raise ValueError("invalid run id")
        return run_dir


class BacktestJobManager:
    def __init__(
        self,
        *,
        project_root: str | Path,
        default_cache_db: str | Path,
        output_dir: str | Path,
        runner: Callable[[BacktestConfig], RunManifest] | None = None,
        synchronous: bool = False,
    ):
        self.project_root = Path(project_root).resolve()
        self.default_cache_db = Path(default_cache_db)
        self.output_dir = Path(output_dir)
        self.runner = runner or _run_local_backtest
        self.synchronous = synchronous
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}

    def start(self, request: BacktestRequest) -> dict:
        config = self._config_from_request(request)
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "strategy_name": Path(config.strategy_path).stem,
            "strategy_path": config.strategy_path,
            "start_date": config.start_date,
            "end_date": config.end_date,
            "initial_cash": config.initial_cash,
            "cache_db": config.cache_db,
            "output_dir": config.output_dir,
            "optimize_data": config.optimize_data,
            "run_id": None,
            "run_dir": None,
            "report_path": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = job

        if self.synchronous:
            self._run_job(job_id, config)
        else:
            thread = threading.Thread(target=self._run_job, args=(job_id, config), daemon=True)
            thread.start()
        return self.get(job_id)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [dict(job) for job in sorted(self._jobs.values(), key=lambda item: item["created_at"], reverse=True)]

    def get(self, job_id: str) -> dict:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            return dict(self._jobs[job_id])

    def _run_job(self, job_id: str, config: BacktestConfig) -> None:
        self._update(job_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))
        try:
            manifest = self.runner(config)
        except Exception as exc:
            self._update(
                job_id,
                status="failed",
                finished_at=datetime.now().isoformat(timespec="seconds"),
                error=str(exc),
                traceback=traceback.format_exc(limit=20),
            )
            return
        self._update(
            job_id,
            status="completed",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            run_id=manifest.run_id,
            run_dir=str(manifest.run_dir),
            report_path=f"{manifest.run_id}/reports/report.html",
        )

    def _update(self, job_id: str, **changes) -> None:
        with self._lock:
            self._jobs[job_id].update(changes)

    def _config_from_request(self, request: BacktestRequest) -> BacktestConfig:
        if not request.strategy_path:
            raise ValueError("strategy_path is required")
        if not _DATE_RE.match(request.start_date) or not _DATE_RE.match(request.end_date):
            raise ValueError("start_date and end_date must use YYYY-MM-DD")
        if request.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        strategy_path = self._resolve_strategy_path(request.strategy_path)
        cache_db = Path(request.cache_db) if request.cache_db else self.default_cache_db
        output_dir = Path(request.output_dir) if request.output_dir else self.output_dir
        return BacktestConfig(
            strategy_path=str(strategy_path),
            start_date=request.start_date,
            end_date=request.end_date,
            initial_cash=float(request.initial_cash),
            cache_db=str(cache_db),
            output_dir=str(output_dir),
            optimize_data=bool(request.optimize_data),
        )

    def _resolve_strategy_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.project_root):
            raise ValueError("strategy_path must be inside project_root")
        if candidate.suffix != ".py" or not candidate.is_file():
            raise ValueError("strategy_path must point to an existing Python file")
        return candidate


def discover_strategies(project_root: str | Path) -> list[dict]:
    root = Path(project_root).resolve()
    if not root.exists():
        return []
    strategies = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if _is_excluded_strategy_path(rel):
            continue
        if not _looks_like_joinquant_strategy(path):
            continue
        strategies.append(
            {
                "name": path.stem,
                "path": str(path),
                "relative_path": rel.as_posix(),
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return sorted(strategies, key=lambda item: item["relative_path"])


def save_uploaded_strategy_file(project_root: str | Path, payload: dict) -> dict:
    root = Path(project_root).resolve()
    filename = _safe_upload_filename(str(payload.get("filename") or "strategy.py"))
    content = str(payload.get("content") or "")
    if not filename.endswith(".py"):
        raise ValueError("strategy file must be a .py file")
    if not _looks_like_joinquant_strategy_text(content):
        raise ValueError("strategy file must define initialize(context)")

    upload_dir = root / ".jqts_web" / "strategies"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    target = (upload_dir / f"{stamp}_{suffix}_{filename}").resolve()
    target.write_text(content, encoding="utf-8")
    relative = target.relative_to(root).as_posix()
    return {
        "filename": filename,
        "path": str(target),
        "relative_path": relative,
        "strategy_path": relative,
    }


def default_backtest_dates(cache_db: str | Path, *, today: date | None = None) -> dict[str, str]:
    today = today or date.today()
    open_days = _cached_open_trade_days(cache_db, before=today)
    end_date = open_days[-1] if open_days else _previous_weekday(today)
    target_start = _subtract_one_month(end_date)
    start_date = _first_open_on_or_after(open_days, target_start, end_date) if open_days else target_start
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def serve_web_console(
    *,
    project_root: str = ".",
    cache_db: str = "data/jq_tushare_cache.db",
    output_dir: str = "backtest_runs",
    host: str = "127.0.0.1",
    port: int = 8790,
) -> int:
    root = Path(project_root).resolve()
    manager = BacktestJobManager(
        project_root=root,
        default_cache_db=Path(cache_db),
        output_dir=Path(output_dir),
    )
    run_store = RunStore(output_dir)
    handler = _handler_factory(
        project_root=root,
        cache_db=Path(cache_db),
        output_dir=Path(output_dir),
        manager=manager,
        run_store=run_store,
    )
    server = ThreadingHTTPServer((host, port), handler)
    actual_port = server.server_address[1]
    print(f"JQ Tushare SDK Web console: http://{host}:{actual_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _handler_factory(
    *,
    project_root: Path,
    cache_db: Path,
    output_dir: Path,
    manager: BacktestJobManager,
    run_store: RunStore,
    today: Callable[[], date] | None = None,
):
    class WebConsoleHandler(BaseHTTPRequestHandler):
        server_version = "JQTushareWeb/0.1"

        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/":
                    self._write_html(_render_app_html(project_root, cache_db, output_dir), include_body=False)
                elif path == "/history":
                    self._write_html(_render_history_html(), include_body=False)
                elif path.startswith("/runs/"):
                    self._serve_run_file(path[len("/runs/") :], include_body=False)
                elif path.startswith("/api/"):
                    self._write_json({}, include_body=False)
                else:
                    self._write_error(HTTPStatus.NOT_FOUND, "not found", include_body=False)
            except Exception as exc:
                self._write_error(HTTPStatus.BAD_REQUEST, str(exc), include_body=False)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/":
                    self._write_html(_render_app_html(project_root, cache_db, output_dir))
                elif path == "/history":
                    self._write_html(_render_history_html())
                elif path == "/api/config":
                    self._write_json(
                        {
                            "sdk_version": __version__,
                            "project_root": str(project_root),
                            "cache_db": str(cache_db),
                            "output_dir": str(output_dir),
                        }
                    )
                elif path == "/api/default-dates":
                    self._write_json(default_backtest_dates(cache_db, today=today() if today else None))
                elif path == "/api/strategies":
                    self._write_json({"strategies": discover_strategies(project_root)})
                elif path == "/api/runs":
                    self._write_json({"runs": run_store.list_runs()})
                elif path == "/api/jobs":
                    self._write_json({"jobs": manager.list_jobs()})
                elif path.startswith("/api/jobs/"):
                    self._write_json({"job": manager.get(Path(path).name)})
                elif path.startswith("/api/logs/"):
                    self._write_json({"lines": run_store.read_log_tail(Path(path).name)})
                elif path.startswith("/runs/"):
                    self._serve_run_file(path[len("/runs/") :])
                else:
                    self._write_error(HTTPStatus.NOT_FOUND, "not found")
            except KeyError:
                self._write_error(HTTPStatus.NOT_FOUND, "not found")
            except Exception as exc:
                self._write_error(HTTPStatus.BAD_REQUEST, str(exc))

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                payload = self._read_payload()
                if parsed.path == "/api/backtests":
                    job = manager.start(BacktestRequest.from_payload(payload))
                    self._write_json({"job": job}, status=HTTPStatus.ACCEPTED)
                elif parsed.path == "/api/strategy-file":
                    strategy = save_uploaded_strategy_file(project_root, payload)
                    self._write_json({"strategy": strategy})
                elif parsed.path == "/api/check-data":
                    request = BacktestRequest.from_payload(payload)
                    issues = _check_data_readiness(
                        manager._config_from_request(request),
                    )
                    self._write_json({"ok": not issues, "issues": issues})
                elif parsed.path == "/api/refresh-report":
                    self._write_json(_refresh_report_request(run_store, cache_db, payload))
                else:
                    self._write_error(HTTPStatus.NOT_FOUND, "not found")
            except Exception as exc:
                self._write_error(HTTPStatus.BAD_REQUEST, str(exc))

        def log_message(self, format: str, *args) -> None:
            return

        def _read_payload(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _write_json(
            self,
            payload: dict,
            *,
            status: HTTPStatus = HTTPStatus.OK,
            include_body: bool = True,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _write_html(self, html: str, *, include_body: bool = True) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _write_error(self, status: HTTPStatus, message: str, *, include_body: bool = True) -> None:
            self._write_json({"error": message}, status=status, include_body=include_body)

        def _serve_run_file(self, raw_relative: str, *, include_body: bool = True) -> None:
            relative = Path(unquote(raw_relative))
            if relative.is_absolute():
                raise ValueError("invalid run file")
            root = output_dir.resolve()
            file_path = (root / relative).resolve()
            if not file_path.is_relative_to(root) or not file_path.is_file():
                self._write_error(HTTPStatus.NOT_FOUND, "not found")
                return
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            body = file_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)

    return WebConsoleHandler


def _run_local_backtest(config: BacktestConfig) -> RunManifest:
    from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
    from jq_tushare_sdk.runtime.engine import BacktestEngine

    backend = TushareCacheBackend(
        config.cache_db,
        token=os.environ.get("TUSHARE_TOKEN"),
        cache_mode="strict_local",
    )
    issues = _check_data_readiness(config, backend=backend)
    if issues:
        lines = ["Local cache is not ready:"]
        lines.extend(f"{item['api_name']}: {item['message']} {item['suggestion']}" for item in issues)
        raise RuntimeError("\n".join(lines))
    return BacktestEngine(config, backend=backend).run()


def _check_data_readiness(config: BacktestConfig, *, backend=None) -> list[dict]:
    from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
    from jq_tushare_sdk.data.readiness import DataReadinessCheck

    if backend is None:
        backend = TushareCacheBackend(
            config.cache_db,
            token=os.environ.get("TUSHARE_TOKEN"),
            cache_mode="strict_local",
        )
    issues = DataReadinessCheck(backend).check_required(config, _REQUIRED_LOCAL_APIS)
    return [
        {
            "api_name": issue.api_name,
            "message": issue.message,
            "suggestion": issue.suggestion,
        }
        for issue in issues
    ]


def _refresh_report_request(run_store: RunStore, cache_db: Path, payload: dict) -> dict:
    from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend

    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    backend = TushareCacheBackend(
        str(cache_db),
        token=os.environ.get("TUSHARE_TOKEN"),
        cache_mode="strict_local",
    )
    return refresh_backtest_report(run_store._safe_run_dir(run_id), backend=backend)


def _render_app_html(project_root: Path, cache_db: Path, output_dir: Path) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>JQ Tushare SDK Web 控制台</title>
  <style>{_app_css()}</style>
</head>
<body>
  <header class="topbar">
    <div>
      <div class="kicker">Local Backtest Console</div>
      <h1>JQ Tushare SDK</h1>
    </div>
    <div class="top-meta">
      <span>SDK v{escape(__version__)}</span>
      <span>127.0.0.1</span>
    </div>
  </header>
  <main class="workspace">
    <section class="launch-panel" aria-label="新建回测">
      <div class="panel-head">
        <div>
          <div class="section-label">新建回测</div>
          <h2>运行参数</h2>
        </div>
        <span class="badge">CLI 同口径</span>
      </div>
      <form id="backtest-form" class="form-grid">
        <label>
          <span>策略程序</span>
          <select id="strategy-select" name="strategy_path" required></select>
        </label>
        <div class="two-col">
          <label>
            <span>开始日期</span>
            <input id="start-date" name="start_date" type="date" required>
          </label>
          <label>
            <span>结束日期</span>
            <input id="end-date" name="end_date" type="date" required>
          </label>
        </div>
        <label>
          <span>初始资金</span>
          <input id="initial-cash" name="initial_cash" type="number" min="1" step="1" value="1000000" required>
        </label>
        <label>
          <span>缓存数据库</span>
          <input id="cache-db" name="cache_db" type="text" value="{escape(str(cache_db))}" required>
        </label>
        <label>
          <span>结果目录</span>
          <input id="output-dir" name="output_dir" type="text" value="{escape(str(output_dir))}" required>
        </label>
        <label class="switch-row">
          <input id="optimize-data" name="optimize_data" type="checkbox" checked>
          <span>启用数据层批量优化</span>
        </label>
        <div class="action-row">
          <button type="submit" class="primary">运行回测</button>
          <button type="button" id="check-data" class="secondary">检查数据</button>
        </div>
      </form>
      <div id="notice" class="notice">项目目录：{escape(str(project_root))}</div>
    </section>

    <section class="main-panel">
      <div class="status-strip">
        <div>
          <div class="section-label">运行中</div>
          <h2>任务状态</h2>
        </div>
        <button type="button" id="refresh" class="text-button">刷新</button>
      </div>
      <div id="jobs" class="job-list"></div>

      <div class="results-layout">
        <section class="runs-panel">
          <div class="panel-head">
            <div>
              <div class="section-label">历史结果</div>
              <h2>回测记录</h2>
            </div>
            <input id="run-filter" class="search" placeholder="搜索策略或日期">
          </div>
          <div id="runs-list" class="run-list"></div>
        </section>

        <aside class="preview-panel">
          <div class="section-label">报告预览</div>
          <h2 id="preview-title">选择一条记录</h2>
          <div class="metric-grid">
            <div><span>策略收益</span><b id="preview-return">--</b></div>
            <div><span>期末权益</span><b id="preview-final">--</b></div>
            <div><span>成交次数</span><b id="preview-trades">--</b></div>
            <div><span>运行耗时</span><b id="preview-duration">--</b></div>
          </div>
          <div class="preview-actions">
            <button type="button" id="open-report" class="primary" disabled>打开报告</button>
            <button type="button" id="show-log" class="secondary" disabled>查看日志</button>
          </div>
          <pre id="log-tail" class="log-tail"></pre>
        </aside>
      </div>
    </section>
  </main>
  <script>{_app_js()}</script>
</body>
</html>"""


def _app_css() -> str:
    return """
:root {
  --bg: #eef2f6;
  --panel: #ffffff;
  --panel-soft: #f7f9fb;
  --border: #d9e1ea;
  --text: #1f2d3a;
  --muted: #6d7a87;
  --blue: #2568ad;
  --blue-soft: #eef4fb;
  --green: #1c7c46;
  --red: #d84a4a;
  --amber: #a45b11;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
}
.topbar {
  height: 68px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
.kicker, .section-label {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0;
  text-transform: uppercase;
}
h1, h2 { margin: 0; letter-spacing: 0; }
h1 { font-size: 22px; }
h2 { font-size: 18px; }
.top-meta { display: flex; gap: 10px; align-items: center; color: var(--muted); }
.top-meta span, .badge {
  border: 1px solid #c9d8e6;
  border-radius: 999px;
  padding: 5px 10px;
  background: #f5f9ff;
  color: var(--blue);
}
.workspace {
  display: grid;
  grid-template-columns: minmax(320px, 390px) minmax(0, 1fr);
  gap: 16px;
  padding: 16px;
}
.launch-panel, .main-panel, .runs-panel, .preview-panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.launch-panel { padding: 18px; align-self: start; position: sticky; top: 16px; }
.panel-head, .status-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.form-grid { display: grid; gap: 14px; margin-top: 18px; }
label { display: grid; gap: 6px; color: var(--muted); }
label span { font-weight: 700; }
input, select {
  width: 100%;
  height: 38px;
  border: 1px solid #ccd7e3;
  border-radius: 6px;
  padding: 0 10px;
  background: #fbfdff;
  color: var(--text);
  font: inherit;
}
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.switch-row {
  grid-template-columns: 18px 1fr;
  align-items: center;
  color: var(--text);
}
.switch-row input { height: 18px; padding: 0; }
.action-row, .preview-actions { display: flex; gap: 10px; }
button {
  border: 1px solid transparent;
  border-radius: 6px;
  height: 38px;
  padding: 0 14px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}
button:disabled { opacity: .5; cursor: not-allowed; }
.primary { background: var(--blue); color: white; }
.secondary { background: var(--blue-soft); color: var(--blue); border-color: #c6d8eb; }
.text-button { background: transparent; color: var(--blue); height: 32px; }
.notice {
  margin-top: 14px;
  padding: 10px;
  border: 1px solid #e2e8ef;
  border-radius: 6px;
  background: var(--panel-soft);
  color: var(--muted);
  line-height: 1.5;
  word-break: break-all;
}
.main-panel { padding: 16px; min-width: 0; }
.job-list {
  display: grid;
  gap: 8px;
  margin-top: 12px;
  min-height: 44px;
}
.job-item {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) 110px 140px 120px;
  gap: 12px;
  align-items: center;
  padding: 10px 12px;
  background: var(--panel-soft);
  border: 1px solid #e2e8ef;
  border-radius: 6px;
}
.pill {
  display: inline-flex;
  justify-content: center;
  border-radius: 999px;
  padding: 4px 9px;
  font-size: 12px;
  font-weight: 700;
  background: #e8f5ed;
  color: var(--green);
}
.pill.running, .pill.queued { background: #fff2df; color: var(--amber); }
.pill.failed { background: #fdecec; color: var(--red); }
.results-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.45fr) minmax(300px, .75fr);
  gap: 16px;
  margin-top: 16px;
}
.runs-panel, .preview-panel { padding: 16px; min-width: 0; }
#preview-title {
  overflow-wrap: anywhere;
  line-height: 1.25;
}
.search { max-width: 220px; height: 34px; }
.run-list {
  display: grid;
  margin-top: 12px;
  border: 1px solid #e2e8ef;
  border-radius: 6px;
  overflow: hidden;
}
.run-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 78px 70px;
  gap: 10px;
  align-items: center;
  min-height: 68px;
  padding: 10px 12px;
  border-bottom: 1px solid #e7edf3;
  cursor: pointer;
}
.run-row:last-child { border-bottom: 0; }
.run-row:hover, .run-row.selected { background: #f2f7fc; }
.run-main strong, .run-main .muted {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.run-return, .run-duration { text-align: right; }
.run-return span, .run-duration span {
  display: block;
  color: var(--muted);
  font-size: 11px;
  margin-bottom: 3px;
}
.positive { color: var(--red); font-weight: 800; }
.negative { color: var(--green); font-weight: 800; }
.muted { color: var(--muted); }
.metric-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin: 16px 0;
}
.metric-grid div {
  border: 1px solid #e2e8ef;
  border-radius: 6px;
  background: var(--panel-soft);
  padding: 10px;
}
.metric-grid span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }
.metric-grid b { font-size: 18px; }
.log-tail {
  display: none;
  margin: 14px 0 0;
  max-height: 280px;
  overflow: auto;
  border: 1px solid #d8e0e8;
  border-radius: 6px;
  background: #101820;
  color: #dce9f5;
  padding: 12px;
  white-space: pre-wrap;
}
@media (max-width: 1050px) {
  .workspace, .results-layout { grid-template-columns: 1fr; }
  .launch-panel { position: static; }
}
@media (max-width: 680px) {
  .topbar { height: auto; align-items: flex-start; gap: 10px; padding: 14px; flex-direction: column; }
  .workspace { padding: 10px; }
  .two-col, .job-item { grid-template-columns: 1fr; }
  .metric-grid { grid-template-columns: 1fr; }
}
"""


def _app_js() -> str:
    return """
const state = { strategies: [], runs: [], jobs: [], selectedRun: null };

const $ = (id) => document.getElementById(id);
const money = (value) => value == null ? '--' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
const seconds = (value) => value == null ? '--' : `${Number(value).toFixed(2)}s`;
const pct = (value) => value == null ? '--' : `${(Number(value) * 100).toFixed(2)}%`;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function requestPayload() {
  return {
    strategy_path: $('strategy-select').value,
    start_date: $('start-date').value,
    end_date: $('end-date').value,
    initial_cash: Number($('initial-cash').value),
    cache_db: $('cache-db').value,
    output_dir: $('output-dir').value,
    optimize_data: $('optimize-data').checked,
  };
}

function setNotice(message, tone = '') {
  $('notice').textContent = message;
  $('notice').className = `notice ${tone}`;
}

async function loadStrategies() {
  const payload = await api('/api/strategies');
  state.strategies = payload.strategies;
  const select = $('strategy-select');
  select.innerHTML = '';
  for (const strategy of state.strategies) {
    const option = document.createElement('option');
    option.value = strategy.relative_path;
    option.textContent = strategy.relative_path;
    select.appendChild(option);
  }
  if (!state.strategies.length) {
    const option = document.createElement('option');
    option.textContent = '未找到策略文件';
    option.value = '';
    select.appendChild(option);
  }
}

async function loadJobs() {
  const payload = await api('/api/jobs');
  state.jobs = payload.jobs;
  renderJobs();
}

async function loadRuns() {
  const payload = await api('/api/runs');
  state.runs = payload.runs;
  renderRuns();
  if (!state.selectedRun && state.runs.length) {
    selectRun(state.runs[0].run_id);
  }
}

function renderJobs() {
  const root = $('jobs');
  root.innerHTML = '';
  if (!state.jobs.length) {
    root.innerHTML = '<div class="notice">当前没有运行中的 Web 任务。</div>';
    return;
  }
  for (const job of state.jobs.slice(0, 5)) {
    const div = document.createElement('div');
    div.className = 'job-item';
    div.innerHTML = `
      <strong>${escapeHtml(job.strategy_name || 'unknown')}</strong>
      <span class="pill ${job.status}">${statusText(job.status)}</span>
      <span class="muted">${escapeHtml(job.start_date)} - ${escapeHtml(job.end_date)}</span>
      <span>${job.report_path ? `<button class="secondary" data-report="${escapeHtml(job.report_path)}">报告</button>` : ''}</span>
    `;
    const button = div.querySelector('button[data-report]');
    if (button) button.addEventListener('click', () => window.open(`/runs/${button.dataset.report}`, '_blank'));
    root.appendChild(div);
  }
}

function renderRuns() {
  const filter = $('run-filter').value.trim().toLowerCase();
  const list = $('runs-list');
  list.innerHTML = '';
  const rows = state.runs.filter((run) => {
    const text = `${run.strategy_name} ${run.start_date} ${run.end_date} ${run.sdk_version}`.toLowerCase();
    return !filter || text.includes(filter);
  });
  if (!rows.length) {
    list.innerHTML = '<div class="notice">没有匹配的回测记录。</div>';
    return;
  }
  for (const run of rows) {
    const row = document.createElement('div');
    row.className = 'run-row';
    row.dataset.runId = run.run_id;
    if (state.selectedRun && state.selectedRun.run_id === run.run_id) row.classList.add('selected');
    const cls = Number(run.return_rate) >= 0 ? 'positive' : 'negative';
    row.innerHTML = `
      <div class="run-main">
        <strong>${escapeHtml(run.strategy_name || '')}</strong>
        <span class="muted">${escapeHtml(run.start_date || '')} - ${escapeHtml(run.end_date || '')} · SDK ${escapeHtml(run.sdk_version || '--')}</span>
        <span class="muted">${escapeHtml(run.run_id)}</span>
      </div>
      <div class="run-return"><span>收益</span><b class="${cls}">${pct(run.return_rate)}</b></div>
      <div class="run-duration"><span>耗时</span><b>${seconds(run.duration_seconds)}</b></div>
    `;
    row.addEventListener('click', () => selectRun(run.run_id));
    list.appendChild(row);
  }
}

function selectRun(runId) {
  state.selectedRun = state.runs.find((run) => run.run_id === runId) || null;
  const run = state.selectedRun;
  if (!run) return;
  $('preview-title').textContent = run.strategy_name || run.run_id;
  $('preview-return').textContent = pct(run.return_rate);
  $('preview-final').textContent = money(run.final_value);
  $('preview-trades').textContent = run.trade_count == null ? '--' : String(run.trade_count);
  $('preview-duration').textContent = seconds(run.duration_seconds);
  $('open-report').disabled = !run.report_path;
  $('show-log').disabled = false;
  $('log-tail').style.display = 'none';
  $('log-tail').textContent = '';
  renderRuns();
}

function statusText(status) {
  if (status === 'completed') return '完成';
  if (status === 'running') return '运行中';
  if (status === 'queued') return '排队中';
  if (status === 'failed') return '失败';
  return status || '未知';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

async function submitBacktest(event) {
  event.preventDefault();
  try {
    const payload = await api('/api/backtests', {
      method: 'POST',
      body: JSON.stringify(requestPayload()),
    });
    setNotice(`任务已提交：${payload.job.job_id}`);
    await refreshAll();
  } catch (error) {
    setNotice(error.message, 'error');
  }
}

async function checkData() {
  try {
    setNotice('正在检查本地缓存...');
    const payload = await api('/api/check-data', {
      method: 'POST',
      body: JSON.stringify(requestPayload()),
    });
    if (payload.ok) {
      setNotice('数据检查通过，可以运行回测。');
    } else {
      setNotice(payload.issues.map((item) => `${item.api_name}: ${item.message}`).join('\\n'));
    }
  } catch (error) {
    setNotice(error.message, 'error');
  }
}

async function showLog() {
  if (!state.selectedRun) return;
  const payload = await api(`/api/logs/${state.selectedRun.run_id}`);
  const log = $('log-tail');
  log.textContent = payload.lines.join('\\n') || '没有日志内容。';
  log.style.display = 'block';
}

async function refreshAll() {
  await Promise.all([loadJobs(), loadRuns()]);
}

async function setDefaultDates() {
  try {
    const payload = await api('/api/default-dates');
    $('start-date').value = payload.start_date;
    $('end-date').value = payload.end_date;
    return;
  } catch (error) {
    const end = previousWeekday(new Date());
    const start = subtractOneMonth(end);
    $('start-date').value = formatDateInput(start);
    $('end-date').value = formatDateInput(end);
  }
}

function formatDateInput(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

document.addEventListener('DOMContentLoaded', async () => {
  setDefaultDates();
  $('backtest-form').addEventListener('submit', submitBacktest);
  $('check-data').addEventListener('click', checkData);
  $('refresh').addEventListener('click', refreshAll);
  $('run-filter').addEventListener('input', renderRuns);
  $('open-report').addEventListener('click', () => {
    if (state.selectedRun?.report_path) window.open(`/runs/${state.selectedRun.report_path}`, '_blank');
  });
  $('show-log').addEventListener('click', showLog);
  try {
    await loadStrategies();
    await refreshAll();
    setInterval(refreshAll, 3000);
  } catch (error) {
    setNotice(error.message, 'error');
  }
});
"""


def _render_app_html(project_root: Path, cache_db: Path, output_dir: Path) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>JQ Tushare SDK Web 控制台</title>
  <style>{_app_css()}</style>
</head>
<body>
  <header class="topbar">
    <div>
      <div class="kicker">Local Backtest Console</div>
      <h1>JQ Tushare SDK</h1>
    </div>
    <nav class="top-actions" aria-label="页面操作">
      <a id="history-link" class="secondary link-button" href="/history">历史记录</a>
      <span class="badge">SDK v{escape(__version__)}</span>
      <span class="badge">127.0.0.1</span>
    </nav>
  </header>

  <main class="result-workspace">
    <section class="parameter-bar" aria-label="运行参数">
      <form id="backtest-form" class="parameter-form compact">
        <input id="strategy-file" type="file" accept=".py,text/x-python,text/plain" hidden>
        <input id="strategy-path" type="hidden">
        <button type="button" id="choose-strategy" class="secondary">选择策略文件</button>
        <div class="selected-file" id="selected-strategy">未选择策略文件</div>
        <label><span>开始</span><input id="start-date" name="start_date" type="date" required></label>
        <label><span>结束</span><input id="end-date" name="end_date" type="date" required></label>
        <label><span>资金</span><input id="initial-cash" name="initial_cash" type="number" min="1" step="1" value="1000000" required></label>
        <button type="button" id="check-data" class="secondary">检查数据</button>
        <button type="button" id="refresh-report" class="secondary">刷新报告</button>
        <button type="submit" class="primary">运行回测</button>
        <button type="button" id="settings-toggle" class="secondary" aria-expanded="false" aria-controls="settings-panel">设置</button>
      </form>
      <div id="settings-panel" class="settings-panel" hidden>
        <label><span>缓存数据库</span><input id="cache-db" name="cache_db" type="text" value="{escape(str(cache_db))}" required></label>
        <label><span>结果目录</span><input id="output-dir" name="output_dir" type="text" value="{escape(str(output_dir))}" required></label>
        <label class="switch-row"><input id="optimize-data" name="optimize_data" type="checkbox" checked><span>启用数据层批量优化</span></label>
        <div class="settings-note">项目目录：{escape(str(project_root))}</div>
      </div>
      <div id="notice" class="notice inline" hidden></div>
      <div id="jobs" class="job-list compact"></div>
    </section>

    <section class="report-shell" aria-label="回测结果">
      <div id="empty-report" class="empty-report">暂无选中的报告。可以打开历史记录选择一次回测，或在上方选择策略后运行。</div>
      <iframe id="report-frame" title="回测报告" src="about:blank"></iframe>
    </section>
  </main>
  <script>{_app_js()}</script>
</body>
</html>"""


def _render_history_html() -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>历史回测 - JQ Tushare SDK</title>
  <style>{_app_css()}</style>
</head>
<body>
  <header class="topbar">
    <div>
      <div class="kicker">Backtest History</div>
      <h1>历史回测</h1>
    </div>
    <nav class="top-actions">
      <a class="secondary link-button" href="/">返回结果页</a>
      <span class="badge">SDK v{escape(__version__)}</span>
    </nav>
  </header>
  <main class="history-workspace">
    <section class="history-panel">
      <div class="panel-head">
        <div>
          <div class="section-label">选择历史记录</div>
          <h2>回测记录</h2>
        </div>
        <input id="history-filter" class="search" placeholder="搜索策略、日期或版本">
      </div>
      <div id="history-runs" class="history-list"></div>
      <template id="history-link-template"><a href="/?run_id=">选择</a></template>
    </section>
  </main>
  <script>{_history_js()}</script>
</body>
</html>"""


def _app_css() -> str:
    return """
:root {
  --bg: #eef2f6;
  --panel: #ffffff;
  --panel-soft: #f7f9fb;
  --border: #d9e1ea;
  --text: #1f2d3a;
  --muted: #6d7a87;
  --blue: #2568ad;
  --blue-soft: #eef4fb;
  --green: #1c7c46;
  --red: #d84a4a;
  --amber: #a45b11;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
}
.topbar {
  min-height: 64px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 20px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
.kicker, .section-label {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0;
  text-transform: uppercase;
}
h1, h2 { margin: 0; letter-spacing: 0; }
h1 { font-size: 22px; }
h2 { font-size: 18px; }
.top-actions { display: flex; align-items: center; gap: 10px; }
.badge, .link-button {
  border: 1px solid #c9d8e6;
  border-radius: 999px;
  padding: 6px 10px;
  background: #f5f9ff;
  color: var(--blue);
  text-decoration: none;
}
.disabled-link {
  display: inline-flex;
  justify-content: center;
  border: 1px solid #d6dee7;
  border-radius: 999px;
  padding: 6px 10px;
  background: #f2f5f8;
  color: var(--muted);
  font-weight: 700;
}
.result-workspace {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 10px;
  padding: 10px;
  min-height: calc(100vh - 64px);
}
.parameter-bar, .report-shell, .history-panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.parameter-bar { padding: 9px 10px; }
.parameter-form.compact {
  display: grid;
  grid-template-columns: auto minmax(140px, 1fr) 146px 146px 132px auto auto auto auto;
  gap: 8px;
  align-items: center;
}
label { display: grid; gap: 5px; color: var(--muted); }
label span { font-weight: 700; font-size: 12px; }
input {
  width: 100%;
  height: 36px;
  border: 1px solid #ccd7e3;
  border-radius: 6px;
  padding: 0 9px;
  background: #fbfdff;
  color: var(--text);
  font: inherit;
}
.selected-file {
  min-height: 36px;
  display: flex;
  align-items: center;
  padding: 0 10px;
  border: 1px solid #d8e0e8;
  border-radius: 6px;
  background: var(--panel-soft);
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.parameter-form label { min-width: 0; }
.parameter-form.compact label {
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 6px;
}
.parameter-form.compact label span {
  color: var(--muted);
  font-size: 12px;
}
.switch-row {
  grid-template-columns: 18px auto;
  align-items: center;
  gap: 6px;
  color: var(--text);
  height: 36px;
}
.switch-row input { height: 18px; padding: 0; }
.settings-panel {
  margin-top: 8px;
  display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(220px, 1fr) auto minmax(220px, 1.2fr);
  gap: 10px;
  align-items: end;
  padding: 10px;
  border: 1px solid #e2e8ef;
  border-radius: 6px;
  background: var(--panel-soft);
}
.settings-panel[hidden] { display: none; }
.settings-note {
  color: var(--muted);
  line-height: 1.45;
  overflow-wrap: anywhere;
}
button {
  border: 1px solid transparent;
  border-radius: 6px;
  height: 36px;
  padding: 0 13px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
  white-space: nowrap;
}
button:disabled { opacity: .5; cursor: not-allowed; }
.primary { background: var(--blue); color: white; }
.secondary { background: var(--blue-soft); color: var(--blue); border-color: #c6d8eb; }
.notice {
  margin-top: 10px;
  padding: 9px 10px;
  border: 1px solid #e2e8ef;
  border-radius: 6px;
  background: var(--panel-soft);
  color: var(--muted);
  line-height: 1.45;
  white-space: pre-wrap;
}
.notice.error { color: var(--red); background: #fff6f6; border-color: #f1caca; }
.job-list.compact {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}
.job-item {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) 90px 170px auto;
  gap: 10px;
  align-items: center;
  padding: 8px 10px;
  background: var(--panel-soft);
  border: 1px solid #e2e8ef;
  border-radius: 6px;
}
.pill {
  display: inline-flex;
  justify-content: center;
  border-radius: 999px;
  padding: 4px 9px;
  font-size: 12px;
  font-weight: 700;
  background: #e8f5ed;
  color: var(--green);
}
.pill.running, .pill.queued { background: #fff2df; color: var(--amber); }
.pill.failed { background: #fdecec; color: var(--red); }
.report-shell { min-height: 0; overflow: hidden; }
#report-frame {
  width: 100%;
  height: calc(100vh - 150px);
  min-height: 680px;
  border: 0;
  background: white;
  display: none;
}
.empty-report {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 520px;
  color: var(--muted);
}
.history-workspace { padding: 16px; }
.history-panel { max-width: 1180px; margin: 0 auto; padding: 16px; }
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
}
.search { max-width: 320px; }
.history-list {
  display: grid;
  margin-top: 14px;
  border: 1px solid #e2e8ef;
  border-radius: 6px;
  overflow: hidden;
}
.history-row {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) 160px 110px 100px 90px;
  gap: 12px;
  align-items: center;
  min-height: 64px;
  padding: 10px 12px;
  border-bottom: 1px solid #e7edf3;
}
.history-row:last-child { border-bottom: 0; }
.history-row:hover { background: #f2f7fc; }
.history-main strong, .history-main span {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.history-main span, .muted { color: var(--muted); }
.positive { color: var(--red); font-weight: 800; }
.negative { color: var(--green); font-weight: 800; }
@media (max-width: 1180px) {
  .parameter-form.compact {
    grid-template-columns: auto minmax(150px, 1fr) 150px 150px 136px;
  }
  .parameter-form.compact button { padding: 0 10px; }
  .settings-panel { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 760px) {
  .topbar { align-items: flex-start; flex-direction: column; }
  .parameter-form.compact, .settings-panel, .history-row, .job-item { display: grid; grid-template-columns: 1fr; }
  #report-frame { min-height: 560px; }
}
"""


def _app_js() -> str:
    return """
const state = { runs: [], jobs: [], selectedRun: null, strategyPath: '' };

const $ = (id) => document.getElementById(id);
const money = (value) => value == null ? '--' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
const seconds = (value) => value == null ? '--' : `${Number(value).toFixed(2)}s`;
const pct = (value) => value == null ? '--' : `${(Number(value) * 100).toFixed(2)}%`;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function requestPayload() {
  if (!state.strategyPath) throw new Error('请先选择策略文件。');
  return {
    strategy_path: state.strategyPath,
    start_date: $('start-date').value,
    end_date: $('end-date').value,
    initial_cash: Number($('initial-cash').value),
    cache_db: $('cache-db').value,
    output_dir: $('output-dir').value,
    optimize_data: $('optimize-data').checked,
  };
}

function setNotice(message, tone = '') {
  $('notice').textContent = message;
  $('notice').className = `notice inline ${tone}`;
  $('notice').hidden = false;
}

async function chooseStrategyFile() {
  $('strategy-file').click();
}

async function uploadSelectedStrategyFile(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  try {
    const content = await file.text();
    const payload = await api('/api/strategy-file', {
      method: 'POST',
      body: JSON.stringify({ filename: file.name, content }),
    });
    state.strategyPath = payload.strategy.strategy_path;
    $('strategy-path').value = state.strategyPath;
    $('selected-strategy').textContent = `${file.name} -> ${state.strategyPath}`;
    setNotice('策略文件已选择。运行时会使用本项目内的本地副本，不修改原文件。');
  } catch (error) {
    setNotice(error.message, 'error');
  }
}

async function loadJobs() {
  const payload = await api('/api/jobs');
  state.jobs = payload.jobs;
  renderJobs();
}

async function loadRuns() {
  const payload = await api('/api/runs');
  state.runs = payload.runs;
  const requested = new URLSearchParams(window.location.search).get('run_id');
  if (requested) {
    selectRun(requested);
  } else if (!state.selectedRun && state.runs.length) {
    selectRun(state.runs[0].run_id);
  }
}

function renderJobs() {
  const root = $('jobs');
  root.innerHTML = '';
  if (!state.jobs.length) return;
  for (const job of state.jobs.slice(0, 3)) {
    const div = document.createElement('div');
    div.className = 'job-item';
    div.innerHTML = `
      <strong>${escapeHtml(job.strategy_name || 'unknown')}</strong>
      <span class="pill ${job.status}">${statusText(job.status)}</span>
      <span class="muted">${escapeHtml(job.start_date)} - ${escapeHtml(job.end_date)}</span>
      <span>${job.report_path ? `<a class="secondary link-button" href="/?run_id=${escapeHtml(job.run_id)}">打开</a>` : ''}</span>
    `;
    root.appendChild(div);
  }
}

function selectRun(runId) {
  const run = state.runs.find((item) => item.run_id === runId);
  if (!run) return;
  state.selectedRun = run;
  if (!run.report_path) {
    showMissingReport(run);
    return;
  }
  loadReportFrame(run);
}

function loadReportFrame(run, force = false) {
  const frame = $('report-frame');
  const reportSrc = `/runs/${run.report_path}`;
  const currentSrc = frame.getAttribute('src') || '';
  if (force || (frame.getAttribute('src') !== reportSrc && !currentSrc.startsWith(`${reportSrc}?`))) {
    frame.src = force ? `${reportSrc}?t=${Date.now()}` : reportSrc;
  }
  frame.style.display = 'block';
  $('empty-report').style.display = 'none';
}

function forceLoadReport(run) {
  if (!run?.report_path) return;
  loadReportFrame(run, true);
}

function showMissingReport(run) {
  const frame = $('report-frame');
  frame.removeAttribute('src');
  frame.style.display = 'none';
  const empty = $('empty-report');
  empty.textContent = `该回测没有生成报告：${run.run_id}。通常是任务中断、失败或服务重启导致。请查看日志后重新运行。`;
  empty.style.display = 'flex';
}

function toggleSettingsPanel() {
  const panel = $('settings-panel');
  panel.hidden = !panel.hidden;
  $('settings-toggle').textContent = panel.hidden ? '设置' : '收起设置';
  $('settings-toggle').setAttribute('aria-expanded', String(!panel.hidden));
}

function statusText(status) {
  if (status === 'completed') return '完成';
  if (status === 'running') return '运行中';
  if (status === 'queued') return '排队中';
  if (status === 'failed') return '失败';
  if (status === 'incomplete') return '未生成报告';
  return status || '未知';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

async function submitBacktest(event) {
  event.preventDefault();
  try {
    const payload = await api('/api/backtests', {
      method: 'POST',
      body: JSON.stringify(requestPayload()),
    });
    setNotice(`任务已提交：${payload.job.job_id}`);
    await refreshAll();
  } catch (error) {
    setNotice(error.message, 'error');
  }
}

async function checkData() {
  try {
    setNotice('正在检查本地缓存...');
    const payload = await api('/api/check-data', {
      method: 'POST',
      body: JSON.stringify(requestPayload()),
    });
    if (payload.ok) {
      setNotice('数据检查通过，可以运行回测。');
    } else {
      setNotice(payload.issues.map((item) => `${item.api_name}: ${item.message}`).join('\\n'));
    }
  } catch (error) {
    setNotice(error.message, 'error');
  }
}

async function refreshSelectedReport() {
  const params = new URLSearchParams(window.location.search);
  const runId = state.selectedRun?.run_id || params.get('run_id');
  if (!runId) {
    setNotice('请先选择一条历史回测记录。', 'error');
    return;
  }
  try {
    setNotice('正在刷新历史报告...');
    const payload = await api('/api/refresh-report', {
      method: 'POST',
      body: JSON.stringify({ run_id: runId }),
    });
    if (payload.updated) {
      setNotice(`报告已刷新：${payload.benchmark}`);
    } else {
      setNotice(`报告仍有未实现指标：${payload.reason || '基准数据缺失'}`, 'error');
    }
    await refreshAll();
    const run = state.runs.find((item) => item.run_id === runId) || state.selectedRun;
    forceLoadReport(run);
  } catch (error) {
    setNotice(error.message, 'error');
  }
}

async function refreshAll() {
  await Promise.all([loadJobs(), loadRuns()]);
}

async function setDefaultDates() {
  try {
    const payload = await api('/api/default-dates');
    $('start-date').value = payload.start_date;
    $('end-date').value = payload.end_date;
    return;
  } catch (error) {
    const end = previousWeekday(new Date());
    const start = subtractOneMonth(end);
    $('start-date').value = formatDateInput(start);
    $('end-date').value = formatDateInput(end);
  }
}

function formatDateInput(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function previousWeekday(date) {
  const result = new Date(date.getFullYear(), date.getMonth(), date.getDate() - 1);
  while (result.getDay() === 0 || result.getDay() === 6) {
    result.setDate(result.getDate() - 1);
  }
  return result;
}

function subtractOneMonth(date) {
  const firstOfTargetMonth = new Date(date.getFullYear(), date.getMonth() - 1, 1);
  const lastDay = new Date(firstOfTargetMonth.getFullYear(), firstOfTargetMonth.getMonth() + 1, 0).getDate();
  firstOfTargetMonth.setDate(Math.min(date.getDate(), lastDay));
  return firstOfTargetMonth;
}

document.addEventListener('DOMContentLoaded', async () => {
  await setDefaultDates();
  $('choose-strategy').addEventListener('click', chooseStrategyFile);
  $('strategy-file').addEventListener('change', uploadSelectedStrategyFile);
  $('backtest-form').addEventListener('submit', submitBacktest);
  $('check-data').addEventListener('click', checkData);
  $('refresh-report').addEventListener('click', refreshSelectedReport);
  $('settings-toggle').addEventListener('click', toggleSettingsPanel);
  try {
    await refreshAll();
    setInterval(refreshAll, 3000);
  } catch (error) {
    setNotice(error.message, 'error');
  }
});
"""


def _history_js() -> str:
    return """
const $ = (id) => document.getElementById(id);
const pct = (value) => value == null ? '--' : `${(Number(value) * 100).toFixed(2)}%`;
const seconds = (value) => value == null ? '--' : `${Number(value).toFixed(2)}s`;

async function loadHistory() {
  const response = await fetch('/api/runs');
  const payload = await response.json();
  renderHistory(payload.runs || []);
  $('history-filter').addEventListener('input', () => renderHistory(payload.runs || []));
}

function renderHistory(runs) {
  const filter = $('history-filter').value.trim().toLowerCase();
  const root = $('history-runs');
  const rows = runs.filter((run) => {
    const text = `${run.strategy_name} ${run.start_date} ${run.end_date} ${run.sdk_version}`.toLowerCase();
    return !filter || text.includes(filter);
  });
  root.innerHTML = '';
  if (!rows.length) {
    root.innerHTML = '<div class="notice">没有匹配的历史记录。</div>';
    return;
  }
  for (const run of rows) {
    const row = document.createElement('div');
    row.className = 'history-row';
    const cls = Number(run.return_rate) >= 0 ? 'positive' : 'negative';
    let action = `<a class="primary link-button" href="/?run_id=${encodeURIComponent(run.run_id)}">选择</a>`;
    if (!run.report_path) {
      action = '<span class="disabled-link incomplete">未生成报告</span>';
    }
    row.innerHTML = `
      <div class="history-main">
        <strong>${escapeHtml(run.strategy_name || '')}</strong>
        <span>${escapeHtml(run.run_id)}</span>
      </div>
      <div>${escapeHtml(run.start_date || '')}<br>${escapeHtml(run.end_date || '')}</div>
      <div class="${cls}">${pct(run.return_rate)}</div>
      <div>${seconds(run.duration_seconds)}</div>
      ${action}
    `;
    root.appendChild(row);
  }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

document.addEventListener('DOMContentLoaded', loadHistory);
"""


def _is_excluded_strategy_path(relative: Path) -> bool:
    if relative.name.startswith("test_") or relative.name == "__init__.py":
        return True
    for part in relative.parts[:-1]:
        if part.startswith(".") or part in _EXCLUDED_STRATEGY_DIRS:
            return True
    return False


def _looks_like_joinquant_strategy(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return _looks_like_joinquant_strategy_text(text)


def _looks_like_joinquant_strategy_text(text: str) -> bool:
    return re.search(r"(?m)^\s*def\s+initialize\s*\(", text) is not None


def _safe_upload_filename(filename: str) -> str:
    name = Path(filename).name.strip() or "strategy.py"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if not name.endswith(".py"):
        name += ".py"
    return name


def _cached_open_trade_days(cache_db: str | Path, *, before: date) -> list[date]:
    db_path = Path(cache_db)
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute("select cal_date, is_open from trade_calendar").fetchall()
    except sqlite3.Error:
        return []
    days = []
    for cal_date, is_open in rows:
        try:
            if int(is_open) != 1:
                continue
        except (TypeError, ValueError):
            continue
        parsed = _parse_trade_calendar_date(cal_date)
        if parsed is not None and parsed < before:
            days.append(parsed)
    return sorted(set(days))


def _parse_trade_calendar_date(value) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _previous_weekday(today: date) -> date:
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _subtract_one_month(day: date) -> date:
    year = day.year
    month = day.month - 1
    if month == 0:
        year -= 1
        month = 12
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _first_open_on_or_after(open_days: list[date], target: date, end_date: date) -> date:
    for day in open_days:
        if target <= day <= end_date:
            return day
    return target


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _to_float(*values) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _optional_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
