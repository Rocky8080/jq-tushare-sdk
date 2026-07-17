import unittest

from jq_tushare_sdk.adapters.tushare.cache_backend import _API_SPECS, _DEFAULT_UPDATE_APIS


class TestDividendData(unittest.TestCase):
    def test_dividend_is_a_default_cached_api_with_corporate_action_storage(self):
        self.assertIn("dividend", _DEFAULT_UPDATE_APIS)
        spec = _API_SPECS["dividend"]
        self.assertEqual(spec.table, "corporate_actions")
        self.assertIn("record_date", spec.columns)
        self.assertIn("pay_date", spec.columns)
        self.assertIn("cash_div_tax", spec.columns)


if __name__ == "__main__":
    unittest.main()
