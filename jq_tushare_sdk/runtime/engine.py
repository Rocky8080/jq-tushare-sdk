from __future__ import annotations

import time as time_module
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, time

from jq_tushare_sdk.broker.broker import Broker
from jq_tushare_sdk.config import BacktestConfig, RunManifest
from jq_tushare_sdk.data.portal import DataPortal
from jq_tushare_sdk.reports.joinquant_formatter import JoinQuantOutputFormatter
from jq_tushare_sdk.reports.metrics import performance_row
from jq_tushare_sdk.reports.output_manager import OutputManager
from jq_tushare_sdk.runtime.context import Context, Portfolio
from jq_tushare_sdk.runtime.globals_state import RuntimeState
from jq_tushare_sdk.runtime.loader import StrategyLoader
from jq_tushare_sdk.runtime.logger import RuntimeLogger
from jq_tushare_sdk.runtime.scheduler import Scheduler


class BacktestEngine:
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
    ):
        self.config = config
        self.backend = backend
        self.output_manager = output_manager or OutputManager()

    def run(self) -> RunManifest:
        profiler = _PerformanceProfiler()
        output = self.output_manager.create_run(self.config)
        logger = RuntimeLogger(output.logs_dir / "backtest.log")
        portal = DataPortal(
            _ProfilingBackend(self.backend, profiler),
            optimize_data=self.config.optimize_data,
        )
        scheduler = Scheduler()
        context = Context(Portfolio(self.config.initial_cash))
        broker = Broker(context, portal)
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
        with profiler.measure_phase("setup", "基础数据准备"):
            timeline = self._execution_timeline(scheduler)
            security_names = self._security_names(portal)
            benchmark = getattr(state, "benchmark", None) or self.config.benchmark
            trade_days = list(portal.get_trade_days(self.config.start_date, self.config.end_date))
        with profiler.measure_phase("benchmark", "基准数据读取"):
            benchmark_closes = self._benchmark_closes(portal, benchmark, trade_days)
            benchmark_issue = None
            if trade_days and not benchmark_closes:
                benchmark_issue = self._benchmark_missing_reason(benchmark)

        rows = []
        position_rows = []
        previous_total = None
        peak_value = float(context.portfolio.total_value)
        previous_trade_day = None
        for trade_day in trade_days:
            day_start = profiler.now()
            day_callback_seconds = 0.0
            day_mark_seconds = 0.0
            day_callback_count = 0
            context.previous_date = previous_trade_day
            for label, time_text in timeline:
                context.current_dt = datetime.strptime(
                    f"{trade_day} {time_text}",
                    "%Y-%m-%d %H:%M:%S",
                )
                for callback in scheduler.callbacks_for(
                    context.current_dt,
                    label,
                    previous_dt=previous_trade_day,
                ):
                    callback_start = profiler.now()
                    try:
                        callback(context)
                    finally:
                        elapsed = profiler.elapsed(callback_start)
                        day_callback_seconds += elapsed
                        day_callback_count += 1
                        profiler.record_phase_duration("callbacks", "策略回调", elapsed)
                        profiler.record_callback(
                            date=str(trade_day),
                            time_label=str(label),
                            name=self._callback_name(callback),
                            seconds=elapsed,
                        )
            mark_start = profiler.now()
            self._mark_to_market(context, portal, str(trade_day))
            day_mark_seconds = profiler.elapsed(mark_start)
            profiler.record_phase_duration("mark_to_market", "每日估值", day_mark_seconds)
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
            previous_trade_day = datetime.strptime(str(trade_day), "%Y-%m-%d")
            profiler.record_day(
                date=str(trade_day),
                seconds=profiler.elapsed(day_start),
                callback_seconds=day_callback_seconds,
                mark_to_market_seconds=day_mark_seconds,
                callback_count=day_callback_count,
            )
        with profiler.measure_phase("risk_metrics", "收益指标计算"):
            self._apply_benchmark_returns(rows, benchmark_closes)

        formatter = JoinQuantOutputFormatter()
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
            performance_profile=profiler.snapshot(trade_days=len(trade_days)),
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
        return output

    def _summary(
        self,
        *,
        rows,
        context,
        broker,
        state,
        benchmark: str,
        benchmark_issue: str | None = None,
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
            "performance_profile": performance_profile or {},
            **risk,
        }

    def _benchmark_closes(self, portal: DataPortal, benchmark: str, trade_days: list[str]) -> dict[str, float]:
        if not trade_days:
            return {}
        try:
            frame = portal.get_price(
                benchmark,
                start_date=str(trade_days[0]),
                end_date=str(trade_days[-1]),
                fields=["close"],
                panel=False,
            )
        except NotImplementedError:
            return {}
        if frame.empty or "time" not in frame.columns or "close" not in frame.columns:
            return {}
        return {
            str(row["time"]): float(row["close"])
            for _, row in frame.iterrows()
            if row.get("close") is not None
        }

    def _apply_benchmark_returns(self, rows: list[dict], benchmark_closes: dict[str, float]) -> None:
        if not rows or not benchmark_closes:
            return
        base_close = None
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

    def _mark_to_market(self, context: Context, portal: DataPortal, trade_day: str) -> None:
        for code, position in context.portfolio.positions.items():
            frame = portal.get_price(
                code,
                count=1,
                end_date=trade_day,
                fields=["close"],
                panel=False,
            )
            if frame.empty or "close" not in frame.columns:
                continue
            position.price = float(frame["close"].iloc[-1])

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
        if securities.empty or "name" not in securities.columns:
            return {}
        return {
            str(code): str(row["name"])
            for code, row in securities.iterrows()
        }

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

    def snapshot(self, *, trade_days: int) -> dict:
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
        }
