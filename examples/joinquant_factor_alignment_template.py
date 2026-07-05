"""
JoinQuant factor alignment template.

Use this strategy as the shared JoinQuant-side baseline when comparing factor,
universe, and portfolio data against local SDK runs. Keep this file in sync
instead of copying ad hoc variants between team members.
"""

# 导入函数库
from jqdata import *
from jqfactor import Factor, calc_factors
import datetime
import numpy as np

"""
多因子策略选股模板
本策略中使用的因子来源于『我的策略-单因子分析』中的运行结果。
可以先在单因子分析中研究因子， 在这个策略模板中尝试各种组合的结果。
"""


# 策略初始化
def initialize(context):
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    log.set_level("order", "error")
    set_order_cost(
        OrderCost(
            close_tax=0,
            open_commission=0,
            close_commission=0,
            min_commission=0,
        ),
        type="stock",
    )
    # 按周调仓
    run_weekly(market_open, 1, time="open", reference_security="000300.XSHG")


"""
######################策略的交易逻辑######################
每周计算因子值， 并买入前 20 支股票
"""


# 每周开盘运行一次， 按照上一交易日的因子值进行调仓
def market_open(context):
    # 1. 定义计算因子的 universe，
    #    建议使用与 benchmark 相同的指数，方便判断选股带来的 alpha
    universe = get_index_stocks("000300.XSHG")

    # 2. 获取因子值
    #    get_factor_values 有三个参数，context、因子列表、股票池，
    #    返回值是一个 dict，key 是因子类的 name 属性，value 是 pandas.Series
    #    Series 的 index 是股票代码，value 是当前日期能看到的最新因子值
    factor_values = get_factor_values(
        context,
        [ALPHA013(), ALPHA015(), GROSS_PROFITABILITY()],
        universe,
    )

    alpha013 = factor_values["alpha013"]
    alpha015 = factor_values["alpha015"]
    gross_profitability = factor_values["gross_profitability"]

    # 3. 对因子做线性加权处理， 并将结果进行排序。
    #    您在这一步可以研究自己的因子权重模型来优化策略结果。
    #    对因子做 rank 是因为不同的因子间由于量纲等原因无法直接相加，这是一种去量纲的方法。
    final_factor = (
        0.3 * alpha013.rank(ascending=False)
        + 0.2 * alpha015.rank(ascending=False)
        + 0.5 * gross_profitability.rank(ascending=True)
    )

    # 4. 由因子确定每日持仓的股票列表：
    #    采用因子值由大到小排名前 20 只股票作为目标持仓
    try:
        stock_list = list(final_factor.sort_values(ascending=False)[:20].index)
    except Exception:
        stock_list = list(final_factor.order(ascending=False)[:20].index)

    # 5. 根据股票列表进行调仓：
    #    这里采取所有股票等额买入的方式，您可以使用自己的风险模型自由发挥个股的权重搭配
    rebalance_position(context, stock_list)


"""
######################下面是策略中使用的三个因子######################
可以先使用因子分析功能生产出理想的因子， 再加入到策略中
因子分析：https://www.joinquant.com/algorithm/factor/list
"""


# alpha191 中的 alpha013 因子
# 参考链接 https://www.joinquant.com/data/dict/alpha191
class ALPHA013(Factor):
    # 设置因子名称
    name = "alpha013"
    # 设置获取数据的时间窗口长度
    max_window = 1
    # 设置依赖的数据
    dependencies = ["high", "low", "volume", "money"]

    # 计算因子的函数， 需要返回一个 pandas.Series, index 是股票代码，value 是因子值
    def calc(self, data):
        # 最高价的 dataframe ， index 是日期， column 是股票代码
        high = data["high"]

        # 最低价的 dataframe ， index 是日期， column 是股票代码
        low = data["low"]

        # 计算 vwap
        vwap = data["money"] / data["volume"]

        # 返回因子值， 这里求平均值是为了把只有一行的 dataframe 转成 series
        return (np.power(high * low, 0.5) - vwap).mean()


# alpha191 中的 alpha015 因子
# 参考链接 https://www.joinquant.com/data/dict/alpha191
class ALPHA015(Factor):
    # 设置因子名称
    name = "alpha015"
    # 设置获取数据的时间窗口长度
    max_window = 2
    # 设置依赖的数据
    dependencies = ["open", "close"]

    # 计算因子的函数， 需要返回一个 pandas.Series, index 是股票代码，value 是因子值
    def calc(self, data):
        # 获取 T 日的开盘价，open 是一个 pandas.Series， index 是股票代码， value 是开盘价
        open_price = data["open"].iloc[1]

        # 获取 T-1 日的收盘价
        close_delay_1 = data["close"].iloc[0]

        # 计算因子值
        return open_price / close_delay_1 - 1


# GROSS_PROFITABILITY
# 参考链接：https://www.joinquant.com/post/6585
class GROSS_PROFITABILITY(Factor):
    # 设置因子名称
    name = "gross_profitability"
    # 设置获取数据的时间窗口长度
    max_window = 1
    # 设置依赖的数据
    # 在策略中需要使用 get_fundamentals 获取的 income.total_operating_revenue,
    # 在这里可以直接写做total_operating_revenue。 其他数据同理。
    dependencies = [
        "total_operating_revenue",
        "total_operating_cost",
        "total_assets",
    ]

    # 计算因子的函数， 需要返回一个 pandas.Series, index 是股票代码，value 是因子值
    def calc(self, data):
        # 获取单季度的营业总收入数据的 DataFrame , index 是日期，column 是股票代码， value 是营业总收入
        total_operating_revenue = data["total_operating_revenue"]

        # 获取单季度的营业总成本的 DataFrame
        total_operating_cost = data["total_operating_cost"]

        # 获取总资产的 DataFrame
        total_assets = data["total_assets"]

        # 计算 gross_profitability
        gross_profitability = (
            total_operating_revenue - total_operating_cost
        ) / total_assets

        # 由于 gross_profitability 是一个一行 n 列的 dataframe，可以直接求 mean 转成 series
        return gross_profitability.mean()


"""
###################### 工具 ######################

调仓：
先卖出持仓中不在 stock_list 中的股票
再等价值买入 stock_list 中的股票
"""


def rebalance_position(context, stock_list):
    current_holding = context.portfolio.positions.keys()
    stocks_to_sell = list(set(current_holding) - set(stock_list))
    # 卖出
    bulk_orders(stocks_to_sell, 0)
    total_value = context.portfolio.total_value

    # 买入
    bulk_orders(stock_list, total_value / len(stock_list))


# 批量买卖股票
def bulk_orders(stock_list, target_value):
    for stock in stock_list:
        order_target_value(stock, target_value)


"""
# 策略中获取因子数据的函数
每日返回上一日的因子数据
详见 帮助-单因子分析
"""


def get_factor_values(context, factor_list, universe):
    """
    输入： 因子、股票池
    返回： 前一日的因子值
    """
    # 取因子名称
    factor_name = list(factor.name for factor in factor_list)

    # 计算因子值
    values = calc_factors(
        universe,
        factor_list,
        context.previous_date,
        context.previous_date,
    )
    # 装入 dict
    factor_dict = {name: values[name].iloc[0] for name in factor_name}
    return factor_dict
