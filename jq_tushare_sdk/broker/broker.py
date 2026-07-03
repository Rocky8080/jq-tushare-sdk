from __future__ import annotations

from copy import deepcopy
from datetime import time
import inspect

from jq_tushare_sdk.broker.costs import CostModel
from jq_tushare_sdk.broker.order import Order, Trade
from jq_tushare_sdk.broker.portfolio import round_lot_amount, trade_lot_size
from jq_tushare_sdk.runtime.context import Position


class Broker:
    _SUPPORTED_STYLE_NAMES = {"MarketOrderStyle", "LimitOrderStyle"}
    _SUPPORTED_ORDER_KWARGS = frozenset()

    def __init__(self, context, data_portal, cost_model: CostModel | None = None):
        self.context = context
        self.data_portal = data_portal
        self.cost_model = cost_model or CostModel()
        self.orders: list[Order] = []
        self.trades: list[Trade] = []
        self.target_portfolio_signals: list[dict] = []
        self._order_seq = 0
        self._trade_seq = 0

    def order(self, security, amount, style=None, **kwargs):
        self._validate_kwargs(kwargs)
        self._validate_style(style)

        requested_amount = int(amount or 0)
        if requested_amount == 0:
            return self._reject(security, 0, self._style_price(style), "zero amount order")

        side = "buy" if requested_amount > 0 else "sell"
        rounded_amount = round_lot_amount(security, requested_amount, side)
        if rounded_amount == 0:
            return self._reject(security, requested_amount, self._style_price(style), "amount below lot size")

        current_state = self._current_state(security)
        price = self._price(security, rounded_amount, style)
        if getattr(current_state, "paused", False):
            return self._reject(security, rounded_amount, price, "security paused")
        if side == "buy" and self._is_locked_limit(current_state, side, price):
            return self._reject(security, rounded_amount, price, "limit-up buy blocked")
        if side == "sell" and self._is_locked_limit(current_state, side, price):
            return self._reject(security, rounded_amount, price, "limit-down sell blocked")

        closeable_amount = self._closeable_amount(security)
        if side == "sell" and abs(rounded_amount) > closeable_amount:
            return self._reject(security, rounded_amount, price, "insufficient position")

        return self._fill(security, rounded_amount, price)

    def order_value(self, security, value, style=None, **kwargs):
        self._validate_kwargs(kwargs)
        self._validate_style(style)
        price = self._price(security, self._value_order_direction(float(value)), style)
        amount = int(float(value) / price) if price > 0 else 0
        if amount > 0:
            amount = self._cash_safe_buy_amount(security, amount, price, float(value))
        return self.order(security, amount, style=style)

    def order_target(self, security, amount, style=None, **kwargs):
        self._validate_kwargs(kwargs)
        self._validate_style(style)
        current_amount = self._total_amount(security)
        target_amount = int(amount or 0)
        if target_amount < 0:
            raise NotImplementedError("Negative target inventory is unsupported")
        delta = target_amount - current_amount
        return self.order(security, delta, style=style)

    def order_target_value(self, security, value, style=None, **kwargs):
        self._validate_kwargs(kwargs)
        self._validate_style(style)
        current_amount = self._total_amount(security)
        buy_price = self._price(security, 1, style)
        sell_price = self._price(security, -1, style)
        buy_target_amount = int(float(value) / buy_price) if buy_price > 0 else 0
        sell_target_amount = int(float(value) / sell_price) if sell_price > 0 else 0

        if buy_target_amount > current_amount:
            price = buy_price
            target_amount = buy_target_amount
            budget = max(0.0, float(value) - current_amount * price)
            target_amount = current_amount + self._cash_safe_buy_amount(
                security,
                target_amount - current_amount,
                price,
                budget,
            )
        elif sell_target_amount < current_amount:
            target_amount = sell_target_amount
        else:
            target_amount = current_amount
        return self.order_target(security, target_amount, style=style)

    def capture_target_portfolio(self, signal: dict):
        self.target_portfolio_signals.append(deepcopy(dict(signal)))

    def _fill(self, security: str, amount: int, price: float):
        side = "buy" if amount > 0 else "sell"
        value = abs(amount) * price
        commission = self.cost_model.commission(value, side)
        stamp_tax = self.cost_model.tax(value, side)
        transfer_fee = self.cost_model.transfer_fee(value, side, security)
        total_cost = commission + stamp_tax + transfer_fee

        if side == "buy" and self.context.portfolio.available_cash < value + total_cost:
            return self._reject(security, amount, price, "insufficient cash")

        order = self._order(
            security=security,
            amount=amount,
            side=side,
            price=price,
            status="filled",
            filled=abs(amount),
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            reason="",
        )
        trade = self._trade(order, side, abs(amount), price, value, commission, stamp_tax, transfer_fee)

        if side == "buy":
            self.context.portfolio.available_cash -= value + total_cost
        else:
            self.context.portfolio.available_cash += value - total_cost

        self._apply_position(security, amount, price)
        self.orders.append(order)
        self.trades.append(trade)
        return order

    def _apply_position(self, security: str, amount: int, price: float):
        positions = self.context.portfolio.positions
        existing = positions.get(security)
        if existing is None:
            existing = Position(code=security)
            positions[security] = existing

        if amount > 0:
            current_cost = existing.avg_cost * existing.total_amount
            new_total = existing.total_amount + amount
            existing.avg_cost = (current_cost + amount * price) / new_total
            existing.total_amount = new_total
            existing.closeable_amount += amount
            existing.price = price
            return

        existing.total_amount += amount
        existing.closeable_amount = max(0, existing.closeable_amount + amount)
        existing.price = price
        if existing.total_amount <= 0:
            positions.pop(security, None)

    def _price(self, security: str, amount: int, style=None) -> float:
        explicit_price = self._style_price(style)
        if explicit_price is not None:
            return explicit_price

        frame = self._portal_call(
            "get_price",
            security,
            count=1,
            end_date=self._as_of_date(),
            fields=["open", "close"],
            panel=False,
        )
        if frame.empty:
            raise NotImplementedError(f"No local price available for {security}")

        price_field = "open" if self._use_open_price() and "open" in frame.columns else "close"
        if price_field not in frame.columns:
            raise NotImplementedError(f"No local {price_field} price available for {security}")

        raw_price = float(frame[price_field].iloc[-1])
        if raw_price <= 0 and price_field == "open" and "close" in frame.columns:
            raw_price = float(frame["close"].iloc[-1])
        if raw_price <= 0:
            raise NotImplementedError(f"Non-positive local {price_field} price for {security}")
        if amount > 0:
            return round(
                raw_price * (1 + self.cost_model.slippage_rate) + self.cost_model.slippage_fixed,
                4,
            )
        return round(
            raw_price * (1 - self.cost_model.slippage_rate) - self.cost_model.slippage_fixed,
            4,
        )

    def _current_state(self, security: str):
        current = self._portal_call(
            "get_current_data",
            [security],
            date=self._as_of_date(),
        )
        return current[security]

    def _as_of_date(self):
        current_dt = getattr(self.context, "current_dt", None)
        if current_dt is None:
            return None
        date_method = getattr(current_dt, "date", None)
        if callable(date_method):
            return date_method()
        return current_dt

    def _use_open_price(self) -> bool:
        current_dt = getattr(self.context, "current_dt", None)
        time_method = getattr(current_dt, "time", None)
        if not callable(time_method):
            return False
        return time_method() <= time(9, 30)

    def _portal_call(self, method_name: str, *args, **kwargs):
        method = getattr(self.data_portal, method_name)
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return method(*args, **kwargs)

        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        filtered_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in signature.parameters or accepts_kwargs
        }
        return method(*args, **filtered_kwargs)

    def _cash_safe_buy_amount(self, security: str, requested_amount: int, price: float, budget: float) -> int:
        if requested_amount <= 0 or price <= 0 or budget <= 0:
            return requested_amount

        desired_amount = round_lot_amount(security, requested_amount, "buy")
        if desired_amount <= 0:
            return requested_amount

        max_budget = min(float(budget), float(self.context.portfolio.available_cash))
        lot = trade_lot_size(security, "buy")
        low = 0
        high = desired_amount // lot
        best = 0

        while low <= high:
            mid = (low + high) // 2
            candidate = mid * lot
            if self._buy_total_outlay(security, candidate, price) <= max_budget:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1

        return best if best > 0 else requested_amount

    def _buy_total_outlay(self, security: str, amount: int, price: float) -> float:
        value = abs(amount) * price
        commission = self.cost_model.commission(value, "buy")
        stamp_tax = self.cost_model.tax(value, "buy")
        transfer_fee = self.cost_model.transfer_fee(value, "buy", security)
        return value + commission + stamp_tax + transfer_fee

    def _is_locked_limit(self, current_state, side: str, chosen_price: float) -> bool:
        market_price = getattr(current_state, "last_price", None)
        if side == "buy":
            high_limit = getattr(current_state, "high_limit", None)
            if high_limit is None:
                return False
            return any(
                price is not None and float(price) >= float(high_limit)
                for price in (chosen_price, market_price)
            )

        low_limit = getattr(current_state, "low_limit", None)
        if low_limit is None:
            return False
        return any(
            price is not None and float(price) <= float(low_limit)
            for price in (chosen_price, market_price)
        )

    def _total_amount(self, security: str) -> int:
        position = self.context.portfolio.positions.get(security)
        if position is None:
            return 0
        return int(position.total_amount or 0)

    def _closeable_amount(self, security: str) -> int:
        position = self.context.portfolio.positions.get(security)
        if position is None:
            return 0
        return int(position.closeable_amount or 0)

    def _reject(self, security: str, amount: int, price: float | None, reason: str):
        side = "buy" if amount > 0 else "sell"
        order = self._order(
            security=security,
            amount=amount,
            side=side,
            price=float(price or 0.0),
            status="rejected",
            filled=0,
            commission=0.0,
            stamp_tax=0.0,
            transfer_fee=0.0,
            reason=reason,
        )
        self.orders.append(order)
        return order

    def _order(
        self,
        security: str,
        amount: int,
        side: str,
        price: float,
        status: str,
        filled: int,
        commission: float,
        stamp_tax: float,
        transfer_fee: float,
        reason: str,
    ):
        self._order_seq += 1
        return Order(
            order_id=f"O{self._order_seq:08d}",
            security=security,
            amount=amount,
            side=side,
            price=float(price),
            status=status,
            filled=filled,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            created_at=getattr(self.context, "current_dt", None),
            reason=reason,
        )

    def _trade(self, order: Order, side: str, amount: int, price: float, value: float, commission: float, stamp_tax: float, transfer_fee: float):
        self._trade_seq += 1
        return Trade(
            trade_id=f"T{self._trade_seq:08d}",
            order_id=order.order_id,
            security=order.security,
            side=side,
            amount=amount,
            price=price,
            value=value,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            traded_at=getattr(self.context, "current_dt", None),
            reason=order.reason,
        )

    def _validate_kwargs(self, kwargs: dict):
        unsupported = sorted(set(kwargs) - self._SUPPORTED_ORDER_KWARGS)
        if unsupported:
            names = ", ".join(unsupported)
            raise NotImplementedError(f"Unsupported broker order kwargs: {names}")

    def _validate_style(self, style) -> None:
        if style is None:
            return
        style_name = style.__class__.__name__
        if style_name not in self._SUPPORTED_STYLE_NAMES:
            raise NotImplementedError(f"Unsupported order style: {style_name}")
        if style_name == "LimitOrderStyle" and not hasattr(style, "price"):
            raise NotImplementedError(f"Order style must expose price: {style_name}")
        if hasattr(style, "price"):
            price = getattr(style, "price")
            if style_name == "LimitOrderStyle" and price is None:
                raise NotImplementedError(f"Order style must expose positive price: {style_name}")
            if price is not None:
                numeric_price = float(price)
                if numeric_price <= 0:
                    raise NotImplementedError(f"Order style price must be positive: {style_name}")

    def _style_price(self, style):
        if style is None:
            return None
        price = getattr(style, "price", None)
        if price is None:
            return None
        return float(price)

    def _value_order_direction(self, value: float) -> int:
        return 1 if value >= 0 else -1
