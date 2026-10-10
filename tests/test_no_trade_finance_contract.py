"""Forecast research contract: known coverage is separate from historical vintages."""
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.api import finance
from jq_tushare_sdk.api.jqdata import set_runtime_state
from jq_tushare_sdk.api.query import query
from jq_tushare_sdk.data.financial_provenance import FinancialVintageUnavailable
from jq_tushare_sdk.data.portal import DataPortal


class ForecastResearchContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'forecast.db')
        self.backend = TushareCacheBackend(self.path, financial_mode='research_reconstructed')
        self.context = SimpleNamespace(current_dt=datetime(2026, 7, 6, 9, 30), previous_date=date(2026, 7, 3))
        set_runtime_state(SimpleNamespace(data_portal=DataPortal(self.backend), context=self.context))
        self.table = finance.TUSHARE_FORECAST

    def tearDown(self):
        set_runtime_state(None)
        self.backend.close()
        self.tmp.cleanup()

    def certify(self, start='20260401', end='20260703', scope='all', **extra):
        self.backend.cache_data('forecast_coverage', pd.DataFrame([{
            'scope': scope, 'start_date': start, 'end_date': end, 'row_count': 0,
            'observed_at': '2026-10-10T12:00:00', 'source': 'tushare.forecast', **extra,
        }]))

    def request(self, start='2026-04-01', end='2026-07-03', codes=None, fields=None):
        q = query(*(fields or (self.table,))).filter(self.table.pub_date >= start, self.table.pub_date <= end)
        return q if codes is None else q.filter(self.table.code.in_(codes))

    def add(self, code='000001.SZ', announcement='20260630', **extra):
        self.backend.cache_data('forecast', pd.DataFrame([{
            'ts_code': code, 'ann_date': announcement, 'end_date': '20260630', 'type': '预增',
            'p_change_min': 30., 'p_change_max': 40., 'net_profit_min': 1234.5, 'net_profit_max': 1500.,
            'last_parent_net': 1000., 'first_ann_date': announcement, 'summary': '预增',
            'change_reason': '经营增长', **extra,
        }]))

    def test_source_rows_cannot_replace_coverage_evidence(self):
        self.add()
        with self.assertRaisesRegex(finance.FinanceCapabilityUnavailable, 'forecast_coverage_missing'):
            finance.run_query(self.request())

    def test_certified_empty_has_complete_explicit_non_native_research_metadata(self):
        self.certify()
        result = finance.run_query(self.request())
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), list(finance.capabilities()['TUSHARE_FORECAST']['schema']))
        self.assertTrue(result.attrs['coverage_complete'])
        self.assertEqual(result.attrs['financial_mode'], 'research_reconstructed')
        self.assertEqual(result.attrs['source'], 'tushare.forecast')
        self.assertFalse(result.attrs['native_reproduction_eligible'])
        self.assertFalse(result.attrs['native_compatibility_verified'])
        self.assertFalse(result.attrs['pit_verified'])
        self.assertEqual(result.attrs['coverage']['scope'], 'all')
        self.assertEqual(result.attrs['coverage']['records'][0]['observed_at'], '2026-10-10T12:00:00')

    def test_publication_day_segments_form_complete_coverage(self):
        self.certify('20260701', '20260701')
        self.certify('20260702', '20260702')
        self.certify('20260703', '20260703')
        coverage = finance.forecast_coverage(self.backend, '20260701', '20260703')
        self.assertTrue(coverage['complete'])
        self.assertTrue(coverage['all_market_complete'])
        self.assertEqual(len(coverage['records']), 3)

    def test_calendar_gap_is_rejected_even_when_adjacent_days_have_no_rows(self):
        self.certify('20260701', '20260701')
        self.certify('20260703', '20260703')
        with self.assertRaisesRegex(finance.FinanceCapabilityUnavailable, 'forecast_coverage_missing'):
            finance.run_query(self.request(start='20260701'))

    def test_weekend_is_a_required_publication_day(self):
        self.certify('20260626', '20260626')
        self.certify('20260629', '20260703')
        self.assertFalse(finance.forecast_coverage(self.backend, '20260626', '20260703')['complete'])

    def test_multi_code_coverage_does_not_certify_other_codes_or_entire_market(self):
        self.certify(scope='000001.SZ')
        self.certify(scope='600000.SH')
        result = finance.run_query(self.request(codes=['000001.XSHE', '600000.XSHG']))
        self.assertTrue(result.attrs['coverage_complete'])
        self.assertFalse(result.attrs['coverage']['all_market_complete'])
        self.assertFalse(finance.forecast_coverage(self.backend, '20260401', '20260703')['complete'])
        with self.assertRaisesRegex(finance.FinanceCapabilityUnavailable, 'forecast_coverage_missing'):
            finance.run_query(self.request(codes=['000001.XSHE', '600001.XSHG']))

    def test_union_of_market_and_code_coverage(self):
        self.certify('20260401', '20260531')
        self.certify('20260601', '20260703', scope='000001.SZ')
        result = finance.run_query(self.request(codes=['000001.XSHE']))
        self.assertTrue(result.empty)
        self.assertTrue(result.attrs['coverage_complete'])
        self.assertFalse(result.attrs['coverage']['all_market_complete'])

    def test_unattributed_or_invalid_coverage_is_not_a_certificate(self):
        for bad in ({'source': ''}, {'source': 'invented'}, {'observed_at': ''},
                    {'row_count': -1}, {'row_count': 1.5}, {'row_count': float('inf')}):
            with self.subTest(bad=bad):
                self.certify(**bad)
                self.assertFalse(finance.forecast_coverage(self.backend, '20260401', '20260703')['complete'])

    def test_nonfinite_profit_is_rejected_instead_of_generating_a_bonus(self):
        self.certify()
        self.add()
        # A legacy cache can predate append-only revision validation.
        with sqlite3.connect(self.path) as connection:
            connection.execute('UPDATE earnings_forecast SET net_profit_min=?', (float('inf'),))
        with self.assertRaisesRegex(finance.FinanceCapabilityUnavailable, 'forecast_numeric_field_invalid'):
            finance.run_query(self.request())

    def test_two_codes_dates_ordering_projection_and_units(self):
        self.certify()
        self.add('000001.SZ', '20260630')
        self.add('600000.SH', '20260702', net_profit_min=2345.6, p_change_min=-25.)
        self.add('600001.SH', '20260702')
        self.add('000001.SZ', '20260705')
        fields = (self.table.code, self.table.pub_date, self.table.net_profit_min, self.table.p_change_min)
        q = self.request(codes=['000001.XSHE', '600000.SH'], fields=fields).order_by(self.table.pub_date.desc())
        result = finance.run_query(q)
        self.assertEqual(result.code.tolist(), ['600000.XSHG', '000001.XSHE'])
        self.assertEqual(result.pub_date.tolist(), ['2026-07-02', '2026-06-30'])
        self.assertEqual(result.net_profit_min.tolist(), [2345.6, 1234.5])
        self.assertEqual(result.p_change_min.tolist(), [-25., 30.])
        self.assertEqual(result.attrs['field_units']['net_profit_min'], 'ten_thousand_cny')
        self.assertEqual(result.attrs['field_units']['p_change_min'], 'percent')

    def test_inclusive_and_exclusive_date_predicates_use_actual_requested_days(self):
        self.certify('20260702', '20260702')
        self.add(announcement='20260702')
        q = query(self.table).filter(self.table.pub_date > '20260701', self.table.pub_date < '20260703')
        result = finance.run_query(q)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.attrs['coverage']['publication_start'], '20260702')
        self.assertEqual(result.attrs['coverage']['publication_end'], '20260702')

    def test_intersection_of_code_predicates_is_applied(self):
        self.certify(scope='000001.SZ')
        self.add()
        q = self.request(codes=['000001.XSHE', '600000.SH']).filter(self.table.code == '000001.SZ')
        self.assertEqual(finance.run_query(q).code.tolist(), ['000001.XSHE'])

    def test_weekend_before_current_date_is_rejected_when_after_previous_trade_date(self):
        self.certify(end='20260705')
        with self.assertRaisesRegex(FinancialVintageUnavailable, 'previous trading date 20260703'):
            finance.run_query(self.request(end='20260705'))

    def test_current_and_future_cutoffs_are_rejected(self):
        for end in ('20260706', '20260707'):
            with self.subTest(end=end), self.assertRaisesRegex(FinancialVintageUnavailable, 'forecast_unavailable_at'):
                finance.run_query(self.request(end=end))

    def test_calendar_fallback_uses_real_open_date(self):
        del self.context.previous_date
        self.backend.cache_data('trade_cal', pd.DataFrame([
            {'exchange': 'SSE', 'cal_date': '20260703', 'is_open': 1},
            {'exchange': 'SSE', 'cal_date': '20260704', 'is_open': 0},
            {'exchange': 'SSE', 'cal_date': '20260705', 'is_open': 0},
        ]))
        self.certify()
        self.assertEqual(finance.run_query(self.request()).attrs['publication_cutoff'], '20260703')

    def test_missing_calendar_does_not_guess_yesterday(self):
        del self.context.previous_date
        self.certify()
        with self.assertRaisesRegex(FinancialVintageUnavailable, 'forecast_calendar_missing'):
            finance.run_query(self.request())

    def test_native_schema_is_still_explicitly_unavailable(self):
        native = finance.STK_FIN_FORCAST
        with self.assertRaisesRegex(finance.FinanceCapabilityUnavailable, 'native_forecast_schema_unverified'):
            finance.run_query(query(native).filter(native.pub_date >= '20260401', native.pub_date <= '20260703'))

    def test_strict_mode_rejects_today_capture_with_historical_announcement(self):
        self.certify()
        self.add()
        with sqlite3.connect(self.path) as connection:
            before = connection.execute('SELECT observed_at,available_at FROM financial_revision').fetchall()
        self.assertTrue(before)
        self.assertGreater(before[0][1], '2026-07-03T23:59:59')
        self.backend.strict_pit = True
        self.backend.financial_mode = 'strict_pit'
        with self.assertRaisesRegex(FinancialVintageUnavailable, 'financial_vintage_missing'):
            finance.run_query(self.request())
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(before, connection.execute('SELECT observed_at,available_at FROM financial_revision').fetchall())

    def test_cached_mode_is_disclosed_instead_of_promoted_to_research_or_pit(self):
        self.certify()
        self.backend.financial_mode = 'cached_snapshot'
        result = finance.run_query(self.request())
        self.assertEqual(result.attrs['financial_mode'], 'cached_snapshot')
        self.assertFalse(result.attrs['pit_verified'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
