"""One atomic daily-simulation calculation, committed only by its caller.

Checkpoint bytes are trusted internal state. Never accept a checkpoint uploaded
by an untrusted user: pickle is deliberately not an interchange format.
"""
from __future__ import annotations

import hashlib
import inspect
import math
import pickle
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from jq_tushare_sdk import __version__ as sdk_version

from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.reports.metrics import performance_row
from jq_tushare_sdk.runtime.context import Context, Portfolio
from jq_tushare_sdk.runtime.engine import BacktestEngine
from jq_tushare_sdk.runtime.globals_state import RuntimeState
from jq_tushare_sdk.runtime.loader import StrategyLoader
from jq_tushare_sdk.runtime.logger import RuntimeLogger
from jq_tushare_sdk.runtime.scheduler import Scheduler
from jq_tushare_sdk.runtime.daily.daily_broker import DailySimulationBroker
from jq_tushare_sdk.runtime.daily.daily_portal import DailyDataUnavailable, DailySimulationPortal


CHECKPOINT_VERSION = 1
RUNTIME_CONTRACT = "daily_close_next_open/1"
_TRANSIENT_RUNTIME_FIELDS = {"data_portal", "scheduler", "broker", "context", "g", "log", "records", "benchmark"}


class _DailyAccountingEngine(BacktestEngine):
    def _infer_etf_cash_dividend(self, portal, trade_day, security):
        # A price discontinuity is a data-readiness signal, never authority to
        # manufacture an account cash movement. Explicit events are required.
        return 0.0


class DailySimulationScheduler(Scheduler):
    def __init__(self):
        super().__init__()
        self.registration_allowed = False
        self.clear_generation = 0

    def _validate_registration(self, func, label):
        if not self.registration_allowed:
            raise NotImplementedError("Register schedules in initialize/after_code_changed, not at import time")
        text = str(label)
        fixed = re.fullmatch(r"(\d{2}):(\d{2})", text)
        after_close = (fixed is not None and 15 <= int(fixed[1]) <= 23 and 0 <= int(fixed[2]) <= 59)
        if text not in {"after_close", "close"} and not after_close:
            raise NotImplementedError("日线版只支持收盘后决策，请使用 after_close 或明确的收盘后时间；不会把盘中调度静默移动到收盘")
        if not inspect.isfunction(inspect.unwrap(func)) or "<locals>" in inspect.unwrap(func).__qualname__:
            raise NotImplementedError("Persistent schedules require strategy top-level functions")

    def run_daily(self, func, time="open", **kwargs):
        self._validate_registration(func, time)
        return super().run_daily(func, time=time, **kwargs)

    def run_weekly(self, func, weekday=1, time="open", **kwargs):
        self._validate_registration(func, time)
        return super().run_weekly(func, weekday=weekday, time=time, **kwargs)

    def run_monthly(self, func, monthday=1, time="open", **kwargs):
        self._validate_registration(func, time)
        return super().run_monthly(func, monthday=monthday, time=time, **kwargs)

    def unschedule_all(self):
        if not self.registration_allowed:
            raise NotImplementedError("Schedules cannot change at module import")
        self.clear_generation += 1
        return super().unschedule_all()


def _date(value):
    text = str(value)
    return datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 and text.isdigit() else date.fromisoformat(text[:10])


def _schedule_state(scheduler, loaded):
    entries = []
    for entry in scheduler.entries:
        callback = inspect.unwrap(entry.func)
        name = getattr(callback, "__name__", "")
        candidate = loaded.namespace.get(name)
        if not name or candidate is None or inspect.unwrap(candidate) is not callback:
            raise NotImplementedError("Persistent callbacks must be named top-level functions in the strategy file")
        entries.append({"name": name, "kind": entry.kind, "time": str(entry.time),
                        "weekday": entry.weekday, "monthday": entry.monthday})
    return entries


def _restore_schedules(scheduler, loaded, descriptors, *, revision_changed):
    missing = []
    for item in descriptors:
        callback = loaded.namespace.get(item["name"])
        if not callable(callback):
            missing.append(item["name"])
            continue
        kwargs = {"time": item["time"]}
        if item["kind"] == "weekly":
            kwargs["weekday"] = item["weekday"]
        elif item["kind"] == "monthly":
            kwargs["monthday"] = item["monthday"]
        if item["kind"] not in {"daily", "weekly", "monthly"}:
            raise ValueError("Checkpoint contains an unsupported schedule kind")
        getattr(scheduler, "run_" + item["kind"])(callback, **kwargs)
    if missing and not revision_changed:
        raise ValueError("Saved strategy callbacks are missing: " + ", ".join(missing))
    return missing


def _persistent_namespace(values, label):
    result = {}
    for name, value in values.items():
        if str(name).startswith("__"):
            continue
        try:
            pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            raise ValueError(f"{label}.{name} cannot be checkpointed; store runtime-only resources under __ names and recreate in process_initialize") from None
        result[name] = value
    return result


def _serializable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serializable(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        return _serializable(value.item())
    return str(value)


def _assert_simulation_mode(context):
    if getattr(context.run_params, "type", None) != "backtest":
        raise RuntimeError("Simulation cannot change run_params.type or enable live execution")


def _bind_complete_calendar(scheduler, portal, day):
    kinds = {entry.kind for entry in scheduler.entries}
    if "monthly" in kinds:
        start = date(day.year, day.month, 1)
        end = date(day.year + (day.month == 12), day.month % 12 + 1, 1) - timedelta(days=1)
    else:
        start = end = day
    if "weekly" in kinds:
        start -= timedelta(days=start.weekday())
        end += timedelta(days=6 - end.weekday())
    scheduler.bind_calendar(portal.calendar(start, end) if kinds & {"weekly", "monthly"} else [day])


def _validate_saved_entitlements(actions, entitlements, broker, start_date, day_text):
    # Tax-lot dictionary keys survive a complete sale, so also cover eligible
    # shares sold after record day and before a newly arrived ex-date event.
    ever_held = set(broker._dividend_tax_lots) | set(broker.context.portfolio.positions)
    economic_fields = ("record_date", "ex_date", "pay_date", "div_listdate", "cash_div", "share_ratio", "bonus_ratio", "transfer_ratio")
    for events in actions["ex_date"].values():
        for event in events:
            if not start_date <= event["record_date"] < day_text:
                continue
            saved = entitlements.get(event["event_id"])
            if saved is None and event["security"] in ever_held:
                raise DailyDataUnavailable("A corporate action arrived after its record date; account entitlement reconciliation is required")
            if saved and float(saved.get("qualified_amount") or 0) > 0:
                if any(saved.get(field) != event.get(field) for field in economic_fields):
                    raise DailyDataUnavailable("A captured corporate action changed; account entitlement reconciliation is required")


def run_day(account: dict, revision: dict, previous_checkpoint: bytes | None,
            trade_day: str, backend, log_path: str) -> dict:
    """Calculate one day; never commit account money, orders, or checkpoint files."""
    day = _date(trade_day)
    day_text = day.isoformat()
    initial_cash = float(account["initial_cash"])
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("Simulation initial cash must be finite and positive")
    source_path = Path(revision["source_path"]).resolve()
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != revision["source_hash"]:
        raise ValueError("Frozen strategy revision hash mismatch")
    saved = None
    if previous_checkpoint is not None:
        try:
            saved = pickle.loads(previous_checkpoint)
        except Exception:
            raise ValueError("Simulation checkpoint is incompatible or unreadable") from None
        if not isinstance(saved, dict) or saved.get("version") != CHECKPOINT_VERSION or str(saved.get("account_id")) != str(account["id"]):
            raise ValueError("Simulation checkpoint version/account mismatch")
        if saved.get("runtime_contract", RUNTIME_CONTRACT) != RUNTIME_CONTRACT:
            raise ValueError("Simulation execution contract is incompatible; explicit migration is required")
        if saved.get("initial_cash") != initial_cash:
            raise ValueError("Initial capital cannot change after simulation starts")
        if str(saved.get("revision_id")) == str(revision["id"]) and saved.get("revision_hash") != revision["source_hash"]:
            raise ValueError("An existing strategy revision cannot change its source hash")
        if saved.get("start_date", _date(account["start_date"]).isoformat()) != _date(account["start_date"]).isoformat():
            raise ValueError("Account start date cannot change after simulation starts")
    portal = DailySimulationPortal(backend, day_text)
    portal.validate_day(previous_day=saved["last_trade_day"] if saved else None, start_date=account["start_date"])
    previous_days = portal.calendar(day - timedelta(days=40), day)
    earlier = [item for item in previous_days if item < day]
    if not earlier:
        raise DailyDataUnavailable("Previous trading day is unavailable")
    previous_date = earlier[-1]
    context = Context(Portfolio(initial_cash)) if saved is None else saved["context"]
    context.current_dt = datetime.combine(day, time(9, 30))
    context.previous_date = previous_date
    context.run_params.type = "backtest"
    context.run_params.simulation = True
    context.run_params.simulation_policy = "close_decision_next_open"
    logger = RuntimeLogger(log_path)
    logger.set_context(trade_date=day_text)
    logger.info("Daily simulation %s: close decisions; next-session opening fills; local account only", day_text)
    scheduler = DailySimulationScheduler()
    broker = DailySimulationBroker(context, portal, revision["id"], saved["broker"] if saved else None)
    state = RuntimeState(data_portal=portal, scheduler=scheduler, broker=broker, context=context,
                         g=SimpleNamespace(), log=logger,
                         benchmark=saved["benchmark_symbol"] if saved else account["benchmark"])
    loaded = StrategyLoader().load(source_path, state)
    _assert_simulation_mode(context)
    if callable(loaded.namespace.get("handle_data")) or callable(loaded.namespace.get("handle_tick")):
        raise NotImplementedError("Daily simulation does not support handle_data/handle_tick; use run_daily/weekly/monthly")
    if saved:
        # Loading imports code, but persistent g belongs to the account.
        vars(state.g).clear()
        vars(state.g).update(saved["g"])
        for key, value in saved.get("runtime_metadata", {}).items():
            if key not in _TRANSIENT_RUNTIME_FIELDS:
                setattr(state, key, value)
    scheduler.registration_allowed = True
    revision_changed = saved is not None and str(saved["revision_id"]) != str(revision["id"])
    missing_callbacks = _restore_schedules(scheduler, loaded, saved["schedules"], revision_changed=revision_changed) if saved else []
    config = BacktestConfig(strategy_path=str(source_path), start_date=day_text, end_date=day_text,
                            initial_cash=initial_cash, cache_db=str(backend.cache_db),
                            benchmark=state.benchmark, financial_mode=getattr(backend, "financial_mode", "cached_snapshot"))
    engine = _DailyAccountingEngine(config, backend)
    # Reuse arithmetic helpers only. active_run_dir stays None, so no historical
    # backtest output or corporate-action file is written by this transaction.
    actions = engine._load_a_share_corporate_actions()
    entitlements = saved["entitlements"] if saved else {}
    applied_actions = saved["applied_actions"] if saved else set()
    applied_etf = saved["applied_etf"] if saved else set()
    if saved:
        engine._corporate_action_ledger = saved["corporate_action_ledger"]
        engine._corporate_action_unsupported_held_count = saved.get("unsupported_actions", 0)
    etf_actions = engine._load_etf_dividend_events()
    related_securities = set(context.portfolio.positions) | {intent["security"] for intent in broker.pending}
    matching_events = {event["security"] for event in actions["ex_date"].get(day_text, [])}
    matching_events.update(event["security"] for event in etf_actions.get(day_text, []))
    factor_changes = portal.validate_adjustment_events(related_securities, previous_date, matching_events)
    _validate_saved_entitlements(actions, entitlements, broker, _date(account["start_date"]).isoformat(), day_text)
    broker.settle_trading_day(day)
    engine._settle_a_share_corporate_actions(context, broker, day_text, actions["ex_date"], entitlements, applied_actions)
    context.current_dt = datetime.combine(day, time(9, 30))
    portal.validate_holdings(context)
    eligible_etf = set(context.portfolio.positions)
    engine._apply_etf_dividends(context, portal, day_text, etf_actions, applied_etf, eligible_etf)
    engine._mark_to_market(context, portal, day_text, current_dt=context.current_dt)
    previous_pending = len(broker.pending)
    broker.execute_pending(day_text)
    logger.info("Opening settlement completed: %s prior intents, %s simulated fills", previous_pending, len(broker.trades))
    context.current_dt = datetime.combine(day, time(15, 30))
    portal.validate_holdings(context)
    engine._mark_to_market(context, portal, day_text)
    broker.phase = "decision"
    if saved is None:
        logger.info("initialize: first account day only")
        loaded.initialize(context)
        _assert_simulation_mode(context)
        if callable(loaded.namespace.get("after_code_changed")):
            logger.info("after_code_changed: initial revision %s", revision["id"])
            loaded.after_code_changed(context)
            _assert_simulation_mode(context)
    elif revision_changed:
        logger.info("after_code_changed: revision %s -> %s; initialize is not repeated", saved["revision_id"], revision["id"])
        before_clear = scheduler.clear_generation
        loaded.after_code_changed(context)
        _assert_simulation_mode(context)
        if missing_callbacks and scheduler.clear_generation == before_clear:
            raise ValueError("New revision removed saved callbacks; after_code_changed must unschedule_all and register replacements")
    else:
        logger.info("Restored account state and schedules; initialize is not repeated")
    # Resources intentionally omitted from g's checkpoint are rebuilt once per
    # fresh worker. Resource hooks cannot silently submit trading decisions.
    broker.phase = "resources"
    loaded.process_initialize(context)
    _assert_simulation_mode(context)
    broker.phase = "decision"
    if saved and state.benchmark != saved["benchmark_symbol"]:
        raise ValueError("Changing a running account's benchmark would break historical comparability; create a separate account")
    _bind_complete_calendar(scheduler, portal, day)
    timeline = engine._execution_timeline(scheduler)
    _schedule_state(scheduler, loaded)
    callback_count = 0
    for label, _original_time in timeline:
        # A preceding callback may register a different period kind.
        _bind_complete_calendar(scheduler, portal, day)
        context.current_dt = datetime.combine(day, time(15, 30))
        for callback in scheduler.callbacks_for(context.current_dt, label, previous_dt=previous_date):
            logger.set_context(trade_date=day_text, callback=callback.__name__)
            logger.info("Close callback %s (original schedule %s; decision time 15:30)", callback.__name__, label)
            callback(context)
            callback_count += 1
            _assert_simulation_mode(context)
    broker.phase = "blocked"
    context.current_dt = datetime.combine(day, time(15, 30))
    portal.validate_holdings(context)
    engine._mark_to_market(context, portal, day_text)
    engine._capture_a_share_entitlements(context, broker, day_text, actions["record_date"], entitlements)
    total = float(context.portfolio.total_value)
    if not all(math.isfinite(number) for number in (total, context.portfolio.cash, context.portfolio.positions_value)) or context.portfolio.cash < -0.005:
        raise ValueError("Simulation portfolio failed financial invariants")
    for position in context.portfolio.positions.values():
        if not 0 <= position.closeable_amount <= position.total_amount:
            raise ValueError("Simulation sellable inventory invariant failed")
    peak = max(total, float(saved["peak_value"]) if saved else initial_cash)
    performance = performance_row(day_text, context.portfolio, initial_cash,
                                  saved["previous_total"] if saved else initial_cash, peak)
    performance = {key: None if value == "unsupported_placeholder" else float(value) if key != "date" else value for key, value in performance.items()}
    performance.update(trade_date=day_text, revision_id=revision["id"], benchmark=state.benchmark)
    closes = engine._benchmark_closes(portal, state.benchmark, [day_text])
    benchmark_base = saved["benchmark_base"] if saved else closes.get("__base__")
    benchmark_last = saved["benchmark_last"] if saved else benchmark_base
    close = closes.get(day_text)
    warnings = []
    if benchmark_base is not None and close is not None and benchmark_base > 0 and close > 0:
        performance["benchmark_return"] = close / benchmark_base - 1
        performance["benchmark_daily_return"] = close / benchmark_last - 1 if benchmark_last else None
        performance["excess_return"] = performance["cumulative_return"] - performance["benchmark_return"]
    else:
        warnings.append("Benchmark baseline or dated close is unavailable; benchmark returns remain empty")
        performance["benchmark_daily_return"] = None
    positions = []
    for code, position in sorted(context.portfolio.positions.items()):
        positions.append({"date": day_text, "trade_date": day_text, "code": code, "security": code,
                          "total_amount": int(position.total_amount), "amount": int(position.total_amount),
                          "closeable_amount": int(position.closeable_amount), "avg_cost": float(position.avg_cost),
                          "price": float(position.price), "value": float(position.value), "market_value": float(position.value),
                          "pnl": (float(position.price) - float(position.avg_cost)) * position.total_amount})
    hits = dict(saved.get("revision_hits", {})) if saved else {}
    hits[str(revision["id"])] = hits.get(str(revision["id"]), 0) + 1
    context_values = _persistent_namespace(vars(context), "context")
    persisted_context = Context(context.portfolio)
    vars(persisted_context).clear()
    vars(persisted_context).update(context_values)
    checkpoint = {"version": CHECKPOINT_VERSION, "account_id": account["id"], "initial_cash": initial_cash,
                  "sdk_version": sdk_version, "runtime_contract": RUNTIME_CONTRACT,
                  "start_date": _date(account["start_date"]).isoformat(),
                  "last_trade_day": day_text, "revision_id": revision["id"], "revision_hash": revision["source_hash"],
                  "revision_hits": hits, "context": persisted_context, "g": _persistent_namespace(vars(state.g), "g"),
                  "runtime_metadata": _persistent_namespace({key: value for key, value in vars(state).items()
                       if key not in _TRANSIENT_RUNTIME_FIELDS}, "runtime"),
                  "broker": broker.checkpoint(), "schedules": _schedule_state(scheduler, loaded),
                  "entitlements": entitlements, "applied_actions": applied_actions, "applied_etf": applied_etf,
                  "corporate_action_ledger": engine._corporate_action_ledger,
                  "unsupported_actions": engine._corporate_action_unsupported_held_count,
                  "previous_total": total, "peak_value": peak, "benchmark_symbol": state.benchmark,
                  "benchmark_base": benchmark_base, "benchmark_last": close or benchmark_last}
    try:
        checkpoint_bytes = pickle.dumps(checkpoint, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        raise ValueError("Simulation state contains an unsupported persistent object; day was not committed") from None
    summary = {"trade_day": day_text, "revision_id": revision["id"], "revision_changed": revision_changed,
               "initialized": saved is None, "callback_count": callback_count, "pending_count": len(broker.pending),
               "filled_count": len(broker.trades), "decision_policy": "close_decision_next_open",
               "execution_policy": "next trading-day raw open plus strategy-configured slippage and fees",
               "default_slippage": 0., "cost_model": broker.checkpoint()["cost_model"],
               "run_type": "backtest", "simulation": True, "actual_fills": False,
               "warnings": warnings, "corporate_actions": engine._corporate_action_summary(),
               "records": _serializable(state.records), "target_portfolio_signals": _serializable(broker.target_portfolio_signals),
               "financial_mode": getattr(backend, "financial_mode", "cached_snapshot")}
    summary["financial_pit_verified"] = bool(getattr(backend, "strict_pit", False))
    summary["corporate_actions"]["adjustment_factor_checked_securities"] = sorted(related_securities)
    summary["corporate_actions"]["adjustment_factor_changed_securities"] = factor_changes
    summary["fundamental_publication_cutoff"] = (day - timedelta(days=1)).isoformat()
    logger.set_context(trade_date=day_text)
    logger.info("Day calculation complete: equity %.2f, pending %s; awaiting atomic store commit", total, len(broker.pending))
    return {"checkpoint": checkpoint_bytes, "performance": performance, "positions": positions,
            "orders": [broker.record(order) for order in broker.orders],
            "trades": [broker.record(trade) for trade in broker.trades], "summary": _serializable(summary)}
