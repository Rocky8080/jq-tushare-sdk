import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jq_tushare_sdk
from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.reports.html_report import JoinQuantHtmlReport
from jq_tushare_sdk.reports.output_manager import OutputManager
from jq_tushare_sdk.reports.refresher import refresh_backtest_report


class RefreshBackend:
    def fetch(self, api_name, **params):
        import pandas as pd

        if api_name == "index_daily":
            return pd.DataFrame(
                [
                    {
                        "ts_code": params.get("ts_code", "000300.SH"),
                        "trade_date": "20240102",
                        "open": 100.0,
                        "high": 100.0,
                        "low": 100.0,
                        "close": 100.0,
                        "vol": 1000.0,
                        "amount": 10000.0,
                    },
                    {
                        "ts_code": params.get("ts_code", "000300.SH"),
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
        return pd.DataFrame()


class TestOutputManager(unittest.TestCase):
    def test_create_run_directory_is_unique_and_structured(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BacktestConfig(
                strategy_path="path/to/strategy.py",
                start_date="2024-01-01",
                end_date="2024-02-01",
                initial_cash=1000000.0,
                cache_db="/tmp/tushare_cache.db",
                output_dir=tmp,
                strategy_name="sample-strategy",
                strategy_version="1.0.0",
            )
            manager = OutputManager(clock=lambda: "20260702-143012")

            first = manager.create_run(config)
            second = manager.create_run(config)

            self.assertNotEqual(first.run_id, second.run_id)
            for manifest in (first, second):
                self.assertTrue(manifest.run_dir.is_dir())
                self.assertTrue(manifest.logs_dir.is_dir())
                self.assertTrue(manifest.trades_dir.is_dir())
                self.assertTrue(manifest.reports_dir.is_dir())
                self.assertTrue(manifest.signals_dir.is_dir())
                self.assertTrue(manifest.artifacts_dir.is_dir())
                self.assertTrue((manifest.run_dir / "manifest.json").is_file())
                self.assertTrue((manifest.run_dir / "config.json").is_file())

            latest = Path(tmp) / "latest"
            self.assertTrue(latest.is_file())
            self.assertEqual(latest.read_text(encoding="utf-8").strip(), second.run_id)

    def test_manifest_contains_reproducibility_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BacktestConfig(
                strategy_path="/repo/strategy.py",
                start_date="2024-01-01",
                end_date="2024-01-31",
                initial_cash=500000.0,
                cache_db="/data/cache.db",
                output_dir=tmp,
                strategy_name="demo",
                strategy_version="1.2.3",
                git_commit="abc1234",
            )
            manifest = OutputManager(clock=lambda: "20260702-150000").create_run(config)
            payload = json.loads((manifest.run_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(payload["strategy_path"], "/repo/strategy.py")
            self.assertEqual(payload["strategy_version"], "1.2.3")
            self.assertEqual(payload["start_date"], "2024-01-01")
            self.assertEqual(payload["end_date"], "2024-01-31")
            self.assertEqual(payload["initial_cash"], 500000.0)
            self.assertEqual(payload["cache_db"], "/data/cache.db")
            self.assertEqual(payload["git_commit"], "abc1234")
            self.assertEqual(payload["sdk_version"], jq_tushare_sdk.__version__)

    def test_create_run_atomically_claims_run_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = BacktestConfig(
                strategy_path="/repo/strategy.py",
                start_date="2024-01-01",
                end_date="2024-02-01",
                initial_cash=1000000.0,
                cache_db="/data/cache.db",
                output_dir=tmp,
                strategy_name="demo",
                strategy_version="1.0.0",
            )

            base_run_id = "20260702-150000_demo_20240101_20240201"
            base_run_dir = Path(tmp) / base_run_id
            base_run_dir.mkdir()

            observed_calls = []
            original_mkdir = Path.mkdir

            def record_mkdir(self, *args, **kwargs):
                observed_calls.append((self, kwargs))
                return original_mkdir(self, *args, **kwargs)

            with patch("pathlib.Path.mkdir", new=record_mkdir):
                manifest = OutputManager(clock=lambda: "20260702-150000").create_run(config)

            self.assertEqual(manifest.run_id, f"{base_run_id}_2")

            claimed_directories = [
                path
                for path, kwargs in observed_calls
                if kwargs.get("exist_ok") is False and kwargs.get("parents") is True
            ]

            self.assertIn(base_run_dir, claimed_directories)
            self.assertIn(Path(tmp) / f"{base_run_id}_2", claimed_directories)

    def test_html_report_starts_with_risk_without_repeated_conclusion_section(self):
        config = SimpleNamespace(
            strategy_name="demo",
            strategy_path="/repo/demo.py",
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
            summary={"final_value": 1100000.0, "trade_count": 0, "order_count": 0},
            trades=[],
            position_rows=[],
            log_lines=[],
            security_names={},
        )

        self.assertNotIn('href="#summary"', html)
        self.assertNotIn('id="summary"', html)
        self.assertNotIn("回测结论", html)
        self.assertNotIn("本次重点", html)
        self.assertNotIn(">结论<", html)
        self.assertIn('href="#risk"', html)
        self.assertIn('id="risk"', html)

    def test_html_report_shows_factor_change_node_compression(self):
        config = SimpleNamespace(
            strategy_name="demo",
            strategy_path="/repo/demo.py",
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
            summary={
                "final_value": 1100000.0,
                "performance_profile": {
                    "data_portal": {
                        "canonical_cache": {
                            "factor_rows_scanned": 1000,
                            "factor_change_nodes": 25,
                        }
                    }
                },
            },
            trades=[],
            position_rows=[],
            log_lines=[],
            security_names={},
        )

        self.assertIn("因子累计扫描行", html)
        self.assertIn("累计变化节点", html)
        self.assertIn("因子节点占比", html)
        self.assertIn("2.50%", html)

    def test_html_report_risk_section_includes_sharpe_and_turnover_metrics(self):
        config = SimpleNamespace(
            strategy_name="demo",
            strategy_path="/repo/demo.py",
            start_date="2026-06-01",
            end_date="2026-06-03",
            initial_cash=100000.0,
            cache_db="/repo/data/cache.db",
        )
        manifest = SimpleNamespace(run_id="run-1")
        daily_returns = [0.01, -0.01, 0.02]
        performance_rows = [
            {
                "date": "2026-06-01",
                "total_value": 100000.0,
                "daily_return": daily_returns[0],
                "cumulative_return": 0.01,
                "drawdown": 0.0,
            },
            {
                "date": "2026-06-02",
                "total_value": 103000.0,
                "daily_return": daily_returns[1],
                "cumulative_return": 0.0,
                "drawdown": -0.01,
            },
            {
                "date": "2026-06-03",
                "total_value": 105000.0,
                "daily_return": daily_returns[2],
                "cumulative_return": 0.05,
                "drawdown": 0.0,
            },
        ]
        trades = [
            SimpleNamespace(value=50000.0, side="buy", commission=5.0, stamp_tax=0.0, transfer_fee=0.0),
            SimpleNamespace(value=-25000.0, side="sell", commission=3.0, stamp_tax=2.5, transfer_fee=0.0),
        ]

        html = JoinQuantHtmlReport().render(
            config=config,
            manifest=manifest,
            performance_rows=performance_rows,
            summary={"final_value": 105000.0, "trade_count": 2, "order_count": 2},
            trades=trades,
            position_rows=[],
            log_lines=[],
            security_names={},
        )

        mean_return = sum(daily_returns) / len(daily_returns)
        sample_std = (
            sum((value - mean_return) ** 2 for value in daily_returns) / (len(daily_returns) - 1)
        ) ** 0.5
        sharpe = mean_return / sample_std * math.sqrt(252)

        self.assertIn('id="sharpe-ratio"', html)
        self.assertIn("夏普比率", html)
        self.assertIn(f"{sharpe:.3f}", html)
        self.assertIn('id="capital-turnover"', html)
        self.assertIn("资金换手率", html)
        self.assertIn("73.05%", html)
        self.assertIn('id="daily-turnover"', html)
        self.assertIn("日均换手率", html)
        self.assertIn("24.35%", html)
        self.assertLess(html.index('id="benchmark-return"'), html.index('id="sharpe-ratio"'))
        self.assertLess(html.index('id="volatility"'), html.index('id="capital-turnover"'))

    def test_html_report_charts_expose_hover_tooltip_data(self):
        config = SimpleNamespace(
            strategy_name="demo",
            strategy_path="/repo/demo.py",
            start_date="2026-06-01",
            end_date="2026-06-02",
            initial_cash=100000.0,
            cache_db="/repo/data/cache.db",
        )
        manifest = SimpleNamespace(run_id="run-1")
        performance_rows = [
            {
                "date": "2026-06-01",
                "total_value": 101000.0,
                "cash": 21000.0,
                "positions_value": 80000.0,
                "daily_return": 0.01,
                "cumulative_return": 0.01,
                "benchmark_return": 0.005,
                "excess_return": 0.005,
                "drawdown": 0.0,
            },
            {
                "date": "2026-06-02",
                "total_value": 99000.0,
                "cash": 19000.0,
                "positions_value": 80000.0,
                "daily_return": -0.019802,
                "cumulative_return": -0.01,
                "benchmark_return": -0.003,
                "excess_return": -0.007,
                "drawdown": -0.019802,
            },
        ]

        html = JoinQuantHtmlReport().render(
            config=config,
            manifest=manifest,
            performance_rows=performance_rows,
            summary={
                "final_value": 99000.0,
                "benchmark_return": -0.003,
                "excess_return": -0.007,
                "trade_count": 0,
                "order_count": 0,
            },
            trades=[],
            position_rows=[],
            log_lines=[],
            security_names={},
        )

        self.assertIn('class="chart-svg interactive-chart"', html)
        self.assertIn('data-chart-point="1"', html)
        self.assertIn('data-tooltip-lines=', html)
        self.assertIn("日期：2026-06-02", html)
        self.assertIn("当日收益：-1.98%", html)
        self.assertIn("策略收益：-1.00%", html)
        self.assertIn("基准收益：-0.30%", html)
        self.assertIn("总权益：99,000.00", html)
        self.assertIn("持仓市值：80,000.00", html)
        self.assertIn("chart-tooltip", html)
        self.assertIn("showChartTooltip", html)
        self.assertIn("chart-hover-line", html)

    def test_refresh_backtest_report_recalculates_historical_benchmark_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-1"
            reports_dir = run_dir / "reports"
            trades_dir = run_dir / "trades"
            logs_dir = run_dir / "logs"
            for path in (reports_dir, trades_dir, logs_dir):
                path.mkdir(parents=True)
            config_payload = {
                "strategy_path": "/repo/demo.py",
                "strategy_name": "demo",
                "start_date": "2024-01-02",
                "end_date": "2024-01-03",
                "initial_cash": 100000.0,
                "cache_db": "/tmp/cache.db",
                "output_dir": str(Path(tmp)),
                "benchmark": "000300.XSHG",
            }
            (run_dir / "config.json").write_text(json.dumps(config_payload), encoding="utf-8")
            (run_dir / "manifest.json").write_text(
                json.dumps({"run_id": "run-1", **config_payload}),
                encoding="utf-8",
            )
            (reports_dir / "performance.csv").write_text(
                "\n".join(
                    [
                        "date,total_value,cash,positions_value,daily_return,cumulative_return,benchmark_daily_return,benchmark_return,excess_return,drawdown",
                        "2024-01-02,100000,100000,0,0.000000,0.000000,,unsupported_placeholder,unsupported_placeholder,0.000000",
                        "2024-01-03,90000,90000,0,-0.100000,-0.100000,,unsupported_placeholder,unsupported_placeholder,-0.100000",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (reports_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "initial_cash": 100000.0,
                        "final_value": 90000.0,
                        "cash": 90000.0,
                        "positions_value": 0.0,
                        "benchmark": "000300.XSHG",
                        "benchmark_return": "unsupported_placeholder",
                        "excess_return": "unsupported_placeholder",
                        "alpha": "unsupported_placeholder",
                        "beta": "unsupported_placeholder",
                        "unsupported_reasons": {
                            "benchmark_return": "缺少基准 000300.XSHG 的 index_daily 数据，无法计算基准、超额、阿尔法和贝塔。",
                            "excess_return": "缺少基准 000300.XSHG 的 index_daily 数据，无法计算基准、超额、阿尔法和贝塔。",
                            "alpha": "缺少基准 000300.XSHG 的 index_daily 数据，无法计算基准、超额、阿尔法和贝塔。",
                            "beta": "缺少基准 000300.XSHG 的 index_daily 数据，无法计算基准、超额、阿尔法和贝塔。",
                        },
                        "trade_count": 0,
                        "order_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            (trades_dir / "transactions.csv").write_text(
                "datetime,security,name,side,amount,price,value,commission,stamp_tax,transfer_fee,order_id,trade_id,reason\n",
                encoding="utf-8",
            )
            (logs_dir / "backtest.log").write_text("old log\n", encoding="utf-8")
            (reports_dir / "report.html").write_text(
                '<html><body><span class="runtime sdk-version">SDK v0.6.8</span>'
                '<div><span>SDK版本</span><strong>v0.6.8</strong></div>'
                '<section id="risk" class="panel">未实现</section><section id="trades"></section></body></html>',
                encoding="utf-8",
            )

            result = refresh_backtest_report(run_dir, backend=RefreshBackend())
            summary = json.loads((reports_dir / "summary.json").read_text(encoding="utf-8"))
            with (reports_dir / "performance.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            html = (reports_dir / "report.html").read_text(encoding="utf-8")

        self.assertTrue(result["updated"])
        self.assertAlmostEqual(summary["benchmark_return"], 0.1)
        self.assertAlmostEqual(summary["excess_return"], -0.2)
        self.assertAlmostEqual(summary["beta"], -1.0)
        self.assertNotIn("unsupported_reasons", summary)
        self.assertEqual(rows[1]["benchmark_return"], "0.100000")
        self.assertEqual(rows[1]["excess_return"], "-0.200000")
        self.assertIn("基准收益", html)
        self.assertNotIn("缺少基准", html)
        self.assertNotIn("未实现", html)
        self.assertIn(f"SDK v{jq_tushare_sdk.__version__}", html)
        self.assertIn(f"<strong>v{jq_tushare_sdk.__version__}</strong>", html)
        self.assertNotIn("SDK v0.6.8", html)
        self.assertIn("interactive-chart", html)
        self.assertIn("chart-tooltip", html)
        self.assertIn("showChartTooltip", html)


if __name__ == "__main__":
    unittest.main()
