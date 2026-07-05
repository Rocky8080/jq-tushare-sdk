import unittest
from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from jq_tushare_sdk.broker.broker import Broker
from jq_tushare_sdk.broker.costs import CostModel
from jq_tushare_sdk.runtime.context import Context, Portfolio, Position


class FakePortal:
    def __init__(self, price=10.0, paused=False, high_limit=11.0, low_limit=9.0, last_price=None, open_price=None):
        self.price = price
        self.paused = paused
        self.high_limit = high_limit
        self.low_limit = low_limit
        self.last_price = price if last_price is None else last_price
        self.open_price = price if open_price is None else open_price

    def get_price(self, security, **kwargs):
        fields = kwargs.get("fields")
        if fields is None:
            fields = ["close"]
        elif isinstance(fields, str):
            fields = [fields]
        row = {"time": "2024-01-02", "code": security, "close": self.price, "open": self.open_price}
        return pd.DataFrame([{key: row[key] for key in ["time", "code", *fields] if key in row}])

    def get_current_data(self, securities=None):
        code = securities[0] if isinstance(securities, list) else securities
        return {
            code: SimpleNamespace(
                paused=self.paused,
                is_st=False,
                last_price=self.last_price,
                high_limit=self.high_limit,
                low_limit=self.low_limit,
                day_open=self.open_price,
            )
        }


class DateScopedPortal:
    def __init__(self):
        self.price_end_dates = []
        self.current_data_dates = []

    def get_price(self, security, **kwargs):
        self.price_end_dates.append(kwargs.get("end_date"))
        as_of = kwargs.get("end_date")
        trade_date = "2024-01-02"
        close = 10.0
        if as_of is None or str(as_of)[:10] >= "2024-01-03":
            trade_date = "2024-01-03"
            close = 20.0
        return pd.DataFrame({"time": [trade_date], "code": [security], "close": [close]})

    def get_current_data(self, securities=None, date=None):
        self.current_data_dates.append(date)
        code = securities[0] if isinstance(securities, list) else securities
        last_price = 10.0
        if date is None or str(date)[:10] >= "2024-01-03":
            last_price = 20.0
        return {
            code: SimpleNamespace(
                paused=False,
                is_st=False,
                last_price=last_price,
                high_limit=round(last_price * 1.1, 2),
                low_limit=round(last_price * 0.9, 2),
                day_open=last_price,
            )
        }


class UnsupportedStyle:
    price = 10.0


class LimitOrderStyle:
    def __init__(self, price):
        self.price = price


class MarketOrderStyle:
    pass


class TestBroker(unittest.TestCase):
    def test_order_buy_updates_cash_and_position(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(
            context,
            FakePortal(price=10.0),
            CostModel(
                open_commission=0.0003,
                close_commission=0.0003,
                close_tax=0.001,
                min_commission=5.0,
            ),
        )

        order = broker.order("000001.XSHE", 1000)

        self.assertEqual(order.status, "filled")
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, 1000)
        self.assertAlmostEqual(context.portfolio.available_cash, 89995.0)
        self.assertEqual(len(broker.trades), 1)
        self.assertEqual(broker.trades[0].side, "buy")

    def test_star_lot_rejects_below_200_buy(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(context, FakePortal(price=10.0), CostModel())

        order = broker.order("688001.XSHG", 199)

        self.assertEqual(order.status, "rejected")
        self.assertIn("lot", order.reason)

    def test_order_target_sells_to_target_amount(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(
            context,
            FakePortal(price=10.0),
            CostModel(min_commission=5.0, close_tax=0.001),
        )
        broker.order("000001.XSHE", 1000)

        order = broker.order_target("000001.XSHE", 400)

        self.assertEqual(order.status, "filled")
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, 400)
        self.assertAlmostEqual(context.portfolio.available_cash, 95984.0)
        self.assertAlmostEqual(order.commission, 5.0)
        self.assertAlmostEqual(order.stamp_tax, 6.0)
        self.assertEqual(broker.trades[-1].side, "sell")
        self.assertAlmostEqual(broker.trades[-1].commission, 5.0)
        self.assertAlmostEqual(broker.trades[-1].stamp_tax, 6.0)

    def test_order_target_negative_amount_raises(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(context, FakePortal(price=10.0), CostModel())
        broker.order("000001.XSHE", 600)
        starting_cash = context.portfolio.available_cash
        starting_position = context.portfolio.positions["000001.XSHE"].total_amount
        starting_orders = len(broker.orders)
        starting_trades = len(broker.trades)

        with self.assertRaises(NotImplementedError):
            broker.order_target("000001.XSHE", -500)

        self.assertEqual(context.portfolio.available_cash, starting_cash)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, starting_position)
        self.assertEqual(len(broker.orders), starting_orders)
        self.assertEqual(len(broker.trades), starting_trades)

    def test_positions_iteration_survives_full_liquidation(self):
        context = Context(
            Portfolio(
                0.0,
                positions={
                    "000001.XSHE": Position(
                        code="000001.XSHE",
                        total_amount=100,
                        closeable_amount=100,
                        avg_cost=10.0,
                        price=10.0,
                    ),
                    "000002.XSHE": Position(
                        code="000002.XSHE",
                        total_amount=100,
                        closeable_amount=100,
                        avg_cost=10.0,
                        price=10.0,
                    ),
                },
            )
        )
        broker = Broker(context, FakePortal(price=10.0), CostModel())
        visited = []

        for security in context.portfolio.positions:
            visited.append(security)
            if security == "000001.XSHE":
                broker.order_target(security, 0)

        self.assertEqual(visited, ["000001.XSHE", "000002.XSHE"])
        self.assertNotIn("000001.XSHE", context.portfolio.positions)
        self.assertIn("000002.XSHE", context.portfolio.positions)

    def test_paused_security_rejects_order(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(context, FakePortal(paused=True), CostModel())

        order = broker.order("000001.XSHE", 100)

        self.assertEqual(order.status, "rejected")
        self.assertIn("paused", order.reason)
        self.assertEqual(context.portfolio.available_cash, 100000.0)
        self.assertEqual(context.portfolio.positions, {})
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(len(broker.trades), 0)

    def test_order_value_buys_max_affordable_lot_after_fees(self):
        context = Context(Portfolio(10000.0))
        broker = Broker(context, FakePortal(price=10.0), CostModel(min_commission=5.0))

        order = broker.order_value("000001.XSHE", 10000.0)

        self.assertEqual(order.status, "filled")
        self.assertEqual(order.amount, 900)
        self.assertEqual(order.filled, 900)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, 900)
        self.assertAlmostEqual(context.portfolio.available_cash, 995.0)
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(len(broker.trades), 1)
        self.assertEqual(broker.trades[0].amount, 900)

    def test_order_target_value_buys_max_affordable_lot_after_fees(self):
        context = Context(
            Portfolio(
                5000.0,
                positions={
                    "000001.XSHE": Position(
                        code="000001.XSHE",
                        total_amount=500,
                        closeable_amount=500,
                        avg_cost=10.0,
                        price=10.0,
                    )
                },
            )
        )
        broker = Broker(context, FakePortal(price=10.0), CostModel(min_commission=5.0))

        order = broker.order_target_value("000001.XSHE", 10000.0)

        self.assertEqual(order.status, "filled")
        self.assertEqual(order.amount, 500)
        self.assertEqual(order.filled, 500)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, 1000)
        self.assertAlmostEqual(context.portfolio.available_cash, -5.0)
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(len(broker.trades), 1)
        self.assertEqual(broker.trades[0].amount, 500)

    def test_order_target_value_sizes_buy_with_raw_price_before_slippage(self):
        context = Context(Portfolio(1000000.0))
        broker = Broker(
            context,
            FakePortal(price=1.756),
            CostModel(open_commission=0.0003, min_commission=5.0, slippage_fixed=0.01),
        )

        order = broker.order_target_value("588000.XSHG", 500000.0)

        self.assertEqual(order.status, "filled")
        self.assertEqual(order.amount, 284700)
        self.assertAlmostEqual(order.price, 1.761)
        self.assertAlmostEqual(broker.trades[-1].value, 501356.7)
        self.assertAlmostEqual(order.commission, 150.40701)

    def test_order_target_value_cash_check_uses_raw_price_before_slippage(self):
        context = Context(Portfolio(498492.89))
        broker = Broker(
            context,
            FakePortal(price=3.981),
            CostModel(open_commission=0.0003, min_commission=5.0, slippage_fixed=0.01),
        )

        order = broker.order_target_value("159915.XSHE", 500000.0)

        self.assertEqual(order.status, "filled")
        self.assertEqual(order.amount, 125200)
        self.assertAlmostEqual(order.price, 3.986)
        self.assertAlmostEqual(broker.trades[-1].value, 499047.2)
        self.assertAlmostEqual(order.commission, 149.71416)
        self.assertAlmostEqual(context.portfolio.available_cash, -704.02416)

    def test_order_value_sell_uses_sell_side_slippage_price(self):
        context = Context(
            Portfolio(
                0.0,
                positions={
                    "000001.XSHE": Position(
                        code="000001.XSHE",
                        total_amount=1000,
                        closeable_amount=1000,
                        avg_cost=10.0,
                        price=10.0,
                    )
                },
            )
        )
        broker = Broker(
            context,
            FakePortal(price=10.0, low_limit=8.0),
            CostModel(min_commission=5.0, close_tax=0.001, slippage_rate=0.1),
        )

        order = broker.order_value("000001.XSHE", -1000.0)

        self.assertEqual(order.status, "filled")
        self.assertEqual(order.amount, -100)
        self.assertEqual(order.filled, 100)
        self.assertAlmostEqual(order.price, 9.0)
        self.assertAlmostEqual(context.portfolio.available_cash, 894.1)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, 900)
        self.assertAlmostEqual(order.commission, 5.0)
        self.assertAlmostEqual(order.stamp_tax, 0.9)

    def test_order_target_value_sell_uses_sell_side_slippage_price(self):
        context = Context(
            Portfolio(
                0.0,
                positions={
                    "000001.XSHE": Position(
                        code="000001.XSHE",
                        total_amount=500,
                        closeable_amount=500,
                        avg_cost=10.0,
                        price=10.0,
                    )
                },
            )
        )
        broker = Broker(
            context,
            FakePortal(price=10.0, low_limit=8.0),
            CostModel(min_commission=5.0, close_tax=0.001, slippage_rate=0.1),
        )

        order = broker.order_target_value("000001.XSHE", 2000.0)

        self.assertEqual(order.status, "filled")
        self.assertEqual(order.amount, -300)
        self.assertEqual(order.filled, 300)
        self.assertAlmostEqual(order.price, 9.0)
        self.assertAlmostEqual(context.portfolio.available_cash, 2692.3)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, 200)
        self.assertAlmostEqual(order.commission, 5.0)
        self.assertAlmostEqual(order.stamp_tax, 2.7)

    def test_order_target_uses_total_position_but_rejects_frozen_sell_amount(self):
        context = Context(
            Portfolio(
                100000.0,
                positions={
                    "000001.XSHE": Position(
                        code="000001.XSHE",
                        total_amount=500,
                        closeable_amount=0,
                        avg_cost=10.0,
                        price=10.0,
                    )
                },
            )
        )
        broker = Broker(context, FakePortal(price=10.0), CostModel())

        order = broker.order_target("000001.XSHE", 400)

        self.assertEqual(order.status, "rejected")
        self.assertEqual(order.amount, -100)
        self.assertIn("insufficient position", order.reason)
        self.assertEqual(context.portfolio.available_cash, 100000.0)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, 500)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].closeable_amount, 0)
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(len(broker.trades), 0)

    def test_order_target_value_does_not_increase_holdings_when_slippage_flips_target_amount(self):
        context = Context(
            Portfolio(
                0.0,
                positions={
                    "000001.XSHE": Position(
                        code="000001.XSHE",
                        total_amount=500,
                        closeable_amount=500,
                        avg_cost=10.0,
                        price=10.0,
                    )
                },
            )
        )
        broker = Broker(
            context,
            FakePortal(price=10.0, low_limit=8.0),
            CostModel(min_commission=5.0, close_tax=0.001, slippage_rate=0.1),
        )

        order = broker.order_target_value("000001.XSHE", 4600.0)

        self.assertEqual(order.status, "rejected")
        self.assertEqual(order.amount, -40)
        self.assertIn("amount below lot", order.reason)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, 500)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].closeable_amount, 500)
        self.assertEqual(context.portfolio.available_cash, 0.0)
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(len(broker.trades), 0)

    def test_limit_up_buy_rejects_when_market_locked_even_if_limit_price_is_lower(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(
            context,
            FakePortal(price=10.0, high_limit=10.0, low_limit=9.0, last_price=10.0),
            CostModel(),
        )

        order = broker.order("000001.XSHE", 100, style=LimitOrderStyle(9.8))

        self.assertEqual(order.status, "rejected")
        self.assertIn("limit-up", order.reason)
        self.assertEqual(context.portfolio.available_cash, 100000.0)
        self.assertEqual(context.portfolio.positions, {})
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(len(broker.trades), 0)

    def test_limit_down_sell_rejects_when_market_locked_even_if_limit_price_is_higher(self):
        context = Context(
            Portfolio(
                100000.0,
                positions={
                    "000001.XSHE": Position(
                        code="000001.XSHE",
                        total_amount=300,
                        closeable_amount=300,
                        avg_cost=10.0,
                        price=10.0,
                    )
                },
            )
        )
        broker = Broker(
            context,
            FakePortal(price=9.0, high_limit=11.0, low_limit=9.0, last_price=9.0),
            CostModel(),
        )

        order = broker.order("000001.XSHE", -100, style=LimitOrderStyle(9.2))

        self.assertEqual(order.status, "rejected")
        self.assertIn("limit-down", order.reason)
        self.assertEqual(context.portfolio.available_cash, 100000.0)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, 300)
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(len(broker.trades), 0)

    def test_insufficient_cash_rejection_does_not_mutate_portfolio_or_add_trade(self):
        context = Context(Portfolio(10000.0))
        broker = Broker(context, FakePortal(price=10.0), CostModel(min_commission=5.0))

        order = broker.order("000001.XSHE", 1000)

        self.assertEqual(order.status, "rejected")
        self.assertIn("insufficient cash", order.reason)
        self.assertEqual(context.portfolio.available_cash, 10000.0)
        self.assertEqual(context.portfolio.positions, {})
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(len(broker.trades), 0)

    def test_unsupported_kwargs_raise(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(context, FakePortal(price=10.0), CostModel())

        with self.assertRaises(NotImplementedError):
            broker.order("000001.XSHE", 100, pindex=0)

    def test_unsupported_style_raises(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(context, FakePortal(price=10.0), CostModel())

        with self.assertRaises(NotImplementedError):
            broker.order("000001.XSHE", 100, style=UnsupportedStyle())

    def test_limit_order_style_requires_non_none_price(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(context, FakePortal(price=10.0), CostModel())

        with self.assertRaises(NotImplementedError):
            broker.order("000001.XSHE", 100, style=LimitOrderStyle(None))

    def test_limit_order_style_requires_positive_price(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(context, FakePortal(price=10.0), CostModel())

        with self.assertRaises(NotImplementedError):
            broker.order("000001.XSHE", 100, style=LimitOrderStyle(0))

    def test_market_order_style_without_price_uses_local_slippage_pricing(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(
            context,
            FakePortal(price=10.0),
            CostModel(min_commission=5.0, slippage_rate=0.02),
        )

        order = broker.order("000001.XSHE", 100, style=MarketOrderStyle())

        self.assertEqual(order.status, "filled")
        self.assertEqual(order.amount, 100)
        self.assertAlmostEqual(order.price, 10.2)
        self.assertAlmostEqual(context.portfolio.available_cash, 98975.0)
        self.assertEqual(context.portfolio.positions["000001.XSHE"].total_amount, 100)

    def test_fixed_slippage_uses_half_spread_for_each_side(self):
        context = Context(Portfolio(100000.0))
        context.portfolio.positions["000001.XSHE"] = Position(
            code="000001.XSHE",
            total_amount=100,
            closeable_amount=100,
            avg_cost=10.0,
            price=10.0,
        )
        broker = Broker(
            context,
            FakePortal(price=10.0),
            CostModel(min_commission=5.0, slippage_fixed=0.02),
        )

        buy_order = broker.order("000001.XSHE", 100)
        sell_order = broker.order("000001.XSHE", -100)

        self.assertAlmostEqual(buy_order.price, 10.01)
        self.assertAlmostEqual(sell_order.price, 9.99)

    def test_open_callback_market_order_uses_open_price_with_slippage(self):
        context = Context(Portfolio(100000.0), current_dt=datetime(2024, 1, 2, 9, 30))
        broker = Broker(
            context,
            FakePortal(price=10.0, open_price=9.0, high_limit=12.0, low_limit=8.0),
            CostModel(min_commission=5.0, slippage_rate=0.01),
        )

        order = broker.order("000001.XSHE", 100, style=MarketOrderStyle())

        self.assertEqual(order.status, "filled")
        self.assertAlmostEqual(order.price, 9.09)

    def test_capture_target_portfolio_copies_signal(self):
        context = Context(Portfolio(100000.0))
        broker = Broker(context, FakePortal(price=10.0), CostModel())
        signal = {"type": "target_portfolio", "positions": [{"stock_code": "000001.XSHE", "volume": 1000}]}

        broker.capture_target_portfolio(signal)
        signal["positions"].append({"stock_code": "000002.XSHE", "volume": 500})

        self.assertEqual(len(broker.target_portfolio_signals), 1)
        self.assertEqual(len(broker.target_portfolio_signals[0]["positions"]), 1)

    def test_order_uses_context_date_for_price_and_current_data(self):
        context = Context(Portfolio(100000.0), current_dt=datetime(2024, 1, 2, 9, 30))
        portal = DateScopedPortal()
        broker = Broker(context, portal, CostModel(min_commission=5.0))

        order = broker.order("000001.XSHE", 100)

        self.assertEqual(order.status, "filled")
        self.assertAlmostEqual(order.price, 10.0)
        self.assertAlmostEqual(context.portfolio.available_cash, 98995.0)
        self.assertEqual([str(value)[:10] for value in portal.price_end_dates], ["2024-01-02"])
        self.assertEqual([str(value)[:10] for value in portal.current_data_dates], ["2024-01-02"])


if __name__ == "__main__":
    unittest.main()
