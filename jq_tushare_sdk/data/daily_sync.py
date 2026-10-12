"""Prepare real daily source data before a simulation is allowed to advance.

The shared cache lock separates provider writes from simulation-day reads. A
successful result covers core A-share daily data and statically identifiable
price dependencies; it never relaxes the worker's financial/PIT checks.
"""
from __future__ import annotations

import ast
import calendar
import math
from datetime import date, datetime, time, timedelta
import os
from pathlib import Path
import sqlite3
import threading

import pandas as pd

from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.data.cache_lock import cache_write_lock
from jq_tushare_sdk.data.income_periods import required_income_periods, income_period_end
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
                    if any(word in detail.lower() for word in (
                            "参数校验", "参数不能都为空", "invalid parameter", "missing required parameter")):
                        raise DataSyncError("parameters", f"Tushare {api} 请求参数无效：{detail}") from None
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
                "SELECT row_count,updated_at FROM simulation_data_coverage WHERE api_name=? AND scope=? AND trade_date=?",
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
            if api == "stock_st":
                # The provider publishes the daily ST list at 09:20. An early
                # partial response is not a completed session snapshot, even
                # when its cached row count still matches the coverage marker.
                try:
                    captured_at = datetime.fromisoformat(marker[1])
                except (TypeError, ValueError):
                    return False
                if captured_at.tzinfo is None or captured_at < datetime.combine(_day(day), time(9, 20), CHINA):
                    return False
            if api in {"stock_st", "stk_limit"}:
                complete = connection.execute(
                    "SELECT row_count FROM security_state_coverage WHERE api_name=? AND trade_date=?", (api, day)).fetchone()
                return complete is not None and complete[0] == count and count > 0
            return count > 0 or api in {"dividend", "suspend_d"}

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
        if api == "stock_st" and datetime.now(CHINA) < datetime.combine(_day(day), time(9, 20), CHINA):
            raise DataSyncError("waiting_data", f"stock_st 的 {day} 名单在北京时间 09:20 更新；等待发布后取得完整当日状态，盘前策略尚未执行。")
        if self._covered(api, scope, day):
            return False
        self._progress("daily", f"正在补齐 {day} 的 {api}", api=api, date=_day(day).isoformat())
        backend = self._backend
        if api == "suspend_d":
            frame = backend._security_pages(api, {"trade_date": day})
            if not frame.empty and set(frame.trade_date.map(normalize_date)) != {day}:
                raise DataSyncError("waiting_data", f"suspend_d 返回了不同日期的停牌记录：{day}")
            spec = backend._spec(api)
            frame = backend._normalize_frame(api, frame, spec)
            with self._connect() as connection:
                connection.execute(f"DELETE FROM {spec.table} WHERE trade_date=?", (day,))
                if not frame.empty:
                    columns = list(frame.columns)
                    rows = list(frame.astype(object).where(pd.notna(frame), None).itertuples(index=False, name=None))
                    connection.executemany(f"INSERT INTO {spec.table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", rows)
                connection.execute("INSERT OR REPLACE INTO simulation_data_coverage VALUES (?,?,?,?,?)",
                                   (api, scope, day, len(frame), datetime.now(CHINA).isoformat()))
            self._add(api, len(frame))
            return True
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
            observed_at = datetime.now(CHINA)
            connection.execute("INSERT OR REPLACE INTO simulation_data_coverage VALUES (?,?,?,?,?)",
                               (api, scope, day, len(rows), observed_at.isoformat()))
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
        elif api == "dividend" and scope == "all":
            # The all-security intraday reference covers this ex-dividend day.
            # An unfiltered dividend request is rejected by the provider; it
            # is also not evidence of complete historical corporate actions.
            frame = self._backend._security_pages(api, {"ex_date": day})
            if not frame.empty and (
                    "ex_date" not in frame or
                    set(frame.ex_date.map(normalize_date)) != {day}):
                raise DataSyncError("waiting_data", f"dividend 返回了不同除权日期的记录：{day}")
            count = self._backend.cache_data(api, frame)
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

    def _financial_dependencies(self, lower, upper, source):
        if "get_fundamentals" not in source:
            return
        periods = set(required_income_periods(lower, upper))
        latest = max(int(p[:4]) * 4 + int(p[-1]) - 1 for p in periods)
        periods.update(f"{i//4}q{i%4+1}" for i in range(latest-3, latest+1))
        if source:
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    value = node.value.lower()
                    if len(value) == 6 and value[:4].isdigit() and value[4] == "q" and value[5] in "1234":
                        periods.add(value)
        # Single-quarter values need the preceding cumulative report, and TTM
        # growth requires the same period last year. Source snapshots captured
        # today remain explicitly unverified as historical revisions.
        for period in list(periods):
            year, quarter = int(period[:4]), int(period[-1])
            if quarter > 1:
                periods.add(f"{year}q{quarter-1}")
            if "indicator" in source:
                periods.update(f"{year-1}q{q}" for q in range(1, quarter+1))
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS income_period_coverage (period TEXT PRIMARY KEY,row_count INTEGER NOT NULL,captured_at TEXT NOT NULL)")
        for period in sorted(periods):
            end = income_period_end(period)
            with self._connect() as connection:
                marker = connection.execute("SELECT row_count,captured_at FROM income_period_coverage WHERE period=?", (end,)).fetchone()
                count = connection.execute("SELECT COUNT(*) FROM income_statement WHERE end_date=? AND report_type='1'", (end,)).fetchone()[0]
            if marker and marker[0] == count and count and marker[1][:10] == datetime.now(CHINA).date().isoformat():
                continue
            self._progress("financial", f"正在补齐 {period} 的完整利润表", api="income", period=period)
            self._add("income", self._backend.update_data("income", period=period))
            with self._connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM income_statement WHERE end_date=? AND report_type='1'", (end,)).fetchone()[0]
                if not count:
                    raise DataSyncError("waiting_data", f"利润表 {period} 仍然缺失，策略不能继续")
                connection.execute("INSERT OR REPLACE INTO income_period_coverage VALUES (?,?,?)", (end,count,datetime.now(CHINA).isoformat()))

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
                if self._backend.adjustment_source == "sina_hfq":
                    from jq_tushare_sdk.data.market_reference import refresh_precise_factors
                    factor_codes = [symbol for symbol in symbols if is_tushare_a_share_code(symbol)
                                    and not is_tushare_index_code(symbol) and symbol.endswith((".SH", ".SZ"))]
                    self._progress("precise_factors", "正在补齐明确股票依赖的高精度复权因子")
                    refresh_precise_factors(self.cache_db, factor_codes, cancelled=self._cancelled,
                                           progress=lambda row: self._add("sina_precise_factors", row["nodes"]))
                if os.environ.get("JQTS_SYNC_ANNOUNCEMENTS") == "szse":
                    from jq_tushare_sdk.data.market_reference import sync_szse_announcements
                    announcement_codes = [symbol for symbol in symbols if is_tushare_a_share_code(symbol)
                                          and not is_tushare_index_code(symbol) and symbol.endswith(".SZ")]
                    self._progress("announcements", "正在补齐深交所公告的原始发布时间")
                    receipts = sync_szse_announcements(self.cache_db, announcement_codes,
                        lower - timedelta(days=45), upper, cancelled=self._cancelled)
                    self._add("szse_announcements", sum(len(row["rows"]) for row in receipts))
                lookback = 0
                if source:
                    tree = ast.parse(source)
                    env = _module_static_env(tree)
                    context = _context_numeric_env(tree, env)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Call):
                            name=getattr(node.func,"id",getattr(node.func,"attr",""))
                            keyword,position=("frequency",4) if name=="get_price" else ("unit",1 if name=="history" else 2)
                            unit=next((k.value for k in node.keywords if k.arg==keyword),node.args[position] if len(node.args)>position else None)
                            if isinstance(unit,ast.Constant) and unit.value in {"minute","1m","1min"}:
                                continue  # minute counts must not become daily lookbacks
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
                    for api in ("daily", "daily_basic", "adj_factor", "stock_st", "stk_limit", "suspend_d"):
                        if not is_target:
                            if api in {"daily_basic", "stock_st", "stk_limit"}:
                                continue
                            # A missing historical bar needs official suspension
                            # evidence too. Some suspension rows are not proof of
                            # a complete day; reuse only its full-fetch marker.
                            if api != "suspend_d" and self._count(api, day=stamp):
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
                self._financial_dependencies(lower, upper, source)
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
            if self._backend.adjustment_source == "daily_ex_reference":
                from jq_tushare_sdk.data.market_reference import prepare_reference_price_factors
                factor_codes = [symbol for symbol in symbols if is_tushare_a_share_code(symbol)
                                and not is_tushare_index_code(symbol)]
                self._progress("reference_factors", "正在归档除权参考报价及可复验的复权规则")
                references = prepare_reference_price_factors(self.cache_db, factor_codes,
                                                             price_lower, upper)
                result["reference_price_factors"] = references
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
