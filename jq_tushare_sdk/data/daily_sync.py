"""Prepare real daily source data before a simulation is allowed to advance.

The shared cache lock separates provider writes from simulation-day reads. A
successful result covers core A-share daily data and statically identifiable
price dependencies; it never relaxes the worker's financial/PIT checks.
"""
from __future__ import annotations

import ast
import calendar
import math
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import sqlite3
import threading

import pandas as pd

from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.data.cache_lock import cache_write_lock
from jq_tushare_sdk.data.code_map import (
    normalize_date, to_tushare_code, is_tushare_a_share_code,
    is_tushare_fund_code, is_tushare_index_code, is_tushare_sw_index_code,
)
from jq_tushare_sdk.data.readiness import (
    _module_static_env, _context_numeric_env, _price_count_argument_node,
    _evaluate_numeric_expression,
)
from jq_tushare_sdk.runtime.daily.preflight import analyze_source
from zoneinfo import ZoneInfo
from jq_tushare_sdk.runtime.logger import redact_runtime_text as redact

CHINA = ZoneInfo("Asia/Shanghai")


class DataSyncError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(redact(message))


def _day(value):
    return datetime.strptime(normalize_date(value), "%Y%m%d").date()


def _stamp(value):
    return value.strftime("%Y%m%d")


def _days(first, last):
    while first <= last:
        yield first
        first += timedelta(days=1)


class _Provider:
    """Bound each HTTP request, retry transient failures, redact provider text."""
    def __init__(self, owner, token):
        import tushare
        self.owner = owner
        self.pro = tushare.pro_api(token, timeout=15)

    def __getattr__(self, api):
        def request(**params):
            for attempt in range(3):
                self.owner._check_cancelled()
                try:
                    result = getattr(self.pro, api)(**params)
                    self.owner._check_cancelled()
                    return result
                except DataSyncError:
                    raise
                except Exception as error:
                    detail = redact(str(error))[:400]
                    permission = any(word in detail.lower() for word in (
                        "权限", "积分", "无权", "token", "permission", "unauthorized", "无效", "认证"))
                    if permission:
                        raise DataSyncError("permission", f"Tushare {api} 权限或认证不足：{detail}。请在数据源账号补齐该接口权限后重试。") from None
                    if attempt == 2:
                        raise DataSyncError("waiting_data", f"Tushare {api} 请求失败，稍后自动重试：{detail}") from None
                    self.owner._wait(1.0 * (attempt + 1))
        return request


class _SyncBackend(TushareCacheBackend):
    def __init__(self, owner, token):
        self.owner = owner
        super().__init__(str(owner.cache_db), token=token, request_interval=0.25)
        if token:
            self._pro = _Provider(owner, token)

    def _pro_api(self):
        self.owner._check_cancelled()
        if self._pro is None:
            raise DataSyncError("configuration", "自动补齐需要配置 TUSHARE_TOKEN；当前缓存将保留，策略等待数据。")
        return self._pro

    def _wait_for_rate_limit(self):
        import time
        elapsed = time.monotonic() - self._last_request_ts
        self.owner._wait(max(0.0, self.request_interval - elapsed))
        self._last_request_ts = time.monotonic()


class DailyDataSync:
    """One synchronous, cancellable preparation job for one canonical cache.

    ``prepare`` includes ``start_date`` (usually the account's last committed
    date, so the factor predecessor is present) and never includes a trading
    date beyond ``end_date``. Calendar lookahead is only for schedule semantics.
    ``securities`` may add held/pending symbols whose names are dynamic in code.
    """
    def __init__(self, cache_db, project_root, stop_event=None):
        self.project_root = Path(project_root).resolve()
        path = Path(cache_db)
        self.cache_db = (path if path.is_absolute() else self.project_root / path).resolve()
        self.lock_path = Path(str(self.cache_db) + ".sync.lock")
        self.stop_event = stop_event or threading.Event()
        self._mutex = threading.Lock()
        self._cancel_callback = None
        self._progress_callback = None
        self._updated = {}
        self._backend = None

    def close(self):
        self.stop_event.set()

    def _cancelled(self):
        return self.stop_event.is_set() or bool(self._cancel_callback and self._cancel_callback())

    def _check_cancelled(self):
        if self._cancelled():
            raise DataSyncError("cancelled", "数据补齐已停止，已完成的数据保留供下次继续。")

    def _wait(self, seconds):
        import time
        until = time.monotonic() + seconds
        while True:
            self._check_cancelled()
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(remaining, 0.2))

    def _progress(self, phase, message, **values):
        self._check_cancelled()
        if self._progress_callback:
            self._progress_callback({"phase": phase, "message": message, **values})

    def _connect(self):
        return sqlite3.connect(str(self.cache_db), timeout=30)

    def _covered(self, api, scope, day):
        spec = self._backend._spec(api)
        with self._connect() as connection:
            marker = connection.execute(
                "SELECT row_count FROM simulation_data_coverage WHERE api_name=? AND scope=? AND trade_date=?",
                (api, scope, day)).fetchone()
            if marker is None:
                return False
            conditions, params = [], []
            if spec.date_column == "trade_date":
                conditions.append("trade_date=?")
                params.append(day)
            if scope != "all" and "ts_code" in spec.columns:
                conditions.append("ts_code=?")
                params.append(scope)
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            count = connection.execute(f"SELECT COUNT(*) FROM {spec.table}{where}", params).fetchone()[0]
            if count != marker[0]:
                return False
            if api in {"stock_st", "stk_limit"}:
                complete = connection.execute(
                    "SELECT row_count FROM security_state_coverage WHERE api_name=? AND trade_date=?", (api, day)).fetchone()
                return complete is not None and complete[0] == count and count > 0
            return count > 0 or api == "dividend"

    def _mark(self, api, scope, day, count):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO simulation_data_coverage VALUES (?,?,?,?,?)",
                (api, scope, day, int(count), datetime.now(CHINA).isoformat()))

    def _count(self, api, scope="all", day=None):
        spec = self._backend._spec(api)
        clauses, params = [], []
        if day is not None and spec.date_column == "trade_date":
            clauses.append("trade_date=?")
            params.append(day)
        if scope != "all" and "ts_code" in spec.columns:
            clauses.append("ts_code=?")
            params.append(scope)
        with self._connect() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {spec.table}" +
                                      (" WHERE " + " AND ".join(clauses) if clauses else ""), params).fetchone()[0]

    def _add(self, api, count):
        self._updated[api] = self._updated.get(api, 0) + int(count)

    def _daily_snapshot(self, api, day, scope="all"):
        if self._covered(api, scope, day):
            return False
        self._progress("daily", f"正在补齐 {day} 的 {api}", api=api, date=_day(day).isoformat())
        backend = self._backend
        if api in {"stock_st", "stk_limit"}:
            count = backend.update_data(api, trade_date=day)
            self._mark(api, scope, day, count)
            self._add(api, count)
            return True
        params = {"trade_date": day}
        if scope != "all":
            params["ts_code"] = scope
        # The ordinary cache updater is single-page for these APIs. The same
        # overlap-detecting pagination used for security state prevents a 5000
        # row response from silently omitting the rest of the market.
        frame = backend._security_pages(api, params)
        spec = backend._spec(api)
        if frame.empty or "trade_date" not in frame or set(frame.trade_date.map(normalize_date)) != {day}:
            raise DataSyncError("waiting_data", f"{api} 尚未提供 {day} 的完整有效数据，保留等待状态。")
        if scope != "all" and set(frame.ts_code.astype(str)) != {scope}:
            raise DataSyncError("waiting_data", f"{api} 返回的证券与请求 {scope} 不一致。")
        if frame.duplicated(list(spec.primary_keys)).any():
            raise DataSyncError("waiting_data", f"{api} 返回重复记录，不能确认完整快照。")
        if api in {"daily", "fund_daily", "index_daily", "sw_daily"}:
            required = ("open", "close", "vol")
        elif api in {"adj_factor", "fund_adj"}:
            required = ("adj_factor",)
        else:
            required = ("close",)
        for column in required:
            values = pd.to_numeric(frame[column], errors="coerce") if column in frame else pd.Series(dtype=float)
            if len(values) != len(frame) or not values.map(math.isfinite).all():
                raise DataSyncError("waiting_data", f"{api} 返回缺失或无效必需字段 {column}。")
            if (values < 0).any() or (column in {"close", "adj_factor"} and (values <= 0).any()):
                raise DataSyncError("waiting_data", f"{api} 返回无效必需字段 {column}。")
        frame = backend._normalize_frame(api, frame, spec)
        columns = list(frame.columns)
        rows = list(frame.astype(object).where(pd.notna(frame), None).itertuples(index=False, name=None))
        with self._connect() as connection:
            clause, arguments = "trade_date=?", [day]
            if scope != "all":
                clause += " AND ts_code=?"
                arguments.append(scope)
            connection.execute(f"DELETE FROM {spec.table} WHERE {clause}", arguments)
            connection.executemany(f"INSERT INTO {spec.table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", rows)
            connection.execute("INSERT OR REPLACE INTO simulation_data_coverage VALUES (?,?,?,?,?)",
                               (api, scope, day, len(rows), datetime.now(CHINA).isoformat()))
        self._add(api, len(rows))
        return True

    def _metadata(self, api, day, scope="all"):
        if self._covered(api, scope, day):
            return
        self._progress("metadata", f"正在更新 {api}", api=api)
        if api == "stock_basic":
            count = 0
            for status in ("L", "D", "P"):
                frame = self._backend._security_pages(api, {"list_status": status})
                if status in {"L", "D"} and frame.empty:
                    raise DataSyncError("waiting_data", f"stock_basic 缺少 {status} 状态证券，不能确认完整基础资料。")
                if "list_status" not in frame:
                    frame["list_status"] = status
                count += self._backend.cache_data(api, frame)
        else:
            count = self._backend.update_data(api, **({"ts_code": scope} if scope != "all" else {}))
        current = self._count(api, scope)
        if not current and api != "dividend":
            raise DataSyncError("waiting_data", f"{api} 返回为空，基础资料尚未准备好。")
        self._mark(api, scope, day, current)
        self._add(api, count)

    def _official_calendar(self, lower, upper):
        frame = self._backend.fetch("trade_cal", exchange="SSE", start_date=_stamp(lower), end_date=_stamp(upper))
        def valid(values):
            if not {"cal_date", "is_open"}.issubset(values.columns):
                return False
            dates = values.cal_date.map(normalize_date)
            flags = pd.to_numeric(values.is_open, errors="coerce")
            return (not dates.duplicated().any() and set(dates) == {_stamp(day) for day in _days(lower, upper)}
                    and flags.isin([0, 1]).all())
        if not valid(frame):
            self._progress("calendar", "正在补齐官方交易日历", api="trade_cal")
            self._add("trade_cal", self._backend.update_data("trade_cal", start_date=_stamp(lower), end_date=_stamp(upper), exchange="SSE"))
            frame = self._backend.fetch("trade_cal", exchange="SSE", start_date=_stamp(lower), end_date=_stamp(upper))
        if not valid(frame):
            raise DataSyncError("waiting_data", "官方交易日历仍不完整，不能用工作日推断交易日。")
        return sorted(_day(value) for value in frame.loc[pd.to_numeric(frame.is_open) == 1, "cal_date"])

    def _validate_day(self, day):
        with self._connect() as connection:
            for table, column in (("daily_basic", "close"), ("adj_factor", "adj_factor"), ("stk_limit", "up_limit")):
                missing = connection.execute(
                    f"SELECT q.ts_code FROM daily_quote q LEFT JOIN {table} d ON q.ts_code=d.ts_code AND q.trade_date=d.trade_date "
                    f"WHERE q.trade_date=? AND q.vol>0 AND (d.{column} IS NULL OR d.{column}<=0) LIMIT 5", (day,)).fetchall()
                if missing:
                    # Leave a repairable gap: a later successful provider
                    # response must be fetched even if its old row count was
                    # internally consistent.
                    connection.execute("DELETE FROM simulation_data_coverage WHERE api_name=? AND trade_date=?", (table, day))
                    connection.commit()
                    raise DataSyncError("waiting_data", f"{day} 的 {table} 缺少实际交易证券：{','.join(row[0] for row in missing)}。")
            # Unchanged names may legitimately have no namechange history.
            # The existing resolver uses an honest security-code label for
            # these rows; the complete dated ST list remains mandatory.

    def prepare(self, start_date, end_date, benchmark="000300.XSHG", progress=None,
                cancelled=None, source="", securities=()):
        if not self._mutex.acquire(blocking=False):
            raise DataSyncError("busy", "当前已有数据补齐任务，等待完成后继续。")
        self._cancel_callback, self._progress_callback = cancelled, progress
        self._updated = {}
        try:
            lower, upper = _day(start_date), _day(end_date)
            if lower > upper:
                return {"state": "ready", "trade_dates": [], "required_trade_date": None, "updated_rows": {}, "reused_days": 0}
            self._progress("waiting_lock", "正在等待数据缓存写入锁")
            with cache_write_lock(self.cache_db, cancelled=self._cancelled):
                self._check_cancelled()
                self._backend = _SyncBackend(self, os.environ.get("TUSHARE_TOKEN"))
                with self._connect() as connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS simulation_data_coverage (api_name TEXT NOT NULL,scope TEXT NOT NULL,trade_date TEXT NOT NULL,row_count INTEGER NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(api_name,scope,trade_date))")
                symbols = set(analyze_source(source).get("securities", ())) if source else set()
                symbols.update(securities)
                symbols.add(benchmark)
                symbols = {to_tushare_code(symbol) for symbol in symbols}
                lookback = 0
                if source:
                    tree = ast.parse(source)
                    env = _module_static_env(tree)
                    context = _context_numeric_env(tree, env)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Call):
                            count_node = _price_count_argument_node(node)
                            count = _evaluate_numeric_expression(count_node, env, context) if count_node else None
                            if isinstance(count, (int, float)) and 0 < count <= 10000:
                                lookback = max(lookback, int(count))
                price_lower = lower - timedelta(days=max(lookback * 3 + 7, 14)) if lookback else lower
                calendar_lower = min(price_lower, lower - timedelta(days=45))
                calendar_upper = upper.replace(day=calendar.monthrange(upper.year, upper.month)[1]) + timedelta(days=7)
                open_days = self._official_calendar(calendar_lower, calendar_upper)
                target_days = [day for day in open_days if lower <= day <= upper]
                required = max((day for day in open_days if day <= upper), default=None)
                if required is None:
                    raise DataSyncError("waiting_data", "日历中找不到当前截止日之前的交易日。")
                self._progress("calendar", f"已确认最近应处理交易日 {required.isoformat()}",
                               required_trade_date=required.isoformat())
                day_stamp = _stamp(required)
                predecessors = [day for day in open_days if day < (target_days[0] if target_days else lower)]
                full_lower = min(lower, predecessors[-1]) if predecessors else lower
                price_lower = min(price_lower, full_lower)
                for api in ("stock_basic", "namechange", "bse_mapping"):
                    self._metadata(api, day_stamp)
                # Historical lookbacks reuse the existing cache when all three
                # base tables cover the date. Missing interior dates are fetched.
                price_days = [day for day in open_days if price_lower <= day <= upper]
                completed, reused = 0, 0
                for day in price_days:
                    self._check_cancelled()
                    stamp = _stamp(day)
                    is_target = day >= full_lower
                    changed = False
                    for api in ("daily", "daily_basic", "adj_factor", "stock_st", "stk_limit"):
                        if not is_target:
                            if api in {"daily_basic", "stock_st", "stk_limit"}:
                                continue
                            if self._count(api, day=stamp):
                                continue
                        changed = self._daily_snapshot(api, stamp) or changed
                    for symbol in sorted(symbols):
                        if is_tushare_sw_index_code(symbol):
                            apis = ("sw_daily",)
                        elif is_tushare_index_code(symbol):
                            apis = ("index_daily",)
                        elif is_tushare_fund_code(symbol):
                            apis = ("fund_daily", "fund_adj")
                        else:
                            continue
                        for api in apis:
                            if not is_target and self._count(api, symbol, stamp):
                                continue
                            changed = self._daily_snapshot(api, stamp, symbol) or changed
                    if is_target:
                        self._validate_day(stamp)
                    completed += 1
                    reused += not changed
                    self._progress("daily", f"已准备 {day.isoformat()} 的日线与配套数据", date=day.isoformat(), completed=completed, total=len(price_days))
                for symbol in sorted(symbols):
                    if is_tushare_a_share_code(symbol) and not is_tushare_index_code(symbol):
                        self._metadata("dividend", day_stamp, symbol)
                if "get_index_stocks" in source:
                    for symbol in sorted(symbols):
                        if is_tushare_index_code(symbol):
                            self._progress("index_members", f"正在补齐 {symbol} 的历史指数成分", api="index_weight")
                            self._add("index_weight", self._backend.update_data("index_weight", start_date=_stamp(lower - timedelta(days=90)), end_date=_stamp(upper), index_code=symbol))
                result = {"state": "ready", "requested_start": lower.isoformat(), "requested_end": upper.isoformat(),
                          "required_trade_date": required.isoformat(), "trade_dates": [day.isoformat() for day in target_days],
                          "prepared_from": price_lower.isoformat(),
                          "updated_rows": dict(self._updated), "reused_days": reused,
                          "scope": "核心 A 股日线、历史状态和明确证券依赖；动态证券、行业与财务历史仍由策略执行时严格检查。"}
                self._progress("ready", "日线及配套数据已准备，接下来推进策略", completed=len(price_days), total=len(price_days))
                return result
        except InterruptedError:
            raise DataSyncError("cancelled", "数据补齐已停止，已完成的数据保留供下次继续。") from None
        except DataSyncError:
            raise
        except Exception as error:
            raise DataSyncError("waiting_data", f"数据补齐未完成：{redact(str(error))[:500]}") from None
        finally:
            if self._backend is not None:
                self._backend.close()
                self._backend = None
            self._cancel_callback = self._progress_callback = None
            self._mutex.release()
