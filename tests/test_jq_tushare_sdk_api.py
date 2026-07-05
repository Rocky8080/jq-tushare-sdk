import unittest
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd

from jq_tushare_sdk.broker.broker import Broker
from jq_tushare_sdk.broker.costs import CostModel
from jq_tushare_sdk.api.finance_tables import income, valuation
from jq_tushare_sdk.api.globals import exported_globals, set_runtime_state
from jq_tushare_sdk.api.jqdata import (
    attribute_history,
    get_fundamentals_continuously,
    get_fundamentals,
    get_current_data,
    history,
    get_index_stocks,
    get_industry,
    get_price,
    get_security_info,
    get_trade_days,
    runtime_state,
)
from jq_tushare_sdk.api.query import query
from jq_tushare_sdk.data.portal import DataPortal
from jq_tushare_sdk.runtime.context import Context, Portfolio
from jq_tushare_sdk.runtime.globals_state import RuntimeState


class FakePortal:
    def get_price(self, security, **kwargs):
        return pd.DataFrame({"time": ["2024-01-02"], "code": [security], "close": [10.5]})

    def get_trade_days(self, start_date, end_date):
        return ["2024-01-02"]

    def get_index_stocks(self, index_symbol, date=None):
        return ["000001.XSHE", "600000.XSHG"]

    def get_fundamentals(self, q, date=None, statDate=None):
        return pd.DataFrame(
            [
                {"code": "000001.XSHE", "market_cap": 50.0, "np_parent_company_owners": 100.0},
                {"code": "600000.XSHG", "market_cap": 90.0, "np_parent_company_owners": 200.0},
            ]
        )

    def get_current_data(self, securities=None):
        return {"000001.XSHE": SimpleNamespace(is_st=False, paused=False, last_price=10.5, high_limit=11.55, low_limit=9.45)}

    def get_all_securities(self, types=None, date=None):
        return pd.DataFrame({"display_name": ["平安银行"]}, index=["000001.XSHE"])


class APIPortalBackend:
    def __init__(self):
        self.frames = {
            "daily": pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20240102",
                        "open": 10.0,
                        "high": 11.0,
                        "low": 9.5,
                        "close": 10.5,
                        "vol": 1000.0,
                        "amount": 10500.0,
                    },
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20240103",
                        "open": 10.6,
                        "high": 10.8,
                        "low": 10.1,
                        "close": 10.2,
                        "vol": 900.0,
                        "amount": 9300.0,
                    },
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20240105",
                        "open": 11.0,
                        "high": 11.1,
                        "low": 10.9,
                        "close": 10.9,
                        "vol": 950.0,
                        "amount": 10355.0,
                    },
                    {
                        "ts_code": "600000.SH",
                        "trade_date": "20240103",
                        "open": 20.5,
                        "high": 21.0,
                        "low": 20.2,
                        "close": 20.8,
                        "vol": 1800.0,
                        "amount": 37440.0,
                    },
                ]
            ),
            "daily_basic": pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "trade_date": "20240102", "total_mv": 45.0, "turnover_rate": 1.2},
                    {"ts_code": "000001.SZ", "trade_date": "20240103", "total_mv": 50.0, "turnover_rate": 1.5},
                    {"ts_code": "000001.SZ", "trade_date": "20240105", "total_mv": 52.0, "turnover_rate": 2.1},
                    {"ts_code": "600000.SH", "trade_date": "20240103", "total_mv": 90.0, "turnover_rate": 3.2},
                ]
            ),
            "trade_cal": pd.DataFrame(
                [
                    {"exchange": "SSE", "cal_date": "20240102", "is_open": 1},
                    {"exchange": "SSE", "cal_date": "20240103", "is_open": 1},
                    {"exchange": "SSE", "cal_date": "20240104", "is_open": 1},
                    {"exchange": "SSE", "cal_date": "20240105", "is_open": 1},
                ]
            ),
            "income": pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "period": "20231231", "n_income_attr_p": 100.0},
                    {"ts_code": "600000.SH", "period": "20231231", "n_income_attr_p": 200.0},
                ]
            ),
            "stock_basic": pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行", "list_date": "19910403"},
                    {"ts_code": "600000.SH", "name": "浦发银行", "industry": "银行", "list_date": "19991110"},
                ]
            ),
        }

    def fetch(self, api_name, **params):
        df = self.frames.get(api_name, pd.DataFrame()).copy()
        if "ts_code" in params and "ts_code" in df.columns:
            codes = str(params["ts_code"]).split(",")
            df = df[df["ts_code"].isin(codes)]
        if "start_date" in params and "trade_date" in df.columns and params["start_date"] is not None:
            df = df[df["trade_date"] >= str(params["start_date"]).replace("-", "")]
        if "end_date" in params and "trade_date" in df.columns and params["end_date"] is not None:
            df = df[df["trade_date"] <= str(params["end_date"]).replace("-", "")]
        if "start_date" in params and "cal_date" in df.columns and params["start_date"] is not None:
            df = df[df["cal_date"] >= str(params["start_date"]).replace("-", "")]
        if "end_date" in params and "cal_date" in df.columns and params["end_date"] is not None:
            df = df[df["cal_date"] <= str(params["end_date"]).replace("-", "")]
        if "trade_date" in params and "trade_date" in df.columns and params["trade_date"] is not None:
            df = df[df["trade_date"] == params["trade_date"]]
        if "period" in params and "period" in df.columns and params["period"] is not None:
            df = df[df["period"] == params["period"]]
        return df.reset_index(drop=True)


class TestJoinQuantAPI(unittest.TestCase):
    def setUp(self):
        set_runtime_state(RuntimeState(data_portal=FakePortal()))

    def test_exported_globals_contains_core_api(self):
        exports = exported_globals()
        for name in (
            "get_price",
            "get_fundamentals",
            "get_fundamentals_continuously",
            "get_security_info",
            "query",
            "valuation",
            "income",
            "run_daily",
            "order",
        ):
            self.assertIn(name, exports)

    def test_get_price_delegates_to_portal(self):
        df = get_price("000001.XSHE", count=1, fields="close", panel=False)
        self.assertEqual(df["close"].iloc[0], 10.5)

    def test_get_trade_days_and_index_stocks_delegate_to_portal(self):
        self.assertEqual(get_trade_days(start_date="2024-01-01", end_date="2024-01-03"), ["2024-01-02"])
        self.assertEqual(get_index_stocks("399006.XSHE", date="2024-01-02"), ["000001.XSHE", "600000.XSHG"])

    def test_get_security_info_delegates_to_portal(self):
        set_runtime_state(RuntimeState(data_portal=DataPortal(APIPortalBackend())))

        info = get_security_info("000001.XSHE")

        self.assertEqual(info.code, "000001.XSHE")
        self.assertEqual(info.display_name, "平安银行")
        self.assertEqual(info.start_date, date(1991, 4, 3))

    def test_get_index_stocks_defaults_date_to_visible_data_date_at_open(self):
        class RecordingPortal(FakePortal):
            def __init__(self):
                self.calls = []

            def get_index_stocks(self, index_symbol, date=None):
                self.calls.append({"index_symbol": index_symbol, "date": date})
                return super().get_index_stocks(index_symbol, date=date)

        portal = RecordingPortal()
        set_runtime_state(
            RuntimeState(
                data_portal=portal,
                context=SimpleNamespace(current_dt=pd.Timestamp("2024-01-03 09:30:00")),
            )
        )

        get_index_stocks("399006.XSHE")

        self.assertEqual(str(portal.calls[0]["date"])[:10], "2024-01-02")

    def test_get_fundamentals_applies_query_filters(self):
        q = query(valuation.code, valuation.market_cap, income.np_parent_company_owners).filter(
            valuation.market_cap <= 80,
            income.np_parent_company_owners > 0,
        )
        df = get_fundamentals(q, date="2024-01-02")

        self.assertEqual(df["code"].tolist(), ["000001.XSHE"])
        self.assertEqual(df["market_cap"].tolist(), [50.0])

    def test_get_fundamentals_defaults_date_to_visible_data_date_at_open(self):
        class RecordingPortal(FakePortal):
            def __init__(self):
                self.calls = []

            def get_fundamentals(self, q, date=None, statDate=None):
                self.calls.append({"date": date, "statDate": statDate})
                return super().get_fundamentals(q, date=date, statDate=statDate)

        portal = RecordingPortal()
        set_runtime_state(
            RuntimeState(
                data_portal=portal,
                context=SimpleNamespace(current_dt=pd.Timestamp("2024-01-03 09:30:00")),
            )
        )

        get_fundamentals(query(valuation.code), statDate="2023q4")

        self.assertEqual(str(portal.calls[0]["date"])[:10], "2024-01-02")
        self.assertEqual(portal.calls[0]["statDate"], "2023q4")

    def test_get_price_real_portal_drops_compat_noop_kwargs(self):
        set_runtime_state(RuntimeState(data_portal=DataPortal(APIPortalBackend())))

        df = get_price(
            "000001.XSHE",
            end_date="2024-01-03",
            count=1,
            fields="close",
            panel=False,
            skip_paused=True,
            fq="pre",
            fill_paused=True,
        )

        self.assertEqual(df["close"].tolist(), [10.2])

    def test_get_price_at_open_clamps_context_end_date_to_previous_trade_day(self):
        set_runtime_state(
            RuntimeState(
                data_portal=DataPortal(APIPortalBackend()),
                context=SimpleNamespace(current_dt=pd.Timestamp("2024-01-03 09:30:00")),
            )
        )

        df = get_price(
            "000001.XSHE",
            end_date=pd.Timestamp("2024-01-03 09:30:00"),
            count=1,
            fields="close",
            panel=False,
        )
        future_df = get_price(
            "000001.XSHE",
            end_date="2024-01-05",
            count=1,
            fields="close",
            panel=False,
        )

        self.assertEqual(df["time"].tolist(), ["2024-01-02"])
        self.assertEqual(df["close"].tolist(), [10.5])
        self.assertEqual(future_df["time"].tolist(), ["2024-01-02"])
        self.assertEqual(future_df["close"].tolist(), [10.5])

    def test_get_fundamentals_at_open_clamps_context_date_to_previous_trade_day(self):
        set_runtime_state(
            RuntimeState(
                data_portal=DataPortal(APIPortalBackend()),
                context=SimpleNamespace(current_dt=pd.Timestamp("2024-01-03 09:30:00")),
            )
        )

        df = get_fundamentals(
            query(valuation.code, valuation.market_cap, valuation.turnover_ratio),
            date=pd.Timestamp("2024-01-03 09:30:00"),
        )

        self.assertEqual(df["code"].tolist(), ["000001.XSHE"])
        self.assertEqual(df["market_cap"].tolist(), [0.0045])
        self.assertEqual(df["turnover_ratio"].tolist(), [1.2])

    def test_get_current_data_at_open_uses_previous_trade_day_close(self):
        set_runtime_state(
            RuntimeState(
                data_portal=DataPortal(APIPortalBackend()),
                context=SimpleNamespace(current_dt=pd.Timestamp("2024-01-03 09:30:00")),
            )
        )

        current = get_current_data(["000001.XSHE"])

        self.assertEqual(current["000001.XSHE"].last_price, 10.5)
        self.assertEqual(current["000001.XSHE"].day_open, 10.0)

    def test_attribute_history_real_portal_supports_default_compat_kwargs(self):
        set_runtime_state(
            RuntimeState(
                data_portal=DataPortal(APIPortalBackend()),
                context=SimpleNamespace(current_dt=pd.Timestamp("2024-01-03 14:30:00")),
            )
        )

        df = attribute_history(
            "000001.XSHE",
            2,
            unit="1d",
            fields=("close",),
            skip_paused=True,
            df=True,
            fq="pre",
        )

        self.assertEqual(df["time"].tolist(), ["2024-01-02", "2024-01-03"])
        self.assertEqual(df["close"].tolist(), [10.5, 10.2])

    def test_history_real_portal_uses_context_current_dt_as_of_boundary(self):
        set_runtime_state(
            RuntimeState(
                data_portal=DataPortal(APIPortalBackend()),
                context=SimpleNamespace(current_dt=pd.Timestamp("2024-01-03 14:30:00")),
            )
        )

        df = history(
            3,
            unit="1d",
            field="close",
            security_list=["000001.XSHE"],
            skip_paused=True,
            df=True,
            fq="pre",
        )

        self.assertNotIn("2024-01-05", df["time"].tolist())

    def test_get_fundamentals_real_portal_supports_turnover_ratio_filter(self):
        set_runtime_state(RuntimeState(data_portal=DataPortal(APIPortalBackend())))

        q = query(valuation.code, valuation.turnover_ratio).filter(
            valuation.turnover_ratio <= 2.0,
        )
        df = get_fundamentals(q, date="2024-01-03")

        self.assertEqual(df["code"].tolist(), ["000001.XSHE"])
        self.assertEqual(df["turnover_ratio"].tolist(), [1.5])

    def test_get_fundamentals_raises_when_selected_field_missing(self):
        q = query(valuation.code, valuation.turnover_ratio)

        with self.assertRaises(NotImplementedError):
            get_fundamentals(q, date="2024-01-02")

    def test_get_fundamentals_raises_when_filter_field_missing(self):
        q = query(valuation.code).filter(valuation.turnover_ratio > 1.0)

        with self.assertRaises(NotImplementedError):
            get_fundamentals(q, date="2024-01-02")

    def test_get_fundamentals_raises_for_unsupported_balance_query(self):
        from jq_tushare_sdk.api.finance_tables import balance

        set_runtime_state(RuntimeState(data_portal=DataPortal(APIPortalBackend())))

        with self.assertRaises(NotImplementedError):
            get_fundamentals(query(balance.code), date="2024-01-03")

    def test_get_fundamentals_raises_for_unsupported_cash_flow_query(self):
        from jq_tushare_sdk.api.finance_tables import cash_flow

        set_runtime_state(RuntimeState(data_portal=DataPortal(APIPortalBackend())))

        with self.assertRaises(NotImplementedError):
            get_fundamentals(query(cash_flow.code), date="2024-01-03")

    def test_get_industry_raises_when_portal_lacks_implementation(self):
        with self.assertRaises(NotImplementedError):
            get_industry(["000001.XSHE"], date="2024-01-02")

    def test_get_industry_real_portal_returns_joinquant_style_mapping(self):
        set_runtime_state(RuntimeState(data_portal=DataPortal(APIPortalBackend())))

        result = get_industry(["000001.XSHE"], date="2024-01-03")

        self.assertEqual(result["000001.XSHE"]["sw_l1"]["industry_name"], "银行")
        self.assertEqual(result["000001.XSHE"]["industry_name"], "银行")

    def test_get_current_data_real_portal_uses_context_current_dt_boundary(self):
        set_runtime_state(
            RuntimeState(
                data_portal=DataPortal(APIPortalBackend()),
                context=SimpleNamespace(current_dt=pd.Timestamp("2024-01-03 14:30:00")),
            )
        )

        current = get_current_data(["000001.XSHE"])

        self.assertEqual(current["000001.XSHE"].last_price, 10.2)

    def test_set_order_cost_updates_broker_fees_for_later_orders(self):
        exports = exported_globals()
        context = Context(Portfolio(100000.0), current_dt=datetime(2024, 1, 3, 9, 30))
        broker = Broker(context, DataPortal(APIPortalBackend()), CostModel())
        set_runtime_state(
            RuntimeState(
                data_portal=broker.data_portal,
                broker=broker,
                context=context,
            )
        )

        exports["set_order_cost"](
            exports["OrderCost"](
                open_tax=0.0,
                close_tax=0.002,
                open_commission=0.001,
                close_commission=0.003,
                min_commission=8.0,
            ),
            type="stock",
        )
        broker.order("000001.XSHE", 100)
        order = broker.order("000001.XSHE", -100)

        self.assertAlmostEqual(order.commission, 8.0)
        self.assertAlmostEqual(order.stamp_tax, 2.12)

    def test_set_order_cost_accepts_zero_close_today_commission(self):
        exports = exported_globals()
        set_runtime_state(
            RuntimeState(
                data_portal=SimpleNamespace(),
                broker=SimpleNamespace(cost_model=CostModel()),
            )
        )

        exports["set_order_cost"](
            exports["OrderCost"](
                open_tax=0.0,
                close_tax=0.001,
                open_commission=0.0004,
                close_commission=0.0005,
                close_today_commission=0,
                min_commission=6.0,
            ),
            type="stock",
        )

        broker_cost = runtime_state().broker.cost_model
        self.assertAlmostEqual(broker_cost.open_commission, 0.0004)
        self.assertAlmostEqual(broker_cost.close_commission, 0.0005)
        self.assertAlmostEqual(broker_cost.min_commission, 6.0)

    def test_set_order_cost_rejects_unsupported_variants(self):
        exports = exported_globals()
        set_runtime_state(
            RuntimeState(
                data_portal=SimpleNamespace(),
                broker=SimpleNamespace(cost_model=CostModel()),
            )
        )

        with self.assertRaises(NotImplementedError):
            exports["set_order_cost"](exports["OrderCost"](open_tax=0.001), type="stock")
        with self.assertRaises(NotImplementedError):
            exports["set_order_cost"](exports["OrderCost"](), type="fund")

    def test_set_slippage_supports_price_related_style(self):
        exports = exported_globals()
        context = Context(Portfolio(100000.0), current_dt=datetime(2024, 1, 3, 9, 30))
        broker = Broker(context, DataPortal(APIPortalBackend()), CostModel())
        set_runtime_state(
            RuntimeState(
                data_portal=broker.data_portal,
                broker=broker,
                context=context,
            )
        )

        exports["set_slippage"](exports["PriceRelatedSlippage"](0.02))
        order = broker.order("000001.XSHE", 100)

        self.assertAlmostEqual(order.price, 10.812)

    def test_set_slippage_supports_fixed_style(self):
        exports = exported_globals()
        context = Context(Portfolio(100000.0), current_dt=datetime(2024, 1, 3, 9, 30))
        broker = Broker(context, DataPortal(APIPortalBackend()), CostModel())
        set_runtime_state(
            RuntimeState(
                data_portal=broker.data_portal,
                broker=broker,
                context=context,
            )
        )

        exports["set_slippage"](exports["FixedSlippage"](0.01))
        buy_order = broker.order("000001.XSHE", 100)
        sell_order = broker.order("000001.XSHE", -100)

        self.assertAlmostEqual(buy_order.price, 10.605)
        self.assertAlmostEqual(sell_order.price, 10.595)

    def test_set_slippage_rejects_unsupported_styles(self):
        class UnsupportedSlippage:
            pass

        exports = exported_globals()
        set_runtime_state(
            RuntimeState(
                data_portal=SimpleNamespace(),
                broker=SimpleNamespace(cost_model=CostModel()),
            )
        )

        with self.assertRaises(NotImplementedError):
            exports["set_slippage"](UnsupportedSlippage())

    def test_get_fundamentals_continuously_returns_turnover_history_by_security(self):
        set_runtime_state(RuntimeState(data_portal=DataPortal(APIPortalBackend())))

        result = get_fundamentals_continuously(
            query(valuation.code, valuation.turnover_ratio).filter(
                valuation.code == "000001.XSHE",
            ),
            end_date="2024-01-05",
            count=2,
        )

        stock_frame = result.minor_xs("000001.XSHE")
        self.assertEqual(stock_frame["turnover_ratio"].tolist(), [1.5, 2.1])
        self.assertEqual(stock_frame.index.tolist(), ["2024-01-03", "2024-01-05"])


if __name__ == "__main__":
    unittest.main()
