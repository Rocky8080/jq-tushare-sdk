"""Corporate actions must conserve shares/cash without hiding missing contracts."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from jq_tushare_sdk.broker.broker import Broker
from jq_tushare_sdk.broker.costs import CostModel
from jq_tushare_sdk.runtime.context import Context, Portfolio
from jq_tushare_sdk.runtime.engine import BacktestEngine


COLUMNS = "ts_code TEXT, div_proc TEXT, record_date TEXT, ex_date TEXT, pay_date TEXT, div_listdate REAL, cash_div REAL, cash_div_tax REAL, stk_div REAL, stk_bo_rate REAL, stk_co_rate REAL"
DEFAULT_ROW = dict(ts_code="000001.SZ", div_proc="实施", record_date="20260701", ex_date="20260702",
                   pay_date="20260702", div_listdate=None, cash_div=.8, cash_div_tax=1.,
                   stk_div=0., stk_bo_rate=None, stk_co_rate=None)
CODE = "000001.XSHE"


def write_db(path, rows, columns=COLUMNS):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE corporate_actions (" + columns + ")")
        for row in rows:
            conn.execute("INSERT INTO corporate_actions (" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", list(row.values()))


def engine(path):
    e = BacktestEngine.__new__(BacktestEngine)
    e.config = SimpleNamespace(cache_db=str(path))
    e.backend = SimpleNamespace()
    e.active_run_dir = None
    return e


class Portal:
    price = 10.

    def get_price(self, security, **kwargs):
        return pd.DataFrame([dict(open=self.price, close=self.price)])

    def get_current_data(self, securities, **kwargs):
        return {code: SimpleNamespace(paused=False, is_st=False, last_price=self.price,
                                     high_limit=self.price * 2, low_limit=self.price / 2, day_open=self.price)
                for code in securities}


def account(amount=100, acquired_at=datetime(2026, 6, 1, 9, 30)):
    context = Context(Portfolio(10000.), current_dt=acquired_at)
    b = Broker(context, Portal(), CostModel(open_commission=0., close_commission=0., close_tax=0., min_commission=0.))
    assert b.order(CODE, amount).status == "filled"
    return context, b


def prepare(tmp_path, amount=100, **overrides):
    p = tmp_path / "cache.db"
    write_db(p, [{**DEFAULT_ROW, **overrides}])
    e = engine(p)
    groups = e._load_a_share_corporate_actions()
    context, broker = account(amount)
    entitlements = {}
    context.current_dt = datetime(2026, 7, 1, 15)
    broker.settle_trading_day("2026-07-01")
    e._capture_a_share_entitlements(context, broker, "2026-07-01", groups["record_date"], entitlements)
    return e, groups, context, broker, entitlements, set()


def settle(args, day="2026-07-02"):
    e, groups, context, broker, entitlements, applied = args
    broker.settle_trading_day(day)
    e._settle_a_share_corporate_actions(context, broker, day, groups["ex_date"], entitlements, applied)


def test_unified_cache_loads_source_dates_and_total_ratio_without_writes(tmp_path):
    path = tmp_path / "cache.db"
    row = {**DEFAULT_ROW, "stk_div": .4, "stk_co_rate": .4, "div_listdate": 20260702.0}
    write_db(path, [row, {**DEFAULT_ROW, "ts_code": "900001.SH"},
                    {**DEFAULT_ROW, "ts_code": "200001.SZ"}, {**DEFAULT_ROW, "ts_code": "600001.SH", "div_proc": "预案"}])
    before = path.read_bytes()
    e = engine(path); groups = e._load_a_share_corporate_actions()
    event, = groups["ex_date"]["2026-07-02"]
    assert event["cash_per_share"] == 1.
    assert event["share_ratio"] == .4
    assert event["div_listdate"] == "2026-07-02"
    assert event["source"] == "tushare.dividend" and event["source_row"] == row
    assert event["event_id"] == "tushare.dividend:000001.SZ:2026-07-01:2026-07-02"
    assert path.read_bytes() == before


def test_unified_and_year_shards_deduplicate_and_conflicts_fail(tmp_path):
    write_db(tmp_path / "cache.db", [DEFAULT_ROW])
    shard = tmp_path / "a_share" / "a_share_2026.db"
    write_db(shard, [DEFAULT_ROW])
    e = engine(tmp_path / "cache.db")
    event, = e._load_a_share_corporate_actions()["ex_date"]["2026-07-02"]
    assert len(event["source_databases"]) == 2
    with sqlite3.connect(shard) as conn:
        conn.execute("UPDATE corporate_actions SET cash_div_tax=2")
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        e._load_a_share_corporate_actions()


@pytest.mark.parametrize("overrides,match", [
    ({"stk_div": .4, "stk_co_rate": .3}, "total/component"),
    ({"cash_div_tax": None}, "Gross cash_div_tax"),
    ({"cash_div_tax": float("inf")}, "Invalid corporate action cash_div_tax"),
    ({"cash_div_tax": -1}, "Invalid corporate action"),
    ({"ex_date": "20260230"}, "Invalid corporate action ex_date"),
    ({"ex_date": "20260701"}, "record/ex dates"),
])
def test_bad_implemented_data_is_not_silently_skipped(tmp_path, overrides, match):
    write_db(tmp_path / "cache.db", [{**DEFAULT_ROW, **overrides}])
    with pytest.raises(ValueError, match=match):
        engine(tmp_path / "cache.db")._load_a_share_corporate_actions()


def test_present_bad_schema_raises_absent_table_is_allowed(tmp_path):
    path = tmp_path / "cache.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE other (value TEXT)")
    assert engine(path)._load_a_share_corporate_actions()["ex_date"] == {}
    write_db(path, [], columns="ts_code TEXT")
    with pytest.raises(ValueError, match="Invalid corporate_actions schema"):
        engine(path)._load_a_share_corporate_actions()


def test_missing_total_uses_components_once(tmp_path):
    write_db(tmp_path / "cache.db", [{**DEFAULT_ROW, "stk_div": None, "stk_bo_rate": .1, "stk_co_rate": .3}])
    event, = engine(tmp_path / "cache.db")._load_a_share_corporate_actions()["ex_date"]["2026-07-02"]
    assert event["share_ratio"] == pytest.approx(.4)


def test_cash_only_gross_tax_cost_and_idempotency(tmp_path):
    args = prepare(tmp_path)
    e, _, context, broker, _, _ = args
    settle(args)
    assert context.current_dt == datetime(2026, 7, 2)
    assert context.portfolio.available_cash == pytest.approx(9080.)
    assert context.portfolio.positions[CODE].avg_cost == pytest.approx(9.)
    assert context.portfolio.positions[CODE].price == pytest.approx(9.)
    assert context.portfolio.total_value == pytest.approx(9980.)
    settle(args)
    assert context.portfolio.available_cash == pytest.approx(9080.)
    assert e._corporate_action_summary()["net_cash"] == 80.
    assert broker._dividend_tax_lots[CODE][0].pending_tax == 20.


def test_total_stock_ratio_cash_and_fifo_lots_conserve_value(tmp_path):
    args = prepare(tmp_path, stk_div=.4, stk_co_rate=.4, div_listdate=20260702.)
    e, _, context, broker, _, _ = args
    original_acquired = broker._dividend_tax_lots[CODE][0].acquired_at
    settle(args)
    position = context.portfolio.positions[CODE]
    assert position.total_amount == 140 and position.closeable_amount == 140
    assert position.avg_cost == pytest.approx(9. / 1.4)
    assert context.portfolio.total_value == pytest.approx(9980.)
    assert sum(lot.remaining_amount for lot in broker._dividend_tax_lots[CODE]) == 140
    assert broker._dividend_tax_lots[CODE][0].acquired_at == original_acquired
    assert broker._dividend_tax_lots[CODE][0].pending_tax == 20.
    # Every new share participates in a later entitlement, with no missing lot.
    assert sum(a["amount"] for a in broker.capture_dividend_lots(CODE, 140)) == 140
    broker.data_portal.price = position.price
    context.current_dt = datetime(2026, 7, 2, 9, 30)
    order = broker.order(CODE, -140)
    assert order.status == "filled" and order.dividend_tax == pytest.approx(-10.)
    assert CODE not in context.portfolio.positions
    assert e._corporate_action_summary()["added_shares"] == 40


def test_no_cash_stock_event_uses_listing_and_not_missing_pay_date(tmp_path):
    args = prepare(tmp_path, cash_div=0., cash_div_tax=0., pay_date=None,
                   stk_div=.4, stk_co_rate=.4, div_listdate=20260702.)
    settle(args)
    assert args[2].portfolio.positions[CODE].total_amount == 140
    assert args[2].portfolio.available_cash == 9000.


@pytest.mark.parametrize("overrides,match", [
    ({"pay_date": "20260703"}, "receivable accounting"),
    ({"pay_date": None}, "receivable accounting"),
    ({"stk_div": .4, "stk_co_rate": .4, "div_listdate": 20260703.}, "unlisted-share"),
    ({"stk_div": .4, "stk_co_rate": .4}, "unlisted-share"),
    ({"stk_div": .1985111, "stk_co_rate": .1985111, "div_listdate": 20260702.}, "fractional"),
    ({"stk_div": .4, "stk_bo_rate": .4, "div_listdate": 20260702.}, "bonus-share dividend tax basis"),
])
def test_unsupported_held_actions_reject_before_cash_or_position_mutation(tmp_path, overrides, match):
    args = prepare(tmp_path, **overrides)
    with pytest.raises(NotImplementedError, match=match):
        settle(args)
    assert args[2].portfolio.available_cash == 9000.
    assert args[2].portfolio.positions[CODE].total_amount == 100
    assert args[5] == set()
    assert args[0]._corporate_action_summary()["ending_receivables"] is None


def test_unsupported_unheld_event_does_not_block_a_share_run(tmp_path):
    write_db(tmp_path / "cache.db", [{**DEFAULT_ROW, "pay_date": "20260703"}])
    e = engine(tmp_path / "cache.db"); groups = e._load_a_share_corporate_actions()
    context = Context(Portfolio(10000.)); broker = Broker(context, Portal())
    entitlements = {}
    e._capture_a_share_entitlements(context, broker, "2026-07-01", groups["record_date"], entitlements)
    e._settle_a_share_corporate_actions(context, broker, "2026-07-02", groups["ex_date"], entitlements, set())
    assert context.portfolio.total_value == 10000.
    assert e._corporate_action_summary()["unsupported_held_count"] == 0


def test_sold_after_record_retains_cash_entitlement_and_tax(tmp_path):
    args = prepare(tmp_path, ex_date="20260703", pay_date="20260703")
    _, _, context, broker, _, _ = args
    context.current_dt = datetime(2026, 7, 2, 9, 30)
    assert broker.order(CODE, -100).status == "filled"
    assert CODE not in context.portfolio.positions
    settle(args, "2026-07-03")
    assert context.portfolio.available_cash == pytest.approx(10090.)
    assert args[0]._corporate_action_summary()["withheld_tax"] == 10.
    assert CODE not in context.portfolio.positions


def test_sold_record_shares_then_rebuy_does_not_reduce_new_cost(tmp_path):
    args = prepare(tmp_path, ex_date="20260703", pay_date="20260703")
    _, _, context, broker, _, _ = args
    context.current_dt = datetime(2026, 7, 2, 9, 30)
    broker.order(CODE, -100)
    broker.data_portal.price = 12.
    broker.order(CODE, 100)
    settle(args, "2026-07-03")
    assert context.portfolio.positions[CODE].avg_cost == 12.
    assert context.portfolio.available_cash == pytest.approx(8890.)


def test_stock_entitlement_survives_sold_lot_and_restores_original_fifo_cohort(tmp_path):
    args = prepare(tmp_path, ex_date="20260703", pay_date="20260703", div_listdate=20260703., stk_div=.4, stk_co_rate=.4)
    _, _, context, broker, _, _ = args
    original = broker._dividend_tax_lots[CODE][0]
    context.current_dt = datetime(2026, 7, 2, 9, 30)
    broker.order(CODE, -100)
    settle(args, "2026-07-03")
    assert context.portfolio.positions[CODE].total_amount == 40
    assert context.portfolio.positions[CODE].closeable_amount == 40
    assert broker._dividend_tax_lots[CODE][0].lot_id == original.lot_id
    assert broker._dividend_tax_lots[CODE][0].acquired_at == datetime(2026, 6, 1, 9, 30)
    assert broker._dividend_tax_lots[CODE][0].remaining_amount == 40.


def test_t_plus_one_for_new_purchases_remains_locked_after_distribution(tmp_path):
    args = prepare(tmp_path, stk_div=.4, stk_co_rate=.4, div_listdate=20260702.)
    settle(args)
    _, _, context, broker, _, _ = args
    context.current_dt = datetime(2026, 7, 2, 9, 30)
    broker.order(CODE, 100)
    assert context.portfolio.positions[CODE].total_amount == 240
    assert context.portfolio.positions[CODE].closeable_amount == 140


def test_action_ledger_is_persisted_with_sources_and_complete_dates(tmp_path):
    args = prepare(tmp_path)
    e = args[0]
    e.active_run_dir = tmp_path / "run"
    (e.active_run_dir / "artifacts").mkdir(parents=True)
    settle(args)
    rows = json.loads((e.active_run_dir / "artifacts" / "corporate_action_ledger.json").read_text())
    assert [row["phase"] for row in rows] == ["capture", "settlement"]
    assert rows[1]["source_row"]["cash_div"] == .8
    assert rows[1]["cash_per_share"] == 1.
    assert rows[1]["eligible_quantity"] == 100.
    assert rows[1]["record_date"] == "2026-07-01"
    assert rows[1]["settlement_date"] == "2026-07-02"
    assert rows[1]["gross_cash"] == 100. and rows[1]["net_cash"] == 80.
    assert e._corporate_action_summary()["data_completeness_verified"] is False
