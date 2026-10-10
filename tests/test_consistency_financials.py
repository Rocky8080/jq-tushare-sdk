import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.api import finance
from jq_tushare_sdk.api.jqdata import set_runtime_state
from jq_tushare_sdk.api.query import query, FieldRef
from jq_tushare_sdk.data.financial_provenance import FinancialVintageUnavailable
from jq_tushare_sdk.data.portal import DataPortal


class FinancialConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'cache.db')
        self.backend = TushareCacheBackend(self.db)

    def tearDown(self):
        set_runtime_state(None)
        self.backend.close()
        self.tmp.cleanup()

    def income(self, end='20251231', profit=-10000000., announcement='20260301', **extra):
        return {'ts_code': '000001.SZ', 'end_date': end, 'report_type': '1', 'ann_date': announcement,
                'f_ann_date': announcement, 'n_income_attr_p': profit, 'total_revenue': 50000000., **extra}

    def valuation(self, pe=None, pe_ttm=None, day='20260928'):
        self.backend.cache_data('daily_basic', pd.DataFrame([{'ts_code': '000001.SZ', 'trade_date': day, 'pe': pe,
                                                             'pe_ttm': pe_ttm, 'total_mv': 10000., 'pb': 2., 'turnover_rate': 1.}]))

    def test_pe_uses_ttm_without_static_fallback(self):
        self.valuation(pe=8, pe_ttm=20)
        result = DataPortal(self.backend)._valuation_frame('20260928')
        self.assertEqual(result.iloc[0].pe_ratio, 20.)

    def test_missing_ttm_is_nan_not_zero(self):
        self.valuation(pe=8, pe_ttm=None)
        result = DataPortal(self.backend)._valuation_frame('20260928')
        self.assertTrue(np.isnan(result.iloc[0].pe_ratio))
        self.assertEqual(result.attrs['valuation_provenance']['pe_status_by_code']['000001.XSHE'], 'ttm_source_missing')

    def test_research_signed_annual_ttm_has_disclosed_provenance(self):
        self.backend.cache_data('income', pd.DataFrame([self.income()]))
        self.backend.financial_mode = 'research_reconstructed'
        self.valuation(pe_ttm=None)
        result = DataPortal(self.backend)._valuation_frame('20260928')
        self.assertEqual(result.iloc[0].pe_ratio, -10.)
        self.assertFalse(result.attrs['valuation_provenance']['pit_verified'])
        self.assertEqual(result.attrs['valuation_provenance']['pe_status_by_code']['000001.XSHE'], 'retrospective_reconstructed')

    def test_cumulative_ttm_formula(self):
        rows = [self.income('20250630', 10000000., '20250801'), self.income('20251231', 30000000., '20260301'), self.income('20260630', 20000000., '20260801')]
        self.backend.cache_data('income', pd.DataFrame(rows))
        self.backend.financial_mode = 'research_reconstructed'
        self.valuation(pe_ttm=None)
        self.assertAlmostEqual(DataPortal(self.backend)._valuation_frame('20260928').iloc[0].pe_ratio, 2.5)

    def test_zero_ttm_keeps_nan_with_reason(self):
        self.backend.cache_data('income', pd.DataFrame([self.income(profit=0.)]))
        self.backend.financial_mode = 'research_reconstructed'
        self.valuation(pe_ttm=None)
        result = DataPortal(self.backend)._valuation_frame('20260928')
        self.assertTrue(np.isnan(result.iloc[0].pe_ratio))
        self.assertEqual(result.attrs['valuation_provenance']['pe_status_by_code']['000001.XSHE'], 'zero_ttm_profit')

    def test_missing_ttm_input_cannot_be_filled_zero(self):
        self.backend.cache_data('income', pd.DataFrame([self.income('20260630', -10000000., '20260801')]))
        self.backend.financial_mode = 'research_reconstructed'
        self.valuation(pe_ttm=None)
        result = DataPortal(self.backend)._valuation_frame('20260928')
        self.assertTrue(np.isnan(result.iloc[0].pe_ratio))
        self.assertEqual(result.attrs['valuation_provenance']['pe_status_by_code']['000001.XSHE'], 'ttm_inputs_missing')

    def test_announcement_future_cannot_reconstruct(self):
        self.backend.cache_data('income', pd.DataFrame([self.income(announcement='20261001')]))
        self.backend.financial_mode = 'research_reconstructed'
        self.valuation(pe_ttm=None)
        self.assertTrue(np.isnan(DataPortal(self.backend)._valuation_frame('20260928').iloc[0].pe_ratio))

    def test_appends_multiple_versions_without_overwriting_revision(self):
        self.backend.cache_data('income', pd.DataFrame([self.income(profit=10.), self.income(profit=20.)]))
        revisions = self.backend.financial_revisions()
        self.assertEqual(len(revisions), 2)
        self.assertEqual(self.backend.fetch('income').iloc[0].n_income_attr_p, 20.)
        self.backend.cache_data('income', pd.DataFrame([self.income(profit=20.)]))
        self.assertEqual(len(self.backend.financial_revisions()), 2)
        with sqlite3.connect(self.db) as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                conn.execute('UPDATE financial_revision SET available_at=?', ('2000-01-01T00:00:00',))
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                conn.execute('DELETE FROM financial_revision')

    def test_legacy_migration_never_claims_past_vintage(self):
        self.backend.cache_data('income', pd.DataFrame([self.income()]))
        readiness = self.backend.financial_readiness('20260928')
        self.assertFalse(readiness['ready'])
        self.assertEqual(readiness['reason'], 'financial_vintage_missing')
        self.assertFalse(readiness['legacy_cache_historical_vintages_recovered'])

    def test_readiness_requires_explicit_clock(self):
        self.backend.cache_data('income', pd.DataFrame([self.income()]))
        result = self.backend.financial_readiness(None)
        self.assertFalse(result['ready'])
        self.assertEqual(result['reason'], 'financial_asof_missing')

    def test_unknown_announcement_legacy_row_cannot_hide_behind_other_revision(self):
        self.backend.cache_data('income', pd.DataFrame([
            self.income(_version_verified=True, _source_version_id='known-v1', _version_available_at='2026-03-01'),
            {**self.income(end='20260630'), 'ann_date': '', 'f_ann_date': ''},
        ]))
        result = self.backend.financial_readiness('20260928')
        self.assertFalse(result['ready'])
        self.assertEqual(result['reason'], 'financial_announcement_missing')
        self.assertEqual(result['unknown_announcement_key_count'], 1)

    def test_valuation_status_survives_fundamentals_query_projection(self):
        from jq_tushare_sdk.api.finance_tables import valuation
        self.backend.cache_data('income', pd.DataFrame([self.income(profit=0., _version_verified=True, _source_version_id='known-zero', _version_available_at='2026-03-01')]))
        self.backend.strict_pit = True
        self.backend.financial_mode = 'strict_pit'
        self.backend.cache_data('trade_cal', pd.DataFrame([{'exchange': 'SSE', 'cal_date': '20260928', 'is_open': 1}]))
        self.valuation(pe_ttm=None)
        frame = DataPortal(self.backend).get_fundamentals(query(valuation.code, valuation.pe_ratio), date='20260928')
        provenance = frame.attrs['valuation_provenance']
        self.assertTrue(provenance['pit_verified'])
        self.assertEqual(provenance['pe_status_by_code']['000001.XSHE'], 'zero_ttm_profit')

    def test_strict_mode_rejects_old_latest_cache(self):
        self.backend.cache_data('income', pd.DataFrame([self.income()]))
        self.backend.strict_pit = True
        self.backend.financial_mode = 'strict_pit'
        self.valuation(pe_ttm=10.)
        with self.assertRaisesRegex(FinancialVintageUnavailable, 'financial_vintage_missing'):
            DataPortal(self.backend)._valuation_frame('20260928')

    def test_verified_revision_correction_does_not_rewrite_before_cutoff(self):
        rows = [self.income(profit=-10000000., _version_verified=True, _source_version_id='native-v1', _version_available_at='2026-03-01'),
                self.income(profit=20000000., announcement='20261001', _version_verified=True, _source_version_id='native-v2', _version_available_at='2026-10-01')]
        self.backend.cache_data('income', pd.DataFrame(rows))
        self.backend.strict_pit = True
        self.backend.financial_mode = 'strict_pit'
        self.valuation(pe_ttm=None)
        result = DataPortal(self.backend)._valuation_frame('20260928')
        self.assertEqual(result.iloc[0].pe_ratio, -10.)
        self.assertEqual(len(self.backend.financial_revisions(as_of='20260928')), 1)
        self.assertEqual(len(self.backend.financial_revisions(as_of='20261002')), 2)

    def test_announcement_date_missing_is_unavailable(self):
        data = pd.DataFrame([{'ts_code': '000001.SZ', 'end_date': '20251231', 'n_income_attr_p': 1.}])
        result = DataPortal(self.backend)._filter_income_asof(data, '20260928')
        self.assertTrue(result.empty)
        self.assertEqual(result.attrs['financial_gap'], 'announcement_date_missing')

    def test_stale_valuation_is_not_forward_filled(self):
        self.valuation(pe_ttm=10., day='20260920')
        result = DataPortal(self.backend)._valuation_frame('20260928')
        self.assertTrue(np.isnan(result.iloc[0].pe_ratio))
        self.assertTrue(np.isnan(result.iloc[0].market_cap))
        self.assertEqual(result.attrs['valuation_provenance']['stale_codes'], ['000001.XSHE'])

    def test_long_paused_identity_preserved_beyond_fourteen_days(self):
        self.backend.cache_data('stock_basic', pd.DataFrame([{'ts_code': '000001.SZ', 'name': 'sample', 'list_date': '19910101'}]))
        self.valuation(pe_ttm=10., day='20260101')
        result = DataPortal(self.backend)._valuation_frame('20260928')
        self.assertEqual(result['code'].tolist(), ['000001.XSHE'])
        self.assertTrue(result['market_cap'].isna().all())
        provenance = result.attrs['valuation_provenance']
        self.assertEqual(provenance['pe_status_by_code']['000001.XSHE'], 'stale_valuation_snapshot')
        self.assertEqual(provenance['snapshot_date_by_code']['000001.XSHE'], '20260101')

    def test_missing_active_snapshot_identity_has_explicit_gap(self):
        self.backend.cache_data('stock_basic', pd.DataFrame([{'ts_code': '000001.SZ', 'list_date': '19910101'}, {'ts_code': '000002.SZ', 'list_date': '19910101'}]))
        self.valuation(pe_ttm=10.)
        result = DataPortal(self.backend)._valuation_frame('20260928')
        self.assertEqual(set(result['code']), {'000001.XSHE', '000002.XSHE'})
        missing = result[result.code == '000002.XSHE'].iloc[0]
        self.assertTrue(np.isnan(missing.market_cap))
        self.assertEqual(result.attrs['valuation_provenance']['pe_status_by_code']['000002.XSHE'], 'valuation_snapshot_missing')

    def test_future_listing_and_delisted_lifecycle_not_forward_filled(self):
        self.backend.cache_data('stock_basic', pd.DataFrame([{'ts_code': '000001.SZ', 'list_date': '19910101', 'delist_date': '20260701'}, {'ts_code': '000002.SZ', 'list_date': '20261001', 'delist_date': ''}]))
        self.valuation(pe_ttm=10., day='20260101')
        result = DataPortal(self.backend)._valuation_frame('20260928')
        self.assertTrue(result.empty)
        self.assertEqual(result.attrs['valuation_provenance']['missing_codes'], [])

    def test_delist_day_is_inclusive_and_day_after_excluded(self):
        self.backend.cache_data('stock_basic', pd.DataFrame([{'ts_code': '000001.SZ', 'name': 'sample', 'list_date': '19910101', 'delist_date': '20260928'}]))
        self.valuation(pe_ttm=10.)
        portal = DataPortal(self.backend)
        day = portal._valuation_frame('20260928')
        after = portal._valuation_frame('20260929')
        self.assertEqual(day['code'].tolist(), ['000001.XSHE'])
        self.assertTrue(after.empty)
        self.assertIn('000001.XSHE', portal.get_all_securities(date='20260928').index)
        self.assertNotIn('000001.XSHE', portal.get_all_securities(date='20260929').index)

    def test_future_latest_correction_cannot_hide_missing_original_vintage(self):
        self.backend.cache_data('income', pd.DataFrame([self.income()]))
        self.backend.cache_data('income', pd.DataFrame([self.income(profit=20000000., announcement='20261001')]))
        self.backend.cache_data('income', pd.DataFrame([{**self.income(_version_verified=True, _source_version_id='known-other', _version_available_at='2026-03-01'), 'ts_code': '000002.SZ'}]))
        readiness = self.backend.financial_readiness('20260928')
        self.assertFalse(readiness['ready'])
        self.assertEqual(readiness['reason'], 'financial_vintage_missing')
        self.assertIn(['000001.SZ', '20251231', '1'], readiness['missing_keys'])
        self.backend.strict_pit = True
        self.backend.financial_mode = 'strict_pit'
        self.valuation(pe_ttm=10.)
        with self.assertRaisesRegex(FinancialVintageUnavailable, 'financial_vintage_missing'):
            DataPortal(self.backend)._valuation_frame('20260928')

    def test_verified_identical_payload_appends_evidence_without_rewriting_capture(self):
        self.backend.cache_data('income', pd.DataFrame([self.income()]))
        with sqlite3.connect(self.db) as conn:
            original = conn.execute('SELECT * FROM financial_revision').fetchall()
        self.backend.cache_data('income', pd.DataFrame([self.income(_version_verified=True, _source_version_id='known-same', _version_available_at='2026-03-01')]))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT * FROM financial_revision').fetchall(), original)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_revision_evidence').fetchone()[0], 1)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                conn.execute('DELETE FROM financial_revision_evidence')
        visible = self.backend.financial_revisions(as_of='20260928')
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible.iloc[0]._provenance_kind, 'source_version_verified')
        self.assertTrue(self.backend.financial_readiness('20260928')['ready'])
        self.backend.cache_data('income', pd.DataFrame([self.income(_version_verified=True, _source_version_id='known-same', _version_available_at='2026-03-01')]))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM financial_revision_evidence').fetchone()[0], 1)

    def test_capture_clock_uses_explicit_shanghai_timezone(self):
        from zoneinfo import ZoneInfo
        from jq_tushare_sdk.data.financial_provenance import capture_timestamp
        with patch('jq_tushare_sdk.data.financial_provenance.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 9, 22, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
            self.assertEqual(capture_timestamp(), '2026-10-09T22:00:00')
            self.assertEqual(str(clock.now.call_args.args[0]), 'Asia/Shanghai')

    def test_full_market_forecast_queries_each_announcement_day(self):
        calls = []
        def forecast(**params):
            if not params.get('ts_code') and not params.get('ann_date'):
                raise ValueError('ann_date or ts_code required')
            calls.append(params)
            self.assertNotIn('start_date', params)
            self.assertNotIn('end_date', params)
            day = params['ann_date']
            return pd.DataFrame([{'ts_code': '000001.SZ', 'ann_date': day, 'end_date': '20260630', 'type': '预增'}]) if day == '20260702' else pd.DataFrame()
        self.backend.token = 'test-token'
        self.backend.request_interval = 0
        self.backend._pro = SimpleNamespace(forecast=forecast)
        count = self.backend.update_data('forecast', start_date='20260701', end_date='20260703')
        self.assertEqual(count, 1)
        self.assertEqual([call['ann_date'] for call in calls], ['20260701', '20260702', '20260703'])
        coverage = self.backend.fetch('forecast_coverage')
        self.assertEqual(len(coverage[(coverage.start_date == '20260701') & (coverage.end_date == '20260703')]), 1)

    def test_failed_announcement_day_never_marks_entire_range_covered(self):
        def forecast(**params):
            if params['ann_date'] == '20260702':
                raise ValueError('provider failed')
            return pd.DataFrame()
        self.backend.token = 'test-token'
        self.backend.request_interval = 0
        self.backend._pro = SimpleNamespace(forecast=forecast)
        with self.assertRaisesRegex(ValueError, 'provider failed'):
            self.backend.update_data('forecast', start_date='20260701', end_date='20260703')
        coverage = self.backend.fetch('forecast_coverage')
        self.assertEqual(len(coverage), 1)
        self.assertEqual(coverage.iloc[0].end_date, '20260701')

    def test_single_security_forecast_uses_code_with_date_range(self):
        calls = []
        def forecast(**params):
            if not params.get('ts_code') and not params.get('ann_date'):
                raise ValueError('ann_date or ts_code required')
            calls.append(params)
            return pd.DataFrame()
        self.backend.token = 'test-token'
        self.backend.request_interval = 0
        self.backend._pro = SimpleNamespace(forecast=forecast)
        self.assertEqual(self.backend.update_data('forecast', start_date='20260701', end_date='20260703', ts_code='000001.SZ'), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['ts_code'], '000001.SZ')
        self.assertEqual(calls[0]['start_date'], '20260701')
        self.assertEqual(self.backend.fetch('forecast_coverage').iloc[0].scope, '000001.SZ')

    def forecast_runtime(self):
        set_runtime_state(SimpleNamespace(data_portal=DataPortal(self.backend), context=SimpleNamespace(current_dt=datetime(2026, 10, 9, 9, 30), previous_date=datetime(2026, 10, 8).date())))

    def forecast_query(self, native=False):
        table = finance.STK_FIN_FORCAST if native else finance.TUSHARE_FORECAST
        return query(table).filter(table.code.in_(['000001.XSHE']), table.pub_date >= '2026-07-01', table.pub_date <= '2026-10-08')

    def forecast_coverage(self):
        self.backend.cache_data('forecast_coverage', pd.DataFrame([{'scope': 'all', 'start_date': '20260701', 'end_date': '20261008', 'row_count': 1, 'observed_at': '2026-10-09T09:00:00', 'source': 'tushare.forecast'}]))

    def test_native_forecast_capability_explicitly_unsupported(self):
        self.forecast_runtime()
        self.assertFalse(finance.capabilities()['STK_FIN_FORCAST']['supported'])
        with self.assertRaisesRegex(finance.FinanceCapabilityUnavailable, 'native_forecast_schema_unverified'):
            finance.run_query(self.forecast_query(native=True))

    def test_missing_forecast_coverage_does_not_fake_empty(self):
        self.forecast_runtime()
        with self.assertRaisesRegex(finance.FinanceCapabilityUnavailable, 'forecast_coverage_missing'):
            finance.run_query(self.forecast_query())

    def test_complete_empty_forecast_returns_schema_with_coverage(self):
        self.forecast_runtime()
        self.forecast_coverage()
        result = finance.run_query(self.forecast_query())
        self.assertTrue(result.empty)
        self.assertIn('pub_date', result)
        self.assertIn('coverage', result.attrs)

    def test_tushare_forecast_mapping_preserves_units_and_cutoff(self):
        self.forecast_runtime()
        self.forecast_coverage()
        self.backend.cache_data('forecast', pd.DataFrame([{'ts_code': '000001.SZ', 'ann_date': '20260801', 'end_date': '20260630', 'type': '预增', 'net_profit_min': 1000., 'net_profit_max': 1200., 'summary': '预增'}, {'ts_code': '000001.SZ', 'ann_date': '20261009', 'end_date': '20260930', 'type': '预减', 'net_profit_min': 500.}]))
        result = finance.run_query(self.forecast_query())
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0].net_profit_min, 1000.)
        self.assertEqual(result.iloc[0].pub_date, '2026-08-01')
        self.assertFalse(result.attrs['native_compatibility_verified'])

    def test_unsupported_forecast_field_rejected(self):
        with self.assertRaisesRegex(finance.FinanceCapabilityUnavailable, 'unverified field'):
            finance.TUSHARE_FORECAST.imaginary_profit
        self.forecast_runtime()
        with self.assertRaisesRegex(finance.FinanceCapabilityUnavailable, 'Unsupported forecast fields'):
            q = self.forecast_query()
            finance.run_query(type(q)((FieldRef('TUSHARE_FORECAST', 'imaginary_profit'),), q.filters))

    def test_forecast_requires_both_publication_bounds(self):
        self.forecast_runtime()
        with self.assertRaisesRegex(finance.FinanceCapabilityUnavailable, 'lower and upper'):
            finance.run_query(query(finance.TUSHARE_FORECAST))


if __name__ == '__main__':
    unittest.main(verbosity=2)
