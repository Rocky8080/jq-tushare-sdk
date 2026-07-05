import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jq_tushare_sdk import cli as cli_module
from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.reports.html_report import JoinQuantHtmlReport
from jq_tushare_sdk.web import app as web_app
from jq_tushare_sdk.web.app import BacktestJobManager, BacktestRequest, RunStore, discover_strategies


class TestWebConsole(unittest.TestCase):
    def test_discover_strategies_excludes_internal_and_test_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self._write(root / "alpha_strategy.py")
            self._write(root / "nested" / "beta_strategy.py")
            self._write(root / "helper_module.py", "def helper():\n    pass\n")
            self._write(root / "test_alpha_strategy.py")
            self._write(root / ".hidden" / "hidden_strategy.py")
            self._write(root / "__pycache__" / "cached_strategy.py")
            self._write(root / "backtest_runs" / "old_strategy.py")
            self._write(root / "data" / "cache_helper.py")

            strategies = discover_strategies(root)

        self.assertEqual(
            [item["relative_path"] for item in strategies],
            ["alpha_strategy.py", "nested/beta_strategy.py"],
        )
        self.assertEqual(strategies[0]["name"], "alpha_strategy")
        self.assertTrue(strategies[0]["path"].endswith("alpha_strategy.py"))

    def test_run_store_lists_runs_with_summary_metrics_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            self._write_run(
                output_dir,
                "20260703-100000_alpha_20260601_20260630",
                strategy_name="alpha",
                initial_cash=1000000.0,
                final_value=1100000.0,
                total_seconds=42.5,
            )
            self._write_run(
                output_dir,
                "20260703-110000_beta_20260601_20260630",
                strategy_name="beta",
                initial_cash=2000000.0,
                final_value=2100000.0,
                total_seconds=17.0,
            )

            runs = RunStore(output_dir).list_runs()

        self.assertEqual([run["run_id"] for run in runs], ["20260703-110000_beta_20260601_20260630", "20260703-100000_alpha_20260601_20260630"])
        self.assertEqual(runs[0]["strategy_name"], "beta")
        self.assertEqual(runs[0]["status"], "completed")
        self.assertAlmostEqual(runs[0]["return_rate"], 0.05)
        self.assertEqual(runs[0]["duration_seconds"], 17.0)
        self.assertEqual(runs[0]["report_path"], "20260703-110000_beta_20260601_20260630/reports/report.html")

    def test_run_store_returns_strategy_reproducibility_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            self._write_run(
                output_dir,
                "20260703-110000_beta_20260601_20260630",
                strategy_name="beta",
                initial_cash=1000000.0,
                final_value=1100000.0,
                total_seconds=17.0,
                strategy_version="2.0.0",
                strategy_source="project_file",
                strategy_hash="abc123",
            )

            runs = RunStore(output_dir).list_runs()

        self.assertEqual(runs[0]["strategy_version"], "2.0.0")
        self.assertEqual(runs[0]["strategy_source"], "project_file")
        self.assertEqual(runs[0]["strategy_hash"], "abc123")

    def test_run_store_marks_runs_without_report_as_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            self._write_incomplete_run(
                output_dir,
                "20260703-211136_20260703-211052_8c5d54c3_sample_strategy_20260601_20260630",
                strategy_name="sample_strategy",
            )

            runs = RunStore(output_dir).list_runs()

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "incomplete")
        self.assertIsNone(runs[0]["report_path"])
        self.assertIsNone(runs[0]["return_rate"])
        self.assertIsNone(runs[0]["final_value"])

    def test_job_manager_converts_request_to_backtest_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root / "alpha_strategy.py", 'VERSION = "1.2.0"\ndef initialize(context):\n    pass\n')
            output_dir = root / "runs"
            cache_db = root / "data" / "cache.db"
            captured = []

            def runner(config: BacktestConfig):
                captured.append(config)
                run_dir = output_dir / "run-1"
                run_dir.mkdir(parents=True)
                return SimpleNamespace(run_id="run-1", run_dir=run_dir)

            manager = BacktestJobManager(
                project_root=root,
                default_cache_db=cache_db,
                output_dir=output_dir,
                runner=runner,
                synchronous=True,
            )

            job = manager.start(
                BacktestRequest(
                    strategy_path="alpha_strategy.py",
                    start_date="2026-06-01",
                    end_date="2026-06-30",
                    initial_cash=2720000.0,
                    optimize_data=False,
                )
            )

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["run_id"], "run-1")
        self.assertEqual(len(captured), 1)
        config = captured[0]
        self.assertTrue(config.strategy_path.endswith("alpha_strategy.py"))
        self.assertEqual(config.start_date, "2026-06-01")
        self.assertEqual(config.end_date, "2026-06-30")
        self.assertEqual(config.initial_cash, 2720000.0)
        self.assertEqual(config.cache_db, str(cache_db))
        self.assertEqual(config.output_dir, str(output_dir))
        self.assertFalse(config.optimize_data)
        self.assertEqual(config.strategy_version, "1.2.0")
        self.assertEqual(config.strategy_source, "project_file")
        self.assertRegex(config.strategy_hash or "", r"^[0-9a-f]{64}$")

    def test_job_manager_marks_uploaded_snapshot_and_matches_project_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_strategy = root / "examples" / "alpha_strategy.py"
            uploaded_strategy = root / ".jqts_web" / "strategies" / "20260705-120000_abcd_alpha_strategy.py"
            self._write(project_strategy, 'VERSION = "2.0.0"\ndef initialize(context):\n    pass\n')
            self._write(uploaded_strategy, 'VERSION = "1.0.0"\ndef initialize(context):\n    pass\n')
            output_dir = root / "runs"
            cache_db = root / "data" / "cache.db"

            manager = BacktestJobManager(
                project_root=root,
                default_cache_db=cache_db,
                output_dir=output_dir,
                runner=lambda config: SimpleNamespace(run_id="run-1", run_dir=output_dir / "run-1"),
                synchronous=True,
            )

            config = manager._config_from_request(
                BacktestRequest(
                    strategy_path=uploaded_strategy.relative_to(root).as_posix(),
                    start_date="2026-06-01",
                    end_date="2026-06-30",
                    initial_cash=1000000.0,
                )
            )

        self.assertEqual(config.strategy_version, "1.0.0")
        self.assertEqual(config.strategy_source, "uploaded_snapshot")
        self.assertEqual(config.project_strategy_path, "examples/alpha_strategy.py")
        self.assertEqual(config.project_strategy_version, "2.0.0")
        self.assertTrue(config.project_strategy_is_newer)

    def test_strategy_warning_detects_stale_uploaded_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root / "examples" / "alpha_strategy.py", 'VERSION = "2.0.0"\ndef initialize(context):\n    pass\n')
            self._write(
                root / ".jqts_web" / "strategies" / "20260705-120000_abcd_alpha_strategy.py",
                'VERSION = "1.0.0"\ndef initialize(context):\n    pass\n',
            )

            warning = web_app.strategy_run_warning(
                root,
                ".jqts_web/strategies/20260705-120000_abcd_alpha_strategy.py",
            )

        self.assertTrue(warning["should_warn"])
        self.assertEqual(warning["strategy_source"], "uploaded_snapshot")
        self.assertEqual(warning["strategy_version"], "1.0.0")
        self.assertEqual(warning["project_strategy_path"], "examples/alpha_strategy.py")
        self.assertEqual(warning["project_strategy_version"], "2.0.0")

    def test_web_handler_exposes_strategy_warning_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "runs"
            cache_db = root / "data" / "cache.db"
            self._write(root / "examples" / "alpha_strategy.py", 'VERSION = "2.0.0"\ndef initialize(context):\n    pass\n')
            self._write(
                root / ".jqts_web" / "strategies" / "20260705-120000_abcd_alpha_strategy.py",
                'VERSION = "1.0.0"\ndef initialize(context):\n    pass\n',
            )
            manager = BacktestJobManager(
                project_root=root,
                default_cache_db=cache_db,
                output_dir=output_dir,
                synchronous=True,
            )
            handler = web_app._handler_factory(
                project_root=root,
                cache_db=cache_db,
                output_dir=output_dir,
                manager=manager,
                run_store=RunStore(output_dir),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/strategy-warning"
                request = urllib.request.Request(
                    url,
                    data=json.dumps(
                        {
                            "strategy_path": ".jqts_web/strategies/20260705-120000_abcd_alpha_strategy.py",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()

        self.assertTrue(payload["should_warn"])
        self.assertEqual(payload["project_strategy_path"], "examples/alpha_strategy.py")

    def test_cli_web_command_starts_console_without_backtest_dates(self):
        with mock.patch.object(cli_module, "serve_web_console", return_value=None) as serve:
            result = cli_module.main(
                [
                    "web",
                    "--project-root",
                    "/repo",
                    "--cache-db",
                    "/repo/data/cache.db",
                    "--output-dir",
                    "/repo/backtest_runs",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8790",
                ]
            )

        self.assertEqual(result, 0)
        serve.assert_called_once()
        self.assertEqual(serve.call_args.kwargs["project_root"], "/repo")
        self.assertEqual(serve.call_args.kwargs["cache_db"], "/repo/data/cache.db")
        self.assertEqual(serve.call_args.kwargs["output_dir"], "/repo/backtest_runs")
        self.assertEqual(serve.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(serve.call_args.kwargs["port"], 8790)

    def test_default_backtest_dates_use_previous_cached_trade_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_db = Path(tmp) / "cache.db"
            self._write_trade_calendar(
                cache_db,
                [
                    ("20260601", 1),
                    ("20260602", 1),
                    ("20260701", 1),
                    ("20260702", 1),
                    ("20260703", 1),
                ],
            )

            dates = web_app.default_backtest_dates(cache_db, today=date(2026, 7, 3))

        self.assertEqual(
            dates,
            {"start_date": "2026-06-02", "end_date": "2026-07-02"},
        )

    def test_default_backtest_dates_fallback_to_previous_weekday_without_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            dates = web_app.default_backtest_dates(
                Path(tmp) / "missing.db",
                today=date(2026, 7, 6),
            )

        self.assertEqual(
            dates,
            {"start_date": "2026-06-03", "end_date": "2026-07-03"},
        )

    def test_web_console_uses_local_date_formatting(self):
        script = web_app._app_js()

        self.assertIn("api('/api/default-dates')", script)
        self.assertIn("await setDefaultDates();", script)
        self.assertIn("formatDateInput", script)
        self.assertNotIn("toISOString", script)

    def test_submit_backtest_delegates_data_preparation_to_backend_job(self):
        script = web_app._app_js()
        submit_script = script[
            script.index("async function submitBacktest") : script.index("async function runDataReadinessCheck")
        ]

        self.assertNotIn("runDataReadinessCheck", submit_script)
        self.assertIn("strategy-warning", submit_script)
        self.assertIn("confirm(", submit_script)
        self.assertIn("自动检查并补齐", submit_script)
        self.assertIn("api('/api/backtests'", submit_script)

    def test_main_console_shows_current_strategy_metadata(self):
        html = web_app._render_app_html(
            Path("/repo"),
            Path("data/jq_tushare_cache.db"),
            Path("backtest_runs"),
        )
        script = web_app._app_js()

        self.assertIn('id="strategy-meta"', html)
        self.assertIn("策略版本", script)
        self.assertIn("strategy_source", script)

    def test_main_console_uses_top_file_picker_and_report_workspace(self):
        html = web_app._render_app_html(
            Path("/repo"),
            Path("data/jq_tushare_cache.db"),
            Path("backtest_runs"),
        )

        self.assertIn('id="strategy-file"', html)
        self.assertIn('id="choose-strategy"', html)
        self.assertIn('id="report-frame"', html)
        self.assertIn('id="history-link"', html)
        self.assertNotIn('id="strategy-select"', html)
        self.assertNotIn('class="launch-panel"', html)
        self.assertNotIn('id="runs-list"', html)

    def test_main_console_keeps_quick_run_controls_compact(self):
        html = web_app._render_app_html(
            Path("/repo"),
            Path("data/jq_tushare_cache.db"),
            Path("backtest_runs"),
        )

        self.assertIn('class="parameter-form compact"', html)
        self.assertIn('id="settings-toggle"', html)
        self.assertIn('id="settings-panel"', html)
        settings_index = html.index('id="settings-panel"')
        top_form = html[html.index('id="backtest-form"'):settings_index]
        self.assertIn('id="choose-strategy"', top_form)
        self.assertIn('id="start-date"', top_form)
        self.assertIn('id="end-date"', top_form)
        self.assertIn('id="initial-cash"', top_form)
        self.assertNotIn('id="check-data"', top_form)
        self.assertNotIn('id="refresh-report"', top_form)
        self.assertNotIn('id="cache-db"', top_form)
        self.assertNotIn('id="output-dir"', top_form)
        self.assertNotIn('id="optimize-data"', top_form)
        self.assertGreater(html.index('id="cache-db"'), settings_index)
        self.assertGreater(html.index('id="output-dir"'), settings_index)
        self.assertGreater(html.index('id="optimize-data"'), settings_index)
        self.assertGreater(html.index('id="check-data"'), settings_index)
        self.assertGreater(html.index('id="refresh-report"'), settings_index)

    def test_main_console_accepts_one_million_initial_cash(self):
        html = web_app._render_app_html(
            Path("/repo"),
            Path("data/jq_tushare_cache.db"),
            Path("backtest_runs"),
        )

        self.assertIn('id="initial-cash"', html)
        self.assertIn('value="1000000"', html)
        self.assertIn('step="1"', html)
        self.assertNotIn('step="10000"', html)

    def test_main_console_does_not_duplicate_embedded_report_summary(self):
        html = web_app._render_app_html(
            Path("/repo"),
            Path("data/jq_tushare_cache.db"),
            Path("backtest_runs"),
        )

        self.assertIn('id="report-frame"', html)
        self.assertNotIn('class="report-header"', html)
        self.assertNotIn('id="active-report-title"', html)
        self.assertNotIn('id="active-report-return"', html)
        self.assertNotIn('id="active-report-duration"', html)

    def test_main_console_does_not_reload_selected_report_during_polling(self):
        script = web_app._app_js()

        self.assertIn("const reportSrc = `/runs/${run.report_path}`;", script)
        self.assertIn("frame.getAttribute('src') !== reportSrc", script)

    def test_main_console_can_refresh_selected_historical_report(self):
        script = web_app._app_js()

        self.assertIn("async function refreshSelectedReport()", script)
        self.assertIn("api('/api/refresh-report'", script)
        self.assertIn("forceLoadReport", script)

    def test_main_console_shows_missing_report_message_for_incomplete_run(self):
        script = web_app._app_js()

        self.assertIn("showMissingReport(run);", script)
        self.assertIn("该回测没有生成报告", script)
        self.assertIn("frame.removeAttribute('src')", script)

    def test_history_page_is_separate_and_links_back_to_main_report(self):
        html = web_app._render_history_html()

        self.assertIn("历史回测", html)
        self.assertIn('id="history-runs"', html)
        self.assertIn('href="/"', html)
        self.assertIn('/?run_id=', html)

    def test_history_page_marks_incomplete_runs_without_select_link(self):
        script = web_app._history_js()

        self.assertIn("if (!run.report_path)", script)
        self.assertIn("未生成报告", script)
        self.assertIn("disabled-link", script)
        self.assertIn("incomplete", script)

    def test_html_report_logs_strategy_reproducibility_metadata(self):
        config = SimpleNamespace(
            strategy_path="/repo/examples/alpha.py",
            strategy_name="alpha",
            strategy_version="2.0.0",
            strategy_source="project_file",
            strategy_hash="abc123",
            start_date="2026-06-01",
            end_date="2026-06-30",
            initial_cash=1000000.0,
            cache_db="/repo/data/cache.db",
        )
        manifest = SimpleNamespace(run_id="run-1")

        html = JoinQuantHtmlReport().render(
            config=config,
            manifest=manifest,
            performance_rows=[],
            summary={},
            trades=[],
            position_rows=[],
        )

        self.assertIn("策略版本", html)
        self.assertIn("2.0.0", html)
        self.assertIn("project_file", html)
        self.assertIn("abc123", html)

    def test_uploaded_strategy_file_is_copied_inside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            saved = web_app.save_uploaded_strategy_file(
                root,
                {
                    "filename": "../alpha strategy.py",
                    "content": "def initialize(context):\n    pass\n",
                },
            )

            saved_path = Path(saved["path"])
            self.assertTrue(saved_path.is_file())
            self.assertTrue(saved_path.is_relative_to(root))
            self.assertEqual(saved_path.suffix, ".py")
            self.assertIn(".jqts_web/strategies/", saved["relative_path"])
            self.assertIn("def initialize", saved_path.read_text(encoding="utf-8"))

    def test_web_handler_supports_head_for_run_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "runs"
            cache_db = root / "data" / "cache.db"
            self._write(root / "alpha_strategy.py")
            self._write_run(
                output_dir,
                "20260703-110000_alpha_20260601_20260630",
                strategy_name="alpha",
                initial_cash=1000000.0,
                final_value=1100000.0,
                total_seconds=42.5,
            )
            manager = BacktestJobManager(
                project_root=root,
                default_cache_db=cache_db,
                output_dir=output_dir,
                synchronous=True,
            )
            handler = web_app._handler_factory(
                project_root=root,
                cache_db=cache_db,
                output_dir=output_dir,
                manager=manager,
                run_store=RunStore(output_dir),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = (
                    f"http://127.0.0.1:{server.server_address[1]}"
                    "/runs/20260703-110000_alpha_20260601_20260630/reports/report.html"
                )
                request = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get_content_type(), "text/html")
            finally:
                server.shutdown()
                server.server_close()

    def test_web_handler_returns_default_dates_from_cached_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "runs"
            cache_db = root / "data" / "cache.db"
            self._write(root / "alpha_strategy.py")
            self._write_trade_calendar(
                cache_db,
                [
                    ("20260602", 1),
                    ("20260701", 1),
                    ("20260702", 1),
                    ("20260703", 1),
                ],
            )
            manager = BacktestJobManager(
                project_root=root,
                default_cache_db=cache_db,
                output_dir=output_dir,
                synchronous=True,
            )
            handler = web_app._handler_factory(
                project_root=root,
                cache_db=cache_db,
                output_dir=output_dir,
                manager=manager,
                run_store=RunStore(output_dir),
                today=lambda: date(2026, 7, 3),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/default-dates"
                with urllib.request.urlopen(url, timeout=3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["start_date"], "2026-06-02")
                self.assertEqual(payload["end_date"], "2026-07-02")
            finally:
                server.shutdown()
                server.server_close()

    def test_web_handler_refreshes_selected_historical_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "runs"
            cache_db = root / "data" / "cache.db"
            run_id = "20260703-213513_alpha_20260601_20260630"
            self._write(root / "alpha_strategy.py")
            self._write_run(
                output_dir,
                run_id,
                strategy_name="alpha",
                initial_cash=1000000.0,
                final_value=1100000.0,
                total_seconds=42.5,
            )
            manager = BacktestJobManager(
                project_root=root,
                default_cache_db=cache_db,
                output_dir=output_dir,
                synchronous=True,
            )
            handler = web_app._handler_factory(
                project_root=root,
                cache_db=cache_db,
                output_dir=output_dir,
                manager=manager,
                run_store=RunStore(output_dir),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.object(
                    web_app,
                    "refresh_backtest_report",
                    return_value={
                        "updated": True,
                        "benchmark": "000985.XSHG",
                        "rows": 20,
                        "reason": None,
                        "report_path": str(output_dir / run_id / "reports" / "report.html"),
                    },
                ) as refresh:
                    url = f"http://127.0.0.1:{server.server_address[1]}/api/refresh-report"
                    request = urllib.request.Request(
                        url,
                        data=json.dumps({"run_id": run_id}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=3) as response:
                        payload = json.loads(response.read().decode("utf-8"))

                self.assertTrue(payload["updated"])
                self.assertEqual(payload["benchmark"], "000985.XSHG")
                refresh.assert_called_once()
                self.assertEqual(Path(refresh.call_args.args[0]).resolve(), (output_dir / run_id).resolve())
                self.assertIn("backend", refresh.call_args.kwargs)
            finally:
                server.shutdown()
                server.server_close()

    def _write(self, path: Path, content: str = "def initialize(context):\n    pass\n") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_trade_calendar(self, cache_db: Path, rows: list[tuple[str, int]]) -> None:
        cache_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(cache_db) as connection:
            connection.execute("create table trade_calendar (cal_date text, is_open integer)")
            connection.executemany("insert into trade_calendar values (?, ?)", rows)

    def _write_run(
        self,
        output_dir: Path,
        run_id: str,
        *,
        strategy_name: str,
        initial_cash: float,
        final_value: float,
        total_seconds: float,
        strategy_version: str | None = None,
        strategy_source: str | None = None,
        strategy_hash: str | None = None,
    ) -> None:
        run_dir = output_dir / run_id
        reports_dir = run_dir / "reports"
        reports_dir.mkdir(parents=True)
        (reports_dir / "report.html").write_text("<html></html>", encoding="utf-8")
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "strategy_name": strategy_name,
                    "strategy_path": f"{strategy_name}.py",
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-30",
                    "initial_cash": initial_cash,
                    "cache_db": "data/cache.db",
                    "sdk_version": "0.5.1",
                    "strategy_version": strategy_version,
                    "strategy_source": strategy_source,
                    "strategy_hash": strategy_hash,
                }
            ),
            encoding="utf-8",
        )
        (reports_dir / "summary.json").write_text(
            json.dumps(
                {
                    "initial_cash": initial_cash,
                    "final_value": final_value,
                    "trade_count": 12,
                    "performance_profile": {"total_seconds": total_seconds},
                }
            ),
            encoding="utf-8",
        )

    def _write_incomplete_run(
        self,
        output_dir: Path,
        run_id: str,
        *,
        strategy_name: str,
    ) -> None:
        run_dir = output_dir / run_id
        (run_dir / "logs").mkdir(parents=True)
        (run_dir / "logs" / "backtest.log").write_text(
            "2026-07-03 21:11:37 INFO 首次运行，执行初始调仓\n",
            encoding="utf-8",
        )
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "strategy_name": strategy_name,
                    "strategy_path": f".jqts_web/strategies/{strategy_name}.py",
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-30",
                    "initial_cash": 1000001.0,
                    "cache_db": "data/cache.db",
                    "sdk_version": "0.6.6",
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
