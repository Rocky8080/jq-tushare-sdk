"""
Minimal JoinQuant dual moving-average momentum baseline.

This template intentionally uses only common jqdata APIs so it can run both on
JoinQuant and in the local JQ Tushare SDK. It is meant as a simple baseline for
checking data alignment before comparing more complex factor strategies.
"""

from jqdata import *


VERSION = "0.1.0"
UPDATE_DATE = "2026-07-05"

TARGET_SECURITY = "510300.XSHG"
BENCHMARK = "000300.XSHG"
SHORT_WINDOW = 20
LONG_WINDOW = 60
TRADE_TIME = "open"


def initialize(context):
    set_benchmark(BENCHMARK)
    set_option("use_real_price", True)
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0.001,
            open_commission=0.0003,
            close_commission=0.0003,
            close_today_commission=0,
            min_commission=5,
        ),
        type="stock",
    )
    set_slippage(FixedSlippage(0.01))
    run_daily(rebalance, time=TRADE_TIME)


def rebalance(context):
    signal = dual_ma_signal(TARGET_SECURITY)
    if signal is None:
        log.info("Not enough history for dual MA baseline.")
        return

    current_amount = current_position_amount(context, TARGET_SECURITY)
    if signal["invested"] and current_amount <= 0:
        order_target_value(TARGET_SECURITY, context.portfolio.total_value)
    elif not signal["invested"] and current_amount > 0:
        order_target_value(TARGET_SECURITY, 0)

    record(
        short_ma=round(signal["short_ma"], 4),
        long_ma=round(signal["long_ma"], 4),
        invested=1 if signal["invested"] else 0,
    )
    log.info(
        "dual_ma %s short=%.4f long=%.4f invested=%s"
        % (
            TARGET_SECURITY,
            signal["short_ma"],
            signal["long_ma"],
            signal["invested"],
        )
    )


def current_position_amount(context, security):
    position = context.portfolio.positions.get(security)
    if position is None:
        return 0
    return int(
        getattr(
            position,
            "total_amount",
            getattr(position, "closeable_amount", getattr(position, "amount", 0)),
        )
        or 0
    )


def dual_ma_signal(security):
    history = attribute_history(
        security,
        LONG_WINDOW,
        unit="1d",
        fields=["close"],
        skip_paused=True,
        df=True,
    )
    if history is None or len(history) < LONG_WINDOW:
        return None

    close = history["close"].dropna()
    if len(close) < LONG_WINDOW:
        return None

    short_ma = float(close.tail(SHORT_WINDOW).mean())
    long_ma = float(close.tail(LONG_WINDOW).mean())
    return {
        "short_ma": short_ma,
        "long_ma": long_ma,
        "invested": short_ma > long_ma,
    }
