from jq_tushare_sdk.api import jqdata
from jq_tushare_sdk.api.finance_tables import balance, cash_flow, income, valuation
from jq_tushare_sdk.api.query import query
from jq_tushare_sdk.broker.costs import CostModel


def set_runtime_state(state):
    jqdata.set_runtime_state(state)


def exported_globals() -> dict:
    def unsupported(name):
        def _inner(*_args, **_kwargs):
            raise NotImplementedError(f"JoinQuant API is not implemented locally: {name}")

        return _inner

    def record(**kwargs):
        jqdata.runtime_state().records.append(dict(kwargs))

    def run_daily(func, time="open", **kwargs):
        jqdata.runtime_state().scheduler.run_daily(func, time=time, **kwargs)

    def run_weekly(func, weekday=1, time="open", **kwargs):
        jqdata.runtime_state().scheduler.run_weekly(func, weekday=weekday, time=time, **kwargs)

    def run_monthly(func, monthday=1, time="open", **kwargs):
        jqdata.runtime_state().scheduler.run_monthly(func, monthday=monthday, time=time, **kwargs)

    def unschedule_all():
        jqdata.runtime_state().scheduler.unschedule_all()

    def set_option(*_args, **_kwargs):
        return None

    def set_benchmark(security, *_args, **_kwargs):
        jqdata.runtime_state().benchmark = str(security)
        return None

    def set_order_cost(order_cost, type="stock", **kwargs):
        if kwargs:
            names = ", ".join(sorted(kwargs))
            raise NotImplementedError(f"Unsupported set_order_cost kwargs: {names}")
        if type != "stock":
            raise NotImplementedError(f"Unsupported order cost type: {type}")

        payload = dict(getattr(order_cost, "__dict__", {}))
        close_today_commission = float(payload.pop("close_today_commission", 0.0) or 0.0)
        if close_today_commission != 0.0:
            raise NotImplementedError("Non-zero close_today_commission is unsupported")
        allowed = {
            "open_tax",
            "close_tax",
            "open_commission",
            "close_commission",
            "min_commission",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            names = ", ".join(unknown)
            raise NotImplementedError(f"Unsupported OrderCost fields: {names}")
        if float(payload.get("open_tax", 0.0) or 0.0) != 0.0:
            raise NotImplementedError("Non-zero open_tax is unsupported")

        broker = jqdata.runtime_state().broker
        current = getattr(broker, "cost_model", CostModel())
        broker.cost_model = CostModel(
            open_commission=float(payload.get("open_commission", current.open_commission)),
            close_commission=float(payload.get("close_commission", current.close_commission)),
            close_tax=float(payload.get("close_tax", current.close_tax)),
            min_commission=float(payload.get("min_commission", current.min_commission)),
            slippage_rate=float(current.slippage_rate),
            slippage_fixed=float(current.slippage_fixed),
        )

    def set_slippage(slippage, type="stock", **kwargs):
        if kwargs:
            names = ", ".join(sorted(kwargs))
            raise NotImplementedError(f"Unsupported set_slippage kwargs: {names}")
        if type != "stock":
            raise NotImplementedError(f"Unsupported slippage type: {type}")
        if isinstance(slippage, PriceRelatedSlippage):
            slippage_rate = float(slippage.rate)
            slippage_fixed = 0.0
        elif isinstance(slippage, FixedSlippage):
            slippage_rate = 0.0
            slippage_fixed = float(slippage.value)
        else:
            raise NotImplementedError(
                f"Unsupported slippage style: {slippage.__class__.__name__}"
            )

        broker = jqdata.runtime_state().broker
        current = getattr(broker, "cost_model", CostModel())
        broker.cost_model = CostModel(
            open_commission=float(current.open_commission),
            close_commission=float(current.close_commission),
            close_tax=float(current.close_tax),
            min_commission=float(current.min_commission),
            slippage_rate=slippage_rate,
            slippage_fixed=slippage_fixed,
        )

    class OrderCost:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PriceRelatedSlippage:
        def __init__(self, rate):
            self.rate = float(rate)

    class FixedSlippage:
        def __init__(self, value):
            self.value = float(value)

    class MarketOrderStyle:
        def __init__(self, price=None):
            self.price = price

    class LimitOrderStyle:
        def __init__(self, price):
            self.price = price

    exports = {
        "get_price": jqdata.get_price,
        "attribute_history": jqdata.attribute_history,
        "history": jqdata.history,
        "get_trade_days": jqdata.get_trade_days,
        "get_index_stocks": jqdata.get_index_stocks,
        "get_all_securities": jqdata.get_all_securities,
        "get_security_info": jqdata.get_security_info,
        "get_current_data": jqdata.get_current_data,
        "get_industry": jqdata.get_industry,
        "get_fundamentals": jqdata.get_fundamentals,
        "get_fundamentals_continuously": jqdata.get_fundamentals_continuously,
        "query": query,
        "valuation": valuation,
        "income": income,
        "balance": balance,
        "cash_flow": cash_flow,
        "record": record,
        "run_daily": run_daily,
        "run_weekly": run_weekly,
        "run_monthly": run_monthly,
        "unschedule_all": unschedule_all,
        "set_option": set_option,
        "set_benchmark": set_benchmark,
        "set_order_cost": set_order_cost,
        "set_slippage": set_slippage,
        "OrderCost": OrderCost,
        "PriceRelatedSlippage": PriceRelatedSlippage,
        "FixedSlippage": FixedSlippage,
        "MarketOrderStyle": MarketOrderStyle,
        "LimitOrderStyle": LimitOrderStyle,
        "order": lambda *args, **kwargs: jqdata.runtime_state().broker.order(*args, **kwargs),
        "order_value": lambda *args, **kwargs: jqdata.runtime_state().broker.order_value(*args, **kwargs),
        "order_target": lambda *args, **kwargs: jqdata.runtime_state().broker.order_target(*args, **kwargs),
        "order_target_value": lambda *args, **kwargs: jqdata.runtime_state().broker.order_target_value(*args, **kwargs),
    }

    for name in (
        "get_factor_values",
        "get_bars",
        "get_ticks",
        "get_extras",
        "get_mtss",
        "get_money_flow",
        "get_locked_shares",
        "get_valuation",
        "get_billboard_list",
        "get_industry_stocks",
        "get_concept_stocks",
        "get_instrument",
        "set_universe",
        "order_target_percent",
        "order_percent",
    ):
        exports[name] = unsupported(name)
    return exports
