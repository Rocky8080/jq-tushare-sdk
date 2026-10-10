import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.api.finance_tables import income
from jq_tushare_sdk.api.jqdata import get_data_contract, set_runtime_state
from jq_tushare_sdk.api.query import query
from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.data.portal import DataPortal
from jq_tushare_sdk.runtime.engine import BacktestEngine
from jq_tushare_sdk.runtime.loader import StrategyLoader
from jq_tushare_sdk.runtime.globals_state import RuntimeState
from jq_tushare_sdk.runtime.context import Context, Portfolio
from jq_tushare_sdk.runtime.logger import RuntimeLogger
from jq_tushare_sdk.runtime.scheduler import Scheduler


class ResearchDataContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = TushareCacheBackend(str(self.root/'cache.db'), financial_mode='research_reconstructed')
        self.portal = DataPortal(self.backend)

    def tearDown(self):
        set_runtime_state(None)
        self.backend.close()
        self.tmp.cleanup()

    def test_runtime_contract_is_explicit_and_snapshot_changes_with_cache(self):
        set_runtime_state(SimpleNamespace(data_portal=self.portal))
        first = get_data_contract()
        self.assertEqual(first['financial_mode'], 'research_reconstructed')
        self.assertFalse(first['native_reproduction_eligible'])
        self.assertEqual(first['data_snapshot_id'], get_data_contract()['data_snapshot_id'])
        self.backend.cache_data('daily_basic', pd.DataFrame([{'ts_code':'000001.SZ','trade_date':'20260630','pe_ttm':10,'pb':2}]))
        self.assertNotEqual(first['data_snapshot_id'], get_data_contract()['data_snapshot_id'])

    def test_strategy_import_receives_the_real_finance_extension(self):
        strategy = self.root/'import_strategy.py'
        strategy.write_text("from jqdata import finance, get_data_contract\ndef initialize(context):\n    g.forecast_ready = finance.capabilities()['TUSHARE_FORECAST']['supported']\n    g.mode = get_data_contract()['financial_mode']\n")
        broker = SimpleNamespace(order=lambda *a:None, order_value=lambda *a:None,
                                 order_target=lambda *a:None, order_target_value=lambda *a:None)
        state = RuntimeState(data_portal=self.portal, scheduler=Scheduler(), broker=broker,
                             context=Context(Portfolio(100000)), log=RuntimeLogger(self.root/'load.log'))
        loaded = StrategyLoader().load(strategy, state)
        loaded.initialize(state.context)
        self.assertTrue(state.g.forecast_ready)
        self.assertEqual(state.g.mode,'research_reconstructed')

    def test_current_quarter_cannot_see_later_announcement(self):
        self.backend.cache_data('income', pd.DataFrame([
            {'ts_code':'000001.SZ','end_date':'20260331','report_type':'1','ann_date':'20260430','f_ann_date':'20260430','n_income_attr_p':100},
            {'ts_code':'000001.SZ','end_date':'20260630','report_type':'1','ann_date':'20260820','f_ann_date':'20260820','n_income_attr_p':900},
        ]))
        frame = self.portal.get_fundamentals(query(income.code, income.np_parent_company_owners), statDate='2026q2', as_of=datetime(2026,7,1,9,30))
        self.assertTrue(frame.empty)
        visible = self.portal.get_fundamentals(query(income.code, income.np_parent_company_owners), statDate='2026q2', as_of=datetime(2026,8,21,9,30))
        self.assertEqual(visible['np_parent_company_owners'].tolist(), [800])

    def test_current_announcement_is_not_visible_intraday(self):
        self.backend.cache_data('income', pd.DataFrame([{'ts_code':'000001.SZ','end_date':'20251231','report_type':'1','ann_date':'20260430','f_ann_date':'20260430','n_income_attr_p':100}]))
        frame = self.portal.get_fundamentals(query(income.code, income.np_parent_company_owners), statDate='2025', as_of=datetime(2026,4,30,9,30))
        self.assertTrue(frame.empty)

    def test_pb_missing_has_explicit_diagnostic_without_filling(self):
        self.backend.cache_data('daily_basic',pd.DataFrame([{'ts_code':'000001.SZ','trade_date':'20260630','pe_ttm':10,'pb':None}]))
        frame=self.portal._valuation_frame('20260630')
        self.assertTrue(pd.isna(frame.iloc[0].pb_ratio))
        provenance=frame.attrs['valuation_provenance']
        self.assertEqual(provenance['financial_mode'],'research_reconstructed')
        self.assertEqual(provenance['pb_status_by_code'],{'000001.XSHE':'source_pb_missing'})

    def test_declared_unsupported_forecast_stops_before_strategy_loading(self):
        strategy=self.root/'strategy.py'
        strategy.write_text("JQTS_DATA_REQUIREMENTS={'research_reconstructed':{'finance_tables':['STK_FIN_FORCAST']}}\nraise RuntimeError('MUST NOT EXECUTE')\n")
        config=BacktestConfig(str(strategy),'2026-07-01','2026-07-01',100000,str(self.root/'cache.db'),output_dir=str(self.root/'runs'),financial_mode='research_reconstructed')
        with self.assertRaisesRegex(ValueError,'unsupported finance tables STK_FIN_FORCAST'):
            BacktestEngine(config,self.backend).run()
        self.assertFalse((self.root/'runs').exists())

    def test_missing_forecast_coverage_stops_before_callbacks(self):
        strategy=self.root/'strategy.py'
        strategy.write_text("JQTS_DATA_REQUIREMENTS={'research_reconstructed':{'finance_tables':['TUSHARE_FORECAST'],'forecast_lookback_days':90}}\nraise RuntimeError('MUST NOT EXECUTE')\n")
        self.backend.cache_data('trade_cal',pd.DataFrame([{'exchange':'SSE','cal_date':d.strftime('%Y%m%d'),'is_open':int(d.weekday()<5)} for d in pd.date_range('2026-05-22','2026-07-01')]))
        config=BacktestConfig(str(strategy),'2026-07-01','2026-07-01',100000,str(self.root/'cache.db'),output_dir=str(self.root/'runs'),financial_mode='research_reconstructed')
        with self.assertRaisesRegex(ValueError,'forecast_coverage_missing'):
            BacktestEngine(config,self.backend).run()


if __name__=='__main__':
    unittest.main()
