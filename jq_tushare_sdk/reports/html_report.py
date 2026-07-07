from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Callable, Iterable

from jq_tushare_sdk import __version__ as SDK_VERSION


class JoinQuantHtmlReport:
    def render(
        self,
        *,
        config,
        manifest,
        performance_rows: list[dict],
        summary: dict,
        trades: list,
        position_rows: list[dict],
        log_lines: Iterable[str] | None = None,
        security_names: dict[str, str] | None = None,
    ) -> str:
        names = security_names or {}
        strategy_name = config.strategy_name or Path(config.strategy_path).stem
        metrics = self._metrics(config, performance_rows, summary, trades)
        profile = summary.get("performance_profile") or {}
        equity_svg = self._equity_svg(performance_rows)
        returns_svg = self._bar_svg(
            performance_rows,
            "daily_return",
            positive="#5b8c3b",
            negative="#6f5a8f",
            value_format="percent",
        )
        exposure_svg = self._bar_svg(
            performance_rows,
            "positions_value",
            positive="#1b8ea7",
            negative="#c46f2f",
            value_format="money",
        )

        return "\n".join(
            [
                "<!doctype html>",
                '<html lang="zh-CN">',
                "<head>",
                '<meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width, initial-scale=1">',
                f"<title>{escape(strategy_name)} 回测报告</title>",
                f"<style>{self._css()}</style>",
                "</head>",
                "<body>",
                self._topbar(config, summary, strategy_name),
                self._tabs(),
                '<main class="workspace">',
                self._risk_section(metrics, equity_svg, returns_svg, exposure_svg, config, performance_rows, summary),
                self._transactions_section(trades, names),
                self._positions_section(position_rows, performance_rows, names),
                self._performance_section(profile),
                self._logs_section(config, manifest, log_lines or []),
                "</main>",
                f"<!-- run_id: {escape(str(manifest.run_id))} -->",
                self._navigation_script(),
                "</body>",
                "</html>",
            ]
        )

    def _topbar(self, config, summary: dict, strategy_name: str) -> str:
        final_value = self._money(summary.get("final_value"))
        initial_cash = self._money(config.initial_cash)
        return f"""
<header class="topbar">
  <div class="topbar-main">
    <div class="report-kicker">本地回测报告</div>
    <div class="report-title">{escape(strategy_name)} 回测报告</div>
  </div>
  <div class="run-strip" aria-label="回测设置">
    <span class="setting">设置： {escape(str(config.start_date))} 到 {escape(str(config.end_date))}</span>
    <span class="divider"></span>
    <span>初始资金 {initial_cash}</span>
    <span class="divider"></span>
    <span>每天</span>
    <span class="divider"></span>
    <span>状态： <span class="status-dot">●</span> 回测完成</span>
    <span class="runtime">Python3</span>
    <span class="runtime sdk-version">SDK v{escape(SDK_VERSION)}</span>
  </div>
  <div class="final-value">期末权益 {final_value}</div>
</header>"""

    def _tabs(self) -> str:
        tabs = [
            ("risk", "收益风险"),
            ("trades", "交易"),
            ("holdings", "持仓"),
            ("performance", "性能"),
            ("logs", "日志"),
        ]
        links = "\n".join(
            f'  <a class="tab-link{" active" if index == 0 else ""}" href="#{anchor}">{escape(label)}</a>'
            for index, (anchor, label) in enumerate(tabs)
        )
        return f"""
<nav class="report-tabs" aria-label="报告视图">
{links}
</nav>"""

    def _risk_section(
        self,
        metrics: list[dict],
        equity_svg: str,
        returns_svg: str,
        exposure_svg: str,
        config,
        performance_rows: list[dict],
        summary: dict,
    ) -> str:
        metric_cards = "\n".join(
            f"""
      <div class="metric-card compact" id="{escape(item['anchor'])}">
        <div class="metric-label">{escape(item['label'])}</div>
        <div class="metric-value {escape(item['tone'])}">{escape(item['value'])}</div>
      </div>"""
            for item in metrics
        )
        return_legend = self._return_legend(performance_rows)
        risk_note = self._risk_note(summary)
        return f"""
<section id="risk" class="panel">
  <div class="section-heading">
    <div>
      <div class="section-eyebrow">收益风险</div>
      <h2>收益与风险</h2>
    </div>
    <div class="section-note">{escape(risk_note)}</div>
  </div>
  <div class="metric-grid">{metric_cards}
  </div>
  <div class="chart-toolbar">
    {return_legend}
    <span class="toolbar-time">时间： <b>{escape(str(config.start_date))}</b> - <b>{escape(str(config.end_date))}</b></span>
  </div>
  <div class="chart-block">{equity_svg}</div>
  <div class="mini-chart-grid">
    <div>
      <div class="chart-caption">每日收益</div>
      <div class="chart-block small">{returns_svg}</div>
    </div>
    <div>
      <div class="chart-caption">每日持仓市值</div>
      <div class="chart-block small">{exposure_svg}</div>
    </div>
  </div>
</section>"""

    def _risk_note(self, summary: dict) -> str:
        reasons = summary.get("unsupported_reasons")
        if isinstance(reasons, dict):
            for key in ("benchmark_return", "excess_return", "alpha", "beta"):
                reason = reasons.get(key)
                if reason:
                    return str(reason)
        return "曲线保留策略、基准和超额收益口径。"

    def _transactions_section(self, trades: list, security_names: dict[str, str]) -> str:
        trades_by_date: dict[str, list[tuple[str, object]]] = defaultdict(list)
        for trade in trades:
            date, time_text = self._trade_date_time(trade)
            trades_by_date[date].append((time_text, trade))

        total_value = sum(abs(float(getattr(trade, "value", 0.0) or 0.0)) for trade in trades)
        total_fee = sum(
            sum(float(getattr(trade, attr, 0.0) or 0.0) for attr in ("commission", "stamp_tax", "transfer_fee"))
            for trade in trades
        )
        buys = sum(1 for trade in trades if getattr(trade, "side", "") == "buy")
        sells = len(trades) - buys
        summary_cards = self._small_stat_cards(
            [
                ("成交次数", str(len(trades))),
                ("买入/卖出", f"{buys}/{sells}"),
                ("成交额", self._money(total_value)),
                ("手续费税费", self._money(total_fee)),
            ]
        )
        groups = []
        for date in sorted(trades_by_date):
            day_trades = trades_by_date[date]
            day_value = sum(abs(float(getattr(trade, "value", 0.0) or 0.0)) for _time, trade in day_trades)
            day_fee = sum(
                sum(float(getattr(trade, attr, 0.0) or 0.0) for attr in ("commission", "stamp_tax", "transfer_fee"))
                for _time, trade in day_trades
            )
            rows = []
            for time_text, trade in day_trades:
                code = str(getattr(trade, "security", ""))
                name = security_names.get(code, code)
                side = "买" if getattr(trade, "side", "") == "buy" else "卖"
                signed_amount = int(getattr(trade, "amount", 0) or 0)
                if side == "卖":
                    signed_amount = -signed_amount
                fee = sum(
                    float(getattr(trade, attr, 0.0) or 0.0)
                    for attr in ("commission", "stamp_tax", "transfer_fee")
                )
                rows.append(
                    "<tr>"
                    f"<td>{escape(time_text)}</td>"
                    f"<td>{escape(name)}({escape(code)})</td>"
                    f"<td>{side}</td>"
                    "<td>市价单</td>"
                    f"<td>{escape(self._shares(signed_amount))}</td>"
                    f"<td>{escape(self._price(getattr(trade, 'price', 0.0)))}</td>"
                    f"<td>{escape(self._money(getattr(trade, 'value', 0.0)))}</td>"
                    "<td>0.00</td>"
                    f"<td>{escape(self._money(fee))}</td>"
                    "</tr>"
                )
            groups.append(
                f"""
      <details class="day-group">
        <summary>
          <span class="detail-action">展开交易明细</span>
          <strong>{escape(date)}</strong>
          <span>{len(day_trades)} 笔</span>
          <span>成交额 {escape(self._money(day_value))}</span>
          <span>费用 {escape(self._money(day_fee))}</span>
        </summary>
        <div class="table-wrap">
          <table class="report-table">
            <thead><tr><th>委托时间</th><th>标的</th><th>交易类型</th><th>下单类型</th><th>成交数量</th><th>成交价</th><th>成交额</th><th>平仓盈亏</th><th>手续费</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
      </details>"""
            )

        empty = '<div class="empty">无交易记录</div>'
        return f"""
<section id="trades" class="panel">
  <div class="section-heading">
    <div>
      <div class="section-eyebrow">交易</div>
      <h2>交易审计</h2>
    </div>
    <div class="section-note">默认按交易日收起，展开后核对成交价、成交额和费用。</div>
  </div>
  <div class="stat-grid">{summary_cards}</div>
  <div class="detail-list">{''.join(groups) if groups else empty}</div>
</section>"""

    def _positions_section(
        self,
        position_rows: list[dict],
        performance_rows: list[dict],
        security_names: dict[str, str],
    ) -> str:
        rows_by_date = self._group_dicts_by_date(position_rows)
        performance_by_date = {str(row.get("date", "")): row for row in performance_rows}
        all_dates = sorted(set(performance_by_date) | set(rows_by_date))
        last_performance = performance_rows[-1] if performance_rows else {}
        last_positions = rows_by_date.get(str(last_performance.get("date", "")), [])
        summary_cards = self._small_stat_cards(
            [
                ("交易日", str(len(all_dates))),
                ("期末持仓", str(len(last_positions))),
                ("期末现金", self._money(last_performance.get("cash"))),
                ("期末权益", self._money(last_performance.get("total_value"))),
            ]
        )
        groups = []
        for date in all_dates:
            rows = rows_by_date.get(date, [])
            performance = performance_by_date.get(date, {})
            table_rows = []
            for row in rows:
                code = str(row.get("security", ""))
                name = str(row.get("name") or security_names.get(code) or code)
                table_rows.append(
                    "<tr>"
                    f"<td>{escape(name)}({escape(code)})</td>"
                    f"<td>{escape(self._shares(row.get('amount')))}</td>"
                    f"<td>{escape(self._price(row.get('price')))}</td>"
                    f"<td>{escape(self._money(row.get('market_value')))}</td>"
                    f"<td class=\"{self._number_class(row.get('pnl'))}\">{escape(self._money(row.get('pnl')))}</td>"
                    "</tr>"
                )
            table_rows.append(
                "<tr>"
                '<td><span class="cash-pill">Cash</span></td>'
                "<td></td><td></td>"
                f"<td>{escape(self._money(performance.get('cash')))}</td>"
                "<td>0.00</td>"
                "</tr>"
            )
            table_rows.append(
                '<tr class="total-row">'
                '<td colspan="3"></td>'
                f"<td>总共:{escape(self._money(performance.get('total_value')))}</td>"
                f"<td>{escape(self._money(self._daily_position_pnl(rows)))}</td>"
                "</tr>"
            )
            groups.append(
                f"""
      <details class="day-group">
        <summary>
          <span class="detail-action">展开持仓明细</span>
          <strong>{escape(date)}</strong>
          <span>{len(rows)} 只持仓</span>
          <span>总权益 {escape(self._money(performance.get('total_value')))}</span>
          <span>日收益 {escape(self._percent(performance.get('daily_return')))}</span>
        </summary>
        <div class="table-wrap">
          <table class="report-table">
            <thead><tr><th>标的</th><th>数量</th><th>收盘价/结算价</th><th>市值/价值</th><th>盈亏/逐笔浮盈</th></tr></thead>
            <tbody>{''.join(table_rows)}</tbody>
          </table>
        </div>
      </details>"""
            )

        empty = '<div class="empty">无持仓记录</div>'
        return f"""
<section id="holdings" class="panel">
  <div class="section-heading">
    <div>
      <div class="section-eyebrow">持仓</div>
      <h2>持仓与日收益</h2>
    </div>
    <div class="section-note">按交易日查看现金、个股市值和逐笔浮盈，避免长表一次性铺开。</div>
  </div>
  <div class="stat-grid">{summary_cards}</div>
  <div class="detail-list">{''.join(groups) if groups else empty}</div>
</section>"""

    def _performance_section(self, profile: dict) -> str:
        if not profile:
            return """
<section id="performance" class="panel">
  <div class="section-heading">
    <div>
      <div class="section-eyebrow">性能</div>
      <h2>性能瓶颈</h2>
    </div>
    <div class="section-note">本次回测未记录性能采样。</div>
  </div>
  <div class="empty">无性能采集数据</div>
</section>"""

        top_callback = (profile.get("slowest_callbacks") or [{}])[0]
        top_api = (profile.get("data_api_calls") or [{}])[0]
        summary_cards = self._small_stat_cards(
            [
                ("总耗时", self._duration(profile.get("total_seconds"))),
                ("交易日", str(profile.get("trade_days", 0))),
                ("主要瓶颈", self._top_phase_label(profile)),
                ("最慢回调", self._callback_label(top_callback)),
                ("最重数据接口", self._api_label(top_api)),
            ]
        )
        phase_bars = self._phase_bars(profile.get("phase_timings") or [])
        phase_rows = self._performance_table_rows(
            profile.get("phase_timings") or [],
            columns=[
                ("label", "环节"),
                ("seconds", "耗时"),
                ("percent", "占比"),
            ],
            formatters={"seconds": self._duration, "percent": self._percent_number},
        )
        callback_rows = self._performance_table_rows(
            profile.get("slowest_callbacks") or [],
            columns=[
                ("date", "日期"),
                ("time_label", "时点"),
                ("name", "回调"),
                ("seconds", "耗时"),
            ],
            formatters={"seconds": self._duration},
        )
        api_rows = self._performance_table_rows(
            profile.get("data_api_calls") or [],
            columns=[
                ("api", "接口"),
                ("count", "次数"),
                ("seconds", "耗时"),
            ],
            formatters={"seconds": self._duration},
        )
        day_rows = self._performance_table_rows(
            profile.get("slowest_days") or [],
            columns=[
                ("date", "日期"),
                ("seconds", "总耗时"),
                ("callback_seconds", "回调耗时"),
                ("mark_to_market_seconds", "估值耗时"),
                ("callback_count", "回调数"),
            ],
            formatters={
                "seconds": self._duration,
                "callback_seconds": self._duration,
                "mark_to_market_seconds": self._duration,
            },
        )
        recommendations = "".join(
            f"<li>{escape(item)}</li>"
            for item in self._performance_recommendations(profile)
        )
        return f"""
<section id="performance" class="panel">
  <div class="section-heading">
    <div>
      <div class="section-eyebrow">性能</div>
      <h2>性能瓶颈</h2>
    </div>
    <div class="section-note">优先看阶段耗时，再下钻到回调、接口和交易日。</div>
  </div>
  <div class="stat-grid">{summary_cards}</div>
  <div class="perf-callout">
    <div class="perf-heading">瓶颈定位</div>
    <p>{escape(self._performance_bottleneck(profile))}</p>
  </div>
  <div class="phase-bars">{phase_bars}</div>
  <details class="detail-block" open>
    <summary>阶段耗时明细</summary>
    <div class="table-wrap">
      <table class="report-table perf-table">
        <thead><tr><th>环节</th><th>耗时</th><th>占比</th></tr></thead>
        <tbody>{phase_rows}</tbody>
      </table>
    </div>
  </details>
  <details class="detail-block">
    <summary>最慢回调</summary>
    <div class="table-wrap">
      <table class="report-table perf-table">
        <thead><tr><th>日期</th><th>时点</th><th>回调</th><th>耗时</th></tr></thead>
        <tbody>{callback_rows}</tbody>
      </table>
    </div>
  </details>
  <details class="detail-block">
    <summary>数据接口耗时</summary>
    <div class="table-wrap">
      <table class="report-table perf-table">
        <thead><tr><th>接口</th><th>次数</th><th>耗时</th></tr></thead>
        <tbody>{api_rows}</tbody>
      </table>
    </div>
  </details>
  <details class="detail-block">
    <summary>最慢交易日</summary>
    <div class="table-wrap">
      <table class="report-table perf-table">
        <thead><tr><th>日期</th><th>总耗时</th><th>回调耗时</th><th>估值耗时</th><th>回调数</th></tr></thead>
        <tbody>{day_rows}</tbody>
      </table>
    </div>
  </details>
  <div class="advice-block">
    <div class="perf-heading">优化建议</div>
    <ul class="perf-advice">{recommendations}</ul>
  </div>
</section>"""

    def _logs_section(self, config, manifest, log_lines: Iterable[str]) -> str:
        lines = [line.rstrip("\n") for line in log_lines]
        important = [
            line
            for line in lines
            if any(keyword in line.upper() for keyword in ("ERROR", "WARNING", "WARN", "EXCEPTION", "TRACEBACK"))
        ]
        preview_lines = important[-80:] if important else lines[-120:]
        preview = "\n".join(escape(line) for line in preview_lines) or "无日志输出"
        run_items = [
            ("Run ID", str(manifest.run_id)),
            ("SDK版本", f"v{SDK_VERSION}"),
            ("策略文件", Path(config.strategy_path).name),
            ("策略版本", str(getattr(config, "strategy_version", None) or "--")),
            ("策略来源", str(getattr(config, "strategy_source", None) or "--")),
            ("策略Hash", str(getattr(config, "strategy_hash", None) or "--")),
            ("回测区间", f"{config.start_date} 到 {config.end_date}"),
            ("缓存库", Path(str(config.cache_db)).name),
        ]
        if getattr(config, "project_strategy_path", None):
            run_items.extend(
                [
                    ("项目原文件", str(config.project_strategy_path)),
                    ("项目原文件版本", str(getattr(config, "project_strategy_version", None) or "--")),
                ]
            )
        run_info = "\n".join(
            f"""
      <div class="run-item">
        <span>{escape(label)}</span>
        <strong>{escape(value)}</strong>
      </div>"""
            for label, value in run_items
        )
        return f"""
<section id="logs" class="panel">
  <div class="section-heading">
    <div>
      <div class="section-eyebrow">日志</div>
      <h2>日志与复现</h2>
    </div>
    <div class="section-note">默认展示警告/错误或最后一段日志，完整日志仍保存在本次回测目录。</div>
  </div>
  <div class="run-info">
    <div class="run-info-title">复现信息</div>
    <div class="run-grid">{run_info}
    </div>
  </div>
  <details class="log-details" open>
    <summary>展开日志预览</summary>
    <pre class="log-view">{preview}</pre>
  </details>
</section>"""

    def _metrics(self, config, performance_rows: list[dict], summary: dict, trades: list) -> list[dict]:
        initial_cash = float(config.initial_cash or 0.0)
        final_value = float(summary.get("final_value") or 0.0)
        cumulative = final_value / initial_cash - 1 if initial_cash else 0.0
        day_count = max(1, len(performance_rows))
        annualized = (1 + cumulative) ** (252 / day_count) - 1 if cumulative > -1 else None
        daily_returns = [self._to_float(row.get("daily_return")) for row in performance_rows]
        drawdowns = [self._to_float(row.get("drawdown")) for row in performance_rows]
        sharpe = self._annualized_sharpe(daily_returns)
        turnover_base = self._average_equity(performance_rows, initial_cash, final_value)
        total_trade_value = sum(abs(float(getattr(trade, "value", 0.0) or 0.0)) for trade in trades)
        capital_turnover = total_trade_value / turnover_base if turnover_base else None
        daily_turnover = capital_turnover / day_count if capital_turnover is not None else None
        total_cost = sum(
            sum(float(getattr(trade, attr, 0.0) or 0.0) for attr in ("commission", "stamp_tax", "transfer_fee"))
            for trade in trades
        )
        positive_days = sum(1 for value in daily_returns if value > 0)
        negative_days = sum(1 for value in daily_returns if value < 0)

        return [
            {"anchor": "strategy-return", "label": "策略收益", "value": self._percent(cumulative), "tone": self._tone(cumulative)},
            {"anchor": "annual-return", "label": "策略年化收益", "value": self._percent(annualized), "tone": self._tone(annualized)},
            {"anchor": "excess-return", "label": "超额收益", "value": self._placeholder(summary.get("excess_return")), "tone": self._tone(self._optional_float(summary.get("excess_return")))},
            {"anchor": "benchmark-return", "label": "基准收益", "value": self._placeholder(summary.get("benchmark_return")), "tone": self._tone(self._optional_float(summary.get("benchmark_return")))},
            {"anchor": "sharpe-ratio", "label": "夏普比率", "value": self._ratio_placeholder(sharpe), "tone": self._tone(sharpe)},
            {"anchor": "max-drawdown", "label": "最大回撤", "value": self._percent(min(drawdowns) if drawdowns else 0.0), "tone": "negative"},
            {"anchor": "alpha", "label": "阿尔法", "value": self._placeholder(summary.get("alpha")), "tone": self._tone(self._optional_float(summary.get("alpha")))},
            {"anchor": "beta", "label": "贝塔", "value": self._ratio_placeholder(summary.get("beta")), "tone": "normal"},
            {"anchor": "volatility", "label": "策略波动率", "value": self._percent(self._sample_std(daily_returns)), "tone": "normal"},
            {"anchor": "capital-turnover", "label": "资金换手率", "value": self._percent(capital_turnover), "tone": "normal"},
            {"anchor": "daily-turnover", "label": "日均换手率", "value": self._percent(daily_turnover), "tone": "normal"},
            {"anchor": "cost", "label": "手续费税费", "value": self._money(total_cost), "tone": "normal"},
            {"anchor": "trade-count", "label": "成交次数", "value": str(summary.get("trade_count", len(trades))), "tone": "normal"},
            {"anchor": "order-count", "label": "订单次数", "value": str(summary.get("order_count", 0)), "tone": "normal"},
            {"anchor": "win-days", "label": "上涨/下跌日", "value": f"{positive_days}/{negative_days}", "tone": "normal"},
        ]

    def _equity_svg(self, rows: list[dict]) -> str:
        series = [
            ("strategy", [self._to_float(row.get("cumulative_return")) for row in rows]),
        ]
        excess_values = self._numeric_series(rows, "excess_return")
        if excess_values:
            series.append(("excess", excess_values))
        benchmark_values = self._numeric_series(rows, "benchmark_return")
        if benchmark_values:
            series.append(("benchmark", benchmark_values))
        dates = [str(row.get("date", "")) for row in rows]
        width = 1080
        height = 270
        pad = 42
        return self._multi_line_svg(series, dates, width, height, pad, rows)

    def _multi_line_svg(
        self,
        series: list[tuple[str, list[float]]],
        dates: list[str],
        width: int,
        height: int,
        pad: int,
        rows: list[dict],
    ) -> str:
        all_values = [value for _, values in series for value in values]
        if not all_values:
            return '<div class="empty chart-empty">无收益数据</div>'
        low = min(min(all_values), 0.0)
        high = max(max(all_values), 0.0)
        if high == low:
            high += 0.01
            low -= 0.01
        inner_w = width - pad * 2
        inner_h = height - pad * 2
        point_count = max(len(values) for _, values in series)

        def x(index: int) -> float:
            return pad + (inner_w * index / max(1, point_count - 1))

        def y(value: float) -> float:
            return pad + (high - value) / (high - low) * inner_h

        strategy_values = series[0][1]
        strategy_points = " ".join(f"{x(i):.1f},{y(value):.1f}" for i, value in enumerate(strategy_values))
        area_points = f"{pad},{height - pad} {strategy_points} {width - pad},{height - pad}"
        zero_y = y(0.0)
        grid = "\n".join(
            f'<line x1="{pad}" y1="{pad + inner_h * step / 4:.1f}" x2="{width - pad}" y2="{pad + inner_h * step / 4:.1f}" class="grid" />'
            for step in range(5)
        )
        vertical = "\n".join(
            f'<line x1="{pad + inner_w * step / 6:.1f}" y1="{pad}" x2="{pad + inner_w * step / 6:.1f}" y2="{height - pad}" class="grid" />'
            for step in range(7)
        )
        polylines = "\n".join(
            (
                f'<polyline points="{" ".join(f"{x(i):.1f},{y(value):.1f}" for i, value in enumerate(values))}" '
                f'class="equity-line {escape(name)}" />'
            )
            for name, values in series
        )
        x_positions = [x(index) for index in range(len(dates))]
        tooltip_lines = [self._equity_tooltip_lines(row) for row in rows]
        hit_rects = self._chart_hit_rects(x_positions, pad, height - pad, pad, width - pad, tooltip_lines)
        first_date = escape(dates[0])
        last_date = escape(dates[-1])
        return f"""
<svg class="chart-svg interactive-chart" viewBox="0 0 {width} {height}" role="img" aria-label="策略收益曲线" data-chart-title="策略收益曲线">
  {grid}
  {vertical}
  <line x1="{pad}" y1="{zero_y:.1f}" x2="{width - pad}" y2="{zero_y:.1f}" class="axis-zero" />
  <polygon points="{area_points}" class="equity-area" />
  {polylines}
  <line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height - pad}" class="chart-hover-line" />
  {hit_rects}
  <text x="{pad}" y="{height - 12}" class="axis-label">{first_date}</text>
  <text x="{width - pad - 90}" y="{height - 12}" class="axis-label">{last_date}</text>
  <text x="{width - pad + 8}" y="{y(high):.1f}" class="axis-label">{escape(self._percent(high))}</text>
  <text x="{width - pad + 8}" y="{y(low):.1f}" class="axis-label">{escape(self._percent(low))}</text>
</svg>"""

    def _return_legend(self, rows: list[dict]) -> str:
        items = [("blue", "策略收益")]
        if self._numeric_series(rows, "excess_return"):
            items.append(("orange", "超额收益"))
        if self._numeric_series(rows, "benchmark_return"):
            items.append(("red", "基准收益"))
        return "".join(
            f'<span class="legend {escape(color)}"></span><span>{escape(label)}</span>'
            for color, label in items
        )

    def _numeric_series(self, rows: list[dict], field: str) -> list[float]:
        values = []
        has_value = False
        for row in rows:
            value = row.get(field)
            if value in (None, "", "unsupported_placeholder"):
                values.append(0.0)
                continue
            has_value = True
            values.append(self._to_float(value))
        return values if has_value else []

    def _bar_svg(
        self,
        rows: list[dict],
        field: str,
        positive: str,
        negative: str,
        value_format: str,
    ) -> str:
        values = [self._to_float(row.get(field)) for row in rows]
        if not values:
            return '<div class="empty chart-empty">无数据</div>'
        width = 1080
        height = 155
        pad = 34
        max_abs = max(max(abs(value) for value in values), 0.000001)
        inner_w = width - pad * 2
        mid_y = height / 2
        bar_w = max(3.0, inner_w / max(1, len(values)) * 0.36)
        bars = []
        x_positions = []
        for index, value in enumerate(values):
            x = pad + inner_w * (index + 0.5) / len(values) - bar_w / 2
            x_positions.append(x + bar_w / 2)
            bar_h = abs(value) / max_abs * (height / 2 - 18)
            y = mid_y - bar_h if value >= 0 else mid_y
            color = positive if value >= 0 else negative
            bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" fill="{color}" />')
        upper_label = self._percent(max_abs) if value_format == "percent" else self._compact_money(max_abs)
        lower_label = self._percent(-max_abs) if value_format == "percent" else self._compact_money(-max_abs)
        tooltip_lines = [self._bar_tooltip_lines(row, field, value_format) for row in rows]
        hit_rects = self._chart_hit_rects(x_positions, 8, height - 8, pad, width - pad, tooltip_lines)
        return f"""
<svg class="chart-svg interactive-chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(field)}" data-chart-title="{escape(self._chart_field_label(field))}">
  <line x1="{pad}" y1="{mid_y:.1f}" x2="{width - pad}" y2="{mid_y:.1f}" class="axis-zero" />
  {''.join(bars)}
  <line x1="{pad}" y1="8" x2="{pad}" y2="{height - 8}" class="chart-hover-line" />
  {hit_rects}
  <text x="{width - pad + 8}" y="{mid_y - 8:.1f}" class="axis-label">{escape(upper_label)}</text>
  <text x="{width - pad + 8}" y="{mid_y + 18:.1f}" class="axis-label">{escape(lower_label)}</text>
</svg>"""

    def _chart_hit_rects(
        self,
        x_positions: list[float],
        top: float,
        bottom: float,
        left_bound: float,
        right_bound: float,
        tooltip_lines: list[list[str]],
    ) -> str:
        rects = []
        for index, point_x in enumerate(x_positions):
            left = left_bound if index == 0 else (x_positions[index - 1] + point_x) / 2
            right = right_bound if index == len(x_positions) - 1 else (point_x + x_positions[index + 1]) / 2
            lines = tooltip_lines[index] if index < len(tooltip_lines) else []
            aria_label = "，".join(lines)
            rects.append(
                '<rect class="chart-hit-rect" '
                f'x="{left:.1f}" y="{top:.1f}" width="{max(1.0, right - left):.1f}" height="{max(1.0, bottom - top):.1f}" '
                f'data-chart-point="1" data-x="{point_x:.1f}" data-tooltip-lines="{self._json_attr(lines)}" '
                f'tabindex="0" aria-label="{escape(aria_label, quote=True)}" />'
            )
        return "\n  ".join(rects)

    def _equity_tooltip_lines(self, row: dict) -> list[str]:
        lines = [f"日期：{row.get('date') or '--'}"]
        self._append_percent_line(lines, "当日收益", row.get("daily_return"))
        self._append_percent_line(lines, "策略收益", row.get("cumulative_return"))
        self._append_percent_line(lines, "基准收益", row.get("benchmark_return"))
        self._append_percent_line(lines, "超额收益", row.get("excess_return"))
        self._append_money_line(lines, "总权益", row.get("total_value"))
        self._append_money_line(lines, "持仓市值", row.get("positions_value"))
        return lines

    def _bar_tooltip_lines(self, row: dict, field: str, value_format: str) -> list[str]:
        lines = [f"日期：{row.get('date') or '--'}"]
        label = self._chart_field_label(field)
        value = row.get(field)
        if value_format == "percent":
            self._append_percent_line(lines, label, value)
        else:
            self._append_money_line(lines, label, value)
        if field != "daily_return":
            self._append_percent_line(lines, "当日收益", row.get("daily_return"))
        if field != "positions_value":
            self._append_money_line(lines, "持仓市值", row.get("positions_value"))
        self._append_money_line(lines, "总权益", row.get("total_value"))
        return lines

    def _chart_field_label(self, field: str) -> str:
        labels = {
            "daily_return": "当日收益",
            "positions_value": "持仓市值",
            "cash": "现金",
            "total_value": "总权益",
        }
        return labels.get(field, field)

    def _append_percent_line(self, lines: list[str], label: str, value) -> None:
        if self._has_metric_value(value):
            lines.append(f"{label}：{self._percent(self._to_float(value))}")

    def _append_money_line(self, lines: list[str], label: str, value) -> None:
        if self._has_metric_value(value):
            lines.append(f"{label}：{self._money(self._to_float(value))}")

    def _json_attr(self, value) -> str:
        return escape(json.dumps(value, ensure_ascii=False), quote=True)

    def _group_dicts_by_date(self, rows: list[dict]) -> dict[str, list[dict]]:
        grouped = defaultdict(list)
        for row in rows:
            grouped[str(row.get("date", ""))].append(row)
        for values in grouped.values():
            values.sort(key=lambda item: str(item.get("security", "")))
        return grouped

    def _trade_date_time(self, trade) -> tuple[str, str]:
        value = getattr(trade, "traded_at", None)
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d"), value.strftime("%H:%M:%S")
        text = str(value or "")
        if " " in text:
            date, time_text = text.split(" ", 1)
            return date, time_text[:8]
        return text[:10] or "未记录", ""

    def _daily_position_pnl(self, rows: list[dict]) -> float:
        return sum(self._to_float(row.get("pnl")) for row in rows)

    def _small_stat_cards(self, items: list[tuple[str, str]]) -> str:
        return "\n".join(
            f"""
      <div class="stat-card">
        <div class="metric-label">{escape(label)}</div>
        <div class="stat-value">{escape(value)}</div>
      </div>"""
            for label, value in items
        )

    def _phase_bars(self, phases: list[dict]) -> str:
        if not phases:
            return '<div class="empty">无阶段耗时数据</div>'
        rows = []
        for phase in phases[:8]:
            percent = max(0.0, min(100.0, self._to_float(phase.get("percent"))))
            label = str(phase.get("label") or phase.get("name") or "未知环节")
            rows.append(
                f"""
    <div class="phase-row">
      <div class="phase-label">{escape(label)}</div>
      <div class="phase-meter"><span style="width: {percent:.2f}%"></span></div>
      <div class="phase-time">{escape(self._duration(phase.get('seconds')))}</div>
      <div class="phase-percent">{escape(self._percent_number(phase.get('percent')))}</div>
    </div>"""
            )
        return "".join(rows)

    def _top_phase_label(self, profile: dict) -> str:
        phases = profile.get("phase_timings") or []
        if not phases:
            return "无采样"
        phase = phases[0]
        return f"{phase.get('label') or phase.get('name') or '未知'} {self._percent_number(phase.get('percent'))}"

    def _callback_label(self, row: dict) -> str:
        name = str(row.get("name") or "无")
        seconds = row.get("seconds")
        return name if seconds in (None, "") else f"{name} {self._duration(seconds)}"

    def _api_label(self, row: dict) -> str:
        name = str(row.get("api") or "无")
        count = int(self._to_float(row.get("count")))
        return name if not count else f"{name} {count} 次"

    def _placeholder(self, value) -> str:
        if value in (None, "", "unsupported_placeholder"):
            return "未实现"
        return self._percent(self._to_float(value))

    def _ratio_placeholder(self, value) -> str:
        if value in (None, "", "unsupported_placeholder"):
            return "未实现"
        return f"{self._to_float(value):.3f}"

    def _optional_float(self, value):
        if value in (None, "", "unsupported_placeholder"):
            return None
        return self._to_float(value)

    def _has_metric_value(self, value) -> bool:
        return value not in (None, "", "unsupported_placeholder")

    def _number_class(self, value) -> str:
        number = self._to_float(value)
        if number > 0:
            return "positive"
        if number < 0:
            return "negative"
        return ""

    def _tone(self, value) -> str:
        if value is None:
            return "muted"
        return "positive" if float(value) >= 0 else "negative"

    def _shares(self, value) -> str:
        try:
            return f"{int(value):d}股"
        except (TypeError, ValueError):
            return ""

    def _price(self, value) -> str:
        number = self._to_float(value)
        return f"{number:.2f}".rstrip("0").rstrip(".") if number else "0"

    def _money(self, value) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "0.00"
        return f"{number:,.2f}"

    def _compact_money(self, value) -> str:
        number = float(value)
        sign = "-" if number < 0 else ""
        number = abs(number)
        if number >= 100000000:
            return f"{sign}{number / 100000000:.2f}亿"
        if number >= 10000:
            return f"{sign}{number / 10000:.2f}万"
        return f"{sign}{number:.0f}"

    def _percent(self, value) -> str:
        if value is None:
            return "未实现"
        return f"{float(value) * 100:.2f}%"

    def _to_float(self, value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _sample_std(self, values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        return variance ** 0.5

    def _annualized_sharpe(self, daily_returns: list[float]):
        if len(daily_returns) < 2:
            return None
        volatility = self._sample_std(daily_returns)
        if volatility <= 0:
            return None
        return sum(daily_returns) / len(daily_returns) / volatility * math.sqrt(252)

    def _average_equity(self, rows: list[dict], initial_cash: float, final_value: float) -> float:
        values = [
            self._to_float(row.get("total_value"))
            for row in rows
            if row.get("total_value") not in (None, "")
        ]
        positive_values = [value for value in values if value > 0]
        if positive_values:
            return sum(positive_values) / len(positive_values)
        fallback_values = [value for value in (initial_cash, final_value) if value > 0]
        return sum(fallback_values) / len(fallback_values) if fallback_values else 0.0

    def _duration(self, value) -> str:
        seconds = self._to_float(value)
        if seconds < 1:
            return f"{seconds * 1000:.2f} ms"
        return f"{seconds:.2f} s"

    def _percent_number(self, value) -> str:
        return f"{self._to_float(value):.2f}%"

    def _performance_table_rows(
        self,
        rows: list[dict],
        *,
        columns: list[tuple[str, str]],
        formatters: dict[str, Callable] | None = None,
    ) -> str:
        if not rows:
            return f'<tr><td colspan="{len(columns)}" class="empty">无数据</td></tr>'
        formatters = formatters or {}
        body = []
        for row in rows:
            cells = []
            for key, _label in columns:
                value = row.get(key, "")
                formatter = formatters.get(key)
                text = formatter(value) if formatter else str(value)
                cells.append(f"<td>{escape(text)}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        return "".join(body)

    def _performance_bottleneck(self, profile: dict) -> str:
        phases = profile.get("phase_timings") or []
        if not phases:
            return "当前没有足够的阶段耗时数据，先确认回测是否完整执行。"
        top_phase = phases[0]
        label = str(top_phase.get("label") or top_phase.get("name") or "未知环节")
        seconds = self._duration(top_phase.get("seconds"))
        percent = self._percent_number(top_phase.get("percent"))
        if str(top_phase.get("name")) == "mark_to_market":
            return f"当前最耗时环节是{label}（{seconds}，占 {percent}）。优先检查持仓数量和每日估值读取 close 价的次数。"
        if str(top_phase.get("name")) == "callback":
            return f"当前最耗时环节是{label}（{seconds}，占 {percent}）。优先检查策略回调中的逐股循环和重复因子计算。"
        return f"当前最耗时环节是{label}（{seconds}，占 {percent}）。建议先从该环节的调用次数和数据读取模式入手。"

    def _performance_recommendations(self, profile: dict) -> list[str]:
        recommendations = [
            "先看最慢回调，优先把回调内重复的 get_price/get_fundamentals 调用改为批量读取或日内缓存。",
            "如果数据接口耗时集中在 daily、daily_basic 或 income，优先检查本地缓存命中和查询日期范围是否过宽。",
            "如果每日估值耗时较高，优先减少持仓逐只读取 close 价，改为批量行情读取后一次更新。",
        ]
        slowest_callbacks = profile.get("slowest_callbacks") or []
        if slowest_callbacks:
            name = str(slowest_callbacks[0].get("name") or "")
            recommendations.insert(0, f"当前最慢回调是 {name}，先检查该函数内的筛选、排序和循环。")
        data_calls = profile.get("data_api_calls") or []
        if data_calls:
            api = str(data_calls[0].get("api") or "")
            count = int(self._to_float(data_calls[0].get("count")))
            recommendations.append(f"当前最重数据接口是 {api}，共调用 {count} 次；调用次数高时优先合并请求。")
        return recommendations

    def _navigation_script(self) -> str:
        return """
<script>
(() => {
  const links = Array.from(document.querySelectorAll('.tab-link[href^="#"]'));
  const sections = links
    .map((link) => document.querySelector(link.getAttribute('href')))
    .filter(Boolean);
  const setActive = (id) => {
    links.forEach((link) => {
      const active = link.getAttribute('href') === `#${id}`;
      link.classList.toggle('active', active);
      if (active) {
        link.setAttribute('aria-current', 'page');
      } else {
        link.removeAttribute('aria-current');
      }
    });
  };
  const stickyOffset = () => {
    const topbar = document.querySelector('.topbar')?.getBoundingClientRect().height || 0;
    const tabs = document.querySelector('.report-tabs')?.getBoundingClientRect().height || 0;
    return topbar + tabs + 12;
  };
  const scrollToSection = (target) => {
    const top = window.scrollY + target.getBoundingClientRect().top - stickyOffset();
    window.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
  };
  let ticking = false;
  const syncActiveFromScroll = () => {
    ticking = false;
    const offset = stickyOffset();
    const hashTarget = location.hash ? document.querySelector(location.hash) : null;
    if (hashTarget) {
      const rect = hashTarget.getBoundingClientRect();
      if (rect.top <= window.innerHeight * 0.45 && rect.bottom >= offset) {
        setActive(hashTarget.id);
        return;
      }
    }
    let current = sections[0]?.id;
    for (const section of sections) {
      if (section.getBoundingClientRect().top <= offset) {
        current = section.id;
      }
    }
    if (current) setActive(current);
  };

  links.forEach((link) => {
    link.addEventListener('click', (event) => {
      const target = document.querySelector(link.getAttribute('href'));
      if (!target) return;
      event.preventDefault();
      scrollToSection(target);
      history.replaceState(null, '', link.getAttribute('href'));
      setActive(target.id);
    });
  });

  window.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(syncActiveFromScroll);
  }, { passive: true });
  syncActiveFromScroll();
})();

(() => {
  const tooltip = document.createElement('div');
  tooltip.className = 'chart-tooltip';
  tooltip.setAttribute('role', 'status');
  tooltip.setAttribute('aria-live', 'polite');
  document.body.appendChild(tooltip);

  const hideChartTooltip = () => {
    tooltip.classList.remove('visible');
    document.querySelectorAll('.chart-hover-line').forEach((line) => {
      line.style.display = 'none';
    });
  };

  const tooltipLines = (point) => {
    try {
      return JSON.parse(point.dataset.tooltipLines || '[]');
    } catch (_error) {
      return [];
    }
  };

  const renderTooltip = (lines) => {
    tooltip.replaceChildren();
    if (!lines.length) return;
    const title = document.createElement('div');
    title.className = 'chart-tooltip-title';
    title.textContent = lines[0];
    tooltip.appendChild(title);
    lines.slice(1).forEach((line) => {
      const row = document.createElement('div');
      row.className = 'chart-tooltip-row';
      const splitAt = line.indexOf('：');
      const label = document.createElement('span');
      const value = document.createElement('strong');
      if (splitAt >= 0) {
        label.textContent = line.slice(0, splitAt);
        value.textContent = line.slice(splitAt + 1);
      } else {
        label.textContent = line;
        value.textContent = '';
      }
      row.append(label, value);
      tooltip.appendChild(row);
    });
  };

  const positionTooltip = (clientX, clientY) => {
    const gap = 14;
    tooltip.classList.add('visible');
    const box = tooltip.getBoundingClientRect();
    const left = Math.min(window.innerWidth - box.width - gap, clientX + gap);
    const top = Math.min(window.innerHeight - box.height - gap, clientY + gap);
    tooltip.style.left = `${Math.max(gap, left)}px`;
    tooltip.style.top = `${Math.max(gap, top)}px`;
  };

  const showChartTooltip = (point, clientX, clientY) => {
    const lines = tooltipLines(point);
    if (!lines.length) {
      hideChartTooltip();
      return;
    }
    renderTooltip(lines);
    document.querySelectorAll('.chart-hover-line').forEach((line) => {
      line.style.display = 'none';
    });
    const marker = point.ownerSVGElement?.querySelector('.chart-hover-line');
    if (marker) {
      const x = point.dataset.x || '0';
      marker.setAttribute('x1', x);
      marker.setAttribute('x2', x);
      marker.style.display = 'block';
    }
    positionTooltip(clientX, clientY);
  };

  const pointFromEvent = (event) => {
    const target = event.target;
    return target instanceof Element ? target.closest('[data-chart-point]') : null;
  };

  document.addEventListener('mousemove', (event) => {
    const point = pointFromEvent(event);
    if (point) {
      showChartTooltip(point, event.clientX, event.clientY);
    } else if (!(event.target instanceof Element && event.target.closest('.interactive-chart'))) {
      hideChartTooltip();
    }
  });

  document.addEventListener('focusin', (event) => {
    const point = pointFromEvent(event);
    if (!point) return;
    const rect = point.getBoundingClientRect();
    showChartTooltip(point, rect.left + rect.width / 2, rect.top + rect.height / 2);
  });

  document.addEventListener('focusout', (event) => {
    if (pointFromEvent(event)) hideChartTooltip();
  });

  document.addEventListener('touchstart', (event) => {
    const point = pointFromEvent(event);
    const touch = event.touches[0];
    if (point && touch) {
      showChartTooltip(point, touch.clientX, touch.clientY);
    }
  }, { passive: true });

  window.addEventListener('scroll', hideChartTooltip, { passive: true });
  window.addEventListener('resize', hideChartTooltip);
})();
</script>"""

    def _css(self) -> str:
        return """
:root {
  --blue: #245c9f;
  --blue-soft: #e7f0fa;
  --ink: #1f2933;
  --muted: #6f7a86;
  --subtle: #8b96a3;
  --line: #dfe6ee;
  --grid: #d9dee4;
  --paper: #ffffff;
  --bg: #eef2f6;
  --green: #1f8f55;
  --red: #d44545;
  --orange: #c7792f;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background: var(--bg);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
}
.topbar {
  position: sticky;
  top: 0;
  z-index: 30;
  min-height: 72px;
  display: grid;
  grid-template-columns: minmax(190px, 280px) minmax(0, 1fr) auto;
  gap: 18px;
  align-items: center;
  padding: 12px 24px;
  background: var(--paper);
  border-bottom: 1px solid var(--line);
}
.topbar-main { min-width: 0; }
.report-kicker { color: var(--subtle); font-size: 12px; font-weight: 700; letter-spacing: .02em; }
.report-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #0c2d48; font-size: 18px; font-weight: 800; }
.run-strip {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  overflow: hidden;
  white-space: nowrap;
  color: #4f5c68;
}
.setting { font-weight: 700; color: #394753; }
.divider { width: 1px; height: 18px; flex: 0 0 auto; background: #cbd4dd; }
.status-dot { color: #31a65b; font-size: 18px; vertical-align: -1px; }
.runtime { color: var(--blue); background: var(--blue-soft); padding: 4px 10px; border-radius: 5px; }
.final-value { color: #344250; font-weight: 800; white-space: nowrap; }
.report-tabs {
  position: sticky;
  top: 72px;
  z-index: 25;
  display: flex;
  gap: 4px;
  padding: 8px 24px;
  background: rgba(238, 242, 246, .96);
  border-bottom: 1px solid var(--line);
  backdrop-filter: blur(8px);
}
.tab-link {
  display: inline-flex;
  align-items: center;
  min-height: 34px;
  padding: 0 14px;
  border: 1px solid transparent;
  border-radius: 6px;
  color: #52616f;
  text-decoration: none;
  font-weight: 700;
}
.tab-link:hover { background: #f7f9fb; border-color: #dce3ea; }
.tab-link.active { color: #fff; background: var(--blue); border-color: var(--blue); }
.tab-link:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
.workspace {
  width: min(1240px, calc(100vw - 40px));
  margin: 20px auto 44px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.panel {
  scroll-margin-top: 126px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
  overflow: hidden;
}
.section-heading {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  padding: 22px 24px 16px;
  border-bottom: 1px solid #edf1f5;
}
.section-eyebrow { color: var(--blue); font-size: 12px; font-weight: 800; }
h2 { margin: 3px 0 0; color: #0c2d48; font-size: 20px; line-height: 1.25; }
.section-note { max-width: 520px; color: var(--muted); text-align: right; }
.metric-grid,
.stat-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  padding: 18px 24px;
}
.metric-grid { grid-template-columns: repeat(6, minmax(0, 1fr)); }
.stat-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); border-bottom: 1px solid #edf1f5; }
.metric-card,
.stat-card {
  min-width: 0;
  min-height: 82px;
  padding: 14px 14px 12px;
  border: 1px solid #e4eaf0;
  border-radius: 6px;
  background: #fbfcfd;
}
.metric-card.compact { min-height: 76px; }
.metric-label { color: var(--muted); margin-bottom: 6px; font-size: 13px; }
.metric-value,
.stat-value {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 20px;
  line-height: 1.2;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
.stat-value { font-size: 18px; color: #243241; }
.positive { color: var(--red); }
.negative { color: var(--green); }
.muted { color: #858f99; }
.normal { color: #243241; }
.perf-advice {
  margin: 0;
  padding-left: 18px;
  color: #3d4a56;
}
.perf-advice li { margin: 0 0 6px; }
.chart-toolbar {
  padding: 14px 24px 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  color: #4d5965;
}
.legend { width: 12px; height: 12px; display: inline-block; border-radius: 2px; }
.legend.blue { background: #4778b3; }
.legend.orange { background: #ff9d4d; }
.legend.red { background: #b45149; }
.toolbar-time { margin-left: auto; }
.toolbar-time b { border: 1px solid #cfd6de; padding: 5px 12px; margin: 0 4px; font-weight: 600; background: #fff; }
.chart-block { padding: 0 24px 16px; min-width: 0; }
.chart-block.small { padding: 0 0 10px; }
.mini-chart-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
  padding: 0 24px 20px;
}
.chart-caption { padding: 8px 0 6px; color: #5c6874; font-weight: 800; }
.chart-svg { display: block; width: 100%; height: auto; overflow: visible; }
.interactive-chart { touch-action: manipulation; }
.chart-hit-rect {
  fill: transparent;
  cursor: crosshair;
  pointer-events: all;
}
.chart-hit-rect:focus { outline: none; }
.chart-hover-line {
  display: none;
  stroke: var(--blue);
  stroke-width: 1.4;
  stroke-dasharray: 4 4;
  pointer-events: none;
}
.chart-tooltip {
  position: fixed;
  z-index: 90;
  min-width: 190px;
  max-width: min(280px, calc(100vw - 28px));
  padding: 10px 12px;
  border: 1px solid #cfd9e4;
  border-radius: 6px;
  background: rgba(255, 255, 255, .98);
  box-shadow: 0 10px 28px rgba(20, 35, 50, .16);
  color: #263441;
  pointer-events: none;
  opacity: 0;
  transform: translateY(4px);
  transition: opacity .12s ease, transform .12s ease;
}
.chart-tooltip.visible {
  opacity: 1;
  transform: translateY(0);
}
.chart-tooltip-title {
  margin-bottom: 6px;
  color: #0c2d48;
  font-weight: 800;
}
.chart-tooltip-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 18px;
  min-height: 22px;
  color: #5d6874;
}
.chart-tooltip-row strong {
  color: #22303d;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.grid { stroke: var(--grid); stroke-width: 1; }
.axis-zero { stroke: #30363d; stroke-width: 1.2; }
.equity-area { fill: rgba(71, 120, 179, 0.18); }
.equity-line { fill: none; stroke-width: 2.4; }
.equity-line.strategy { stroke: #4778b3; stroke-width: 3; }
.equity-line.excess { stroke: #ff9d4d; }
.equity-line.benchmark { stroke: #b45149; }
.axis-label { fill: #707b86; font-size: 15px; }
.detail-list { padding: 14px 24px 22px; display: flex; flex-direction: column; gap: 10px; }
.day-group,
.detail-block,
.log-details {
  border: 1px solid #e2e8ef;
  border-radius: 6px;
  background: #fff;
  overflow: hidden;
}
.day-group summary,
.detail-block summary,
.log-details summary {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 16px;
  min-height: 46px;
  padding: 12px 16px;
  color: #32404d;
  background: #f8fafc;
  font-weight: 700;
}
.day-group summary span { color: #657381; font-weight: 600; }
.day-group summary strong { color: #1f2933; font-size: 16px; font-variant-numeric: tabular-nums; }
.detail-action { color: var(--blue) !important; }
.table-wrap { width: 100%; overflow-x: auto; }
.report-table {
  width: 100%;
  min-width: 920px;
  border-collapse: collapse;
  font-size: 14px;
}
#trades .report-table { min-width: 1080px; }
.report-table th {
  background: #f4f6f8;
  color: #65717d;
  font-weight: 800;
  text-align: right;
  border-bottom: 1px solid #e1e7ee;
  padding: 11px 14px;
}
.report-table td {
  border-bottom: 1px solid #e6ebf0;
  padding: 11px 14px;
  text-align: right;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.report-table th:first-child,
.report-table td:first-child,
.report-table th:nth-child(2),
.report-table td:nth-child(2) { text-align: left; }
.total-row td { background: #f7f9fb; font-weight: 800; }
.cash-pill { color: #fff; background: #14a455; padding: 2px 7px; border-radius: 4px; font-weight: 800; }
.empty,
.chart-empty {
  color: #89929b;
  text-align: center;
  padding: 28px;
}
.perf-callout,
.advice-block,
.run-info {
  margin: 0 24px 16px;
  padding: 16px;
  border: 1px solid #e2e8ef;
  border-radius: 6px;
  background: #fbfcfd;
}
.perf-callout p { margin: 6px 0 0; color: #3e4b57; }
.perf-heading,
.run-info-title { color: #0c2d48; font-weight: 800; }
.phase-bars { padding: 0 24px 16px; display: flex; flex-direction: column; gap: 9px; }
.phase-row {
  display: grid;
  grid-template-columns: 150px minmax(120px, 1fr) 88px 70px;
  gap: 12px;
  align-items: center;
  color: #42505d;
}
.phase-label { font-weight: 700; }
.phase-meter { height: 10px; background: #e9eef3; border-radius: 999px; overflow: hidden; }
.phase-meter span { display: block; height: 100%; background: var(--blue); }
.phase-time,
.phase-percent {
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: #5c6874;
}
.detail-block,
.log-details { margin: 0 24px 12px; }
.run-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 18px;
  margin-top: 12px;
}
.run-item {
  display: grid;
  grid-template-columns: 82px minmax(0, 1fr);
  gap: 8px;
  align-items: baseline;
}
.run-item span { color: var(--muted); }
.run-item strong { min-width: 0; overflow-wrap: anywhere; font-weight: 700; }
.log-view {
  margin: 0;
  max-height: 520px;
  padding: 16px;
  overflow: auto;
  background: #101820;
  color: #d7e2ea;
  font: 13px/1.6 SFMono-Regular, Consolas, "Liberation Mono", monospace;
}
@media (max-width: 1080px) {
  .topbar { grid-template-columns: 1fr; gap: 8px; }
  .run-strip { flex-wrap: wrap; white-space: normal; }
  .final-value { display: none; }
  .report-tabs { top: 133px; overflow-x: auto; }
  .panel { scroll-margin-top: 188px; }
  .workspace { width: calc(100vw - 24px); margin-top: 12px; }
  .metric-grid,
  .stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 14px; }
  .section-heading { flex-direction: column; padding: 18px 16px 12px; }
  .section-note { text-align: left; }
  .mini-chart-grid,
  .run-grid { grid-template-columns: 1fr; }
  .detail-list,
  .chart-block,
  .mini-chart-grid,
  .phase-bars { padding-left: 14px; padding-right: 14px; }
  .perf-callout,
  .advice-block,
  .detail-block,
  .log-details,
  .run-info { margin-left: 14px; margin-right: 14px; }
  .phase-row { grid-template-columns: 1fr; gap: 5px; }
  .phase-time,
  .phase-percent { text-align: left; }
  .day-group summary { flex-wrap: wrap; gap: 8px 12px; }
  .toolbar-time { width: 100%; margin-left: 0; }
}
@media (max-width: 560px) {
  .report-tabs { top: 154px; padding: 8px 12px; }
  .metric-grid,
  .stat-grid { grid-template-columns: 1fr; }
  .metric-value,
  .stat-value { font-size: 18px; }
  .report-table { font-size: 13px; }
}
"""
