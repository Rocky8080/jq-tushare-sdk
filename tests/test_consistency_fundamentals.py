import unittest
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd

from jq_tushare_sdk.api.finance_tables import income, indicator, valuation
from jq_tushare_sdk.api.jqdata import get_fundamentals, get_fundamentals_continuously, set_runtime_state
from jq_tushare_sdk.api.query import query
from jq_tushare_sdk.data.code_map import is_tushare_a_share_code, to_joinquant_code, to_tushare_code
from jq_tushare_sdk.data.fundamentals_contract import FundamentalsUnavailableError
from jq_tushare_sdk.data.portal import DataPortal
from jq_tushare_sdk.runtime.globals_state import RuntimeState


class Backend:
    strict_pit = False

    def __init__(self):
        self.calls = []
        dates = pd.date_range("2025-01-01", "2026-10-10")
        self.frames = {
            "trade_cal": pd.DataFrame({"cal_date": [day.strftime("%Y%m%d") for day in dates], "is_open": [int(day.weekday() < 5 and not ("20261001" <= day.strftime("%Y%m%d") <= "20261007")) for day in dates]}),
            "income": pd.DataFrame(),
            "daily_basic": pd.DataFrame(),
            "stock_basic": pd.DataFrame(),
        }

    def fetch(self, api_name, **params):
        self.calls.append((api_name, dict(params)))
        frame = self.frames.get(api_name, pd.DataFrame()).copy()
        day_column = "cal_date" if api_name == "trade_cal" else "trade_date"
        if day_column in frame:
            for parameter, operator in (("start_date", "ge"), ("end_date", "le"), ("trade_date", "eq")):
                if params.get(parameter) is not None:
                    frame = frame[getattr(frame[day_column], operator)(str(params[parameter]).replace("-", ""))]
        return frame.reset_index(drop=True)


class ContractPortal(DataPortal):
    """Isolate join/request tests from vendor PE/financial revision mapping."""
    def _valuation_frame(self, day):
        self.last_valuation_date = str(day)
        frame = self._fetch("daily_basic", end_date=day)
        if frame.empty:
            return frame
        frame = self._latest_per_security(frame).rename(columns={"ts_code": "code", "total_mv": "market_cap"})
        frame["code"] = frame["code"].map(to_joinquant_code)
        return frame

    def _load_income_raw(self, day=None):
        return self._fetch("income")


class FundamentalsContractTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.backend.frames["daily_basic"] = pd.DataFrame([
            {"ts_code": "000001.SZ", "trade_date": "20260630", "total_mv": 10.0},
            {"ts_code": "000001.SZ", "trade_date": "20260928", "total_mv": 20.0},
            {"ts_code": "000001.SZ", "trade_date": "20260929", "total_mv": 30.0},
            {"ts_code": "000001.SZ", "trade_date": "20260930", "total_mv": 40.0},
        ])
        self.portal = ContractPortal(self.backend)
        self.q = query(valuation.code, valuation.market_cap)

    def runtime(self, timestamp):
        set_runtime_state(RuntimeState(data_portal=self.portal, context=SimpleNamespace(current_dt=datetime.fromisoformat(timestamp))))

    def test_default_snapshot_is_previous_completed_day_at_all_intraday_times(self):
        for timestamp in ("2026-09-29 09:30:00", "2026-09-29 10:00:00", "2026-09-29 15:30:00"):
            self.runtime(timestamp)
            frame = get_fundamentals(self.q)
            self.assertEqual(frame["market_cap"].tolist(), [20.0])
            self.assertEqual(self.portal.last_valuation_date, "20260928")

    def test_holiday_default_uses_calendar(self):
        self.runtime("2026-10-08 09:30:00")
        get_fundamentals(self.q)
        self.assertEqual(self.portal.last_valuation_date, "20260930")

    def test_explicit_same_day_does_not_silently_shift_or_fetch_eod(self):
        self.runtime("2026-09-29 09:30:00")
        with self.assertRaises(FundamentalsUnavailableError) as caught:
            get_fundamentals(self.q, date="2026-09-29")
        self.assertEqual(caught.exception.reason, "daily_snapshot_not_yet_available")
        self.assertEqual(caught.exception.details["effective_date"], "20260929")
        self.assertFalse(any(api == "daily_basic" for api, params in self.backend.calls))

    def test_explicit_future_daily_snapshot_rejected(self):
        self.runtime("2026-09-29 09:30:00")
        with self.assertRaises(FundamentalsUnavailableError):
            get_fundamentals(self.q, date="2026-09-30")

    def test_continuous_fundamentals_cannot_read_same_day_eod(self):
        self.runtime("2026-09-29 10:00:00")
        with self.assertRaises(FundamentalsUnavailableError):
            get_fundamentals_continuously(query(valuation.code, valuation.turnover_ratio), end_date="2026-09-29")

    def test_explicit_old_date_preserved(self):
        self.runtime("2026-09-29 10:00:00")
        frame = get_fundamentals(self.q, date="2026-06-30")
        self.assertEqual(frame["market_cap"].tolist(), [10.0])
        self.assertEqual(self.portal.last_valuation_date, "20260630")

    def test_date_and_statdate_exclusive_at_api_and_portal(self):
        self.runtime("2026-09-29 10:00:00")
        for invoke in (get_fundamentals, self.portal.get_fundamentals):
            with self.assertRaises(ValueError):
                invoke(self.q, date="2026-09-28", statDate="2026q2")

    def test_research_explicit_date_can_reconstruct_eod_and_marks_audit(self):
        frame = self.portal.get_fundamentals(self.q, date="2026-09-29")
        self.assertEqual(frame["market_cap"].tolist(), [30.0])
        self.assertEqual(frame.attrs["fundamentals_audit"]["visibility_policy"], "research_eod_reconstruction")

    def test_statdate_resolves_valuation_to_quarter_end(self):
        self.runtime("2026-09-29 09:30:00")
        get_fundamentals(self.q, statDate="2026q2")
        self.assertEqual(self.portal.last_valuation_date, "20260630")
        self.assertEqual(self.portal.fundamentals_audit[-1]["income_as_of"], None)

    def test_closed_quarter_end_resolves_backward(self):
        self.backend.frames["trade_cal"].loc[self.backend.frames["trade_cal"]["cal_date"] == "20260630", "is_open"] = 0
        self.portal.get_fundamentals(self.q, statDate="2026q2")
        self.assertEqual(self.portal.last_valuation_date, "20260629")

    def test_missing_calendar_coverage_rejected_instead_of_stale_day(self):
        self.backend.frames["trade_cal"] = self.backend.frames["trade_cal"].query("cal_date < '20260928'")
        self.runtime("2026-09-29 09:30:00")
        with self.assertRaises(FundamentalsUnavailableError) as caught:
            get_fundamentals(self.q)
        self.assertEqual(caught.exception.reason, "trade_calendar_coverage_missing")

    def reports(self):
        self.backend.frames["income"] = pd.DataFrame([
            {"ts_code": "000001.SZ", "end_date": "20250930", "total_revenue": 300.0, "n_income_attr_p": 30.0},
            {"ts_code": "000001.SZ", "end_date": "20251231", "total_revenue": 460.0, "n_income_attr_p": 55.0},
        ])

    def test_annual_cumulative_distinct_from_q4(self):
        self.reports()
        q = query(income.code, income.np_parent_company_owners)
        self.assertEqual(self.portal.get_fundamentals(q, statDate="2025")["np_parent_company_owners"].tolist(), [55.0])
        self.assertEqual(self.portal.get_fundamentals(q, statDate="2025q4")["np_parent_company_owners"].tolist(), [25.0])

    def test_q2_missing_previous_report_is_nan_not_cumulative(self):
        self.backend.frames["income"] = pd.DataFrame([{"ts_code": "000001.SZ", "end_date": "20260630", "n_income_attr_p": 55.0}])
        frame = self.portal.get_fundamentals(query(income.code, income.np_parent_company_owners), statDate="2026q2")
        self.assertTrue(pd.isna(frame.iloc[0]["np_parent_company_owners"]))

    def test_first_quarter_needs_no_previous_report(self):
        self.backend.frames["income"] = pd.DataFrame([{"ts_code": "000001.SZ", "end_date": "20260331", "n_income_attr_p": 55.0}])
        frame = self.portal.get_fundamentals(query(income.code, income.np_parent_company_owners), statDate="2026q1")
        self.assertEqual(frame["np_parent_company_owners"].tolist(), [55.0])

    def test_join_is_intersection(self):
        self.backend.frames["daily_basic"] = pd.DataFrame([{"ts_code": code, "trade_date": "20260331", "total_mv": 10.0} for code in ("000001.SZ", "000002.SZ")])
        self.backend.frames["income"] = pd.DataFrame([{"ts_code": code, "end_date": "20260331", "n_income_attr_p": 5.0} for code in ("000002.SZ", "000003.SZ")])
        frame = self.portal.get_fundamentals(query(valuation.code, valuation.market_cap, income.np_parent_company_owners), statDate="2026q1")
        self.assertEqual(frame["code"].tolist(), ["000002.XSHE"])

    def test_empty_required_income_table_propagates(self):
        self.backend.frames["daily_basic"] = pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20260331", "total_mv": 10.0}])
        q = query(valuation.code, valuation.market_cap).filter(income.np_parent_company_owners > 0).order_by(income.total_operating_revenue.desc())
        frame = self.portal.get_fundamentals(q, statDate="2026q1")
        self.assertTrue(frame.empty)
        self.assertEqual(frame.columns.tolist(), ["code", "market_cap"])
        self.assertEqual(frame.attrs["fundamentals_audit"]["empty_tables"], ["income"])

    def test_empty_valuation_does_not_return_income_universe(self):
        self.backend.frames["daily_basic"] = pd.DataFrame()
        self.backend.frames["income"] = pd.DataFrame([{"ts_code": "000001.SZ", "end_date": "20260331", "n_income_attr_p": 5.0}])
        frame = self.portal.get_fundamentals(query(valuation.code, income.np_parent_company_owners), statDate="2026q1")
        self.assertTrue(frame.empty)

    def test_filter_order_unselected_fields_executed_before_projection_once(self):
        self.backend.frames["daily_basic"] = pd.DataFrame([{"ts_code": code, "trade_date": "20260928", "total_mv": cap} for code, cap in (("000001.SZ", 10.0), ("000002.SZ", 30.0), ("000003.SZ", 20.0))])
        self.runtime("2026-09-29 09:30:00")
        frame = get_fundamentals(query(valuation.code).filter(valuation.market_cap >= 20).order_by(valuation.market_cap.desc()))
        self.assertEqual(frame["code"].tolist(), ["000002.XSHE", "000003.XSHE"])

    def test_missing_nonempty_backend_field_not_fabricated(self):
        with self.assertRaises(NotImplementedError):
            self.portal.get_fundamentals(query(valuation.code, valuation.unknown_field), date="2026-09-28")

    def test_duplicate_security_in_table_rejected(self):
        self.portal._valuation_frame = lambda day: pd.DataFrame({"code": ["000001.XSHE", "000001.XSHE"], "market_cap": [1.0, 2.0]})
        with self.assertRaises(FundamentalsUnavailableError) as caught:
            self.portal.get_fundamentals(self.q, date="2026-09-28")
        self.assertEqual(caught.exception.reason, "duplicate_fundamental_security")

    def test_overlapping_table_field_names_do_not_merge_suffix(self):
        self.backend.frames["income"] = pd.DataFrame([{"ts_code": "000001.SZ", "end_date": "20260331", "market_cap": 99.0, "n_income_attr_p": 5.0}])
        q = query(valuation.code, valuation.market_cap, income.market_cap)
        frame = self.portal.get_fundamentals(q, statDate="2026q1")
        self.assertEqual(frame.columns.tolist(), ["code", "market_cap", "market_cap"])

    def test_302_identity_and_exchange_validation(self):
        self.assertEqual(to_tushare_code("302132.XSHE"), "302132.SZ")
        self.assertEqual(to_joinquant_code("302132.SZ"), "302132.XSHE")
        self.assertTrue(is_tushare_a_share_code("302132.SZ"))
        self.assertFalse(is_tushare_a_share_code("302132.SH"))
        self.assertFalse(is_tushare_a_share_code("302132bad.SZ"))
        self.assertFalse(self.portal._is_a_share_tushare_code("159001.SZ"))

    def test_lifecycle_302_before_listing_through_delisting(self):
        self.backend.frames["stock_basic"] = pd.DataFrame([{"ts_code": "302132.SZ", "name": "test", "list_date": "20260928", "delist_date": "20260930"}])
        for date, expected in (("20260927", []), ("20260928", ["302132.XSHE"]), ("20260930", ["302132.XSHE"]), ("20261001", [])):
            self.assertEqual(self.portal.get_all_securities(date=date).index.tolist(), expected)

    def test_unsupported_statdate_is_explicit_error(self):
        for stat_date in ("2026q5", "20260601", "junk"):
            with self.assertRaises(ValueError):
                self.portal.get_fundamentals(query(income.code), statDate=stat_date)

    def test_audit_has_code_hash_even_when_code_not_projected(self):
        frame = self.portal.get_fundamentals(query(valuation.market_cap), date="2026-09-28")
        self.assertEqual(len(frame.attrs["fundamentals_audit"]["code_set_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
