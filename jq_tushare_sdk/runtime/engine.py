from __future__ import annotations

import csv
import json
import math
import ast
import re
import sqlite3
import time as time_module
from dataclasses import replace
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from jq_tushare_sdk.broker.broker import Broker
from jq_tushare_sdk.broker.costs import CostModel
from jq_tushare_sdk.config import BacktestConfig, RunManifest
from jq_tushare_sdk.data.code_map import is_tushare_a_share_code, is_tushare_fund_code, normalize_date, to_joinquant_code
from jq_tushare_sdk.data.portal import DataPortal
from jq_tushare_sdk.data.readiness import infer_strategy_price_lookback_start
from jq_tushare_sdk.reports.joinquant_formatter import JoinQuantOutputFormatter
from jq_tushare_sdk.reports.metrics import performance_row
from jq_tushare_sdk.reports.output_manager import OutputManager
from jq_tushare_sdk.runtime.context import Context, Portfolio, Position
from jq_tushare_sdk.runtime.globals_state import RuntimeState
from jq_tushare_sdk.runtime.loader import StrategyLoader
from jq_tushare_sdk.runtime.logger import RuntimeLogger
from jq_tushare_sdk.runtime.scheduler import Scheduler


class BacktestEngine:
    _JOINQUANT_DISPLAY_NAME_ALIASES = {
        "159915.XSHE": "创业板ETF易方达",
        "510300.XSHG": "300ETF",
        "510500.XSHG": "500ETF",
        "512100.XSHG": "1000ETF",
        "588000.XSHG": "科创50",
    }

    _TIMELINE = [
        ("before_open", "09:00:00"),
        ("open", "09:30:00"),
        ("09:31", "09:31:00"),
        ("09:35", "09:35:00"),
        ("after_close", "15:00:00"),
    ]

    def __init__(
        self,
        config: BacktestConfig,
        backend,
        output_manager: OutputManager | None = None,
        progress_callback: Callable[[float, str, str | None], None] | None = None,
    ):
        effective_mode = getattr(backend, "financial_mode", None)
        if effective_mode is None:
            effective_mode = "strict_pit" if getattr(backend, "strict_pit", False) else "cached_snapshot"
        if config.financial_mode != "cached_snapshot" and config.financial_mode != effective_mode:
            raise ValueError(f"financial_mode mismatch: requested {config.financial_mode}, backend {effective_mode}")
        # Record the backend actually used, including a stricter backend passed
        # programmatically with a default config. Never silently downgrade a
        # nondefault financial contract.
        self.config = replace(config, financial_mode=effective_mode, financial_provenance=None)
        self.backend = backend
        self.output_manager = output_manager or OutputManager()
        self.progress_callback = progress_callback
        self.active_run_dir = None

    def _new_broker(self, context, portal):
        # This engine runs JoinQuant strategies. Generic Broker/CostModel defaults
        # remain unchanged; initialize() may override this through set_slippage.
        return Broker(context, portal, CostModel(slippage_rate=0.00246 / 2.0))

    def _initial_previous_trade_day(self, portal, trade_days):
        if not trade_days:
            return None
        calendar_days = portal.get_trade_days(end_date=trade_days[0], count=2)
        previous_days = [day for day in calendar_days if day < trade_days[0]]
        if not previous_days:
            raise NotImplementedError("Previous trading day is missing from the local calendar")
        return previous_days[-1]

    def _bind_schedule_calendar(self, scheduler, portal, trade_days):
        if not trade_days:
            scheduler.bind_calendar([])
            return
        kinds = {entry.kind for entry in scheduler.entries}
        if not {"weekly", "monthly"}.intersection(kinds):
            scheduler.bind_calendar(trade_days)
            return
        first, last = trade_days[0], trade_days[-1]
        if "monthly" in kinds:
            month_start = date(first.year, first.month, 1)
            next_month = date(last.year + (last.month == 12), last.month % 12 + 1, 1)
            month_end = next_month - timedelta(days=1)
        else:
            month_start, month_end = first, last
        # Include the boundary weeks too, so a week spanning two months has all
        # its trading days even when the replay starts in the middle of it.
        calendar_start = month_start - timedelta(days=month_start.weekday())
        calendar_end = month_end + timedelta(days=6 - month_end.weekday())
        full_days = portal.get_trade_days(start_date=calendar_start, end_date=calendar_end)
        fetch = getattr(portal, "_fetch", None)
        if not callable(fetch):
            raise NotImplementedError("Schedule calendar coverage cannot be verified")
        calendar = fetch("trade_cal", exchange="SSE", start_date=normalize_date(calendar_start),
                         end_date=normalize_date(calendar_end))
        if not {"cal_date", "is_open"}.issubset(calendar.columns):
            raise NotImplementedError("Schedule calendar is missing dated exchange open/closed rows")
        expected = {normalize_date(calendar_start + timedelta(days=offset))
                    for offset in range((calendar_end - calendar_start).days + 1)}
        received = {normalize_date(value) for value in calendar["cal_date"]}
        missing = sorted(expected - received)
        if missing:
            raise NotImplementedError("Incomplete schedule exchange calendar: missing " + ",".join(missing[:10]))
        open_states = {}
        for value, flag in zip(calendar["cal_date"], calendar["is_open"]):
            day_key, opened = normalize_date(value), float(flag)
            if opened not in (0., 1.) or (day_key in open_states and open_states[day_key] != opened):
                raise NotImplementedError("Invalid or conflicting exchange calendar open flags")
            open_states[day_key] = opened
        expected_open = {day for day, opened in open_states.items() if day in expected and opened == 1.}
        if expected_open != {normalize_date(day) for day in full_days}:
            raise NotImplementedError("Schedule calendar open-day projection is inconsistent")
        scheduler.bind_calendar(full_days)

    def _preflight_declared_data(self):
        """Validate explicit strategy requirements before loading any callbacks."""
        from jq_tushare_sdk.api import finance

        tree = ast.parse(Path(self.config.strategy_path).read_text(encoding="utf-8"))
        declaration = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "JQTS_DATA_REQUIREMENTS" for target in node.targets):
                declaration = ast.literal_eval(node.value)
        if declaration is None:
            return {"declared": False}
        if not isinstance(declaration, dict) or self.config.financial_mode not in declaration:
            raise ValueError("Strategy data preflight failed: missing mode-specific JQTS_DATA_REQUIREMENTS")
        required = declaration[self.config.financial_mode]
        if not isinstance(required, dict):
            raise ValueError("Strategy data preflight failed: invalid JQTS_DATA_REQUIREMENTS")
        tables = required.get("finance_tables", [])
        capabilities = finance.capabilities()
        unavailable = [str(table) for table in tables if not capabilities.get(table, {}).get("supported")]
        if unavailable:
            raise ValueError("Strategy data preflight failed: unsupported finance tables " + ", ".join(unavailable)
                             + "; use a verified source contract or explicit research_reconstructed mode")
        result = {"declared": True, "financial_mode": self.config.financial_mode, "finance_tables": list(tables)}
        if "TUSHARE_FORECAST" in tables:
            start = pd.Timestamp(self.config.start_date)
            end = pd.Timestamp(self.config.end_date)
            calendar_start = start - pd.Timedelta(days=40)
            calendar = self.backend.fetch("trade_cal", exchange="SSE",
                start_date=calendar_start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
            if calendar.empty or not {"cal_date", "is_open"}.issubset(calendar.columns):
                raise ValueError("Strategy data preflight failed: forecast calendar missing")
            expected = {day.strftime("%Y%m%d") for day in pd.date_range(calendar_start, end)}
            open_flags = {}
            for day, flag in zip(calendar.cal_date, calendar.is_open):
                key, value = normalize_date(day), float(flag)
                if value not in (0., 1.) or key in open_flags and open_flags[key] != value:
                    raise ValueError("Strategy data preflight failed: invalid forecast calendar flags")
                open_flags[key] = value
            if expected - set(open_flags):
                raise ValueError("Strategy data preflight failed: incomplete forecast calendar")
            open_dates = sorted(day for day, opened in open_flags.items() if day in expected and opened == 1.)
            replay_dates = [day for day in open_dates if start.strftime("%Y%m%d") <= day <= end.strftime("%Y%m%d")]
            if not replay_dates:
                raise ValueError("Strategy data preflight failed: no replay trading days")
            before_first = [day for day in open_dates if day < replay_dates[0]]
            before_last = [day for day in open_dates if day < replay_dates[-1]]
            if not before_first or not before_last:
                raise ValueError("Strategy data preflight failed: forecast previous trading day missing")
            lookback = int(required.get("forecast_lookback_days", 90))
            if lookback <= 0:
                raise ValueError("Strategy data preflight failed: invalid forecast lookback")
            publication_start = (pd.Timestamp(before_first[-1]) - pd.Timedelta(days=lookback)).strftime("%Y%m%d")
            publication_end = before_last[-1]
            coverage = finance.forecast_coverage(self.backend, publication_start, publication_end)
            if coverage.get("complete") is not True:
                raise ValueError("Strategy data preflight failed: forecast_coverage_missing " + publication_start + ".." + publication_end)
            result["forecast_coverage"] = coverage
        return result

    def run(self) -> RunManifest:
        self._report_progress(0.0, "初始化策略")
        profiler = _PerformanceProfiler()
        strategy_data = self._preflight_declared_data()
        as_of = f"{self.config.start_date} 00:00:00"
        provenance = {"as_of": as_of, "verified": False,
                      "reason": "legacy_snapshot_not_pit_verified",
                      "strategy_data_requirements": strategy_data}
        if self.config.financial_mode == "strict_pit":
            check = getattr(self.backend, "financial_readiness", None)
            readiness = check(as_of) if callable(check) else {"ready": False, "reason": "financial_vintage_missing"}
            provenance.update(readiness)
            provenance["verified"] = readiness.get("ready") is True
            if not provenance["verified"]:
                raise ValueError(f"Strict PIT financial preflight failed: {readiness.get('reason', 'financial_vintage_missing')}")
        elif self.config.financial_mode == "research_reconstructed":
            provenance["reason"] = "retrospective_reconstruction_not_pit_verified"
        self.config = replace(self.config, financial_provenance=provenance)
        output = self.output_manager.create_run(self.config)
        self.active_run_dir = output.run_dir
        logger = RuntimeLogger(output.logs_dir / "backtest.log")
        skipped_rebalance_records = []
        logger.add_listener(
            lambda level, text, context: self._capture_skipped_rebalance_log(
                skipped_rebalance_records,
                level,
                text,
                context,
            )
        )
        price_cache_start = infer_strategy_price_lookback_start(
            self.config.strategy_path,
            self.config.start_date,
        )
        portal = DataPortal(
            _ProfilingBackend(self.backend, profiler),
            optimize_data=self.config.optimize_data,
            price_cache_start=price_cache_start,
            price_cache_end=self.config.end_date,
        )
        scheduler = Scheduler()
        context = Context(Portfolio(self.config.initial_cash))
        broker = self._new_broker(context, portal)
        state = RuntimeState(
            data_portal=portal,
            scheduler=scheduler,
            broker=broker,
            context=context,
            log=logger,
            benchmark=self.config.benchmark,
        )

        with profiler.measure_phase("initialize", "策略初始化"):
            loaded = StrategyLoader().load(self.config.strategy_path, state)
            loaded.initialize(context)
            loaded.after_code_changed(context)
            loaded.process_initialize(context)
        with profiler.measure_phase("setup", "基础数据准备"):
            timeline = self._execution_timeline(scheduler)
            security_names = self._security_names(portal)
            benchmark = getattr(state, "benchmark", None) or self.config.benchmark
            trade_days = list(portal.get_trade_days(self.config.start_date, self.config.end_date))
            self._bind_schedule_calendar(scheduler, portal, trade_days)
            etf_dividend_events = self._load_etf_dividend_events()
            a_share_corporate_actions = self._load_a_share_corporate_actions()
        self._report_progress(0.04, "准备行情数据", f"共 {len(trade_days)} 个交易日")
        with profiler.measure_phase("benchmark", "基准数据读取"):
            benchmark_closes = self._benchmark_closes(portal, benchmark, trade_days)
            benchmark_issue = None
            if trade_days and not benchmark_closes:
                benchmark_issue = self._benchmark_missing_reason(benchmark)
        rows = []
        position_rows = []
        previous_total = None
        peak_value = float(context.portfolio.total_value)
        previous_trade_day = self._initial_previous_trade_day(portal, trade_days)
        applied_etf_dividends: set[tuple[str, str]] = set()
        a_share_entitlements: dict[str, dict] = {}
        applied_a_share_actions: set[str] = set()
        total_trade_days = len(trade_days)
        for day_index, trade_day in enumerate(trade_days, start=1):
            dividend_eligible_securities = set(context.portfolio.positions.keys())
            day_start = profiler.now()
            day_callback_seconds = 0.0
            day_mark_seconds = 0.0
            day_callback_count = 0
            context.previous_date = previous_trade_day
            broker.settle_trading_day(trade_day)
            self._settle_a_share_corporate_actions(
                context,
                broker,
                str(trade_day),
                a_share_corporate_actions["ex_date"],
                a_share_entitlements,
                applied_a_share_actions,
            )
            for label, time_text in timeline:
                context.current_dt = datetime.strptime(
                    f"{trade_day} {time_text}",
                    "%Y-%m-%d %H:%M:%S",
                )
                callbacks = scheduler.callbacks_for(
                    context.current_dt,
                    label,
                    previous_dt=previous_trade_day,
                )
                if callbacks:
                    mark_start = profiler.now()
                    self._mark_to_market(
                        context,
                        portal,
                        str(trade_day),
                        current_dt=context.current_dt,
                    )
                    self._apply_etf_dividends(
                        context,
                        portal,
                        str(trade_day),
                        etf_dividend_events,
                        applied_etf_dividends,
                        dividend_eligible_securities,
                    )
                    elapsed = profiler.elapsed(mark_start)
                    day_mark_seconds += elapsed
                    profiler.record_phase_duration("mark_to_market", "每日估值", elapsed)
                for callback in callbacks:
                    callback_start = profiler.now()
                    callback_name = self._callback_name(callback)
                    logger.set_context(
                        trade_date=str(trade_day),
                        callback=callback_name,
                    )
                    try:
                        callback(context)
                    finally:
                        logger.clear_context()
                        elapsed = profiler.elapsed(callback_start)
                        day_callback_seconds += elapsed
                        day_callback_count += 1
                        profiler.record_phase_duration("callbacks", "策略回调", elapsed)
                        profiler.record_callback(
                            date=str(trade_day),
                            time_label=str(label),
                            name=callback_name,
                            seconds=elapsed,
                        )
            self._capture_a_share_entitlements(
                context,
                broker,
                str(trade_day),
                a_share_corporate_actions["record_date"],
                a_share_entitlements,
            )
            self._apply_etf_dividends(
                context,
                portal,
                str(trade_day),
                etf_dividend_events,
                applied_etf_dividends,
                dividend_eligible_securities,
            )
            mark_start = profiler.now()
            self._mark_to_market(context, portal, str(trade_day))
            elapsed = profiler.elapsed(mark_start)
            day_mark_seconds += elapsed
            profiler.record_phase_duration("mark_to_market", "每日估值", elapsed)
            position_rows.extend(
                self._position_rows(context, str(trade_day), security_names)
            )
            current_total = float(context.portfolio.total_value)
            peak_value = max(peak_value, current_total)
            rows.append(
                performance_row(
                    date=str(trade_day),
                    portfolio=context.portfolio,
                    initial_cash=self.config.initial_cash,
                    previous_total=previous_total,
                    peak_value=peak_value,
                )
            )
            previous_total = current_total
            previous_trade_day = trade_day
            profiler.record_day(
                date=str(trade_day),
                seconds=profiler.elapsed(day_start),
                callback_seconds=day_callback_seconds,
                mark_to_market_seconds=day_mark_seconds,
                callback_count=day_callback_count,
            )
            day_progress = day_index / total_trade_days if total_trade_days else 1.0
            self._report_progress(
                0.05 + day_progress * 0.88,
                "执行策略回调",
                f"{trade_day}（{day_index}/{total_trade_days}）",
            )
        self._report_progress(0.95, "计算收益指标")
        with profiler.measure_phase("risk_metrics", "收益指标计算"):
            self._apply_benchmark_returns(rows, benchmark_closes)

        formatter = JoinQuantOutputFormatter()
        self._report_progress(0.97, "生成回测报告")
        with profiler.measure_phase("output", "结果文件写入"):
            formatter.write_transactions(
                output.trades_dir / "transactions.csv",
                broker.trades,
                security_names=security_names,
            )
            formatter.write_orders(
                output.trades_dir / "orders.csv",
                broker.orders,
                security_names=security_names,
            )
            formatter.write_performance(output.reports_dir / "performance.csv", rows)
            formatter.write_jsonl(output.artifacts_dir / "records.jsonl", list(state.records))
            formatter.write_jsonl(
                output.signals_dir / "target_portfolio_signals.jsonl",
                list(broker.target_portfolio_signals),
            )
        summary = self._summary(
            rows=rows,
            context=context,
            broker=broker,
            state=state,
            benchmark=benchmark,
            benchmark_issue=benchmark_issue,
            skipped_rebalance_records=skipped_rebalance_records,
            performance_profile=profiler.snapshot(
                trade_days=len(trade_days),
                data_portal=portal.performance_snapshot(),
            ),
        )
        summary["corporate_actions"] = self._corporate_action_summary()
        (output.artifacts_dir / "corporate_action_ledger.json").write_text(
            json.dumps(self._corporate_action_ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        formatter.write_summary(output.reports_dir / "summary.json", summary)
        formatter.write_html_report(
            output.reports_dir / "report.html",
            config=self.config,
            manifest=output,
            performance_rows=rows,
            summary=summary,
            trades=broker.trades,
            position_rows=position_rows,
            log_lines=self._read_log_lines(output.logs_dir / "backtest.log"),
            security_names=security_names,
        )
        self._report_progress(1.0, "回测完成")
        return output

    def _report_progress(self, value: float, stage: str, detail: str | None = None) -> None:
        if self.progress_callback is None:
            return
        self.progress_callback(max(0.0, min(1.0, float(value))), stage, detail)

    def _summary(
        self,
        *,
        rows,
        context,
        broker,
        state,
        benchmark: str,
        benchmark_issue: str | None = None,
        skipped_rebalance_records: list[dict] | None = None,
        performance_profile: dict | None = None,
    ) -> dict:
        risk = self._risk_metrics(rows, benchmark=benchmark, benchmark_issue=benchmark_issue)
        return {
            "initial_cash": self.config.initial_cash,
            "final_value": float(context.portfolio.total_value),
            "cash": float(context.portfolio.cash),
            "positions_value": float(context.portfolio.positions_value),
            "order_count": len(broker.orders),
            "trade_count": len(broker.trades),
            "record_count": len(state.records),
            "signal_count": len(broker.target_portfolio_signals),
            "benchmark": benchmark,
            "skipped_rebalance_events": self._extract_skipped_rebalance_events(
                skipped_rebalance_records or [],
            ),
            "performance_profile": performance_profile or {},
            **risk,
        }

    _SKIPPED_REBALANCE_MARKERS = (
        "本轮普通换仓顺延",
        "质量闸门拦截或执行异常，本轮未执行调仓",
    )
    _REBALANCE_NAME_KEYWORDS = (
        "select",
        "rebalance",
        "holding",
        "first_run",
        "empty_position",
        "weekly",
        "monthly",
        "position",
    )

    def _capture_skipped_rebalance_log(
        self,
        records: list[dict],
        level: str,
        text: str,
        context: dict,
    ) -> None:
        callback = str(context.get("callback") or "")
        trade_date = str(context.get("trade_date") or "")
        if not callback or not trade_date:
            return
        if not any(keyword in callback.lower() for keyword in self._REBALANCE_NAME_KEYWORDS):
            return
        if not any(marker in text for marker in self._SKIPPED_REBALANCE_MARKERS):
            return
        records.append(
            {
                "callback": callback,
                "date": trade_date,
                "level": str(level),
                "message": str(text),
            }
        )

    def _extract_skipped_rebalance_events(self, records: list[dict]) -> list[dict]:
        """Aggregate structured deferral logs captured during callbacks."""
        grouped: dict[str, dict[str, str]] = {}
        for record in records:
            callback = str(record.get("callback") or "")
            date = str(record.get("date") or "")
            if not callback or not date:
                continue
            grouped.setdefault(callback, {})
            grouped[callback].setdefault(date, str(record.get("message") or ""))

        events = []
        for name, date_messages in grouped.items():
            deferred_days = sorted(date_messages)
            events.append(
                {
                    "callback": name,
                    "date": deferred_days[0],
                    "deferred_days": len(deferred_days),
                    "note": date_messages[deferred_days[0]],
                }
            )
        events.sort(key=lambda item: (item.get("date", ""), item.get("callback", "")))
        return events

    def _benchmark_closes(self, portal: DataPortal, benchmark: str, trade_days: list[str]) -> dict[str, float]:
        if not trade_days:
            return {}
        try:
            start_date = self._benchmark_fetch_start_date(str(trade_days[0]))
            frame = portal.get_price(
                benchmark,
                start_date=start_date,
                end_date=str(trade_days[-1]),
                fields=["close"],
                panel=False,
            )
        except NotImplementedError:
            return {}
        if frame.empty or "time" not in frame.columns or "close" not in frame.columns:
            return {}
        closes = {
            str(row["time"]): float(row["close"])
            for _, row in frame.iterrows()
            if row.get("close") is not None
        }
        first_trade_day = str(trade_days[0])
        previous_dates = sorted(date for date in closes if date < first_trade_day)
        if previous_dates:
            closes["__base__"] = closes[previous_dates[-1]]
        return closes

    def _apply_benchmark_returns(self, rows: list[dict], benchmark_closes: dict[str, float]) -> None:
        if not rows or not benchmark_closes:
            return
        base_close = benchmark_closes.get("__base__")
        previous_close = None
        for row in rows:
            close = benchmark_closes.get(str(row.get("date")))
            if close is None or close <= 0:
                continue
            if base_close is None:
                base_close = close
            benchmark_return = close / base_close - 1 if base_close else 0.0
            benchmark_daily_return = close / previous_close - 1 if previous_close else benchmark_return
            previous_close = close
            strategy_return = self._safe_float(row.get("cumulative_return"))
            row["benchmark_return"] = f"{benchmark_return:.6f}"
            row["benchmark_daily_return"] = f"{benchmark_daily_return:.6f}"
            row["excess_return"] = f"{(strategy_return - benchmark_return):.6f}"

    def _benchmark_fetch_start_date(self, first_trade_day: str) -> str:
        try:
            parsed = datetime.strptime(first_trade_day, "%Y-%m-%d").date()
        except ValueError:
            return first_trade_day
        return (parsed - timedelta(days=14)).strftime("%Y-%m-%d")

    def _risk_metrics(self, rows: list[dict], *, benchmark: str | None = None, benchmark_issue: str | None = None) -> dict:
        if not rows:
            return {
                "benchmark_return": "unsupported_placeholder",
                "excess_return": "unsupported_placeholder",
                "alpha": "unsupported_placeholder",
                "beta": "unsupported_placeholder",
                "unsupported_reasons": self._unsupported_reason_map(
                    "回测没有收益序列，无法计算基准、超额、阿尔法和贝塔。"
                ),
            }
        final_row = rows[-1]
        benchmark_return = self._safe_optional_float(final_row.get("benchmark_return"))
        excess_return = self._safe_optional_float(final_row.get("excess_return"))
        if benchmark_return is None or excess_return is None:
            reason = benchmark_issue or self._benchmark_missing_reason(benchmark)
            return {
                "benchmark_return": "unsupported_placeholder",
                "excess_return": "unsupported_placeholder",
                "alpha": "unsupported_placeholder",
                "beta": "unsupported_placeholder",
                "unsupported_reasons": self._unsupported_reason_map(reason),
            }

        strategy_daily = [self._safe_float(row.get("daily_return")) for row in rows]
        benchmark_daily = [
            self._safe_float(row.get("benchmark_daily_return"))
            for row in rows
            if self._safe_optional_float(row.get("benchmark_daily_return")) is not None
        ]
        if len(benchmark_daily) != len(strategy_daily):
            reason = f"基准 {benchmark} 的交易日收益序列不完整，无法计算阿尔法和贝塔。"
            return {
                "benchmark_return": benchmark_return,
                "excess_return": excess_return,
                "alpha": "unsupported_placeholder",
                "beta": "unsupported_placeholder",
                "unsupported_reasons": {
                    "alpha": reason,
                    "beta": reason,
                },
            }

        beta = self._beta(strategy_daily, benchmark_daily)
        day_count = max(1, len(rows))
        strategy_return = self._safe_float(final_row.get("cumulative_return"))
        annual_strategy = self._annualized_return(strategy_return, day_count)
        annual_benchmark = self._annualized_return(benchmark_return, day_count)
        risk_free_rate = 0.04
        alpha = annual_strategy - (risk_free_rate + beta * (annual_benchmark - risk_free_rate))
        return {
            "benchmark_return": benchmark_return,
            "excess_return": excess_return,
            "alpha": alpha,
            "beta": beta,
            "risk_free_rate": risk_free_rate,
        }

    def _benchmark_missing_reason(self, benchmark: str | None) -> str:
        code = benchmark or "未设置基准"
        return f"缺少基准 {code} 的 index_daily 数据，无法计算基准、超额、阿尔法和贝塔。"

    def _unsupported_reason_map(self, reason: str) -> dict[str, str]:
        return {
            "benchmark_return": reason,
            "excess_return": reason,
            "alpha": reason,
            "beta": reason,
        }

    def _beta(self, strategy_daily: list[float], benchmark_daily: list[float]) -> float:
        if len(strategy_daily) != len(benchmark_daily) or len(strategy_daily) < 2:
            return 0.0
        benchmark_mean = sum(benchmark_daily) / len(benchmark_daily)
        strategy_mean = sum(strategy_daily) / len(strategy_daily)
        variance = sum((value - benchmark_mean) ** 2 for value in benchmark_daily)
        if variance == 0:
            return 0.0
        covariance = sum(
            (strategy - strategy_mean) * (benchmark - benchmark_mean)
            for strategy, benchmark in zip(strategy_daily, benchmark_daily)
        )
        return covariance / variance

    def _annualized_return(self, cumulative_return: float, day_count: int) -> float:
        if cumulative_return <= -1:
            return -1.0
        return (1 + cumulative_return) ** (252 / max(1, day_count)) - 1

    def _safe_optional_float(self, value) -> float | None:
        if value in (None, "", "unsupported_placeholder"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _safe_float(self, value) -> float:
        return self._safe_optional_float(value) or 0.0

    def _callback_name(self, callback) -> str:
        return str(getattr(callback, "__name__", callback.__class__.__name__))

    def _execution_timeline(self, scheduler: Scheduler) -> list[tuple[str, str]]:
        timeline_items = []
        seen_labels: set[str] = set()
        for index, (label, time_text) in enumerate(self._TIMELINE):
            timeline_items.append((self._parse_schedule_label(label), index, label, time_text))
            seen_labels.add(label)

        next_index = len(timeline_items)
        for entry in scheduler.entries:
            label = str(entry.time)
            if label in seen_labels:
                continue
            parsed_time = self._parse_schedule_label(label)
            timeline_items.append(
                (parsed_time, next_index, label, parsed_time.strftime("%H:%M:%S"))
            )
            seen_labels.add(label)
            next_index += 1

        timeline_items.sort(key=lambda item: (item[0], item[1]))
        return [(label, time_text) for _, _, label, time_text in timeline_items]

    def _parse_schedule_label(self, label: str) -> time:
        if str(label).strip() == "close":
            return time(15, 0)
        canonical_time = dict(self._TIMELINE).get(label)
        if canonical_time is not None:
            return datetime.strptime(canonical_time, "%H:%M:%S").time()

        normalized = str(label).strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(normalized, fmt).time()
            except ValueError:
                continue
        raise NotImplementedError(
            f"Unsupported schedule label for backtest execution timeline: {label}"
        )

    def _mark_to_market(
        self,
        context: Context,
        portal: DataPortal,
        trade_day: str,
        *,
        current_dt: datetime | None = None,
    ) -> None:
        for code, position in context.portfolio.positions.items():
            fields = ["close"]
            use_open = self._uses_open_valuation(current_dt)
            if use_open:
                fields = ["open", "close"]
            frame = portal.get_price(
                code,
                count=1,
                end_date=trade_day,
                fields=fields,
                panel=False,
            )
            if frame.empty:
                continue
            field = "open" if use_open and "open" in frame.columns else "close"
            if field not in frame.columns:
                continue
            price = float(frame[field].iloc[-1])
            if price <= 0 and field == "open" and "close" in frame.columns:
                price = float(frame["close"].iloc[-1])
            if price > 0:
                position.price = price

    def _uses_open_valuation(self, current_dt: datetime | None) -> bool:
        if current_dt is None:
            return False
        time_method = getattr(current_dt, "time", None)
        if not callable(time_method):
            return False
        return time_method() == time(9, 30)

    def _load_a_share_corporate_actions(self) -> dict[str, dict[str, list[dict]]]:
        # Provider contract: stk_div is the TOTAL distribution, not an extra
        # component; cash_div_tax is gross and cash_div is already after tax.
        # https://tushare.pro/document/2?doc_id=103
        groups = {key: defaultdict(list) for key in ("record_date", "ex_date", "pay_date", "div_listdate")}
        seen: dict[str, dict] = {}
        self._corporate_action_ledger = []
        self._corporate_action_unsupported_held_count = 0
        self._corporate_action_sources = []
        required = {"ts_code", "div_proc", "record_date", "ex_date", "pay_date", "div_listdate",
                    "cash_div", "cash_div_tax", "stk_div", "stk_bo_rate", "stk_co_rate"}
        for db_path in self._a_share_corporate_action_db_paths():
            # Never create an empty file or modify the frozen input database.
            with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='corporate_actions' AND type IN ('table','view')").fetchone()
                if not exists:
                    continue
                columns = {row[1] for row in conn.execute("PRAGMA table_info(corporate_actions)")}
                missing = sorted(required - columns)
                if missing:
                    raise ValueError(f"Invalid corporate_actions schema in {db_path}: missing {', '.join(missing)}")
                rows = conn.execute("SELECT * FROM corporate_actions").fetchall()
            self._corporate_action_sources.append(str(db_path))
            for sqlite_row in rows:
                row = dict(sqlite_row)
                ts_code = str(row.get("ts_code") or "").strip()
                if str(row.get("div_proc") or "").strip() != "实施" or not is_tushare_a_share_code(ts_code):
                    continue
                dates = {key: self._corporate_action_date(row.get(key)) for key in groups}
                for key, value in dates.items():
                    if row.get(key) not in (None, "") and not value:
                        raise ValueError(f"Invalid corporate action {key}: {ts_code} {row.get(key)!r}")
                if not dates["record_date"] or not dates["ex_date"] or dates["record_date"] >= dates["ex_date"]:
                    raise ValueError(f"Invalid A-share corporate action record/ex dates: {ts_code} {dates}")
                values = {}
                for key in ("cash_div", "cash_div_tax", "stk_div", "stk_bo_rate", "stk_co_rate"):
                    value = row.get(key)
                    number = 0.0 if value is None else float(value)
                    if not math.isfinite(number) or number < 0:
                        raise ValueError(f"Invalid corporate action {key}: {ts_code} {value!r}")
                    values[key] = number
                if row.get("cash_div_tax") is None and values["cash_div"] > 0:
                    raise ValueError(f"Gross cash_div_tax missing for corporate action: {ts_code}")
                components = values["stk_bo_rate"] + values["stk_co_rate"]
                share_ratio = components if row.get("stk_div") is None else values["stk_div"]
                if row.get("stk_div") is not None and (row.get("stk_bo_rate") is not None or row.get("stk_co_rate") is not None):
                    if not math.isclose(share_ratio, components, rel_tol=1e-8, abs_tol=1e-8):
                        raise ValueError(f"Conflicting total/component stock distribution: {ts_code}")
                if not values["cash_div_tax"] and not share_ratio:
                    continue
                event_id = f"tushare.dividend:{ts_code}:{dates['record_date']}:{dates['ex_date']}"
                economic = {"event_id": event_id, "security": to_joinquant_code(ts_code), **dates,
                            "cash_div": values["cash_div_tax"], "cash_per_share": values["cash_div_tax"],
                            "share_ratio": share_ratio,
                            "bonus_ratio": values["stk_bo_rate"], "transfer_ratio": values["stk_co_rate"]}
                if event_id in seen:
                    previous = {key: seen[event_id][key] for key in economic}
                    if previous != economic:
                        raise ValueError(f"Conflicting duplicate corporate action: {event_id}")
                    seen[event_id]["source_databases"].append(str(db_path))
                    continue
                event = {**economic, "source": "tushare.dividend", "source_row": row,
                         "source_databases": [str(db_path)]}
                seen[event_id] = event
        for event in sorted(seen.values(), key=lambda item: item["event_id"]):
            for key in groups:
                if event[key]:
                    groups[key][event[key]].append(event)
        self._corporate_action_loaded_count = len(seen)
        self._persist_corporate_action_ledger()
        return {key: dict(grouped) for key, grouped in groups.items()}

    def _a_share_corporate_action_db_paths(self) -> list[Path]:
        roots = [Path(str(self.config.cache_db))]
        backend_cache_dir = getattr(self.backend, "cache_dir", None)
        if backend_cache_dir:
            roots.append(Path(str(backend_cache_dir)))
        paths: set[Path] = set()
        for root in roots:
            if root.is_file():
                paths.add(root.resolve())
            for candidate in (root, root / "a_share", root.parent / "a_share"):
                if candidate.is_dir():
                    paths.update(path.resolve() for path in candidate.glob("a_share_????.db"))
                    unified = candidate / "cache.db"
                    if unified.is_file():
                        paths.add(unified.resolve())
        return sorted(paths)

    def _corporate_action_date(self, value) -> str:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{8}\.0+", text):
            text = text.split(".", 1)[0]
        elif isinstance(value, (date, datetime)):
            text = value.strftime("%Y%m%d")
        else:
            text = text.replace("-", "")
        if not re.fullmatch(r"\d{8}", text):
            return ""
        try:
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            return ""

    def _capture_a_share_entitlements(self, context: Context, broker: Broker, trade_day: str,
                                      events: dict[str, list[dict]], entitlements: dict[str, dict]) -> None:
        for event in events.get(str(trade_day), []):
            event_id = str(event["event_id"])
            if event_id in entitlements:
                continue
            position = context.portfolio.positions.get(str(event["security"]))
            amount = max(0., float(position.total_amount)) if position is not None else 0.
            allocations = broker.capture_dividend_lots(str(event["security"]), amount)
            if not math.isclose(sum(float(item["amount"]) for item in allocations), amount, abs_tol=1e-7):
                raise ValueError(f"Corporate action entitlement has incomplete FIFO tax lots: {event_id}")
            entitlement = {**event, "qualified_amount": amount, "eligible_quantity": amount,
                           "tax_allocations": allocations,
                           "record_avg_cost": float(position.avg_cost) if position is not None else 0.,
                           "record_price": float(position.price) if position is not None else 0.}
            entitlements[event_id] = entitlement
            if amount > 0:
                self._corporate_action_ledger.append({**entitlement, "phase": "capture", "date": str(trade_day),
                    "settlement_date": None, "gross_cash": 0., "withheld_tax": 0., "net_cash": 0., "added_shares": 0})
        self._persist_corporate_action_ledger()

    def _settle_a_share_corporate_actions(self, context: Context, broker: Broker, trade_day: str,
                                        events: dict[str, list[dict]], entitlements: dict[str, dict], applied: set[str]) -> None:
        context.current_dt = datetime.combine(date.fromisoformat(str(trade_day)), time.min)
        for event in events.get(str(trade_day), []):
            event_id = str(event["event_id"])
            if event_id in applied:
                continue
            entitlement = entitlements.get(event_id)
            if entitlement is None or float(entitlement.get("qualified_amount") or 0.) <= 0:
                continue
            amount = float(entitlement["qualified_amount"])
            cash_div = float(entitlement["cash_div"])
            ratio = float(entitlement["share_ratio"])
            # Deferred payments require receivables and unlisted-share balances.
            # This ledger deliberately refuses such HELD events before ex-day
            # valuation; it cannot publish a fake drawdown or spend unpaid cash.
            reason = None
            if float(entitlement.get("bonus_ratio") or 0.) > 0:
                reason = "bonus-share dividend tax basis is not certified by the provider data"
            if cash_div > 0 and event["pay_date"] != event["ex_date"]:
                reason = "deferred/missing cash pay_date requires receivable accounting"
            if ratio > 0 and event["div_listdate"] != event["ex_date"]:
                reason = "deferred/missing stock listing requires unlisted-share accounting"
            shares = amount * ratio
            if not math.isclose(shares, round(shares), rel_tol=0., abs_tol=1e-7):
                reason = "fractional stock entitlement has no verified allocation rule"
            if reason:
                self._corporate_action_unsupported_held_count += 1
                self._corporate_action_ledger.append({**entitlement, "phase": "unsupported", "date": str(trade_day), "reason": reason})
                self._persist_corporate_action_ledger()
                raise NotImplementedError(f"Unsupported held corporate action {event_id}: {reason}")
            added_shares = int(round(shares))
            security = str(entitlement["security"])
            gross_cash = amount * cash_div
            withheld = broker.attach_dividend_tax(security, list(entitlement["tax_allocations"]), cash_per_share=cash_div)
            if added_shares:
                broker.add_corporate_action_shares(security, list(entitlement["tax_allocations"]), ratio)
            context.portfolio.available_cash += gross_cash - withheld
            position = context.portfolio.positions.get(security)
            if position is not None or added_shares:
                if position is None:
                    position = Position(code=security)
                    context.portfolio.positions[security] = position
                old_amount = float(position.total_amount)
                # Cash-only distributions reduce the cost of still-held eligible
                # shares too. New shares carry their proportional record-date
                # basis; shares sold between record/ex retain the entitlement.
                existing_eligible = min(old_amount, broker.remaining_corporate_action_shares(entitlement["tax_allocations"]))
                original_basis = float(entitlement["record_avg_cost"])
                remaining_basis = float(position.avg_cost) * old_amount - existing_eligible * original_basis
                adjusted_basis = max(0., original_basis - cash_div) / (1. + ratio)
                new_total = old_amount + added_shares
                position.avg_cost = max(0., remaining_basis + (existing_eligible + added_shares) * adjusted_basis) / new_total if new_total else 0.
                position.price = max(0., (float(position.price or entitlement["record_price"]) - cash_div) / (1. + ratio))
                position.total_amount += added_shares
                position.closeable_amount += added_shares
            applied.add(event_id)
            self._corporate_action_ledger.append({**entitlement, "phase": "settlement", "date": str(trade_day),
                "settlement_date": str(trade_day), "gross_cash": gross_cash, "withheld_tax": withheld,
                "net_cash": gross_cash - withheld, "added_shares": added_shares,
                "tax_model": "research_prepaid_cash_dividend_tax"})
        self._persist_corporate_action_ledger()

    def _persist_corporate_action_ledger(self) -> None:
        run_dir = getattr(self, "active_run_dir", None)
        if run_dir is not None:
            path = Path(run_dir) / "artifacts" / "corporate_action_ledger.json"
            path.write_text(json.dumps(self._corporate_action_ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _corporate_action_summary(self) -> dict:
        ledger = getattr(self, "_corporate_action_ledger", [])
        settled = [row for row in ledger if row["phase"] == "settlement"]
        unsupported = getattr(self, "_corporate_action_unsupported_held_count", 0)
        return {"loaded_count": getattr(self, "_corporate_action_loaded_count", 0),
                "source_databases": getattr(self, "_corporate_action_sources", []),
                "held_capture_count": sum(row["phase"] == "capture" for row in ledger),
                "settlement_count": len(settled), "unsupported_held_count": unsupported,
                **{key: sum(row[key] for row in settled) for key in ("gross_cash", "withheld_tax", "net_cash", "added_shares")},
                "ending_receivables": 0. if unsupported == 0 else None,
                "supported_scope": "Shanghai/Shenzhen A-share same-ex/pay/listing-date integral distributions",
                "tax_model": "research_prepaid_cash_dividend_tax",
                "tax_model_note": "Cash dividends use gross provider amounts and 20% prepaid tax with FIFO holding-period refunds; exact live tax timing is not reproduced. Held bonus-share events without a certified tax basis are rejected; capital-reserve transfers are modeled as nontaxable, but their reserve origin is not certified.",
                "data_completeness_verified": False,
                "data_completeness_note": "Requires independent held-security adjustment-factor/event coverage audit."}

    def _load_etf_dividend_events(self) -> dict[str, list[dict]]:
        path = self._etf_dividend_events_path()
        if path is None:
            return {}
        events: dict[str, list[dict]] = defaultdict(list)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ex_date = str(row.get("ex_date") or "")[:10]
                ts_code = str(row.get("ts_code") or "").strip()
                cash = self._parse_cash_dividend(row.get("cash"))
                if not ex_date or not ts_code or cash <= 0:
                    continue
                events[ex_date].append(
                    {
                        "security": to_joinquant_code(ts_code),
                        "cash": cash,
                    }
                )
        return dict(events)

    def _etf_dividend_events_path(self) -> Path | None:
        return self._metadata_file_path("etf_adj_events.csv")

    def _metadata_file_path(self, filename: str) -> Path | None:
        candidates = []
        cache_path = Path(str(self.config.cache_db))
        candidates.append(cache_path / "_meta" / filename)
        candidates.append(cache_path.parent / "_meta" / filename)
        backend_cache_dir = getattr(self.backend, "cache_dir", None)
        if backend_cache_dir:
            backend_path = Path(str(backend_cache_dir))
            candidates.append(backend_path / "_meta" / filename)
            candidates.append(backend_path.parent / "_meta" / filename)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _parse_cash_dividend(self, value) -> float:
        match = re.search(r"(\d+(?:\.\d+)?|\.\d+)", str(value or ""))
        if match is None:
            return 0.0
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0

    def _apply_etf_dividends(
        self,
        context: Context,
        portal: DataPortal,
        trade_day: str,
        events: dict[str, list[dict]],
        applied: set[tuple[str, str]],
        eligible_securities: set[str],
    ) -> None:
        for event in events.get(str(trade_day), []):
            security = str(event.get("security") or "")
            key = (str(trade_day), security)
            if not security or key in applied:
                continue
            applied.add(key)
            if security not in eligible_securities:
                continue
            position = context.portfolio.positions.get(security)
            if position is None or int(position.total_amount or 0) <= 0:
                continue
            cash_per_share = float(event.get("cash") or 0.0)
            if cash_per_share <= 0:
                continue
            amount = int(position.total_amount)
            context.portfolio.available_cash += amount * cash_per_share
            position.avg_cost = max(0.0, float(position.avg_cost or 0.0) - cash_per_share)
            position.price = max(0.0, float(position.price or 0.0) - cash_per_share)
        for security in eligible_securities:
            position = context.portfolio.positions.get(security)
            if position is None:
                continue
            key = (str(trade_day), str(security))
            if key in applied or int(position.total_amount or 0) <= 0:
                continue
            cash_per_share = self._infer_etf_cash_dividend(portal, str(trade_day), str(security))
            if cash_per_share <= 0:
                continue
            applied.add(key)
            amount = int(position.total_amount)
            context.portfolio.available_cash += amount * cash_per_share
            position.avg_cost = max(0.0, float(position.avg_cost or 0.0) - cash_per_share)
            position.price = max(0.0, float(position.price or 0.0) - cash_per_share)

    def _infer_etf_cash_dividend(self, portal: DataPortal, trade_day: str, security: str) -> float:
        if not is_tushare_fund_code(security):
            return 0.0
        try:
            frame = portal.get_price(
                security,
                count=2,
                end_date=trade_day,
                fields=["close", "pre_close"],
                panel=False,
                fq=None,
            )
        except (NotImplementedError, KeyError, ValueError):
            return 0.0
        if frame.empty or len(frame) < 2:
            return 0.0
        frame = frame.sort_values("time").reset_index(drop=True)
        current = frame.iloc[-1]
        if str(current.get("time")) != str(trade_day):
            return 0.0
        previous_close = self._positive_float(frame.iloc[-2].get("close"))
        current_pre_close = self._positive_float(current.get("pre_close"))
        if previous_close <= 0 or current_pre_close <= 0:
            return 0.0
        cash = round(previous_close - current_pre_close, 4)
        return cash if cash >= 0.0005 else 0.0

    def _positive_float(self, value) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return number if number > 0 else 0.0

    def _position_rows(
        self,
        context: Context,
        trade_day: str,
        security_names: dict[str, str],
    ) -> list[dict]:
        rows = []
        for code, position in sorted(context.portfolio.positions.items()):
            amount = int(position.total_amount or 0)
            if amount <= 0:
                continue
            price = float(position.price or 0.0)
            avg_cost = float(position.avg_cost or 0.0)
            market_value = float(position.value)
            rows.append(
                {
                    "date": trade_day,
                    "security": code,
                    "name": security_names.get(code, ""),
                    "amount": amount,
                    "price": f"{price:.4f}",
                    "avg_cost": f"{avg_cost:.4f}",
                    "market_value": f"{market_value:.2f}",
                    "pnl": f"{(price - avg_cost) * amount:.2f}",
                }
            )
        return rows

    def _security_names(self, portal: DataPortal) -> dict[str, str]:
        securities = portal.get_all_securities()
        names = {}
        if not securities.empty and "name" in securities.columns:
            names.update(
                {
                    str(code): str(row["name"])
                    for code, row in securities.iterrows()
                }
            )
        names.update(self._etf_security_names())
        names.update(self._JOINQUANT_DISPLAY_NAME_ALIASES)
        return names

    def _etf_security_names(self) -> dict[str, str]:
        path = self._metadata_file_path("etf_basic.csv")
        if path is None:
            return {}
        names = {}
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ts_code = str(row.get("ts_code") or "").strip()
                name = str(row.get("name") or "").strip()
                if ts_code and name:
                    names[to_joinquant_code(ts_code)] = name
        return names

    def _read_log_lines(self, path) -> list[str]:
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()


class _ProfilingBackend:
    def __init__(self, backend, profiler: "_PerformanceProfiler"):
        self._backend = backend
        self._profiler = profiler

    def fetch(self, api_name, **params):
        start = self._profiler.now()
        try:
            return self._backend.fetch(api_name, **params)
        finally:
            self._profiler.record_data_call(str(api_name), self._profiler.elapsed(start))

    def status(self, api_name):
        if not hasattr(self._backend, "status"):
            raise AttributeError("backend does not support status")
        return self._backend.status(api_name)

    def __getattr__(self, name):
        return getattr(self._backend, name)


class _PerformanceProfiler:
    def __init__(self):
        self._started_at = self.now()
        self._phase_seconds: dict[str, float] = defaultdict(float)
        self._phase_labels: dict[str, str] = {}
        self._data_calls: dict[str, dict] = defaultdict(lambda: {"api": "", "count": 0, "seconds": 0.0})
        self._callbacks: list[dict] = []
        self._days: list[dict] = []

    def now(self) -> float:
        return time_module.perf_counter()

    def elapsed(self, started_at: float) -> float:
        return max(0.0, self.now() - started_at)

    @contextmanager
    def measure_phase(self, name: str, label: str):
        start = self.now()
        try:
            yield
        finally:
            self.record_phase_duration(name, label, self.elapsed(start))

    def record_phase_duration(self, name: str, label: str, seconds: float) -> None:
        self._phase_labels[name] = label
        self._phase_seconds[name] += max(0.0, float(seconds))

    def record_data_call(self, api_name: str, seconds: float) -> None:
        payload = self._data_calls[api_name]
        payload["api"] = api_name
        payload["count"] += 1
        payload["seconds"] += max(0.0, float(seconds))

    def record_callback(self, *, date: str, time_label: str, name: str, seconds: float) -> None:
        self._callbacks.append(
            {
                "date": date,
                "time_label": time_label,
                "name": name,
                "seconds": round(max(0.0, float(seconds)), 6),
            }
        )

    def record_day(
        self,
        *,
        date: str,
        seconds: float,
        callback_seconds: float,
        mark_to_market_seconds: float,
        callback_count: int,
    ) -> None:
        self._days.append(
            {
                "date": date,
                "seconds": round(max(0.0, float(seconds)), 6),
                "callback_seconds": round(max(0.0, float(callback_seconds)), 6),
                "mark_to_market_seconds": round(max(0.0, float(mark_to_market_seconds)), 6),
                "callback_count": int(callback_count),
            }
        )

    def snapshot(self, *, trade_days: int, data_portal: dict | None = None) -> dict:
        total_seconds = self.elapsed(self._started_at)
        phase_timings = [
            {
                "name": name,
                "label": self._phase_labels.get(name, name),
                "seconds": round(seconds, 6),
                "percent": round(seconds / total_seconds * 100, 2) if total_seconds else 0.0,
            }
            for name, seconds in sorted(
                self._phase_seconds.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ]
        data_calls = [
            {
                "api": payload["api"],
                "count": int(payload["count"]),
                "seconds": round(float(payload["seconds"]), 6),
            }
            for payload in sorted(
                self._data_calls.values(),
                key=lambda item: (item["seconds"], item["count"]),
                reverse=True,
            )
        ]
        return {
            "total_seconds": round(total_seconds, 6),
            "trade_days": int(trade_days),
            "callback_count": len(self._callbacks),
            "phase_timings": phase_timings,
            "slowest_callbacks": sorted(
                self._callbacks,
                key=lambda item: item["seconds"],
                reverse=True,
            )[:10],
            "slowest_days": sorted(
                self._days,
                key=lambda item: item["seconds"],
                reverse=True,
            )[:10],
            "data_api_calls": data_calls,
            "data_portal": data_portal or {},
        }
