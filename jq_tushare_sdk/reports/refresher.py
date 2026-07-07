from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from types import SimpleNamespace

from jq_tushare_sdk import __version__ as SDK_VERSION
from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.data.portal import DataPortal
from jq_tushare_sdk.reports.html_report import JoinQuantHtmlReport
from jq_tushare_sdk.reports.joinquant_formatter import JoinQuantOutputFormatter
from jq_tushare_sdk.runtime.engine import BacktestEngine


def refresh_backtest_report(run_dir: str | Path, *, backend) -> dict:
    run_path = Path(run_dir)
    reports_dir = run_path / "reports"
    performance_path = reports_dir / "performance.csv"
    summary_path = reports_dir / "summary.json"
    report_path = reports_dir / "report.html"
    if not performance_path.is_file() or not summary_path.is_file():
        raise FileNotFoundError(f"run does not contain refreshable report files: {run_path}")

    config = _load_config(run_path)
    manifest = _load_manifest(run_path)
    rows = _read_csv_dicts(performance_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    benchmark = str(summary.get("benchmark") or getattr(config, "benchmark", "") or "")
    if not benchmark:
        raise ValueError("summary/config does not contain a benchmark")

    engine = BacktestEngine(config, backend=backend)
    trade_days = [str(row.get("date")) for row in rows if row.get("date")]
    portal = DataPortal(backend, optimize_data=getattr(config, "optimize_data", True))
    benchmark_closes = engine._benchmark_closes(portal, benchmark, trade_days)
    benchmark_issue = None if benchmark_closes else engine._benchmark_missing_reason(benchmark)
    engine._apply_benchmark_returns(rows, benchmark_closes)
    risk = engine._risk_metrics(rows, benchmark=benchmark, benchmark_issue=benchmark_issue)
    if "unsupported_reasons" not in risk:
        summary.pop("unsupported_reasons", None)
    summary.update(risk)
    summary["benchmark"] = benchmark

    formatter = JoinQuantOutputFormatter()
    formatter.write_performance(performance_path, rows)
    formatter.write_summary(summary_path, summary)
    _refresh_html_risk_section(
        report_path,
        config=config,
        manifest=manifest,
        performance_rows=rows,
        summary=summary,
        trades=_read_trades(run_path / "trades" / "transactions.csv"),
        log_lines=_read_log_lines(run_path / "logs" / "backtest.log"),
    )
    return {
        "updated": bool(benchmark_closes),
        "benchmark": benchmark,
        "rows": len(rows),
        "reason": benchmark_issue,
        "report_path": str(report_path),
    }


def _load_config(run_path: Path) -> BacktestConfig:
    payload = json.loads((run_path / "config.json").read_text(encoding="utf-8"))
    fields = BacktestConfig.__dataclass_fields__
    values = {name: payload[name] for name in fields if name in payload}
    if "output_dir" not in values:
        values["output_dir"] = str(run_path.parent)
    return BacktestConfig(**values)


def _load_manifest(run_path: Path):
    payload = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
    return SimpleNamespace(
        run_id=payload.get("run_id") or run_path.name,
        run_dir=run_path,
        logs_dir=run_path / "logs",
        trades_dir=run_path / "trades",
        reports_dir=run_path / "reports",
        signals_dir=run_path / "signals",
        artifacts_dir=run_path / "artifacts",
    )


def _read_csv_dicts(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_trades(path: Path) -> list:
    if not path.is_file():
        return []
    trades = []
    for row in _read_csv_dicts(path):
        trades.append(
            SimpleNamespace(
                traded_at=row.get("datetime", ""),
                security=row.get("security", ""),
                side=row.get("side", ""),
                amount=_int(row.get("amount")),
                price=_float(row.get("price")),
                value=_float(row.get("value")),
                commission=_float(row.get("commission")),
                stamp_tax=_float(row.get("stamp_tax")),
                transfer_fee=_float(row.get("transfer_fee")),
                order_id=row.get("order_id", ""),
                trade_id=row.get("trade_id", ""),
                reason=row.get("reason", ""),
            )
        )
    return trades


def _read_log_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _refresh_html_risk_section(
    path: Path,
    *,
    config,
    manifest,
    performance_rows: list[dict],
    summary: dict,
    trades: list,
    log_lines: list[str],
) -> None:
    report = JoinQuantHtmlReport()
    fresh_html = report.render(
        config=config,
        manifest=manifest,
        performance_rows=performance_rows,
        summary=summary,
        trades=trades,
        position_rows=[],
        log_lines=log_lines,
        security_names={},
    )
    risk_section = _extract_between(fresh_html, '<section id="risk"', '<section id="trades"')
    if path.is_file():
        html = path.read_text(encoding="utf-8")
        start = html.find('<section id="risk"')
        end = html.find('<section id="trades"', start)
        if start >= 0 and end > start and risk_section:
            updated = html[:start] + risk_section + "\n" + html[end:]
            updated = _refresh_html_assets(updated, fresh_html)
            path.write_text(_refresh_sdk_version_text(updated), encoding="utf-8")
            return

    path.write_text(_refresh_sdk_version_text(fresh_html) + "\n", encoding="utf-8")


def _extract_between(html: str, start_token: str, end_token: str) -> str:
    start = html.find(start_token)
    end = html.find(end_token, start)
    return html[start:end] if start >= 0 and end > start else ""


def _refresh_html_assets(html: str, fresh_html: str) -> str:
    updated = _replace_tag_block(html, fresh_html, "style")
    if updated is None:
        return fresh_html
    updated = _replace_tag_block(updated, fresh_html, "script")
    return fresh_html if updated is None else updated


def _replace_tag_block(target: str, source: str, tag: str) -> str | None:
    start_token = f"<{tag}>"
    end_token = f"</{tag}>"
    source_start = source.find(start_token)
    source_end = source.find(end_token, source_start)
    target_start = target.find(start_token)
    target_end = target.find(end_token, target_start)
    if min(source_start, source_end, target_start, target_end) < 0:
        return None
    source_end += len(end_token)
    target_end += len(end_token)
    return target[:target_start] + source[source_start:source_end] + target[target_end:]


def _refresh_sdk_version_text(html: str) -> str:
    html = re.sub(r"SDK v\d+\.\d+\.\d+", f"SDK v{SDK_VERSION}", html)
    return re.sub(r"(<span>SDK版本</span>\s*<strong>)v\d+\.\d+\.\d+(</strong>)", rf"\1v{SDK_VERSION}\2", html)


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
