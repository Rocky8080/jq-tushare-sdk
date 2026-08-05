import os
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pandas as pd

from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.data.code_map import to_joinquant_code, to_tushare_code
from jq_tushare_sdk.data.income_periods import required_income_periods
from jq_tushare_sdk.data.portal import DataPortal
from jq_tushare_sdk.data.readiness import (
    DataReadinessCheck,
    DataUpdateRequest,
    update_missing_data,
)


class FakeBackend:
    def __init__(self):
        self.calls = []
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
                        "ts_code": "600000.SH",
                        "trade_date": "20240102",
                        "open": 20.0,
                        "high": 20.6,
                        "low": 19.8,
                        "close": 20.4,
                        "vol": 1500.0,
                        "amount": 30100.0,
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
            "adj_factor": pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "trade_date": "20240102", "adj_factor": 1.0},
                    {"ts_code": "000001.SZ", "trade_date": "20240103", "adj_factor": 2.0},
                    {"ts_code": "600000.SH", "trade_date": "20240102", "adj_factor": 1.0},
                    {"ts_code": "600000.SH", "trade_date": "20240103", "adj_factor": 1.0},
                ]
            ),
            "fund_adj": pd.DataFrame(
                [
                    {"ts_code": "510300.SH", "trade_date": "20240102", "adj_factor": 1.0},
                    {"ts_code": "510300.SH", "trade_date": "20240103", "adj_factor": 1.25},
                ]
            ),
            "fund_daily": pd.DataFrame(
                [
                    {
                        "ts_code": "510300.SH",
                        "trade_date": "20240102",
                        "open": 4.0,
                        "high": 4.1,
                        "low": 3.9,
                        "close": 4.0,
                        "vol": 1000.0,
                        "amount": 4000.0,
                    },
                    {
                        "ts_code": "510300.SH",
                        "trade_date": "20240103",
                        "open": 5.0,
                        "high": 5.1,
                        "low": 4.9,
                        "close": 5.0,
                        "vol": 900.0,
                        "amount": 4500.0,
                    },
                ]
            ),
            "trade_cal": pd.DataFrame(
                [
                    {"exchange": "SSE", "cal_date": "20240101", "is_open": 0},
                    {"exchange": "SSE", "cal_date": "20240102", "is_open": 1},
                    {"exchange": "SSE", "cal_date": "20240103", "is_open": 1},
                    {"exchange": "SSE", "cal_date": "20240104", "is_open": 1},
                ]
            ),
            "index_daily": pd.DataFrame(
                [
                    {
                        "ts_code": "000905.SH",
                        "trade_date": "20240102",
                        "open": 5000.0,
                        "high": 5050.0,
                        "low": 4980.0,
                        "close": 5030.0,
                        "vol": 100000.0,
                        "amount": 503000000.0,
                    },
                    {
                        "ts_code": "000852.SH",
                        "trade_date": "20240102",
                        "open": 6000.0,
                        "high": 6060.0,
                        "low": 5980.0,
                        "close": 6040.0,
                        "vol": 120000.0,
                        "amount": 724800000.0,
                    },
                ]
            ),
            "sw_daily": pd.DataFrame(
                [
                    {
                        "ts_code": "801010.SI",
                        "trade_date": "20240102",
                        "open": 3000.0,
                        "high": 3030.0,
                        "low": 2980.0,
                        "close": 3020.0,
                        "vol": 80000.0,
                        "amount": 241600000.0,
                    },
                    {
                        "ts_code": "801010.SI",
                        "trade_date": "20240103",
                        "open": 3020.0,
                        "high": 3060.0,
                        "low": 3010.0,
                        "close": 3050.0,
                        "vol": 85000.0,
                        "amount": 259250000.0,
                    },
                ]
            ),
            "index_weight": pd.DataFrame(
                [
                    {"index_code": "000905.SH", "con_code": "000001.SZ", "trade_date": "20240102"},
                    {"index_code": "000905.SH", "con_code": "600000.SH", "trade_date": "20240102"},
                    {"index_code": "000905.SH", "con_code": "300001.SZ", "trade_date": "20240101"},
                ]
            ),
            "stock_basic": pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行", "list_date": "19910403"},
                    {"ts_code": "600000.SH", "name": "浦发银行", "industry": "银行", "list_date": "19991110"},
                ]
            ),
            "daily_basic": pd.DataFrame(
                [
                    {"ts_code": "200012.SZ", "trade_date": "20240103", "total_mv": 300000.0, "pe": 8.0, "pb": 0.8, "turnover_rate": 1.1},
                    {"ts_code": "000001.SZ", "trade_date": "20240103", "total_mv": 500000.0, "pe": 10.0, "pb": 1.2, "turnover_rate": 1.5},
                    {"ts_code": "000001.SZ", "trade_date": "20240105", "total_mv": 520000.0, "pe": 10.4, "pb": 1.25, "turnover_rate": 2.1},
                    {"ts_code": "600000.SH", "trade_date": "20240103", "total_mv": 900000.0, "pe": 12.0, "pb": 0.9, "turnover_rate": 3.2},
                    {"ts_code": "600000.SH", "trade_date": "20240105", "total_mv": 920000.0, "pe": 12.4, "pb": 0.95, "turnover_rate": 3.5},
                ]
            ),
            "income": pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "period": "20231231", "n_income_attr_p": 100.0, "total_revenue": 500.0},
                    {"ts_code": "600000.SH", "period": "20231231", "n_income_attr_p": 200.0, "total_revenue": 800.0},
                ]
            ),
        }

    def fetch(self, api_name, **params):
        self.calls.append((api_name, params))
        df = self.frames.get(api_name, pd.DataFrame()).copy()
        if "ts_code" in params and "ts_code" in df.columns:
            codes = str(params["ts_code"]).split(",")
            df = df[df["ts_code"].isin(codes)]
        if "index_code" in params and "index_code" in df.columns:
            df = df[df["index_code"] == params["index_code"]]
        if "start_date" in params and "trade_date" in df.columns:
            df = df[df["trade_date"] >= params["start_date"]]
        if "end_date" in params and "trade_date" in df.columns:
            df = df[df["trade_date"] <= params["end_date"]]
        if "start_date" in params and "cal_date" in df.columns:
            df = df[df["cal_date"] >= params["start_date"]]
        if "end_date" in params and "cal_date" in df.columns:
            df = df[df["cal_date"] <= params["end_date"]]
        if "trade_date" in params and "trade_date" in df.columns and params["trade_date"] is not None:
            df = df[df["trade_date"] == params["trade_date"]]
        if "period" in params and "end_date" in df.columns and params["period"] is not None:
            period = str(params["period"]).lower()
            quarter_ends = {"q1": "0331", "q2": "0630", "q3": "0930", "q4": "1231"}
            if len(period) == 6 and period[:4].isdigit() and period[4:] in quarter_ends:
                period = f"{period[:4]}{quarter_ends[period[4:]]}"
            df = df[df["end_date"] == period]
        return df.reset_index(drop=True)

    def status(self, api_name):
        if api_name == "daily":
            return {
                "exists": True,
                "record_count": 2,
                "min_date": "20240102",
                "max_date": "20240103",
            }
        return {"exists": False, "record_count": 0}


class TestDataLayer(unittest.TestCase):
    def test_default_index_daily_update_includes_000985_benchmark(self):
        class RecordingBackend(TushareCacheBackend):
            def __init__(self):
                self.cache_mode = "api_first"
                self.token = "placeholder"
                self.calls = []

            def update_data(self, api_name, start_date=None, end_date=None, **params):
                if api_name == "index_daily" and "ts_code" not in params:
                    return TushareCacheBackend.update_data(
                        self,
                        api_name,
                        start_date=start_date,
                        end_date=end_date,
                        **params,
                    )
                self.calls.append((api_name, params))
                return 1

        backend = RecordingBackend()

        backend.update_data("index_daily", start_date="20260601", end_date="20260630")

        requested_codes = [params["ts_code"] for _, params in backend.calls]
        self.assertIn("000985.CSI", requested_codes)

    def test_code_conversion(self):
        self.assertEqual(to_tushare_code("000001.XSHE"), "000001.SZ")
        self.assertEqual(to_tushare_code("600000.XSHG"), "600000.SH")
        self.assertEqual(to_tushare_code("399006.XSHE"), "399006.SZ")
        self.assertEqual(to_tushare_code("000688.XSHG"), "000688.SH")
        self.assertEqual(to_tushare_code("000985.XSHG"), "000985.CSI")
        self.assertEqual(to_tushare_code("000985.CSI"), "000985.CSI")
        self.assertEqual(to_joinquant_code("000001.SZ"), "000001.XSHE")
        self.assertEqual(to_joinquant_code("600000.SH"), "600000.XSHG")
        self.assertEqual(to_joinquant_code("000985.CSI"), "000985.XSHG")
        self.assertEqual(to_tushare_code("801010.XSHG"), "801010.SI")
        self.assertEqual(to_joinquant_code("801010.SI"), "801010.XSHG")

    def test_get_price_returns_joinquant_codes_and_fields(self):
        portal = DataPortal(FakeBackend())
        df = portal.get_price(
            "000001.XSHE",
            start_date="20240102",
            end_date="20240103",
            fields=["open", "close", "volume", "money"],
        )

        self.assertEqual(df["code"].tolist(), ["000001.XSHE", "000001.XSHE"])
        self.assertEqual(df["time"].tolist(), ["2024-01-02", "2024-01-03"])
        self.assertEqual(df["volume"].tolist(), [1000.0, 900.0])
        self.assertEqual(df["money"].tolist(), [10500000.0, 9300000.0])

    def test_get_price_applies_pre_adjustment_when_requested(self):
        portal = DataPortal(FakeBackend())
        df = portal.get_price(
            "000001.XSHE",
            start_date="20240102",
            end_date="20240103",
            fields=["open", "close", "money"],
            fq="pre",
        )

        self.assertEqual(df["open"].tolist(), [5.0, 10.6])
        self.assertEqual(df["close"].tolist(), [5.25, 10.2])
        self.assertEqual(df["money"].tolist(), [10500000.0, 9300000.0])

    def test_get_price_supports_opt_in_default_pre_adjustment(self):
        with patch.dict("os.environ", {"JQTS_DEFAULT_FQ": "pre"}):
            portal = DataPortal(FakeBackend())
            df = portal.get_price(
                "000001.XSHE",
                start_date="20240102",
                end_date="20240103",
                fields=["open", "close"],
            )

        self.assertEqual(df["open"].tolist(), [5.0, 10.6])
        self.assertEqual(df["close"].tolist(), [5.25, 10.2])

    def test_get_price_applies_pre_adjustment_to_etf_when_requested(self):
        portal = DataPortal(FakeBackend())
        df = portal.get_price(
            "510300.XSHG",
            start_date="20240102",
            end_date="20240103",
            fields=["open", "close"],
            fq="pre",
        )

        self.assertEqual(df["code"].tolist(), ["510300.XSHG", "510300.XSHG"])
        self.assertEqual(df["open"].tolist(), [3.2, 5.0])
        self.assertEqual(df["close"].tolist(), [3.2, 5.0])
        self.assertIn(
            ("fund_adj", {"start_date": "20240102", "end_date": "20240103"}),
            portal.backend.calls,
        )

    def test_get_price_supports_security_batches_and_count_per_security(self):
        portal = DataPortal(FakeBackend())
        df = portal.get_price(
            pd.Index(["000001.XSHE", "600000.XSHG"]),
            start_date="20240102",
            end_date="20240103",
            count=1,
            fields="close",
            panel=False,
        )

        self.assertEqual(
            portal.backend.calls[0],
            (
                "daily",
                {
                    "ts_code": "000001.SZ,600000.SH",
                    "start_date": "20240102",
                    "end_date": "20240103",
                },
            ),
        )
        self.assertEqual(df.columns.tolist(), ["time", "code", "close"])
        self.assertEqual(df["code"].tolist(), ["000001.XSHE", "600000.XSHG"])
        self.assertEqual(df["time"].tolist(), ["2024-01-03", "2024-01-03"])
        self.assertEqual(df["close"].tolist(), [10.2, 20.8])
        latest = df.groupby("code")["close"].last().to_dict()
        self.assertEqual(latest, {"000001.XSHE": 10.2, "600000.XSHG": 20.8})

    def test_get_price_supports_tuple_security_batches(self):
        portal = DataPortal(FakeBackend())

        df = portal.get_price(
            ("000001.XSHE", "600000.XSHG"),
            start_date="20240102",
            end_date="20240103",
            count=1,
            fields="close",
        )

        self.assertEqual(portal.backend.calls[0][1]["ts_code"], "000001.SZ,600000.SH")
        self.assertEqual(df["code"].tolist(), ["000001.XSHE", "600000.XSHG"])

    def test_get_price_reuses_single_security_count_cache_for_same_date(self):
        class CountingBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fetch_counts = {}

            def fetch(self, api_name, **params):
                self.fetch_counts[api_name] = self.fetch_counts.get(api_name, 0) + 1
                return super().fetch(api_name, **params)

        backend = CountingBackend()
        portal = DataPortal(backend)

        first = portal.get_price("000001.XSHE", end_date="2024-01-03", count=1, fields="close")
        second = portal.get_price("600000.XSHG", end_date="2024-01-03", count=1, fields="close")

        self.assertEqual(first["close"].tolist(), [10.2])
        self.assertEqual(second["close"].tolist(), [20.8])
        self.assertEqual(backend.fetch_counts["daily"], 1)
        self.assertIn("start_date", backend.calls[0][1])

    def test_get_price_reuses_date_range_cache_across_securities(self):
        class CountingBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fetch_counts = {}

            def fetch(self, api_name, **params):
                self.fetch_counts[api_name] = self.fetch_counts.get(api_name, 0) + 1
                return super().fetch(api_name, **params)

        backend = CountingBackend()
        portal = DataPortal(backend)

        first = portal.get_price(
            "000001.XSHE",
            start_date="20240102",
            end_date="20240103",
            fields="close",
        )
        second = portal.get_price(
            "600000.XSHG",
            start_date="20240102",
            end_date="20240103",
            fields="close",
        )

        self.assertEqual(first["close"].tolist(), [10.5, 10.2])
        self.assertEqual(second["close"].tolist(), [20.4, 20.8])
        self.assertEqual(backend.fetch_counts["daily"], 1)
        self.assertNotIn("ts_code", backend.calls[0][1])

    def test_get_price_reuses_pre_adjustment_fetches_and_returns_copies(self):
        class CountingBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fetch_counts = {}

            def fetch(self, api_name, **params):
                self.fetch_counts[api_name] = self.fetch_counts.get(api_name, 0) + 1
                return super().fetch(api_name, **params)

        backend = CountingBackend()
        portal = DataPortal(backend)

        first = portal.get_price(
            "000001.XSHE",
            end_date="2024-01-03",
            count=2,
            fields=["close"],
            fq="pre",
        )
        first.loc[0, "close"] = -999.0
        second = portal.get_price(
            "000001.XSHE",
            end_date="2024-01-03",
            count=2,
            fields=["close"],
            fq="pre",
        )

        self.assertEqual(second["close"].tolist(), [5.25, 10.2])
        self.assertEqual(backend.fetch_counts["daily"], 1)
        self.assertEqual(backend.fetch_counts["adj_factor"], 1)

    def test_get_price_reuses_pre_adjustment_range_cache_across_batches(self):
        class CountingBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fetch_counts = {}

            def fetch(self, api_name, **params):
                self.fetch_counts[api_name] = self.fetch_counts.get(api_name, 0) + 1
                return super().fetch(api_name, **params)

        backend = CountingBackend()
        portal = DataPortal(
            backend,
            price_cache_start="2024-01-01",
            price_cache_end="2024-01-31",
        )

        first = portal.get_price(
            ["000001.XSHE", "600000.XSHG"],
            start_date="20240102",
            end_date="20240103",
            fields=["close"],
            fq="pre",
        )
        first.loc[0, "close"] = -999.0
        second = portal.get_price(
            ["600000.XSHG"],
            start_date="20240102",
            end_date="20240103",
            fields=["close"],
            fq="pre",
        )

        self.assertEqual(second["close"].tolist(), [20.4, 20.8])
        self.assertEqual(backend.fetch_counts["daily"], 1)
        self.assertEqual(backend.fetch_counts["adj_factor"], 1)
        self.assertEqual(
            portal.performance_snapshot()["canonical_cache"]["factor_rows_scanned"],
            4,
        )

    def test_canonical_factor_view_compresses_repeated_values_without_changing_prices(self):
        backend = FakeBackend()
        dates = pd.date_range("2024-01-02", periods=6, freq="B")
        backend.frames["daily"] = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": value.strftime("%Y%m%d"),
                    "close": 12.0,
                }
                for value in dates
            ]
        )
        backend.frames["adj_factor"] = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": value.strftime("%Y%m%d"),
                    "adj_factor": factor,
                }
                for value, factor in zip(dates, [1.0, 1.0, 1.0, 2.0, 2.0, 2.0])
            ]
        )
        portal = DataPortal(
            backend,
            price_cache_start="2024-01-01",
            price_cache_end="2024-01-31",
        )

        frame = portal.get_price(
            "000001.XSHE",
            start_date="2024-01-02",
            end_date="2024-01-09",
            fields="close",
            fq="pre",
            panel=False,
        )
        cache = portal.performance_snapshot()["canonical_cache"]

        self.assertEqual(frame["close"].tolist(), [6.0, 6.0, 6.0, 12.0, 12.0, 12.0])
        self.assertEqual(cache["factor_rows_scanned"], 6)
        self.assertEqual(cache["factor_change_nodes"], 2)

    def test_canonical_compressed_factor_view_preserves_missing_date_fallback(self):
        backend = FakeBackend()
        backend.frames["daily"] = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_date": "20240102", "close": 10.0},
                {"ts_code": "000001.SZ", "trade_date": "20240103", "close": 10.0},
                {"ts_code": "000001.SZ", "trade_date": "20240104", "close": 10.0},
            ]
        )
        backend.frames["adj_factor"] = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_date": "20240102", "adj_factor": 1.0},
                {"ts_code": "000001.SZ", "trade_date": "20240104", "adj_factor": 2.0},
            ]
        )
        portal = DataPortal(
            backend,
            price_cache_start="2024-01-01",
            price_cache_end="2024-01-31",
        )

        frame = portal.get_price(
            "000001.XSHE",
            start_date="2024-01-02",
            end_date="2024-01-04",
            fields="close",
            fq="pre",
            panel=False,
        )

        self.assertEqual(frame["close"].tolist(), [5.0, 10.0, 10.0])

    def test_get_price_reuses_final_result_cache_and_returns_copies(self):
        class CountingPortal(DataPortal):
            def __init__(self, backend):
                super().__init__(backend)
                self.adjustment_calls = 0

            def _apply_price_adjustment(
                self,
                df,
                api_name: str,
                fq,
                adjustment_end_date=None,
            ):
                self.adjustment_calls += 1
                return super()._apply_price_adjustment(
                    df,
                    api_name=api_name,
                    fq=fq,
                    adjustment_end_date=adjustment_end_date,
                )

        portal = CountingPortal(FakeBackend())

        first = portal.get_price(
            "000001.XSHE",
            start_date="20240102",
            end_date="20240103",
            fields=["close"],
            fq="pre",
        )
        first.loc[0, "close"] = -999.0
        second = portal.get_price(
            "000001.XSHE",
            start_date="20240102",
            end_date="20240103",
            fields=["close"],
            fq="pre",
        )

        self.assertEqual(second["close"].tolist(), [5.25, 10.2])
        self.assertEqual(portal.adjustment_calls, 1)

    def test_get_price_result_cache_evicts_old_frames_by_row_budget(self):
        with patch.dict(os.environ, {"JQTS_PRICE_RESULT_CACHE_ROWS": "2"}):
            portal = DataPortal(FakeBackend())

        portal.get_price(
            "000001.XSHE",
            start_date="20240102",
            end_date="20240103",
            fields=["close"],
        )
        portal.get_price(
            "600000.XSHG",
            start_date="20240102",
            end_date="20240103",
            fields=["close"],
        )
        portal.get_price(
            "600000.XSHG",
            start_date="20240102",
            end_date="20240103",
            fields=["close"],
        )

        cache = portal.performance_snapshot()["result_cache"]
        self.assertEqual(cache["entries"], 1)
        self.assertEqual(cache["rows"], 2)
        self.assertEqual(cache["row_budget"], 2)
        self.assertEqual(cache["evictions"], 1)
        self.assertEqual(cache["hits"], 1)

    def test_get_trade_days_reuses_backend_fetch_cache(self):
        class CountingBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fetch_counts = {}

            def fetch(self, api_name, **params):
                self.fetch_counts[api_name] = self.fetch_counts.get(api_name, 0) + 1
                return super().fetch(api_name, **params)

        backend = CountingBackend()
        portal = DataPortal(backend)

        first = portal.get_trade_days(end_date="2024-01-04", count=2)
        second = portal.get_trade_days(end_date="2024-01-04", count=2)

        self.assertEqual(first, ["2024-01-03", "2024-01-04"])
        self.assertEqual(second, first)
        self.assertEqual(backend.fetch_counts["trade_cal"], 1)

    def test_get_trade_days_filters_open_days(self):
        portal = DataPortal(FakeBackend())
        self.assertEqual(
            portal.get_trade_days("2024-01-01", "2024-01-03"),
            ["2024-01-02", "2024-01-03"],
        )

    def test_get_trade_days_supports_end_date_count_and_strftime(self):
        portal = DataPortal(FakeBackend())
        trade_days = portal.get_trade_days(end_date="2024-01-04", count=2)

        self.assertEqual(trade_days, ["2024-01-03", "2024-01-04"])
        self.assertEqual(trade_days[0].strftime("%Y-%m-%d"), "2024-01-03")
        self.assertEqual(trade_days[1].strftime("%Y-%m-%d"), "2024-01-04")

    def test_get_price_routes_shanghai_indexes_to_index_daily(self):
        portal = DataPortal(FakeBackend())

        df_905 = portal.get_price("000905.XSHG", start_date="20240102", end_date="20240102", fields="close")
        df_852 = portal.get_price("000852.XSHG", start_date="20240102", end_date="20240102", fields="close")
        df_stock = portal.get_price("000001.XSHE", start_date="20240102", end_date="20240102", fields="close")

        self.assertEqual(portal.backend.calls[0][0], "index_daily")
        self.assertEqual(portal.backend.calls[1][0], "daily")
        self.assertEqual(df_905["code"].tolist(), ["000905.XSHG"])
        self.assertEqual(df_852["code"].tolist(), ["000852.XSHG"])
        self.assertEqual(df_stock["code"].tolist(), ["000001.XSHE"])

    def test_get_price_routes_csi_benchmark_to_index_daily(self):
        backend = FakeBackend()
        backend.frames["index_daily"] = pd.DataFrame(
            [
                {
                    "ts_code": "000985.CSI",
                    "trade_date": "20240102",
                    "open": 6000.0,
                    "high": 6060.0,
                    "low": 5980.0,
                    "close": 6040.0,
                    "vol": 120000.0,
                    "amount": 724800000.0,
                }
            ]
        )
        portal = DataPortal(backend)

        frame = portal.get_price(
            "000985.XSHG",
            start_date="20240102",
            end_date="20240102",
            fields="close",
        )

        self.assertEqual(backend.calls[0][0], "index_daily")
        self.assertEqual(frame["code"].tolist(), ["000985.XSHG"])

    def test_get_price_routes_sw_industry_indexes_to_sw_daily(self):
        portal = DataPortal(FakeBackend())

        frame = portal.get_price(
            "801010.XSHG",
            start_date="20240102",
            end_date="20240103",
            fields="close",
        )

        self.assertEqual(portal.backend.calls[0][0], "sw_daily")
        self.assertEqual(frame["code"].tolist(), ["801010.XSHG", "801010.XSHG"])
        self.assertEqual(frame["close"].tolist(), [3020.0, 3050.0])

    def test_get_index_stocks_returns_joinquant_constituents(self):
        portal = DataPortal(FakeBackend())

        members = portal.get_index_stocks("000905.XSHG", date="2024-01-02")

        self.assertEqual(members, ["000001.XSHE", "600000.XSHG"])

    def test_get_index_stocks_uses_latest_weight_on_or_before_date(self):
        backend = FakeBackend()
        backend.frames["index_weight"] = pd.DataFrame(
            [
                {"index_code": "000905.SH", "con_code": "000001.SZ", "trade_date": "20240101"},
                {"index_code": "000905.SH", "con_code": "600000.SH", "trade_date": "20240101"},
            ]
        )
        portal = DataPortal(backend)

        members = portal.get_index_stocks("000905.XSHG", date="2024-01-02")

        self.assertEqual(members, ["000001.XSHE", "600000.XSHG"])
        self.assertEqual(
            backend.calls[-1],
            (
                "index_weight",
                {
                    "index_code": "000905.SH",
                    "end_date": "20240102",
                },
            ),
        )

    def test_get_index_stocks_rejects_missing_con_code(self):
        backend = FakeBackend()
        backend.frames["index_weight"] = pd.DataFrame(
            [{"index_code": "000905.SH", "trade_date": "20240102"}]
        )
        portal = DataPortal(backend)

        with self.assertRaises(NotImplementedError):
            portal.get_index_stocks("000905.XSHG", date="2024-01-02")

    def test_get_price_rejects_unknown_kwargs(self):
        portal = DataPortal(FakeBackend())

        with self.assertRaises(NotImplementedError):
            portal.get_price("000001.XSHE", start_date="20240102", end_date="20240103", unsupported=True)

    def test_get_price_rejects_unsupported_frequency(self):
        portal = DataPortal(FakeBackend())

        with self.assertRaises(NotImplementedError):
            portal.get_price("000001.XSHE", start_date="20240102", end_date="20240103", frequency="minute")

    def test_get_price_rejects_panel_true(self):
        portal = DataPortal(FakeBackend())

        with self.assertRaises(NotImplementedError):
            portal.get_price("000001.XSHE", start_date="20240102", end_date="20240103", panel=True)

    def test_get_price_rejects_missing_backend_fields(self):
        portal = DataPortal(FakeBackend())

        with self.assertRaises(NotImplementedError):
            portal.get_price("000001.XSHE", start_date="20240102", end_date="20240103", fields=["open", "avg"])

    def test_get_price_rejects_non_empty_frame_missing_trade_date(self):
        backend = FakeBackend()
        backend.frames["daily"] = pd.DataFrame(
            [{"ts_code": "000001.SZ", "close": 10.2}]
        )
        portal = DataPortal(backend)

        with self.assertRaises(NotImplementedError):
            portal.get_price("000001.XSHE", start_date="20240102", end_date="20240103", fields="close")

    def test_get_price_rejects_non_empty_frame_missing_ts_code(self):
        backend = FakeBackend()
        backend.frames["daily"] = pd.DataFrame(
            [{"trade_date": "20240103", "close": 10.2}]
        )
        portal = DataPortal(backend)

        with self.assertRaises(NotImplementedError):
            portal.get_price("000001.XSHE", start_date="20240102", end_date="20240103", fields="close")

    def test_get_price_rejects_malformed_count_path_before_key_error(self):
        backend = FakeBackend()
        backend.frames["daily"] = pd.DataFrame(
            [{"close": 10.2}]
        )
        portal = DataPortal(backend)

        with self.assertRaises(NotImplementedError):
            portal.get_price(
                "000001.XSHE",
                start_date="20240102",
                end_date="20240103",
                fields="close",
                count=1,
            )

    def test_canonical_count_query_loads_a_bounded_window(self):
        backend = FakeBackend()
        dates = pd.date_range("2024-01-01", "2024-06-30", freq="B")
        backend.frames["daily"] = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": value.strftime("%Y%m%d"),
                    "open": float(index),
                    "high": float(index),
                    "low": float(index),
                    "close": float(index),
                    "vol": 100.0,
                    "amount": 1000.0,
                }
                for index, value in enumerate(dates, start=1)
            ]
        )
        portal = DataPortal(
            backend,
            price_cache_start="2024-01-01",
            price_cache_end="2024-06-30",
        )

        frame = portal.get_price(
            "000001.XSHE",
            end_date="2024-03-01",
            count=2,
            fields="close",
            fq=None,
            panel=False,
        )

        daily_calls = [params for name, params in backend.calls if name == "daily"]
        self.assertEqual(frame["time"].tolist(), ["2024-02-29", "2024-03-01"])
        self.assertEqual(daily_calls[0]["end_date"], "20240415")
        self.assertGreater(daily_calls[0]["start_date"], "20240101")
        self.assertLessEqual(portal.performance_snapshot()["canonical_cache"]["loaded_rows"], 45)

    def test_canonical_count_query_extends_backwards_when_window_is_sparse(self):
        backend = FakeBackend()
        backend.frames["daily"] = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_date": "20240102", "close": 10.0},
                {"ts_code": "000001.SZ", "trade_date": "20240201", "close": 10.1},
                {"ts_code": "000001.SZ", "trade_date": "20240301", "close": 10.2},
            ]
        )
        portal = DataPortal(
            backend,
            price_cache_start="2024-01-01",
            price_cache_end="2024-06-30",
        )

        frame = portal.get_price(
            "000001.XSHE",
            end_date="2024-03-01",
            count=3,
            fields="close",
            fq=None,
            panel=False,
        )

        daily_calls = [params for name, params in backend.calls if name == "daily"]
        self.assertEqual(frame["close"].tolist(), [10.0, 10.1, 10.2])
        self.assertEqual(len(daily_calls), 2)
        self.assertEqual(daily_calls[-1]["start_date"], "20240101")

    def test_canonical_count_query_skips_requested_code_without_price_rows(self):
        backend = FakeBackend()
        backend.frames["daily"] = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_date": "20240102", "close": 10.0},
                {"ts_code": "000001.SZ", "trade_date": "20240103", "close": 10.1},
            ]
        )
        portal = DataPortal(
            backend,
            price_cache_start="2024-01-01",
            price_cache_end="2024-01-31",
        )

        frame = portal.get_price(
            ["000001.XSHE", "999999.XSHE"],
            end_date="2024-01-03",
            count=1,
            fields="close",
            fq=None,
            panel=False,
        )

        self.assertEqual(frame["code"].tolist(), ["000001.XSHE"])
        self.assertEqual(frame["close"].tolist(), [10.1])

    def test_vectorized_price_date_formatter_accepts_normalized_and_hyphenated_dates(self):
        portal = DataPortal(FakeBackend(), optimize_data=False)

        formatted = portal._format_joinquant_dates(
            pd.Series(["20240102", "2024-01-03"])
        )

        self.assertEqual(formatted.tolist(), ["2024-01-02", "2024-01-03"])

    def test_get_current_data_without_securities_uses_stock_basic(self):
        portal = DataPortal(FakeBackend())

        current = portal.get_current_data()

        self.assertEqual(sorted(current.keys()), ["000001.XSHE", "600000.XSHG"])
        self.assertEqual(current["000001.XSHE"].name, "平安银行")
        self.assertEqual(current["000001.XSHE"].last_price, 10.2)
        self.assertEqual(current["600000.XSHG"].day_open, 20.5)

    def test_get_security_info_returns_joinquant_style_metadata(self):
        portal = DataPortal(FakeBackend())

        info = portal.get_security_info("000001.XSHE")

        self.assertEqual(info.code, "000001.XSHE")
        self.assertEqual(info.display_name, "平安银行")
        self.assertEqual(info.name, "平安银行")
        self.assertEqual(info.start_date, date(1991, 4, 3))

    def test_get_current_data_with_security_list_returns_requested_objects(self):
        portal = DataPortal(FakeBackend())

        current = portal.get_current_data(["600000.XSHG"])

        self.assertEqual(list(current.keys()), ["600000.XSHG"])
        self.assertEqual(current["600000.XSHG"].name, "浦发银行")
        self.assertEqual(current["600000.XSHG"].high_limit, round(20.4 * 1.1, 2))
        self.assertEqual(current["600000.XSHG"].day_open, 20.5)

    def test_get_current_data_batches_price_lookup_for_security_list(self):
        class CountingPortal(DataPortal):
            def __init__(self, backend):
                super().__init__(backend)
                self.price_call_count = 0

            def get_price(self, *args, **kwargs):
                self.price_call_count += 1
                return super().get_price(*args, **kwargs)

        portal = CountingPortal(FakeBackend())

        current = portal.get_current_data(["000001.XSHE", "600000.XSHG"], date="2024-01-03")

        self.assertEqual(portal.price_call_count, 1)
        self.assertEqual(current["000001.XSHE"].last_price, 10.2)
        self.assertEqual(current["600000.XSHG"].last_price, 20.8)

    def test_get_current_data_uses_as_of_date_boundary(self):
        backend = FakeBackend()
        backend.frames["daily"] = pd.DataFrame(
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
                    "trade_date": "20240105",
                    "open": 11.0,
                    "high": 11.2,
                    "low": 10.8,
                    "close": 11.1,
                    "vol": 1100.0,
                    "amount": 12210.0,
                },
            ]
        )
        portal = DataPortal(backend)

        current = portal.get_current_data(["000001.XSHE"], date="2024-01-03")

        self.assertEqual(current["000001.XSHE"].last_price, 10.5)

    def test_get_current_data_marks_st_and_paused_from_local_cache(self):
        backend = FakeBackend()
        backend.frames["stock_basic"] = pd.DataFrame(
            [{"ts_code": "000002.SZ", "name": "*ST示例", "industry": "制造"}]
        )
        backend.frames["daily"] = pd.DataFrame(
            [
                {
                    "ts_code": "000002.SZ",
                    "trade_date": "20240103",
                    "open": 8.0,
                    "high": 8.0,
                    "low": 8.0,
                    "close": 8.0,
                    "vol": 0.0,
                    "amount": 0.0,
                }
            ]
        )
        portal = DataPortal(backend)

        current = portal.get_current_data(["000002.XSHE"], date="2024-01-03")

        self.assertTrue(current["000002.XSHE"].is_st)
        self.assertTrue(current["000002.XSHE"].paused)
        self.assertEqual(current["000002.XSHE"].last_price, 8.0)
        self.assertEqual(current["000002.XSHE"].day_open, 8.0)

    def test_get_current_data_marks_stale_price_as_paused(self):
        backend = FakeBackend()
        backend.frames["daily"] = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20240102",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "vol": 1000.0,
                    "amount": 10200.0,
                }
            ]
        )
        portal = DataPortal(backend)

        current = portal.get_current_data(["000001.XSHE"], date="2024-01-03")

        self.assertTrue(current["000001.XSHE"].paused)
        self.assertEqual(current["000001.XSHE"].last_price, 10.2)

    def test_get_current_data_reuses_stock_basic_metadata_cache(self):
        class CountingBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fetch_counts = {}

            def fetch(self, api_name, **params):
                self.fetch_counts[api_name] = self.fetch_counts.get(api_name, 0) + 1
                return super().fetch(api_name, **params)

        backend = CountingBackend()
        portal = DataPortal(backend)

        portal.get_current_data(["000001.XSHE"], date="2024-01-03")
        portal.get_current_data(["600000.XSHG"], date="2024-01-03")

        self.assertEqual(backend.fetch_counts["stock_basic"], 1)

    def test_get_current_data_rejects_malformed_stock_basic(self):
        backend = FakeBackend()
        backend.frames["stock_basic"] = pd.DataFrame([{"name": "平安银行"}])
        portal = DataPortal(backend)

        with self.assertRaises(NotImplementedError):
            portal.get_current_data()

    def test_get_current_data_rejects_requested_security_missing_from_stock_basic(self):
        backend = FakeBackend()
        backend.frames["stock_basic"] = pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行"}])
        portal = DataPortal(backend)

        with self.assertRaises(NotImplementedError):
            portal.get_current_data(["600000.XSHG"])

    def test_get_fundamentals_supports_turnover_ratio_mapping(self):
        from jq_tushare_sdk.api.finance_tables import valuation
        from jq_tushare_sdk.api.query import query

        portal = DataPortal(FakeBackend())

        df = portal.get_fundamentals(
            query(valuation.code, valuation.turnover_ratio),
            date="2024-01-03",
        )

        self.assertEqual(df["code"].tolist(), ["000001.XSHE", "600000.XSHG"])
        self.assertEqual(df["turnover_ratio"].tolist(), [1.5, 3.2])

    def test_get_fundamentals_reuses_valuation_cache_for_same_date(self):
        from jq_tushare_sdk.api.finance_tables import valuation
        from jq_tushare_sdk.api.query import query

        class CountingBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.fetch_counts = {}

            def fetch(self, api_name, **params):
                self.fetch_counts[api_name] = self.fetch_counts.get(api_name, 0) + 1
                return super().fetch(api_name, **params)

        backend = CountingBackend()
        portal = DataPortal(backend)
        q = query(valuation.code, valuation.market_cap)

        portal.get_fundamentals(q, date="2024-01-03")
        portal.get_fundamentals(q, date="2024-01-03")

        self.assertEqual(backend.fetch_counts["daily_basic"], 1)

    def test_get_fundamentals_uses_latest_valuation_on_or_before_non_trading_date(self):
        from jq_tushare_sdk.api.finance_tables import valuation
        from jq_tushare_sdk.api.query import query

        portal = DataPortal(FakeBackend())

        df = portal.get_fundamentals(
            query(valuation.code, valuation.market_cap),
            date="2024-01-04",
        )

        self.assertEqual(df.to_dict("records"), [{"code": "000001.XSHE", "market_cap": 50.0}, {"code": "600000.XSHG", "market_cap": 90.0}])

    def test_get_fundamentals_excludes_non_a_share_valuation_rows(self):
        from jq_tushare_sdk.api.finance_tables import valuation
        from jq_tushare_sdk.api.query import query

        portal = DataPortal(FakeBackend())

        df = portal.get_fundamentals(
            query(valuation.code, valuation.market_cap),
            date="2024-01-03",
        )

        self.assertNotIn("200012.XSHE", df["code"].tolist())

    def test_get_fundamentals_converts_tushare_total_mv_to_joinquant_yi_unit(self):
        from jq_tushare_sdk.api.finance_tables import valuation
        from jq_tushare_sdk.api.query import query

        portal = DataPortal(FakeBackend())

        df = portal.get_fundamentals(
            query(valuation.code, valuation.market_cap),
            date="2024-01-03",
        )

        self.assertEqual(df["market_cap"].tolist(), [50.0, 90.0])

    def test_get_fundamentals_falls_back_to_pe_ttm_when_pe_ratio_is_missing(self):
        from jq_tushare_sdk.api.finance_tables import valuation
        from jq_tushare_sdk.api.query import query

        backend = FakeBackend()
        backend.frames["daily_basic"] = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20240103",
                    "total_mv": 500000.0,
                    "pe": None,
                    "pe_ttm": 18.5,
                    "pb": 1.2,
                    "turnover_rate": 1.5,
                }
            ]
        )
        portal = DataPortal(backend)

        df = portal.get_fundamentals(query(valuation.code, valuation.pe_ratio), date="2024-01-03")

        self.assertEqual(df["pe_ratio"].tolist(), [18.5])

    def test_get_fundamentals_defaults_missing_pe_ratio_to_zero_for_joinquant_compatibility(self):
        from jq_tushare_sdk.api.finance_tables import valuation
        from jq_tushare_sdk.api.query import query

        backend = FakeBackend()
        backend.frames["daily_basic"] = pd.DataFrame(
            [
                {
                    "ts_code": "300825.SZ",
                    "trade_date": "20240103",
                    "total_mv": 500000.0,
                    "pe": None,
                    "pe_ttm": None,
                    "pb": 3.2,
                    "turnover_rate": 1.5,
                }
            ]
        )
        portal = DataPortal(backend)

        df = portal.get_fundamentals(query(valuation.code, valuation.pe_ratio), date="2024-01-03")

        self.assertEqual(df.to_dict("records"), [{"code": "300825.XSHE", "pe_ratio": 0.0}])

    def test_get_fundamentals_converts_income_cumulative_period_to_single_quarter(self):
        from jq_tushare_sdk.api.finance_tables import income
        from jq_tushare_sdk.api.query import query

        backend = FakeBackend()
        backend.frames["income"] = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20250930",
                    "report_type": "1",
                    "total_revenue": 300.0,
                    "n_income_attr_p": 30.0,
                },
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20251231",
                    "report_type": "1",
                    "total_revenue": 460.0,
                    "n_income_attr_p": 55.0,
                },
            ]
        )
        portal = DataPortal(backend)

        df = portal.get_fundamentals(
            query(income.code, income.total_operating_revenue, income.np_parent_company_owners),
            statDate="2025q4",
        )

        self.assertEqual(df["code"].tolist(), ["000001.XSHE"])
        self.assertEqual(df["total_operating_revenue"].tolist(), [160.0])
        self.assertEqual(df["np_parent_company_owners"].tolist(), [25.0])

    def test_get_fundamentals_returns_empty_when_requested_income_period_is_not_announced(self):
        from jq_tushare_sdk.api.finance_tables import income
        from jq_tushare_sdk.api.query import query

        backend = FakeBackend()
        backend.frames["income"] = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20250630",
                    "ann_date": "20250820",
                    "f_ann_date": "20250820",
                    "report_type": "1",
                    "total_revenue": 200.0,
                    "n_income_attr_p": 20.0,
                },
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20250930",
                    "ann_date": "20251025",
                    "f_ann_date": "20251025",
                    "report_type": "1",
                    "total_revenue": 350.0,
                    "n_income_attr_p": 45.0,
                },
                {
                    "ts_code": "000002.SZ",
                    "end_date": "20250930",
                    "ann_date": "20251025",
                    "f_ann_date": "20260107",
                    "report_type": "1",
                    "total_revenue": 100.0,
                    "n_income_attr_p": 10.0,
                },
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20251231",
                    "ann_date": "20260117",
                    "f_ann_date": "20260117",
                    "report_type": "1",
                    "total_revenue": 520.0,
                    "n_income_attr_p": 70.0,
                },
            ]
        )
        portal = DataPortal(backend)

        df = portal.get_fundamentals(
            query(income.code, income.total_operating_revenue, income.np_parent_company_owners),
            date="2026-01-05",
            statDate="2025q4",
        )

        self.assertEqual(
            df.columns.tolist(),
            ["code", "total_operating_revenue", "np_parent_company_owners"],
        )
        self.assertTrue(df.empty)

    def test_get_fundamentals_uses_requested_income_period_after_announcement(self):
        from jq_tushare_sdk.api.finance_tables import income
        from jq_tushare_sdk.api.query import query

        backend = FakeBackend()
        backend.frames["income"] = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20250930",
                    "ann_date": "20251025",
                    "f_ann_date": "20251025",
                    "report_type": "1",
                    "total_revenue": 350.0,
                    "n_income_attr_p": 45.0,
                },
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20251231",
                    "ann_date": "20260117",
                    "f_ann_date": "20260117",
                    "report_type": "1",
                    "total_revenue": 520.0,
                    "n_income_attr_p": 70.0,
                },
            ]
        )
        portal = DataPortal(backend)

        df = portal.get_fundamentals(
            query(income.code, income.total_operating_revenue, income.np_parent_company_owners),
            date="2026-01-20",
            statDate="2025q4",
        )

        self.assertEqual(df["code"].tolist(), ["000001.XSHE"])
        self.assertEqual(df["total_operating_revenue"].tolist(), [170.0])
        self.assertEqual(df["np_parent_company_owners"].tolist(), [25.0])

    def test_get_fundamentals_applies_filters_ordering_and_field_projection(self):
        from jq_tushare_sdk.api.finance_tables import valuation
        from jq_tushare_sdk.api.query import query

        portal = DataPortal(FakeBackend())

        df = portal.get_fundamentals(
            query(valuation.code, valuation.market_cap)
            .filter(valuation.market_cap >= 60.0, valuation.code.in_(["600000.XSHG"]))
            .order_by(valuation.market_cap.desc()),
            date="2024-01-03",
        )

        self.assertEqual(df.columns.tolist(), ["code", "market_cap"])
        self.assertEqual(df.to_dict("records"), [{"code": "600000.XSHG", "market_cap": 90.0}])

    def test_get_fundamentals_returns_empty_when_optional_income_cache_is_missing(self):
        from jq_tushare_sdk.api.finance_tables import income, valuation
        from jq_tushare_sdk.api.query import query

        backend = FakeBackend()
        backend.frames["income"] = pd.DataFrame()
        portal = DataPortal(backend)

        df = portal.get_fundamentals(
            query(valuation.code, income.total_operating_revenue, income.np_parent_company_owners)
            .filter(income.np_parent_company_owners > -1e7),
            date="2024-01-03",
            statDate="2024q1",
        )

        self.assertEqual(
            df.columns.tolist(),
            ["code", "total_operating_revenue", "np_parent_company_owners"],
        )
        self.assertTrue(df.empty)

    def test_get_fundamentals_continuously_supports_turnover_ratio_history(self):
        from jq_tushare_sdk.api.finance_tables import valuation
        from jq_tushare_sdk.api.query import query

        portal = DataPortal(FakeBackend())

        result = portal.get_fundamentals_continuously(
            query(valuation.code, valuation.turnover_ratio).filter(
                valuation.code == "000001.XSHE",
            ),
            end_date="2024-01-05",
            count=2,
        )

        stock_frame = result.minor_xs("000001.XSHE")
        self.assertEqual(stock_frame.index.tolist(), ["2024-01-03", "2024-01-05"])
        self.assertEqual(stock_frame["turnover_ratio"].tolist(), [1.5, 2.1])

    def test_get_industry_returns_joinquant_style_mapping(self):
        portal = DataPortal(FakeBackend())

        result = portal.get_industry(["000001.XSHE"], date="2024-01-03")

        self.assertEqual(result["000001.XSHE"]["sw_l1"]["industry_name"], "银行")
        self.assertEqual(result["000001.XSHE"]["sw_l1"]["industry_code"], "银行")

    def test_get_industry_uses_sw_l1_membership_for_requested_date(self):
        backend = FakeBackend()
        backend.frames["index_classify"] = pd.DataFrame(
            [
                {"index_code": "801780.SI", "industry_name": "银行", "level": "L1"},
                {"index_code": "801790.SI", "industry_name": "非银金融", "level": "L1"},
            ]
        )
        backend.frames["index_member"] = pd.DataFrame(
            [
                {
                    "index_code": "801780.SI",
                    "con_code": "000001.SZ",
                    "in_date": "19910101",
                    "out_date": "20211210",
                    "is_new": "N",
                },
                {
                    "index_code": "801790.SI",
                    "con_code": "000001.SZ",
                    "in_date": "20211213",
                    "out_date": "",
                    "is_new": "Y",
                },
            ]
        )
        portal = DataPortal(backend)

        historical = portal.get_industry(["000001.XSHE"], date="2020-01-02")
        current = portal.get_industry(["000001.XSHE"], date="2024-01-02")
        latest = portal.get_industry(["000001.XSHE"])

        self.assertEqual(historical["000001.XSHE"]["sw_l1"]["industry_code"], "801780")
        self.assertEqual(historical["000001.XSHE"]["sw_l1"]["industry_name"], "银行")
        self.assertEqual(current["000001.XSHE"]["sw_l1"]["industry_code"], "801790")
        self.assertEqual(current["000001.XSHE"]["sw_l1"]["industry_name"], "非银金融")
        self.assertEqual(latest, current)

    def test_get_industry_rejects_malformed_stock_basic(self):
        backend = FakeBackend()
        backend.frames["stock_basic"] = pd.DataFrame([{"ts_code": "000001.SZ", "name": "平安银行"}])
        portal = DataPortal(backend)

        with self.assertRaises(NotImplementedError):
            portal.get_industry(["000001.XSHE"], date="2024-01-03")

    def test_readiness_reports_missing_api(self):
        config = BacktestConfig(
            strategy_path="strategy.py",
            start_date="2024-01-01",
            end_date="2024-01-31",
            initial_cash=1000000.0,
            cache_db="/tmp/cache.db",
        )
        issues = DataReadinessCheck(FakeBackend()).check_required(
            config,
            ["daily", "daily_basic"],
        )

        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0].api_name, "daily")
        self.assertIn("2024-01-01", issues[0].message)
        self.assertEqual(issues[0].update_requests[0].api_name, "daily")
        self.assertEqual(issues[0].update_requests[0].start_date, "20240101")
        self.assertEqual(issues[0].update_requests[0].end_date, "20240102")
        self.assertEqual(issues[1].api_name, "daily_basic")
        self.assertIn("update_data.py --api daily_basic", issues[1].suggestion)
        self.assertEqual(issues[1].update_requests[0].api_name, "daily_basic")
        self.assertEqual(issues[1].update_requests[0].start_date, "20240101")
        self.assertEqual(issues[1].update_requests[0].end_date, "20240131")

    def test_readiness_checks_price_lookback_and_adjustment_factor(self):
        class ShortHistoryBackend(FakeBackend):
            def status(self, api_name):
                if api_name in {"daily", "daily_basic", "adj_factor"}:
                    return {
                        "exists": True,
                        "record_count": 10,
                        "min_date": "20260415",
                        "max_date": "20260721",
                    }
                return super().status(api_name)

        with TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "lookback_strategy.py"
            strategy_path.write_text(
                "LOOKBACK = 84\n\ndef signal(context):\n    return get_price('000001.XSHE', count=LOOKBACK)\n",
                encoding="utf-8",
            )
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2026-06-18",
                end_date="2026-07-21",
                initial_cash=1000000.0,
                cache_db="/tmp/cache.db",
            )

            issues = DataReadinessCheck(ShortHistoryBackend()).check_required(
                config,
                ["daily", "daily_basic", "adj_factor"],
            )

        self.assertEqual([issue.api_name for issue in issues], ["daily", "adj_factor"])
        self.assertEqual(issues[0].update_requests[0].start_date, "20251002")
        self.assertEqual(issues[1].update_requests[0].start_date, "20251002")

    def test_readiness_reports_stock_basic_missing_historical_statuses(self):
        class ListedOnlyBackend(FakeBackend):
            def fetch(self, api_name, **params):
                if api_name == "stock_basic":
                    return pd.DataFrame(
                        [
                            {
                                "ts_code": "000001.SZ",
                                "name": "平安银行",
                                "list_status": "L",
                                "list_date": "19910403",
                            }
                        ]
                    )
                return super().fetch(api_name, **params)

            def status(self, api_name):
                if api_name == "stock_basic":
                    return {"exists": True, "record_count": 1}
                return super().status(api_name)

        config = BacktestConfig(
            strategy_path="strategy.py",
            start_date="2025-01-02",
            end_date="2025-12-02",
            initial_cash=1000000.0,
            cache_db="/tmp/cache.db",
        )

        issues = DataReadinessCheck(ListedOnlyBackend()).check_required(config, ["stock_basic"])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].api_name, "stock_basic")
        self.assertIn("list_status D", issues[0].message)
        self.assertEqual(issues[0].update_requests[0].api_name, "stock_basic")

    def test_readiness_reports_missing_strategy_benchmark_index_daily(self):
        with TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "benchmark_strategy.py"
            strategy_path.write_text(
                """
STOCK_POOL = "all"
BENCHMARK_BY_POOL = {"all": "000985.XSHG", "chinext": "399006.XSHE"}

def initialize(context):
    benchmark = BENCHMARK_BY_POOL.get(STOCK_POOL, "399006.XSHE")
    set_benchmark(benchmark)
""",
                encoding="utf-8",
            )
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2024-01-02",
                end_date="2024-01-03",
                initial_cash=1000000.0,
                cache_db="/tmp/cache.db",
                benchmark="399006.XSHE",
            )

            issues = DataReadinessCheck(FakeBackend()).check_required(config, ["index_daily"])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].api_name, "index_daily")
        self.assertIn("000985.XSHG", issues[0].message)
        self.assertIn("--ts-code 000985.CSI", issues[0].suggestion)
        self.assertEqual(issues[0].update_requests[0].api_name, "index_daily")
        self.assertEqual(dict(issues[0].update_requests[0].params), {"ts_code": "000985.CSI"})

    def test_readiness_infers_benchmark_from_process_initialize(self):
        with TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "process_benchmark_strategy.py"
            strategy_path.write_text(
                """
def initialize(context):
    pass

def process_initialize(context):
    apply_platform_settings()

def apply_platform_settings():
    set_benchmark("000985.XSHG")
""",
                encoding="utf-8",
            )
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2024-01-02",
                end_date="2024-01-03",
                initial_cash=1000000.0,
                cache_db="/tmp/cache.db",
                benchmark="399006.XSHE",
            )

            issues = DataReadinessCheck(FakeBackend()).check_required(config, ["index_daily"])

        self.assertEqual(len(issues), 1)
        self.assertIn("000985.XSHG", issues[0].message)
        self.assertEqual(dict(issues[0].update_requests[0].params), {"ts_code": "000985.CSI"})

    def test_readiness_reports_missing_sw_industry_daily_history(self):
        class MissingOneSwIndexBackend(FakeBackend):
            def fetch(self, api_name, **params):
                if api_name == "index_daily":
                    return pd.DataFrame(
                        [
                            {"ts_code": params.get("ts_code"), "trade_date": "20230101"},
                            {"ts_code": params.get("ts_code"), "trade_date": "20241231"},
                        ]
                    )
                if api_name == "sw_daily":
                    if params.get("ts_code") == "801010.SI":
                        return pd.DataFrame(
                            [
                                {"ts_code": "801010.SI", "trade_date": "20230101"},
                                {"ts_code": "801010.SI", "trade_date": "20241231"},
                            ]
                        )
                    return pd.DataFrame()
                return super().fetch(api_name, **params)

        with TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "sw_strategy.py"
            strategy_path.write_text(
                '''
SW_INDEXES = ("801010.XSHG", "801030.XSHG")

def score_industries(context):
    return get_price(SW_INDEXES, end_date=context.current_dt, count=45, fields=["close"], panel=False)
''',
                encoding="utf-8",
            )
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2024-01-02",
                end_date="2024-01-03",
                initial_cash=1000000.0,
                cache_db="/tmp/cache.db",
            )

            issues = DataReadinessCheck(MissingOneSwIndexBackend()).check_required(config, ["index_daily"])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].api_name, "sw_daily")
        self.assertIn("801030.XSHG", issues[0].message)
        requests = issues[0].update_requests
        self.assertTrue(all(request.api_name == "sw_daily" for request in requests))
        self.assertIn({"ts_code": "801030.SI"}, [dict(request.params) for request in requests])

    def test_readiness_looks_back_for_missing_start_index_weight_snapshot(self):
        class MissingStartIndexWeightBackend(FakeBackend):
            def fetch(self, api_name, **params):
                if api_name == "index_weight":
                    return pd.DataFrame()
                return super().fetch(api_name, **params)

        with TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "index_weight_strategy.py"
            strategy_path.write_text(
                """
INDEX_CODE = "399006.XSHE"

def select_stock(context):
    return get_index_stocks(INDEX_CODE)
""",
                encoding="utf-8",
            )
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2025-01-02",
                end_date="2025-12-02",
                initial_cash=1000000.0,
                cache_db="/tmp/cache.db",
            )

            issues = DataReadinessCheck(MissingStartIndexWeightBackend()).check_required(config, ["index_weight"])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].api_name, "index_weight")
        request = issues[0].update_requests[0]
        self.assertEqual(request.api_name, "index_weight")
        self.assertEqual(request.start_date, "20231229")
        self.assertEqual(request.end_date, "20251202")
        self.assertEqual(dict(request.params), {"index_code": "399006.SZ"})

    def test_readiness_reports_partial_fund_daily_coverage(self):
        class FundBackend(FakeBackend):
            def fetch(self, api_name, **params):
                if api_name == "fund_daily":
                    return pd.DataFrame(
                        [
                            {
                                "ts_code": params["ts_code"],
                                "trade_date": "20260602",
                                "close": 4.0,
                            }
                        ]
                    )
                return super().fetch(api_name, **params)

        with TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "etf_strategy.py"
            strategy_path.write_text(
                """
def initialize(context):
    g.pool = ["510300.XSHG"]
""",
                encoding="utf-8",
            )
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2026-01-01",
                end_date="2026-07-02",
                initial_cash=1000000.0,
                cache_db="/tmp/cache.db",
            )

            issues = DataReadinessCheck(FundBackend()).check_required(config, [])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].api_name, "fund_daily")
        self.assertIn("510300.XSHG", issues[0].message)
        self.assertEqual(len(issues[0].update_requests), 3)
        first_request = issues[0].update_requests[0]
        self.assertEqual(first_request.api_name, "fund_daily")
        self.assertEqual(first_request.start_date, "20260101")
        self.assertEqual(first_request.end_date, "20260602")
        self.assertEqual(dict(first_request.params), {"ts_code": "510300.SH"})
        adj_request = issues[0].update_requests[2]
        self.assertEqual(adj_request.api_name, "fund_adj")
        self.assertEqual(adj_request.start_date, "20260101")
        self.assertEqual(adj_request.end_date, "20260702")
        self.assertEqual(dict(adj_request.params), {"ts_code": "510300.SH"})

    def test_readiness_extends_fund_daily_for_get_price_count_lookback(self):
        class LookbackFundBackend(FakeBackend):
            def fetch(self, api_name, **params):
                if api_name == "trade_cal":
                    return pd.DataFrame(
                        [
                            {"exchange": "SSE", "cal_date": "20250628", "is_open": 0},
                            {"exchange": "SSE", "cal_date": "20250630", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20260105", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20260106", "is_open": 1},
                        ]
                    )
                if api_name == "fund_daily":
                    return pd.DataFrame()
                return super().fetch(api_name, **params)

        with TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "etf_lookback_strategy.py"
            strategy_path.write_text(
                """
def set_params(context):
    context.vol_period = 60

def get_volatility(context):
    return get_price("510300.XSHG", count=context.vol_period, fields=["close"], panel=False)
""",
                encoding="utf-8",
            )
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2026-01-01",
                end_date="2026-01-06",
                initial_cash=1000000.0,
                cache_db="/tmp/cache.db",
            )

            issues = DataReadinessCheck(LookbackFundBackend()).check_required(config, [])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].api_name, "fund_daily")
        self.assertEqual(issues[0].update_requests[0].start_date, "20250630")
        self.assertEqual(issues[0].update_requests[0].end_date, "20260106")
        self.assertEqual(issues[0].update_requests[1].api_name, "fund_adj")

    def test_readiness_extends_fund_daily_for_attribute_history_lookback(self):
        class AttributeHistoryBackend(FakeBackend):
            def fetch(self, api_name, **params):
                if api_name == "trade_cal":
                    return pd.DataFrame(
                        [
                            {"exchange": "SSE", "cal_date": "20250628", "is_open": 0},
                            {"exchange": "SSE", "cal_date": "20250630", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20260105", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20260106", "is_open": 1},
                        ]
                    )
                if api_name == "fund_daily":
                    return pd.DataFrame()
                return super().fetch(api_name, **params)

        with TemporaryDirectory() as tmp:
            strategy_path = Path(tmp) / "etf_attribute_history_strategy.py"
            strategy_path.write_text(
                """
LONG_WINDOW = 60

def get_signal(context):
    return attribute_history("510300.XSHG", LONG_WINDOW, fields=["close"])
""",
                encoding="utf-8",
            )
            config = BacktestConfig(
                strategy_path=str(strategy_path),
                start_date="2026-01-01",
                end_date="2026-01-06",
                initial_cash=1000000.0,
                cache_db="/tmp/cache.db",
            )

            issues = DataReadinessCheck(AttributeHistoryBackend()).check_required(config, [])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].api_name, "fund_daily")
        self.assertEqual(issues[0].update_requests[0].api_name, "fund_daily")
        self.assertEqual(issues[0].update_requests[0].start_date, "20250630")
        self.assertEqual(issues[0].update_requests[0].end_date, "20260106")
        self.assertEqual(issues[0].update_requests[1].api_name, "fund_adj")

    def test_update_missing_data_runs_structured_update_requests_once(self):
        calls = []

        class UpdateBackend:
            def update_data(self, api_name, start_date=None, end_date=None, **params):
                calls.append((api_name, start_date, end_date, params))
                return 7

        issue = DataReadinessCheck(FakeBackend()).check_required(
            BacktestConfig(
                strategy_path="strategy.py",
                start_date="2024-01-01",
                end_date="2024-01-31",
                initial_cash=1000000.0,
                cache_db="/tmp/cache.db",
            ),
            ["daily"],
        )[0]

        counts = update_missing_data(UpdateBackend(), [issue, issue])

        self.assertEqual(calls, [("daily", "20240101", "20240102", {})])
        self.assertEqual(counts, {"daily 20240101-20240102": 7})

    def test_readiness_accepts_first_open_day_after_requested_start(self):
        class HolidayStartBackend(FakeBackend):
            def status(self, api_name):
                if api_name == "daily":
                    return {
                        "exists": True,
                        "record_count": 2,
                        "min_date": "20260105",
                        "max_date": "20260106",
                    }
                return super().status(api_name)

            def fetch(self, api_name, **params):
                if api_name == "trade_cal":
                    return pd.DataFrame(
                        [
                            {"exchange": "SSE", "cal_date": "20260101", "is_open": 0},
                            {"exchange": "SSE", "cal_date": "20260102", "is_open": 0},
                            {"exchange": "SSE", "cal_date": "20260105", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20260106", "is_open": 1},
                        ]
                    )
                return super().fetch(api_name, **params)

        config = BacktestConfig(
            strategy_path="strategy.py",
            start_date="2026-01-01",
            end_date="2026-01-06",
            initial_cash=1000000.0,
            cache_db="/tmp/cache.db",
        )

        issues = DataReadinessCheck(HolidayStartBackend()).check_required(config, ["daily"])

        self.assertEqual(issues, [])

    def test_readiness_warns_on_holiday_short_factor_window(self):
        class HolidayGapBackend(FakeBackend):
            def fetch(self, api_name, **params):
                if api_name == "trade_cal":
                    return pd.DataFrame(
                        [
                            {"exchange": "SSE", "cal_date": "20250924", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20250925", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20250926", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20250929", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20250930", "is_open": 1},
                            # 国庆长假：10-01 至 10-08 休市
                            {"exchange": "SSE", "cal_date": "20251009", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251010", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251013", "is_open": 1},
                        ]
                    )
                return super().fetch(api_name, **params)

        config = BacktestConfig(
            strategy_path="strategy.py",
            start_date="2025-10-09",
            end_date="2025-10-17",
            initial_cash=1000000.0,
            cache_db="/tmp/cache.db",
        )

        issues = DataReadinessCheck(HolidayGapBackend()).check_required(config, ["daily"])

        advisories = [issue for issue in issues if issue.advisory]
        self.assertEqual(len(advisories), 1)
        self.assertEqual(advisories[0].api_name, "trade_cal")
        self.assertIn("2025-10-10", advisories[0].message)
        self.assertIn("1 个交易日", advisories[0].message)
        self.assertEqual(advisories[0].update_requests, ())

    def test_readiness_does_not_warn_without_holiday_gap(self):
        class NormalBackend(FakeBackend):
            def fetch(self, api_name, **params):
                if api_name == "trade_cal":
                    return pd.DataFrame(
                        [
                            {"exchange": "SSE", "cal_date": "20251006", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251007", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251008", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251009", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251010", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251013", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251014", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251015", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251016", "is_open": 1},
                            {"exchange": "SSE", "cal_date": "20251017", "is_open": 1},
                        ]
                    )
                return super().fetch(api_name, **params)

        config = BacktestConfig(
            strategy_path="strategy.py",
            start_date="2025-10-09",
            end_date="2025-10-17",
            initial_cash=1000000.0,
            cache_db="/tmp/cache.db",
        )

        issues = DataReadinessCheck(NormalBackend()).check_required(config, ["daily"])

        self.assertEqual([issue for issue in issues if issue.advisory], [])

    def test_readiness_accepts_consecutive_income_quarters_for_backtest_window(self):
        class IncomeBackend:
            def status(self, api_name):
                self.api_name = api_name
                return {
                    "exists": True,
                    "record_count": 240,
                    "min_date": "20251231",
                    "max_date": "20260331",
                }

            def fetch(self, api_name, **params):
                if api_name == "income" and params.get("period") in {
                    "2026q1",
                    "2025q4",
                }:
                    return pd.DataFrame(
                        [
                            {
                                "ts_code": f"{index:06d}.SZ",
                                "end_date": "20260331" if params.get("period") == "2026q1" else "20251231",
                                "report_type": "1",
                            }
                            for index in range(120)
                        ]
                    )
                return pd.DataFrame()

        config = BacktestConfig(
            strategy_path="strategy.py",
            start_date="2026-06-01",
            end_date="2026-06-30",
            initial_cash=1000000.0,
            cache_db="/tmp/cache.db",
        )

        issues = DataReadinessCheck(IncomeBackend()).check_required(config, ["income"])

        self.assertEqual(issues, [])

    def test_income_period_requirements_follow_reporting_deadlines(self):
        self.assertEqual(
            required_income_periods("2025-10-30", "2025-10-30"),
            ["2025q2", "2025q1"],
        )
        self.assertEqual(
            required_income_periods("2025-10-31", "2025-10-31"),
            ["2025q2", "2025q1"],
        )
        self.assertEqual(
            required_income_periods("2025-11-01", "2025-11-01"),
            ["2025q3", "2025q2"],
        )

    def test_readiness_reports_only_missing_income_comparison_quarter(self):
        class IncomeBackend:
            def status(self, api_name):
                return {
                    "exists": True,
                    "record_count": 240,
                    "min_date": "20250630",
                    "max_date": "20250930",
                }

            def fetch(self, api_name, **params):
                if api_name == "income" and params.get("period") in {"2025q3", "2025q2"}:
                    return pd.DataFrame(
                        [
                            {
                                "ts_code": f"{index:06d}.SZ",
                                "end_date": "20250930" if params.get("period") == "2025q3" else "20250630",
                                "report_type": "1",
                            }
                            for index in range(120)
                        ]
                    )
                return pd.DataFrame()

        config = BacktestConfig(
            strategy_path="strategy.py",
            start_date="2025-10-09",
            end_date="2025-12-30",
            initial_cash=1000000.0,
            cache_db="/tmp/cache.db",
        )

        issues = DataReadinessCheck(IncomeBackend()).check_required(config, ["income"])

        self.assertEqual(len(issues), 1)
        self.assertIn("2025q1", issues[0].message)
        self.assertEqual(
            issues[0].update_requests,
            (
                DataUpdateRequest(
                    api_name="income",
                    params=(("period", "2025q1"),),
                ),
            ),
        )

    def test_readiness_refills_sparse_income_quarter(self):
        class IncomeBackend:
            def status(self, api_name):
                return {
                    "exists": True,
                    "record_count": 245,
                    "min_date": "20251231",
                    "max_date": "20260331",
                }

            def fetch(self, api_name, **params):
                if api_name != "income":
                    return pd.DataFrame()
                period = params.get("period")
                count = 5 if period == "2025q4" else 120
                return pd.DataFrame(
                    [
                        {
                            "ts_code": f"{index:06d}.SZ",
                            "end_date": "20251231" if period == "2025q4" else "20260331",
                            "report_type": "1",
                        }
                        for index in range(count)
                    ]
                )

        config = BacktestConfig(
            strategy_path="strategy.py",
            start_date="2026-06-01",
            end_date="2026-06-30",
            initial_cash=1000000.0,
            cache_db="/tmp/cache.db",
        )

        issues = DataReadinessCheck(IncomeBackend()).check_required(config, ["income"])

        self.assertEqual(len(issues), 1)
        self.assertIn("2025q4 (5 symbols", issues[0].message)
        self.assertEqual(
            issues[0].update_requests,
            (
                DataUpdateRequest(
                    api_name="income",
                    params=(("period", "2025q4"),),
                ),
            ),
        )

    def test_readiness_compares_income_coverage_with_adjacent_quarters(self):
        periods = required_income_periods("2025-01-02", "2026-07-31")
        counts = {
            period: 100 + (len(periods) - index - 1) * 10
            for index, period in enumerate(periods)
        }

        class IncomeBackend:
            def status(self, api_name):
                return {
                    "exists": True,
                    "record_count": sum(counts.values()),
                    "min_date": "20240630",
                    "max_date": "20260331",
                }

            def fetch(self, api_name, **params):
                if api_name != "income":
                    return pd.DataFrame()
                period = params.get("period")
                return pd.DataFrame(
                    [
                        {
                            "ts_code": f"{index:06d}.SZ",
                            "end_date": period,
                            "report_type": "1",
                        }
                        for index in range(counts.get(period, 0))
                    ]
                )

        config = BacktestConfig(
            strategy_path="strategy.py",
            start_date="2025-01-02",
            end_date="2026-07-31",
            initial_cash=1000000.0,
            cache_db="/tmp/cache.db",
        )

        issues = DataReadinessCheck(IncomeBackend()).check_required(config, ["income"])

        self.assertEqual(issues, [])

    def test_cache_backend_uses_project_local_sqlite_without_join_tushare_package(self):
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "data" / "jq_tushare_cache.db"
            backend = TushareCacheBackend(str(cache_path), cache_mode="strict_local")

            inserted = backend.cache_data(
                "daily",
                pd.DataFrame(
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
                        }
                    ]
                ),
            )
            frame = backend.fetch(
                "daily",
                ts_code="000001.SZ",
                start_date="20240101",
                end_date="20240131",
            )
            status = backend.status("daily")

        self.assertEqual(inserted, 1)
        self.assertEqual(frame["close"].tolist(), [10.5])
        self.assertEqual(status["record_count"], 1)
        self.assertEqual(status["min_date"], "20240102")
        self.assertEqual(status["max_date"], "20240102")

    def test_cache_backend_fetches_stock_basic_without_invalid_order_sql(self):
        with TemporaryDirectory() as tmp:
            backend = TushareCacheBackend(str(Path(tmp) / "data" / "jq_tushare_cache.db"))
            backend.cache_data(
                "stock_basic",
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行"},
                        {"ts_code": "600000.SH", "name": "浦发银行", "industry": "银行"},
                    ]
                ),
            )

            frame = backend.fetch("stock_basic")

        self.assertEqual(frame["ts_code"].tolist(), ["000001.SZ", "600000.SH"])

    def test_cache_backend_fetches_latest_price_per_security(self):
        with TemporaryDirectory() as tmp:
            backend = TushareCacheBackend(str(Path(tmp) / "data" / "jq_tushare_cache.db"))
            backend.cache_data(
                "daily",
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": "20240102", "close": 10.0},
                        {"ts_code": "000001.SZ", "trade_date": "20240103", "close": 10.2},
                        {"ts_code": "600000.SH", "trade_date": "20240102", "close": 8.0},
                        {"ts_code": "600000.SH", "trade_date": "20240104", "close": 8.3},
                    ]
                ),
            )

            frame = backend.fetch(
                "daily",
                ts_code="000001.SZ,600000.SH",
                end_date="20240103",
                latest_per_code=True,
            )

        self.assertEqual(
            frame[["ts_code", "trade_date", "close"]].to_dict("records"),
            [
                {"ts_code": "600000.SH", "trade_date": "20240102", "close": 8.0},
                {"ts_code": "000001.SZ", "trade_date": "20240103", "close": 10.2},
            ],
        )

    def test_cache_backend_fetches_latest_active_prices_per_security(self):
        with TemporaryDirectory() as tmp:
            backend = TushareCacheBackend(str(Path(tmp) / "data" / "jq_tushare_cache.db"))
            backend.cache_data(
                "daily",
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": "20240101", "close": 9.8, "vol": 100.0},
                        {"ts_code": "000001.SZ", "trade_date": "20240102", "close": 10.0, "vol": 200.0},
                        {"ts_code": "000001.SZ", "trade_date": "20240103", "close": 10.0, "vol": 0.0},
                        {"ts_code": "000001.SZ", "trade_date": "20240104", "close": 10.0, "vol": 0.0},
                    ]
                ),
            )

            frame = backend.fetch(
                "daily",
                ts_code="000001.SZ",
                end_date="20240104",
                positive_volume=True,
                limit_per_code=2,
            )

        self.assertEqual(frame["trade_date"].tolist(), ["20240101", "20240102"])

    def test_skip_paused_count_adjusts_to_requested_end_factor(self):
        with TemporaryDirectory() as tmp:
            backend = TushareCacheBackend(str(Path(tmp) / "data" / "jq_tushare_cache.db"))
            backend.cache_data(
                "daily",
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": "20240101", "close": 10.0, "vol": 100.0},
                        {"ts_code": "000001.SZ", "trade_date": "20240103", "close": 10.0, "vol": 0.0},
                    ]
                ),
            )
            backend.cache_data(
                "adj_factor",
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": "20240101", "adj_factor": 1.0},
                        {"ts_code": "000001.SZ", "trade_date": "20240103", "adj_factor": 2.0},
                    ]
                ),
            )
            portal = DataPortal(backend)

            frame = portal.get_price(
                "000001.XSHE",
                end_date="20240103",
                count=1,
                fields="close",
                fq="pre",
                skip_paused=True,
                fill_paused=True,
            )

        self.assertEqual(frame["close"].tolist(), [5.0])

    def test_price_seed_adjustment_uses_each_security_seed_date(self):
        backend = FakeBackend()
        backend.frames["adj_factor"] = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_date": "20240101", "adj_factor": 1.0},
                {"ts_code": "000001.SZ", "trade_date": "20240103", "adj_factor": 2.0},
                {"ts_code": "000001.SZ", "trade_date": "20240105", "adj_factor": 4.0},
                {"ts_code": "600000.SH", "trade_date": "20240103", "adj_factor": 3.0},
                {"ts_code": "600000.SH", "trade_date": "20240105", "adj_factor": 6.0},
            ]
        )
        portal = DataPortal(backend)
        seed = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_date": "20240101", "close": 10.0},
                {"ts_code": "600000.SH", "trade_date": "20240103", "close": 20.0},
            ]
        )

        adjusted = portal._adjust_price_seed_to_end(seed, api_name="daily", end_date="20240105")

        self.assertEqual(adjusted["close"].tolist(), [2.5, 10.0])

    def test_cache_backend_update_data_fetches_all_stock_basic_statuses(self):
        pro = Mock()

        def stock_basic(**kwargs):
            status = kwargs.get("list_status")
            code_by_status = {"L": "000001.SZ", "D": "300344.SZ", "P": "001001.SZ"}
            return pd.DataFrame(
                [
                    {
                        "ts_code": code_by_status[status],
                        "name": f"{status}示例",
                        "list_date": "20240101",
                    }
                ]
            )

        pro.stock_basic.side_effect = stock_basic

        with TemporaryDirectory() as tmp, patch("tushare.pro_api", return_value=pro):
            backend = TushareCacheBackend(
                str(Path(tmp) / "data" / "jq_tushare_cache.db"),
                token="placeholder",
                cache_mode="strict_local",
            )
            count = backend.update_data("stock_basic")
            frame = backend.fetch("stock_basic")

        requested_statuses = [call.kwargs.get("list_status") for call in pro.stock_basic.mock_calls]
        self.assertEqual(requested_statuses, ["L", "D", "P"])
        self.assertEqual(count, 3)
        self.assertEqual(set(frame["list_status"].tolist()), {"L", "D", "P"})

    def test_cache_backend_update_data_uses_tushare_client_and_upserts(self):
        pro = Mock()
        pro.daily.return_value = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20240102",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "pre_close": 10.0,
                    "change": 0.1,
                    "pct_chg": 1.0,
                    "vol": 100.0,
                    "amount": 1010.0,
                }
            ]
        )

        with TemporaryDirectory() as tmp, patch("tushare.pro_api", return_value=pro):
            backend = TushareCacheBackend(
                str(Path(tmp) / "data" / "jq_tushare_cache.db"),
                token="placeholder",
                cache_mode="strict_local",
            )
            count = backend.update_data("daily", trade_date="20240102")
            frame = backend.fetch("daily", trade_date="20240102")

        self.assertEqual(count, 1)
        self.assertEqual(frame["close"].tolist(), [10.1])
        pro.daily.assert_called_once_with(trade_date="20240102")

    def test_cache_backend_update_data_supports_adj_factor(self):
        pro = Mock()
        pro.adj_factor.return_value = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20240102",
                    "adj_factor": 1.23,
                }
            ]
        )

        with TemporaryDirectory() as tmp, patch("tushare.pro_api", return_value=pro):
            backend = TushareCacheBackend(
                str(Path(tmp) / "data" / "jq_tushare_cache.db"),
                token="placeholder",
                cache_mode="strict_local",
            )
            count = backend.update_data("adj_factor", trade_date="20240102")
            frame = backend.fetch("adj_factor", trade_date="20240102")

        self.assertEqual(count, 1)
        self.assertEqual(frame["adj_factor"].tolist(), [1.23])
        pro.adj_factor.assert_called_once_with(trade_date="20240102")

    def test_cache_backend_updates_fund_adj_by_fund_code_range(self):
        pro = Mock()
        pro.fund_adj.return_value = pd.DataFrame(
            [
                {
                    "ts_code": "510300.SH",
                    "trade_date": "20240102",
                    "adj_factor": 1.25,
                }
            ]
        )

        tushare_module = Mock()
        tushare_module.pro_api.return_value = pro
        with TemporaryDirectory() as tmp, patch.dict("sys.modules", {"tushare": tushare_module}):
            backend = TushareCacheBackend(
                str(Path(tmp) / "data" / "jq_tushare_cache.db"),
                token="placeholder",
                cache_mode="strict_local",
            )
            count = backend.update_data(
                "fund_adj",
                start_date="20240101",
                end_date="20240131",
                ts_code="510300.SH",
            )
            frame = backend.fetch("fund_adj", ts_code="510300.SH")

        self.assertEqual(count, 1)
        self.assertEqual(frame["adj_factor"].tolist(), [1.25])
        pro.fund_adj.assert_called_once_with(
            ts_code="510300.SH",
            start_date="20240101",
            end_date="20240131",
        )

    def test_cache_backend_updates_income_by_quarter_periods_for_date_range(self):
        pro = Mock()
        pro.income_vip.return_value = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20260331",
                    "report_type": "1",
                    "total_revenue": 100.0,
                    "n_income_attr_p": 10.0,
                }
            ]
        )

        with TemporaryDirectory() as tmp, patch("tushare.pro_api", return_value=pro):
            backend = TushareCacheBackend(
                str(Path(tmp) / "data" / "jq_tushare_cache.db"),
                token="placeholder",
                cache_mode="strict_local",
            )
            count = backend.update_data("income", start_date="20260601", end_date="20260630")
            frame = backend.fetch("income", period="2026q1")

        self.assertGreaterEqual(count, 1)
        self.assertEqual(frame["n_income_attr_p"].tolist(), [10.0])
        self.assertIn(
            "20260331",
            [call.kwargs.get("period") for call in pro.income_vip.mock_calls],
        )
        self.assertEqual(
            {
                call.kwargs.get("period")
                for call in pro.income_vip.mock_calls
            },
            {"20260331", "20251231"},
        )

    def test_cache_backend_updates_sw_members_through_index_member_all(self):
        pro = Mock()
        pro.index_member_all.side_effect = [
            pd.DataFrame(
                [
                    {
                        "l1_code": "801780.SI",
                        "ts_code": "000001.SZ",
                        "in_date": "20211213",
                        "out_date": "",
                        "is_new": "Y",
                    }
                ]
            ),
            pd.DataFrame(
                [
                    {
                        "l1_code": "801780.SI",
                        "ts_code": "000002.SZ",
                        "in_date": "20000101",
                        "out_date": "20211210",
                        "is_new": "N",
                    }
                ]
            ),
        ]

        with TemporaryDirectory() as tmp, patch("tushare.pro_api", return_value=pro):
            backend = TushareCacheBackend(
                str(Path(tmp) / "data" / "jq_tushare_cache.db"),
                token="placeholder",
                cache_mode="strict_local",
            )
            count = backend.update_data("index_member", index_code="801780.SI")
            frame = backend.fetch("index_member", index_code="801780.SI")

        self.assertEqual(count, 2)
        self.assertEqual(frame["con_code"].tolist(), ["000001.SZ", "000002.SZ"])
        self.assertEqual(
            [call.kwargs for call in pro.index_member_all.mock_calls],
            [
                {"l1_code": "801780.SI", "is_new": "Y"},
                {"l1_code": "801780.SI", "is_new": "N"},
            ],
        )

    def test_cache_backend_income_periods_cover_long_backtest_start(self):
        with TemporaryDirectory() as tmp:
            backend = TushareCacheBackend(
                str(Path(tmp) / "data" / "jq_tushare_cache.db"),
                cache_mode="strict_local",
            )

            periods = backend._income_periods_for_range("20250102", "20260731")

        self.assertEqual(periods[0], "20260331")
        self.assertEqual(periods[-1], "20240630")
        self.assertEqual(len(periods), 8)

    def test_cache_backend_fetch_income_period_uses_exact_quarter(self):
        with TemporaryDirectory() as tmp:
            backend = TushareCacheBackend(
                str(Path(tmp) / "data" / "jq_tushare_cache.db"),
                cache_mode="strict_local",
            )
            backend.cache_data(
                "income",
                pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "end_date": "20251231",
                            "report_type": "1",
                            "total_revenue": 90.0,
                            "n_income_attr_p": 9.0,
                        },
                        {
                            "ts_code": "000001.SZ",
                            "end_date": "20260331",
                            "report_type": "1",
                            "total_revenue": 100.0,
                            "n_income_attr_p": 10.0,
                        },
                    ]
                ),
            )

            frame = backend.fetch("income", period="2026q1")

        self.assertEqual(frame["end_date"].tolist(), ["20260331"])
        self.assertEqual(frame["total_revenue"].tolist(), [100.0])

    def test_cache_backend_default_update_set_includes_income(self):
        from jq_tushare_sdk.adapters.tushare import cache_backend as cache_backend_module

        self.assertIn("income", cache_backend_module._DEFAULT_UPDATE_APIS)
        self.assertIn("adj_factor", cache_backend_module._DEFAULT_UPDATE_APIS)

    def test_cache_backend_does_not_depend_on_legacy_join_tushare_package_path(self):
        with TemporaryDirectory() as tmp:
            legacy_path = Path(tmp) / "join_tushare"
            (legacy_path / "tushare_cache").mkdir(parents=True)
            cache_path = Path(tmp) / "project" / "data" / "jq_tushare_cache.db"
            backend = TushareCacheBackend(
                cache_db=str(cache_path),
                package_path=str(legacy_path),
            )
            backend.cache_data(
                "daily",
                pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "trade_date": "20240102",
                            "open": 10.0,
                            "high": 10.0,
                            "low": 10.0,
                            "close": 10.0,
                            "vol": 100.0,
                            "amount": 1000.0,
                        }
                    ]
                ),
            )

            status = backend.status("daily")
            data = backend.fetch("daily", ts_code="000001.SZ")

        self.assertEqual(status["record_count"], 1)
        self.assertEqual(data.iloc[0]["ts_code"], "000001.SZ")

    def test_cache_backend_rejects_legacy_package_resolution(self):
        with self.assertRaisesRegex(ImportError, "self-contained"):
            TushareCacheBackend.resolve_package_path()


if __name__ == "__main__":
    unittest.main()
