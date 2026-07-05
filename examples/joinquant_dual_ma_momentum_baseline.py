"""
JoinQuant dual moving-average momentum baseline for weekly ETF rotation.

This template intentionally uses only common jqdata APIs so it can run both on
JoinQuant and in the local JQ Tushare SDK. It is meant as a simple baseline for
checking data alignment before comparing more complex factor strategies.
"""

from jqdata import *


VERSION = "0.2.0"
UPDATE_DATE = "2026-07-05"

BENCHMARK = "000300.XSHG"
ETF_POOL = (
    "510300.XSHG",
    "510500.XSHG",
    "159915.XSHE",
    "588000.XSHG",
    "512100.XSHG",
)
MAX_HOLDINGS = 2
SHORT_WINDOW = 20
LONG_WINDOW = 60
REBALANCE_WEEKDAY = 1
TRADE_TIME = "open"


def initialize(context):
    set_benchmark(BENCHMARK)
    set_option("use_real_price", True)
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0,
            open_commission=0.0003,
            close_commission=0.0003,
            close_today_commission=0,
            min_commission=5,
        ),
        type="stock",
    )
    set_slippage(FixedSlippage(0.01))
    run_weekly(rebalance, weekday=REBALANCE_WEEKDAY, time=TRADE_TIME)


def rebalance(context):
    ranked = rank_etf_pool()
    selected = ranked[:MAX_HOLDINGS]
    selected_set = set(signal["security"] for signal in selected)
    current_set = current_managed_position_set(context)
    target_set_changed = selected_set != current_set
    target_value = context.portfolio.total_value / len(selected) if selected else 0

    for security in ETF_POOL:
        if security in selected_set:
            continue
        if current_position_amount(context, security) > 0:
            order_target_value(security, 0)

    if target_set_changed:
        for security in ETF_POOL:
            if (
                security in selected_set
                and current_position_amount(context, security) > 0
            ):
                order_target_value(security, target_value)

    for signal in selected:
        security = signal["security"]
        if current_position_amount(context, security) <= 0:
            order_target_value(security, target_value)

    record(
        selected_count=len(selected),
        candidate_count=len(ranked),
        top_score=round(selected[0]["score"], 4) if selected else 0,
    )
    if selected:
        log.info(
            "dual_ma_etf selected=%s candidates=%s"
            % (
                ",".join(signal["security"] for signal in selected),
                len(ranked),
            )
        )
    else:
        log.info("dual_ma_etf no bullish candidates")


def rank_etf_pool():
    signals = []
    for security in ETF_POOL:
        signal = dual_ma_signal(security)
        if signal is None or not signal["invested"]:
            continue
        signal["security"] = security
        signals.append(signal)
    return sorted(signals, key=lambda signal: signal["score"], reverse=True)


def current_managed_position_set(context):
    return set(
        security
        for security in ETF_POOL
        if current_position_amount(context, security) > 0
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
        "score": short_ma / long_ma - 1,
    }
