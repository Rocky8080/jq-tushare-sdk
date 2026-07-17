import csv
import json
from datetime import datetime
from pathlib import Path

from jq_tushare_sdk.reports.html_report import JoinQuantHtmlReport


class JoinQuantOutputFormatter:
    TRANSACTION_FIELDS = [
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
    ]
    ORDER_FIELDS = [
        "datetime",
        "order_id",
        "security",
        "amount",
        "price",
        "status",
        "reason",
    ]
    PERFORMANCE_FIELDS = [
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
    ]

    def write_transactions(self, path: Path, trades: list, security_names: dict[str, str] | None = None):
        names = security_names or {}
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.TRANSACTION_FIELDS)
            writer.writeheader()
            for trade in trades:
                writer.writerow(
                    {
                        "datetime": self._format_dt(getattr(trade, "traded_at", None)),
                        "security": trade.security,
                        "name": names.get(trade.security, ""),
                        "side": trade.side,
                        "amount": trade.amount,
                        "price": f"{trade.price:.4f}",
                        "value": f"{self._signed_trade_value(trade):.2f}",
                        "commission": f"{trade.commission:.2f}",
                        "stamp_tax": f"{trade.stamp_tax:.2f}",
                        "transfer_fee": f"{trade.transfer_fee:.2f}",
                        "dividend_tax": f"{getattr(trade, 'dividend_tax', 0.0):.2f}",
                        "realized_pnl": f"{getattr(trade, 'realized_pnl', 0.0):.2f}",
                        "order_id": trade.order_id,
                        "trade_id": trade.trade_id,
                        "reason": trade.reason,
                    }
                )

    def write_orders(self, path: Path, orders: list, security_names: dict[str, str] | None = None):
        _ = security_names
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.ORDER_FIELDS)
            writer.writeheader()
            for order in orders:
                writer.writerow(
                    {
                        "datetime": self._format_dt(getattr(order, "created_at", None)),
                        "order_id": order.order_id,
                        "security": order.security,
                        "amount": order.amount,
                        "price": f"{order.price:.4f}",
                        "status": order.status,
                        "reason": order.reason,
                    }
                )

    def _signed_trade_value(self, trade) -> float:
        value = float(getattr(trade, "value", 0.0) or 0.0)
        if getattr(trade, "side", "") == "sell" and value > 0:
            return -value
        return value

    def write_performance(self, path: Path, rows: list[dict]):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.PERFORMANCE_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def write_summary(self, path: Path, payload: dict):
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def write_html_report(
        self,
        path: Path,
        *,
        config,
        manifest,
        performance_rows: list[dict],
        summary: dict,
        trades: list,
        position_rows: list[dict],
        log_lines: list[str] | None = None,
        security_names: dict[str, str] | None = None,
    ):
        html = JoinQuantHtmlReport().render(
            config=config,
            manifest=manifest,
            performance_rows=performance_rows,
            summary=summary,
            trades=trades,
            position_rows=position_rows,
            log_lines=log_lines,
            security_names=security_names,
        )
        path.write_text(html + "\n", encoding="utf-8")

    def write_jsonl(self, path: Path, rows: list[dict]):
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def _format_dt(self, value) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value)
