from jq_tushare_sdk.api.query import FieldRef


class FinanceTable:
    def __init__(self, table_name: str):
        self._table_name = table_name

    def __getattr__(self, name: str) -> FieldRef:
        return FieldRef(self._table_name, name)


valuation = FinanceTable("valuation")
income = FinanceTable("income")
indicator = FinanceTable("indicator")
balance = FinanceTable("balance")
cash_flow = FinanceTable("cash_flow")
