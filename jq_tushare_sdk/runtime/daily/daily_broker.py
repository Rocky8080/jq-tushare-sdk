"""Queue close decisions and execute them once at the next session's open."""
from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime

from jq_tushare_sdk.broker.broker import Broker
from jq_tushare_sdk.broker.costs import CostModel
from jq_tushare_sdk.data.code_map import is_tushare_a_share_code, is_tushare_fund_code, to_joinquant_code


class _UnmarketableLimit(ValueError):
    pass


class LimitOrderStyle:
    def __init__(self, price):
        self.price = price


class MarketOrderStyle:
    def __init__(self, price=None):
        self.price = price


_PERSISTENT_FIELDS = ("_settlement_date", "_order_seq", "_trade_seq", "_tax_lot_seq",
                      "_dividend_tax_lots", "_closed_dividend_tax_lots", "_dividend_tax_sales")


class DailySimulationBroker(Broker):
    def __init__(self, context, data_portal, revision_id, saved=None):
        super().__init__(context, data_portal, CostModel())
        self.revision_id = revision_id
        self.phase = "loading"
        self.pending = []
        self._active_intent = None
        if saved:
            for key in _PERSISTENT_FIELDS:
                setattr(self, key, saved[key])
            self.cost_model = CostModel(**saved["cost_model"])
            self.pending = list(saved["pending"])

    def checkpoint(self):
        self._validate_cost_model()
        return {**{key: getattr(self, key) for key in _PERSISTENT_FIELDS},
                "cost_model": asdict(self.cost_model), "pending": self.pending}

    def _validate_cost_model(self):
        if any(not math.isfinite(float(value)) or float(value) < 0 for value in asdict(self.cost_model).values()):
            raise ValueError("Simulation fees and slippage must be finite and nonnegative")

    def _enqueue(self, kind, security, value, style, kwargs):
        if self.phase != "decision":
            raise RuntimeError("Orders are only accepted during the close decision phase")
        self._validate_kwargs(kwargs)
        self._validate_style(style)
        self._validate_cost_model()
        code = to_joinquant_code(security)
        if not (is_tushare_a_share_code(code) or is_tushare_fund_code(code)):
            raise NotImplementedError("Daily simulation orders support A-share stocks and supported funds only")
        numeric = float(value)
        if not math.isfinite(numeric) or (kind.startswith("order_target") and numeric < 0):
            raise ValueError("Order quantity/value must be finite and targets nonnegative")
        if kind in ("order", "order_target") and numeric != int(numeric):
            raise ValueError("Stock order quantity must be an integer")
        style_price = getattr(style, "price", None) if style is not None else None
        if style_price is not None and (not math.isfinite(float(style_price)) or float(style_price) <= 0):
            raise ValueError("Order style price must be finite and positive")
        price = self.data_portal.exact_bar(code)["close"]
        current = self._total_amount(code)
        estimate = int(numeric) if kind in ("order", "order_target") else int(numeric / price)
        if kind.startswith("order_target"):
            estimate -= current
        order = super()._order(security=code, amount=estimate, side="buy" if estimate > 0 else "sell",
                               price=0., status="pending", filled=0, commission=0., stamp_tax=0.,
                               transfer_fee=0., dividend_tax=0., reason="queued for next trading-day open")
        intent = {"order_id": order.order_id, "security": code, "kind": kind, "requested_value": numeric,
                  "style": None if style is None else {"name": type(style).__name__, "price": style_price},
                  "signal_date": self.context.current_dt.date().isoformat(),
                  "signal_at": self.context.current_dt.isoformat(), "revision_id": self.revision_id,
                  "cost_model": asdict(self.cost_model)}
        self.pending.append(intent)
        self._decorate(order, intent, execution_date=None)
        self.orders.append(order)
        return order

    def order(self, security, amount, style=None, **kwargs):
        if self._active_intent is not None:
            return super().order(security, amount, style, **kwargs)
        return self._enqueue("order", security, amount, style, kwargs)

    def order_value(self, security, value, style=None, **kwargs):
        if self._active_intent is not None:
            return super().order_value(security, value, style, **kwargs)
        return self._enqueue("order_value", security, value, style, kwargs)

    def order_target(self, security, amount, style=None, **kwargs):
        if self._active_intent is not None:
            return super().order_target(security, amount, style, **kwargs)
        return self._enqueue("order_target", security, amount, style, kwargs)

    def order_target_value(self, security, value, style=None, **kwargs):
        if self._active_intent is not None:
            return super().order_target_value(security, value, style, **kwargs)
        return self._enqueue("order_target_value", security, value, style, kwargs)

    def _raw_price(self, security):
        row = self.data_portal.exact_bar(security)
        return row["open"] if self._active_intent is not None else row["close"]

    def _reference_price(self, security, style=None):
        return self._raw_price(security)

    def _price(self, security, amount, style=None):
        price = self._slipped_price(self._raw_price(security), amount, security)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Configured slippage produced a non-positive or non-finite execution price")
        limit = self._style_price(style)
        if limit is not None and ((amount > 0 and price > limit) or (amount < 0 and price < limit)):
            raise _UnmarketableLimit("next-open execution price violates limit")
        return price

    def _order(self, **kwargs):
        order = super()._order(**kwargs)
        if self._active_intent:
            order.order_id = self._active_intent["order_id"]
            order.created_at = datetime.fromisoformat(self._active_intent["signal_at"])
            self._decorate(order, self._active_intent, self.context.current_dt.date().isoformat())
        return order

    @staticmethod
    def _decorate(order, intent, execution_date):
        order.signal_date = intent["signal_date"]
        order.revision_id = intent["revision_id"]
        order.execution_date = execution_date
        order.kind = intent["kind"]
        order.requested_value = intent["requested_value"]
        order.limit_price = (intent.get("style") or {}).get("price")

    def execute_pending(self, trade_day):
        self.phase = "execution"
        pending, self.pending = self.pending, []
        original_cost = self.cost_model
        try:
            for intent in pending:
                if intent["signal_date"] >= trade_day:
                    raise ValueError("Pending intent must originate before its execution day")
                self._active_intent = intent
                self.cost_model = CostModel(**intent["cost_model"])
                self._validate_cost_model()
                code, value = intent["security"], intent["requested_value"]
                bar = self.data_portal.exact_bar(code)
                style_record = intent.get("style")
                style = None if style_record is None else (LimitOrderStyle if style_record["name"] == "LimitOrderStyle" else MarketOrderStyle)(style_record["price"])
                amount = int(value) if intent["kind"] in ("order", "order_target") else int(value / bar["open"]) if bar["open"] > 0 else 0
                if intent["kind"].startswith("order_target"):
                    amount -= self._total_amount(code)
                if bar["vol"] <= 0:
                    order = self._reject(code, amount, bar["open"], "security paused; next-open intent expired")
                else:
                    try:
                        order = getattr(self, intent["kind"])(code, value, style=style)
                    except _UnmarketableLimit:
                        order = self._reject(code, amount, bar["open"], "next-open price violates limit; intent expired")
                for trade in self.trades:
                    if trade.order_id == order.order_id:
                        trade.signal_date = intent["signal_date"]
                        trade.revision_id = intent["revision_id"]
                        trade.execution_date = trade_day
        finally:
            self._active_intent = None
            self.cost_model = original_cost
            self.phase = "blocked"

    @staticmethod
    def record(value):
        result = asdict(value)
        result.update({key: item for key, item in vars(value).items() if key not in result})
        for key, item in list(result.items()):
            if isinstance(item, datetime):
                result[key] = item.isoformat()
        return result
