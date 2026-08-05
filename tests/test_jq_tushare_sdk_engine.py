import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from jq_tushare_sdk import __version__
from jq_tushare_sdk import cli as cli_module
from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.data.readiness import DataUpdateRequest, ReadinessIssue
from jq_tushare_sdk.runtime.engine import BacktestEngine


class FakeBackend:
    def fetch(self, api_name, **params):
        if api_name == "trade_cal":
            return pd.DataFrame(
                [
                    {"exchange": "SSE", "cal_date": "20240102", "is_open": 1},
                    {"exchange": "SSE", "cal_date": "20240103", "is_open": 1},
                ]
            )
        if api_name == "daily":
            ts_code = params.get("ts_code", "000001.SZ")
            return pd.DataFrame(
                [
                    {
                        "ts_code": ts_code,
                        "trade_date": "20240102",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.8,
                        "close": 10.0,
                        "vol": 1000.0,
                        "amount": 10000.0,
                    },
                    {
                        "ts_code": ts_code,
                        "trade_date": "20240103",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.8,
                        "close": 10.0,
                        "vol": 1000.0,
                        "amount": 10000.0,
                    },
                ]
            )
        if api_name == "stock_basic":
            return pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "name": "Ping An Bank",
                        "industry": "Banking",
                    }
                ]
            )
        return pd.DataFrame()

    def status(self, api_name):
        return {
            "exists": True,
            "record_count": 2,
            "min_date": "20240102",
            "max_date": "20240103",
        }


class BenchmarkBackend(FakeBackend):
    def fetch(self, api_name, **params):
        if api_name == "index_daily":
            ts_code = params.get("ts_code", "000300.SH")
            return pd.DataFrame(
                [
                    {
                        "ts_code": ts_code,
                        "trade_date": "20240102",
                        "open": 100.0,
                        "high": 100.0,
                        "low": 100.0,
                        "close": 100.0,
                        "vol": 1000.0,
                        "amount": 10000.0,
                    },
                    {
                        "ts_code": ts_code,
                        "trade_date": "20240103",
                        "open": 110.0,
                        "high": 110.0,
                        "low": 110.0,
                        "close": 110.0,
                        "vol": 1000.0,
                        "amount": 10000.0,
                    },
                ]
            )
        return super().fetch(api_name, **params)


class TestBacktestEngine(unittest.TestCase):
    def test_engine_runs_joinquant_startup_hooks_in_order_and_replaces_schedules(self):
        source = """
from jqdata import *

def initialize(context):
    log.info('lifecycle:initialize')
    run_daily(old_open, time='open')

def after_code_changed(context):
    log.info('lifecycle:after_code_changed')
    unschedule_all()
    run_daily(new_open, time='open')

def process_initialize(context):
    log.info('lifecycle:process_initialize')
    g.runtime_ready = True

def old_open(context):
    log.info('callback:old')

def new_open(context):
    log.info('callback:new ready=%s', g.runtime_ready)
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "lifecycle_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2024-01-02",
                end_date="2024-01-03",
                initial_cash=100000.0,
                cache_db="/tmp/cache.db",
                output_dir=str(Path(tmp) / "runs"),
                strategy_name="lifecycle",
            )

            manifest = BacktestEngine(config, backend=FakeBackend()).run()
            log_text = (manifest.logs_dir / "backtest.log").read_text(encoding="utf-8")

        initialize_at = log_text.index("lifecycle:initialize")
        changed_at = log_text.index("lifecycle:after_code_changed")
        process_at = log_text.index("lifecycle:process_initialize")
        self.assertLess(initialize_at, changed_at)
        self.assertLess(changed_at, process_at)
        self.assertNotIn("callback:old", log_text)
        self.assertEqual(log_text.count("callback:new ready=True"), 2)

    def test_engine_writes_joinquant_like_outputs(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_open, time='open')

def handle_open(context):
    order('000001.XSHE', 100)
    log.info('handled open')
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "demo_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            output_root = Path(tmp) / "runs"
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2024-01-02",
                end_date="2024-01-03",
                initial_cash=100000.0,
                cache_db="/tmp/cache.db",
                output_dir=str(output_root),
                strategy_name="demo",
            )

            first_manifest = BacktestEngine(config, backend=FakeBackend()).run()
            second_manifest = BacktestEngine(config, backend=FakeBackend()).run()

            self.assertEqual(first_manifest.run_dir.parent, output_root)
            self.assertEqual(second_manifest.run_dir.parent, output_root)
            self.assertNotEqual(first_manifest.run_dir, second_manifest.run_dir)
            self.assertTrue(first_manifest.run_dir.is_relative_to(output_root))
            self.assertTrue(second_manifest.run_dir.is_relative_to(output_root))

            for path in (
                first_manifest.logs_dir,
                first_manifest.trades_dir,
                first_manifest.reports_dir,
                first_manifest.signals_dir,
                first_manifest.artifacts_dir,
            ):
                self.assertTrue(path.is_dir(), path)

            log_path = first_manifest.logs_dir / "backtest.log"
            transactions_path = first_manifest.trades_dir / "transactions.csv"
            orders_path = first_manifest.trades_dir / "orders.csv"
            performance_path = first_manifest.reports_dir / "performance.csv"
            summary_path = first_manifest.reports_dir / "summary.json"
            html_report_path = first_manifest.reports_dir / "report.html"
            records_path = first_manifest.artifacts_dir / "records.jsonl"
            signals_path = first_manifest.signals_dir / "target_portfolio_signals.jsonl"

            for path in (
                log_path,
                transactions_path,
                orders_path,
                performance_path,
                summary_path,
                html_report_path,
                records_path,
                signals_path,
            ):
                self.assertTrue(path.is_file(), path)

            self.assertIn("handled open", log_path.read_text(encoding="utf-8"))
            self.assertEqual(
                self._csv_header(transactions_path),
                [
                    "datetime",
                    "security",
                    "name",
                    "side",
                    "amount",
                    "price",
                    "value",
                    "commission",
                    "stamp_tax",
                    "transfer_fee",
                    "dividend_tax",
                    "realized_pnl",
                    "order_id",
                    "trade_id",
                    "reason",
                ],
            )
            self.assertEqual(
                self._csv_header(orders_path),
                [
                    "datetime",
                    "order_id",
                    "security",
                    "amount",
                    "price",
                    "status",
                    "reason",
                ],
            )
            self.assertEqual(
                self._csv_header(performance_path),
                [
                    "date",
                    "total_value",
                    "cash",
                    "positions_value",
                    "daily_return",
                    "cumulative_return",
                    "benchmark_daily_return",
                    "benchmark_return",
                    "excess_return",
                    "drawdown",
                ],
            )
            with performance_path.open(encoding="utf-8", newline="") as handle:
                first_row = next(csv.DictReader(handle))
            self.assertEqual(first_row["benchmark_return"], "unsupported_placeholder")
            self.assertEqual(first_row["excess_return"], "unsupported_placeholder")

            html = html_report_path.read_text(encoding="utf-8")
            self.assertIn("<title>demo 回测报告</title>", html)
            self.assertIn("设置： 2024-01-02 到 2024-01-03", html)
            self.assertNotIn("回测结论", html)
            self.assertIn("收益与风险", html)
            self.assertIn("交易审计", html)
            self.assertIn("持仓与日收益", html)
            self.assertIn("性能瓶颈", html)
            self.assertIn("日志与复现", html)
            self.assertIn("Ping An Bank(000001.XSHE)", html)
            self.assertIn("100股", html)
            self.assertIn("买", html)
            self.assertIn("未实现", html)
            self.assertIn('class="tab-link active"', html)
            self.assertNotIn('href="#summary"', html)
            self.assertIn('href="#risk"', html)
            self.assertIn('href="#trades"', html)
            self.assertIn('href="#holdings"', html)
            self.assertIn("展开交易明细", html)
            self.assertIn("展开持仓明细", html)
            self.assertIn("复现信息", html)
            self.assertIn(f"SDK v{__version__}", html)
            self.assertIn("SDK版本", html)
            self.assertNotIn('<aside class="sidebar"', html)
            self.assertNotIn('aria-label="报告章节"', html)
            self.assertNotIn("模拟交易", html)
            self.assertNotIn("归因分析", html)
            self.assertNotIn("Group by day", html)
            self.assertNotIn("缩放：", html)
            self.assertNotIn("普通轴", html)
            self.assertNotIn("对数轴", html)
            self.assertNotIn('href="#strategy-return"', html)

    def test_engine_executes_strategy_callbacks_for_extra_time_labels(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_custom, time='10:15')

def handle_custom(context):
    log.info('handled custom')
"""
        with tempfile.TemporaryDirectory() as tmp:
            config = self._write_strategy_config(tmp, source, strategy_name="custom-time")

            manifest = BacktestEngine(config, backend=FakeBackend()).run()

            log_text = (manifest.logs_dir / "backtest.log").read_text(encoding="utf-8")
            self.assertEqual(log_text.count("handled custom"), 2)

    def test_engine_rejects_unsupported_schedule_labels(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_bad, time='not-a-time')

def handle_bad(context):
    log.info('handled bad')
"""
        with tempfile.TemporaryDirectory() as tmp:
            config = self._write_strategy_config(tmp, source, strategy_name="bad-time")

            with self.assertRaisesRegex(NotImplementedError, "not-a-time"):
                BacktestEngine(config, backend=FakeBackend()).run()

    def test_cli_requires_strategy_path(self):
        with self.assertRaisesRegex(SystemExit, "strategy path is required"):
            cli_module.main(
                [
                    "backtest",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-db",
                    "/tmp/cache.db",
                ]
            )

    def test_cli_check_data_returns_nonzero_with_readiness_issues(self):
        issue = ReadinessIssue(
            api_name="daily",
            message="Local cache has no data for daily.",
            suggestion="python update_data.py --api daily --start-date 20240102 --end-date 20240103",
        )
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "placeholder"}, clear=False),
            mock.patch.object(cli_module, "TushareCacheBackend") as backend_cls,
            mock.patch.object(
                cli_module,
                "DataReadinessCheck",
                return_value=SimpleNamespace(check_required=lambda *args, **kwargs: [issue]),
            ),
            redirect_stdout(stdout),
        ):
            result = cli_module.main(
                [
                    "check-data",
                    "demo.py",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-db",
                    "/tmp/cache.db",
                ]
            )

        self.assertEqual(result, 1)
        self.assertIn("daily: Local cache has no data for daily.", stdout.getvalue())
        backend_cls.assert_called_once_with(
            "/tmp/cache.db",
            token="placeholder",
            cache_mode="strict_local",
        )

    def test_cli_backtest_reports_run_directory(self):
        stdout = io.StringIO()
        manifest = SimpleNamespace(run_dir=Path("/tmp/backtest-runs/run-1"))
        engine = SimpleNamespace(run=lambda: manifest)
        with (
            mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "placeholder"}, clear=False),
            mock.patch.object(cli_module, "TushareCacheBackend") as backend_cls,
            mock.patch.object(
                cli_module,
                "DataReadinessCheck",
                return_value=SimpleNamespace(check_required=lambda *args, **kwargs: []),
            ),
            mock.patch.object(cli_module, "BacktestEngine", return_value=engine) as engine_cls,
            redirect_stdout(stdout),
        ):
            result = cli_module.main(
                [
                    "backtest",
                    "demo.py",
                    "--no-deterministic",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-db",
                    "/tmp/cache.db",
                ]
            )

        self.assertEqual(result, 0)
        self.assertIn("Backtest complete: /tmp/backtest-runs/run-1", stdout.getvalue())
        backend_cls.assert_called_once_with(
            "/tmp/cache.db",
            token="placeholder",
            cache_mode="strict_local",
        )
        engine_cls.assert_called_once()

    def test_cli_backtest_auto_updates_missing_data_before_running(self):
        issue = ReadinessIssue(
            api_name="daily",
            message="daily starts at 20240103, missing requested start 2024-01-02.",
            suggestion="python update_data.py --api daily --start-date 20240102 --end-date 20240103",
            update_requests=(DataUpdateRequest("daily", "20240102", "20240103"),),
        )
        readiness_results = [[issue], []]
        backend = SimpleNamespace(update_data=mock.Mock(return_value=3))
        manifest = SimpleNamespace(run_dir=Path("/tmp/backtest-runs/run-1"))
        engine = SimpleNamespace(run=lambda: manifest)
        stdout = io.StringIO()

        with (
            mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "placeholder"}, clear=False),
            mock.patch.object(cli_module, "TushareCacheBackend", return_value=backend),
            mock.patch.object(
                cli_module,
                "DataReadinessCheck",
                return_value=SimpleNamespace(check_required=lambda *args, **kwargs: readiness_results.pop(0)),
            ),
            mock.patch.object(cli_module, "BacktestEngine", return_value=engine) as engine_cls,
            redirect_stdout(stdout),
        ):
            result = cli_module.main(
                [
                    "backtest",
                    "demo.py",
                    "--no-deterministic",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-db",
                    "/tmp/cache.db",
                ]
            )

        self.assertEqual(result, 0)
        backend.update_data.assert_called_once_with(
            "daily",
            start_date="20240102",
            end_date="20240103",
        )
        self.assertIn("Automatic data update completed", stdout.getvalue())
        self.assertIn("Backtest complete: /tmp/backtest-runs/run-1", stdout.getvalue())
        engine_cls.assert_called_once()

    def test_cli_backtest_can_disable_data_optimization(self):
        stdout = io.StringIO()
        manifest = SimpleNamespace(run_dir=Path("/tmp/backtest-runs/run-1"))
        engine = SimpleNamespace(run=lambda: manifest)
        with (
            mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "placeholder"}, clear=False),
            mock.patch.object(cli_module, "TushareCacheBackend"),
            mock.patch.object(
                cli_module,
                "DataReadinessCheck",
                return_value=SimpleNamespace(check_required=lambda *args, **kwargs: []),
            ),
            mock.patch.object(cli_module, "BacktestEngine", return_value=engine) as engine_cls,
            redirect_stdout(stdout),
        ):
            result = cli_module.main(
                [
                    "backtest",
                    "demo.py",
                    "--no-deterministic",
                    "--no-optimize-data",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-db",
                    "/tmp/cache.db",
                ]
            )

        self.assertEqual(result, 0)
        config = engine_cls.call_args.args[0]
        self.assertFalse(config.optimize_data)

    def test_cli_backtest_reexecs_with_fixed_hash_seed_by_default(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                cli_module.os,
                "execvpe",
                side_effect=SystemExit(99),
            ) as execvpe,
        ):
            with self.assertRaises(SystemExit) as raised:
                cli_module.main(
                    [
                        "backtest",
                        "demo.py",
                        "--start",
                        "2024-01-02",
                        "--end",
                        "2024-01-03",
                        "--cache-db",
                        "/tmp/cache.db",
                    ]
                )

        self.assertEqual(raised.exception.code, 99)
        executable, command, env = execvpe.call_args.args
        self.assertEqual(executable, cli_module.sys.executable)
        self.assertEqual(command[:3], [cli_module.sys.executable, "-m", "jq_tushare_sdk.cli"])
        self.assertIn("backtest", command)
        self.assertEqual(env["PYTHONHASHSEED"], "0")
        self.assertEqual(env["JQ_TUSHARE_SDK_DETERMINISTIC_REEXECED"], "1")

    def test_cli_backtest_stops_before_engine_when_readiness_fails(self):
        issue = ReadinessIssue(
            api_name="daily_basic",
            message="daily_basic ends at 20240102, missing requested end 2024-01-03.",
            suggestion="python update_data.py --api daily_basic --start-date 20240102 --end-date 20240103",
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(cli_module, "TushareCacheBackend", return_value=object()),
            mock.patch.object(
                cli_module,
                "DataReadinessCheck",
                return_value=SimpleNamespace(check_required=lambda *args, **kwargs: [issue]),
            ),
            mock.patch.object(cli_module, "BacktestEngine") as engine_cls,
            redirect_stdout(stdout),
        ):
            result = cli_module.main(
                [
                    "backtest",
                    "demo.py",
                    "--no-deterministic",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-db",
                    "/tmp/cache.db",
                ]
            )

        self.assertEqual(result, 1)
        self.assertIn("daily_basic: daily_basic ends at 20240102", stdout.getvalue())
        engine_cls.assert_not_called()

    def test_cli_readiness_requires_stock_basic_index_daily_and_income(self):
        observed_api_lists = []
        stdout = io.StringIO()

        def _capture_required(_config, apis):
            observed_api_lists.append(list(apis))
            return []

        with (
            mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "placeholder"}, clear=False),
            mock.patch.object(cli_module, "TushareCacheBackend"),
            mock.patch.object(
                cli_module,
                "DataReadinessCheck",
                return_value=SimpleNamespace(check_required=_capture_required),
            ),
            mock.patch.object(
                cli_module,
                "BacktestEngine",
                return_value=SimpleNamespace(run=lambda: SimpleNamespace(run_dir=Path("/tmp/run"))),
            ),
            redirect_stdout(stdout),
        ):
            check_result = cli_module.main(
                [
                    "check-data",
                    "demo.py",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-db",
                    "/tmp/cache.db",
                ]
            )
            backtest_result = cli_module.main(
                [
                    "backtest",
                    "demo.py",
                    "--no-deterministic",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-db",
                    "/tmp/cache.db",
                ]
            )

        self.assertEqual(check_result, 0)
        self.assertEqual(backtest_result, 0)
        self.assertEqual(len(observed_api_lists), 2)
        for api_list in observed_api_lists:
            self.assertIn("stock_basic", api_list)
            self.assertIn("index_daily", api_list)
            self.assertIn("income", api_list)

    def test_cli_update_data_writes_requested_project_cache(self):
        stdout = io.StringIO()
        backend = SimpleNamespace(update_range=lambda *args, **kwargs: {"daily": 3, "daily_basic": 2})

        with (
            mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "placeholder"}, clear=False),
            mock.patch.object(cli_module, "TushareCacheBackend", return_value=backend) as backend_cls,
            redirect_stdout(stdout),
        ):
            result = cli_module.main(
                [
                    "update-data",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-db",
                    "/project/data/jq_tushare_cache.db",
                    "--api",
                    "daily",
                    "--api",
                    "daily_basic",
                ]
            )

        self.assertEqual(result, 0)
        backend_cls.assert_called_once_with(
            "/project/data/jq_tushare_cache.db",
            token="placeholder",
            cache_mode="api_first",
        )
        self.assertIn("daily: 3 rows", stdout.getvalue())
        self.assertIn("daily_basic: 2 rows", stdout.getvalue())

    def test_cli_update_data_accepts_index_daily_ts_code_for_missing_benchmark(self):
        stdout = io.StringIO()
        backend = SimpleNamespace(update_data=mock.Mock(return_value=21))

        with (
            mock.patch.dict(os.environ, {"TUSHARE_TOKEN": "placeholder"}, clear=False),
            mock.patch.object(cli_module, "TushareCacheBackend", return_value=backend),
            redirect_stdout(stdout),
        ):
            result = cli_module.main(
                [
                    "update-data",
                    "--start",
                    "2026-06-01",
                    "--end",
                    "2026-06-30",
                    "--cache-db",
                    "/project/data/jq_tushare_cache.db",
                    "--api",
                    "index_daily",
                    "--ts-code",
                    "000985.SH",
                ]
            )

        self.assertEqual(result, 0)
        backend.update_data.assert_called_once_with(
            "index_daily",
            start_date="2026-06-01",
            end_date="2026-06-30",
            ts_code="000985.SH",
        )
        self.assertIn("index_daily: 21 rows", stdout.getvalue())

    def test_cli_refresh_report_rewrites_existing_history_without_backtest(self):
        stdout = io.StringIO()
        backend = object()

        with (
            mock.patch.object(cli_module, "TushareCacheBackend", return_value=backend) as backend_cls,
            mock.patch.object(
                cli_module,
                "refresh_backtest_report",
                return_value={"updated": True, "benchmark": "000985.XSHG", "report_path": "/runs/report.html"},
            ) as refresh,
            redirect_stdout(stdout),
        ):
            result = cli_module.main(
                [
                    "refresh-report",
                    "/project/backtest_runs/run-1",
                    "--cache-db",
                    "/project/data/jq_tushare_cache.db",
                ]
            )

        self.assertEqual(result, 0)
        backend_cls.assert_called_once_with(
            "/project/data/jq_tushare_cache.db",
            token=os.environ.get("TUSHARE_TOKEN"),
            cache_mode="strict_local",
        )
        refresh.assert_called_once_with("/project/backtest_runs/run-1", backend=backend)
        self.assertIn("Report refreshed", stdout.getvalue())

    def test_engine_summary_marks_benchmark_return_placeholder_as_unsupported(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_open, time='open')

def handle_open(context):
    order('000001.XSHE', 100)
"""
        with tempfile.TemporaryDirectory() as tmp:
            config = self._write_strategy_config(tmp, source, strategy_name="summary-placeholder")

            manifest = BacktestEngine(config, backend=FakeBackend()).run()
            summary = json.loads((manifest.reports_dir / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(summary["benchmark_return"], "unsupported_placeholder")
        self.assertEqual(summary["excess_return"], "unsupported_placeholder")
        self.assertEqual(
            summary["unsupported_reasons"]["benchmark_return"],
            "缺少基准 399006.XSHE 的 index_daily 数据，无法计算基准、超额、阿尔法和贝塔。",
        )
        self.assertIn("缺少基准 399006.XSHE 的 index_daily 数据", summary["unsupported_reasons"]["alpha"])

    def test_engine_report_explains_missing_strategy_benchmark_data(self):
        source = """
from jqdata import *

def initialize(context):
    set_benchmark('000985.XSHG')
    run_daily(handle_open, time='open')

def handle_open(context):
    order('000001.XSHE', 100)
"""
        with tempfile.TemporaryDirectory() as tmp:
            config = self._write_strategy_config(tmp, source, strategy_name="missing-benchmark")

            manifest = BacktestEngine(config, backend=FakeBackend()).run()
            summary = json.loads((manifest.reports_dir / "summary.json").read_text(encoding="utf-8"))
            html = (manifest.reports_dir / "report.html").read_text(encoding="utf-8")

        self.assertEqual(summary["benchmark"], "000985.XSHG")
        self.assertEqual(
            summary["unsupported_reasons"]["benchmark_return"],
            "缺少基准 000985.XSHG 的 index_daily 数据，无法计算基准、超额、阿尔法和贝塔。",
        )
        self.assertIn("缺少基准 000985.XSHG 的 index_daily 数据", html)
        self.assertIn("阿尔法", html)

    def test_engine_calculates_joinquant_style_benchmark_excess_alpha_and_beta(self):
        source = """
from jqdata import *

def initialize(context):
    set_benchmark('000300.XSHG')
"""
        with tempfile.TemporaryDirectory() as tmp:
            config = self._write_strategy_config(tmp, source, strategy_name="benchmark-metrics")

            manifest = BacktestEngine(config, backend=BenchmarkBackend()).run()
            summary = json.loads((manifest.reports_dir / "summary.json").read_text(encoding="utf-8"))
            with (manifest.reports_dir / "performance.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            html = (manifest.reports_dir / "report.html").read_text(encoding="utf-8")

        self.assertEqual(summary["benchmark"], "000300.XSHG")
        self.assertAlmostEqual(summary["benchmark_return"], 0.1)
        self.assertAlmostEqual(summary["excess_return"], -0.1)
        self.assertAlmostEqual(summary["beta"], 0.0)
        self.assertAlmostEqual(summary["alpha"], -0.04)
        self.assertEqual(rows[0]["benchmark_return"], "0.000000")
        self.assertEqual(rows[1]["benchmark_return"], "0.100000")
        self.assertEqual(rows[1]["excess_return"], "-0.100000")
        self.assertNotIn("阿尔法</div>\n          <div class=\"metric-value muted\">未实现", html)
        self.assertNotIn("贝塔</div>\n          <div class=\"metric-value muted\">未实现", html)
        self.assertIn('class="equity-line benchmark"', html)
        self.assertIn('class="equity-line excess"', html)
        self.assertNotIn("创业板指", html)

    def test_engine_records_skipped_rebalance_events(self):
        source = """
from jqdata import *

def initialize(context):
    g.days = 0
    run_daily(select_stock, time='open')
    run_daily(check_holding_count, time='open')

def select_stock(context):
    g.days += 1
    log.info(f"使用上一交易日 {context.current_dt.date()} 进行市值筛选")
    if g.days == 1:
        log.info("本轮普通换仓顺延：正选含高开新票 600000.XSHG(+8.12%)")
        log.info("周度调仓评估完成：候选18只，质量闸门拦截或执行异常，本轮未执行调仓")

def check_holding_count(context):
    log.info("持仓数量检查完成")
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "deferral_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2024-01-02",
                end_date="2024-01-03",
                initial_cash=100000.0,
                cache_db="/tmp/cache.db",
                output_dir=str(Path(tmp) / "runs"),
                strategy_name="deferral",
            )
            manifest = BacktestEngine(config, backend=FakeBackend()).run()
            summary = json.loads((manifest.reports_dir / "summary.json").read_text(encoding="utf-8"))
            html = (manifest.reports_dir / "report.html").read_text(encoding="utf-8")

        events = summary["skipped_rebalance_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["callback"], "select_stock")
        self.assertEqual(events[0]["date"], "2024-01-02")
        self.assertEqual(events[0]["deferred_days"], 1)
        self.assertIn("顺延", events[0]["note"])
        self.assertIn('id="deferrals"', html)
        self.assertIn("调仓顺延", html)

    def test_engine_records_no_deferral_events_without_rebalance_callback(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(housekeeping, time='open')

def housekeeping(context):
    log.info("使用上一交易日 %s 进行市值筛选", context.current_dt.date())
    log.info("本轮普通换仓顺延：正选含高开新票")
"""
        with tempfile.TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "housekeeping_strategy.py"
            strategy_path.write_text(source, encoding="utf-8")
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2024-01-02",
                end_date="2024-01-03",
                initial_cash=100000.0,
                cache_db="/tmp/cache.db",
                output_dir=str(Path(tmp) / "runs"),
                strategy_name="housekeeping",
            )
            manifest = BacktestEngine(config, backend=FakeBackend()).run()
            summary = json.loads((manifest.reports_dir / "summary.json").read_text(encoding="utf-8"))
            html = (manifest.reports_dir / "report.html").read_text(encoding="utf-8")

        self.assertEqual(summary["skipped_rebalance_events"], [])
        self.assertNotIn('href="#deferrals"', html)
        self.assertNotIn('id="deferrals"', html)

    def test_engine_reports_performance_profile_bottlenecks(self):
        source = """
from jqdata import *

def initialize(context):
    run_daily(handle_open, time='open')

def handle_open(context):
    order('000001.XSHE', 100)
"""
        with tempfile.TemporaryDirectory() as tmp:
            config = self._write_strategy_config(tmp, source, strategy_name="profile-report")

            manifest = BacktestEngine(config, backend=FakeBackend()).run()
            summary = json.loads((manifest.reports_dir / "summary.json").read_text(encoding="utf-8"))
            html = (manifest.reports_dir / "report.html").read_text(encoding="utf-8")

        profile = summary["performance_profile"]
        self.assertEqual(profile["trade_days"], 2)
        self.assertEqual(profile["callback_count"], 2)
        self.assertGreaterEqual(profile["total_seconds"], 0.0)
        self.assertIn("handle_open", {item["name"] for item in profile["slowest_callbacks"]})
        self.assertTrue(profile["phase_timings"])
        self.assertTrue(profile["data_api_calls"])
        self.assertIn("性能瓶颈", html)
        self.assertIn("主要瓶颈", html)
        self.assertIn("最重数据接口", html)
        self.assertIn("瓶颈定位", html)
        self.assertIn("优化建议", html)
        self.assertIn("handle_open", html)
        self.assertIn('href="#performance"', html)

    def _write_strategy_config(self, tmp: str, source: str, strategy_name: str) -> BacktestConfig:
        strategy_path = Path(tmp) / f"{strategy_name}.py"
        strategy_path.write_text(source, encoding="utf-8")
        return BacktestConfig(
            strategy_path=str(strategy_path),
            start_date="2024-01-02",
            end_date="2024-01-03",
            initial_cash=100000.0,
            cache_db="/tmp/cache.db",
            output_dir=str(Path(tmp) / "runs"),
            strategy_name=strategy_name,
        )

    def _csv_header(self, path: Path) -> list[str]:
        with path.open(encoding="utf-8", newline="") as handle:
            return next(csv.reader(handle))


if __name__ == "__main__":
    unittest.main()
