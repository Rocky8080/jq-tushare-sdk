import unittest

from jq_tushare_sdk.api.finance_tables import income, valuation
from jq_tushare_sdk.api.query import query


class TestQueryDSL(unittest.TestCase):
    def test_query_records_fields_filters_and_ordering(self):
        q = query(valuation.code, valuation.market_cap).filter(
            valuation.market_cap >= 20,
            valuation.market_cap <= 80,
            income.np_parent_company_owners > -10000000,
        ).order_by(valuation.market_cap.desc())

        self.assertEqual([field.name for field in q.fields], ["code", "market_cap"])
        self.assertEqual(q.filters[0].operator, ">=")
        self.assertEqual(q.filters[0].value, 20)
        self.assertEqual(q.filters[1].operator, "<=")
        self.assertEqual(q.filters[1].value, 80)
        self.assertEqual(q.filters[2].field.name, "np_parent_company_owners")
        self.assertEqual(q.ordering[0].direction, "desc")

    def test_in_filter_records_sequence(self):
        q = query(valuation.code).filter(valuation.code.in_(["000001.XSHE", "600000.XSHG"]))

        self.assertEqual(q.filters[0].operator, "in")
        self.assertEqual(q.filters[0].value, ("000001.XSHE", "600000.XSHG"))


if __name__ == "__main__":
    unittest.main()
