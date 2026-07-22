from __future__ import annotations

import calendar
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time
import inspect

import numpy as np

from jq_tushare_sdk.broker.costs import CostModel
from jq_tushare_sdk.broker.order import Order, Trade
from jq_tushare_sdk.broker.portfolio import round_lot_amount, trade_lot_size
from jq_tushare_sdk.data.code_map import is_tushare_fund_code
from jq_tushare_sdk.runtime.context import Position


@dataclass
class _DividendTaxLot:
    lot_id: int
    remaining_amount: float
    acquired_at: datetime | None = None
    pending_tax: float = 0.0


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
        self._tax_lot_seq = 0
        self._dividend_tax_lots: dict[str, list[_DividendTaxLot]] = {}

    def capture_dividend_lots(self, security: str, amount: float) -> list[dict]:
        remaining = max(0.0, float(amount or 0.0))
        allocations = []
        for lot in self._dividend_tax_lots.get(str(security), []):
            if remaining <= 0:
                break
            allocated = min(remaining, float(lot.remaining_amount or 0.0))
            if allocated <= 0:
                continue
            allocations.append({"lot_id": lot.lot_id, "amount": allocated})
            remaining -= allocated
        return allocations

    def attach_dividend_tax(
        self,
        security: str,
        allocations: list[dict],
        *,
        cash_per_share: float,
        tax_rate: float = 0.20,
    ) -> float:
        lots = {lot.lot_id: lot for lot in self._dividend_tax_lots.get(str(security), [])}
        attached = 0.0
        for allocation in allocations:
            lot = lots.get(allocation.get("lot_id"))
            amount = max(0.0, float(allocation.get("amount") or 0.0))
            if lot is None or amount <= 0:
                continue
            tax = amount * max(0.0, float(cash_per_share or 0.0)) * max(0.0, float(tax_rate))
            lot.pending_tax += tax
            attached += tax
        return attached

    def order(
        self,
        security,
        amount,
        style=None,
        *,
        cash_check_price: float | None = None,
        cash_check_includes_costs: bool = True,
        **kwargs,
    ):
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

        return self._fill(
            security,
            rounded_amount,
            price,
            cash_check_price=cash_check_price,
            cash_check_includes_costs=cash_check_includes_costs,
        )

    def order_value(self, security, value, style=None, **kwargs):
        self._validate_kwargs(kwargs)
        self._validate_style(style)
        price = self._price(security, self._value_order_direction(float(value)), style)
        amount = int(float(value) / price) if price > 0 else 0
        if amount > 0:
            amount = self._cash_safe_buy_amount(security, amount, price, float(value))
        return self.order(security, amount, style=style)

    def order_target(
        self,
        security,
        amount,
        style=None,
        *,
        cash_check_price: float | None = None,
        cash_check_includes_costs: bool = True,
        **kwargs,
    ):
        self._validate_kwargs(kwargs)
        self._validate_style(style)
        current_amount = self._total_amount(security)
        target_amount = int(amount or 0)
        if target_amount < 0:
            raise NotImplementedError("Negative target inventory is unsupported")
        delta = target_amount - current_amount
        return self.order(
            security,
            delta,
            style=style,
            cash_check_price=cash_check_price,
            cash_check_includes_costs=cash_check_includes_costs,
        )

    def order_target_value(self, security, value, style=None, **kwargs):
        self._validate_kwargs(kwargs)
        self._validate_style(style)
        current_amount = self._total_amount(security)
        reference_price = self._reference_price(security, style)
        buy_target_amount = int(float(value) / reference_price) if reference_price > 0 else 0
        sell_target_amount = int(float(value) / reference_price) if reference_price > 0 else 0

        if buy_target_amount > current_amount:
            requested_delta = buy_target_amount - current_amount
            execution_price = self._price(security, requested_delta, style)
            safe_delta = self._cash_safe_buy_amount(
                security,
                requested_delta,
                execution_price,
                self.context.portfolio.available_cash,
            )
            target_amount = current_amount + safe_delta
            return self.order_target(security, target_amount, style=style)
        elif sell_target_amount < current_amount:
            target_amount = sell_target_amount
        else:
            target_amount = current_amount
        return self.order_target(security, target_amount, style=style)

    def capture_target_portfolio(self, signal: dict):
        self.target_portfolio_signals.append(deepcopy(dict(signal)))

    def _fill(
        self,
        security: str,
        amount: int,
        price: float,
        *,
        cash_check_price: float | None = None,
        cash_check_includes_costs: bool = True,
    ):
        side = "buy" if amount > 0 else "sell"
        value = abs(amount) * price
        commission = self.cost_model.commission(value, side)
        stamp_tax = self.cost_model.tax(value, side)
        transfer_fee = self.cost_model.transfer_fee(value, side, security)
        dividend_tax = self._consume_dividend_tax_lots(security, abs(amount)) if side == "sell" else 0.0
        total_cost = commission + stamp_tax + transfer_fee + dividend_tax

        if side == "buy" and self.context.portfolio.available_cash < self._cash_check_outlay(
            security,
            amount,
            cash_check_price if cash_check_price is not None else price,
            include_costs=cash_check_includes_costs,
        ):
            return self._reject(security, amount, price, "insufficient cash")
        realized_pnl = self._realized_pnl(security, amount, price)

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
            dividend_tax=dividend_tax,
            reason="",
        )
        trade = self._trade(
            order,
            side,
            abs(amount),
            price,
            value,
            commission,
            stamp_tax,
            transfer_fee,
            realized_pnl,
            dividend_tax,
        )

        if side == "buy":
            self.context.portfolio.available_cash -= value + total_cost
        else:
            self.context.portfolio.available_cash += value - total_cost

        self._apply_position(security, amount, price)
        if amount > 0:
            self._add_dividend_tax_lot(security, amount)
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

    def _add_dividend_tax_lot(self, security: str, amount: float) -> None:
        if is_tushare_fund_code(security) or amount <= 0:
            return
        self._tax_lot_seq += 1
        self._dividend_tax_lots.setdefault(str(security), []).append(
            _DividendTaxLot(
                lot_id=self._tax_lot_seq,
                remaining_amount=float(amount),
                acquired_at=getattr(self.context, "current_dt", None),
            )
        )

    def _consume_dividend_tax_lots(self, security: str, amount: float) -> float:
        remaining = max(0.0, float(amount or 0.0))
        lots = self._dividend_tax_lots.get(str(security), [])
        adjustment = 0.0
        for lot in lots:
            if remaining <= 0:
                break
            consumed = min(remaining, float(lot.remaining_amount or 0.0))
            if consumed <= 0:
                continue
            if lot.pending_tax > 0 and lot.remaining_amount > 0:
                withheld_tax = lot.pending_tax * consumed / lot.remaining_amount
                final_tax = withheld_tax * self._dividend_tax_rate(lot.acquired_at) / 0.20
                adjustment += final_tax - withheld_tax
                lot.pending_tax -= withheld_tax
            lot.remaining_amount -= consumed
            remaining -= consumed
        self._dividend_tax_lots[str(security)] = [
            lot for lot in lots if lot.remaining_amount > 0
        ]
        return adjustment

    def _dividend_tax_rate(self, acquired_at: datetime | None) -> float:
        acquired_on = self._as_date(acquired_at)
        sold_on = self._as_date(getattr(self.context, "current_dt", None))
        if acquired_on is None or sold_on is None:
            return 0.20
        if sold_on <= self._add_calendar_months(acquired_on, 1):
            return 0.20
        if sold_on <= self._add_calendar_months(acquired_on, 12):
            return 0.10
        return 0.0

    @staticmethod
    def _as_date(value) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return None

    @staticmethod
    def _add_calendar_months(value: date, months: int) -> date:
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def _price(self, security: str, amount: int, style=None) -> float:
        explicit_price = self._style_price(style)
        if explicit_price is not None:
            return explicit_price

        raw_price = self._raw_price(security)
        return self._slipped_price(raw_price, amount, security)

    def _reference_price(self, security: str, style=None) -> float:
        explicit_price = self._style_price(style)
        if explicit_price is not None:
            return explicit_price
        return self._raw_price(security)

    def _raw_price(self, security: str) -> float:
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
        return raw_price

    def _slipped_price(self, raw_price: float, amount: int, security: str) -> float:
        fixed_slip = self.cost_model.slippage_fixed / 2.0
        if amount > 0:
            price = raw_price * (1 + self.cost_model.slippage_rate) + fixed_slip
        else:
            price = raw_price * (1 - self.cost_model.slippage_rate) - fixed_slip
        decimals = 3 if is_tushare_fund_code(security) else 2
        return float(np.round(price, decimals))

    def _current_state(self, security: str):
        current = self._portal_call(
            "get_current_data",
            [security],
            date=getattr(self.context, "current_dt", None),
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

    def _cash_safe_buy_amount(
        self,
        security: str,
        requested_amount: int,
        price: float,
        budget: float,
        *,
        include_costs: bool = True,
    ) -> int:
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
            if self._cash_check_outlay(security, candidate, price, include_costs=include_costs) <= max_budget:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1

        return best if best > 0 else requested_amount

    def _realized_pnl(self, security: str, amount: int, price: float) -> float:
        if amount >= 0:
            return 0.0
        position = self.context.portfolio.positions.get(security)
        if position is None:
            return 0.0
        return (float(price) - float(position.avg_cost or 0.0)) * abs(int(amount))

    def _cash_check_outlay(self, security: str, amount: int, price: float, *, include_costs: bool = True) -> float:
        if include_costs:
            return self._buy_total_outlay(security, amount, price)
        return abs(amount) * price

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
            dividend_tax=0.0,
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
        dividend_tax: float,
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
            dividend_tax=dividend_tax,
            created_at=getattr(self.context, "current_dt", None),
            reason=reason,
        )

    def _trade(
        self,
        order: Order,
        side: str,
        amount: int,
        price: float,
        value: float,
        commission: float,
        stamp_tax: float,
        transfer_fee: float,
        realized_pnl: float = 0.0,
        dividend_tax: float = 0.0,
    ):
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
            dividend_tax=dividend_tax,
            traded_at=getattr(self.context, "current_dt", None),
            reason=order.reason,
            realized_pnl=realized_pnl,
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
