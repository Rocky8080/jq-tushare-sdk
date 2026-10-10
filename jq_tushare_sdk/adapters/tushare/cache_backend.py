from __future__ import annotations

import os
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from jq_tushare_sdk.data.code_map import normalize_date
from jq_tushare_sdk.data.cache_lock import cache_write_lock
from jq_tushare_sdk.data.income_periods import income_period_end
from jq_tushare_sdk.data.income_periods import required_income_periods
from jq_tushare_sdk.data.financial_provenance import (asof_timestamp, normalized_payload, capture_timestamp)


DEFAULT_PACKAGE_ENV = "JQ_TUSHARE_SDK_TUSHARE_PACKAGE"


@dataclass(frozen=True)
class ApiSpec:
    table: str
    primary_keys: tuple[str, ...]
    date_column: str | None
    columns: tuple[str, ...]


_API_SPECS: dict[str, ApiSpec] = {
    "bse_mapping": ApiSpec(
        "bse_mapping", ("o_code",), None, ("name", "o_code", "n_code", "list_date"),
    ),
    "stock_st": ApiSpec(
        "stock_st", ("ts_code", "trade_date"), "trade_date",
        ("ts_code", "name", "trade_date", "type", "type_name"),
    ),
    "stk_limit": ApiSpec(
        "stk_limit", ("ts_code", "trade_date"), "trade_date",
        ("ts_code", "trade_date", "pre_close", "up_limit", "down_limit"),
    ),
    "namechange": ApiSpec(
        "security_name_history", ("ts_code", "start_date", "name"), "start_date",
        ("ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"),
    ),
    # A partial per-symbol fetch is NOT evidence of a complete daily ST list.
    "security_state_coverage": ApiSpec(
        "security_state_coverage", ("api_name", "trade_date"), "trade_date",
        ("api_name", "trade_date", "row_count"),
    ),
    "daily": ApiSpec(
        table="daily_quote",
        primary_keys=("ts_code", "trade_date"),
        date_column="trade_date",
        columns=(
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
        ),
    ),
    "fund_daily": ApiSpec(
        table="fund_daily_quote",
        primary_keys=("ts_code", "trade_date"),
        date_column="trade_date",
        columns=(
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
        ),
    ),
    "daily_basic": ApiSpec(
        table="daily_basic",
        primary_keys=("ts_code", "trade_date"),
        date_column="trade_date",
        columns=(
            "ts_code",
            "trade_date",
            "close",
            "turnover_rate",
            "turnover_rate_f",
            "volume_ratio",
            "pe",
            "pe_ttm",
            "pb",
            "ps",
            "ps_ttm",
            "dv_ratio",
            "dv_ttm",
            "total_share",
            "float_share",
            "free_share",
            "total_mv",
            "circ_mv",
        ),
    ),
    "adj_factor": ApiSpec(
        table="adj_factor",
        primary_keys=("ts_code", "trade_date"),
        date_column="trade_date",
        columns=("ts_code", "trade_date", "adj_factor"),
    ),
    "fund_adj": ApiSpec(
        table="fund_adj_factor",
        primary_keys=("ts_code", "trade_date"),
        date_column="trade_date",
        columns=("ts_code", "trade_date", "adj_factor"),
    ),
    "dividend": ApiSpec(
        table="corporate_actions",
        primary_keys=("ts_code", "record_date", "ex_date", "pay_date"),
        date_column="ex_date",
        columns=(
            "ts_code",
            "end_date",
            "ann_date",
            "div_proc",
            "stk_div",
            "stk_bo_rate",
            "stk_co_rate",
            "cash_div",
            "cash_div_tax",
            "record_date",
            "ex_date",
            "pay_date",
            "div_listdate",
        ),
    ),
    "stock_basic": ApiSpec(
        table="stock_basic",
        primary_keys=("ts_code",),
        date_column=None,
        columns=(
            "ts_code",
            "symbol",
            "name",
            "area",
            "industry",
            "fullname",
            "enname",
            "cnspell",
            "market",
            "exchange",
            "curr_type",
            "list_status",
            "list_date",
            "delist_date",
            "is_hs",
            "act_name",
            "act_ent_type",
        ),
    ),
    "trade_cal": ApiSpec(
        table="trade_calendar",
        primary_keys=("exchange", "cal_date"),
        date_column="cal_date",
        columns=("exchange", "cal_date", "is_open", "pretrade_date"),
    ),
    "index_daily": ApiSpec(
        table="index_daily",
        primary_keys=("ts_code", "trade_date"),
        date_column="trade_date",
        columns=(
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
        ),
    ),
    "sw_daily": ApiSpec(
        table="sw_daily",
        primary_keys=("ts_code", "trade_date"),
        date_column="trade_date",
        columns=(
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
        ),
    ),
    "index_weight": ApiSpec(
        table="index_weight",
        primary_keys=("index_code", "con_code", "trade_date"),
        date_column="trade_date",
        columns=("index_code", "con_code", "trade_date", "weight"),
    ),
    "forecast": ApiSpec(
        table="earnings_forecast",
        primary_keys=("ts_code", "ann_date", "end_date", "type"),
        date_column="ann_date",
        columns=("ts_code", "ann_date", "end_date", "type", "p_change_min", "p_change_max", "net_profit_min", "net_profit_max", "last_parent_net", "first_ann_date", "summary", "change_reason"),
    ),
    "forecast_coverage": ApiSpec(
        table="forecast_coverage", primary_keys=("scope", "start_date", "end_date"), date_column="end_date",
        columns=("scope", "start_date", "end_date", "row_count", "observed_at", "source"),
    ),
    "income": ApiSpec(
        table="income_statement",
        primary_keys=("ts_code", "end_date", "report_type"),
        date_column="end_date",
        columns=(
            "ts_code",
            "ann_date",
            "f_ann_date",
            "end_date",
            "report_type",
            "comp_type",
            "total_revenue",
            "revenue",
            "n_income",
            "n_income_attr_p",
        ),
    ),
    "index_member": ApiSpec(
        table="sw_industry_member",
        primary_keys=("index_code", "con_code", "in_date", "out_date"),
        date_column=None,
        columns=("index_code", "con_code", "in_date", "out_date", "is_new"),
    ),
    "index_member_all": ApiSpec(
        table="sw_industry_member_all",
        primary_keys=("l1_code", "l2_code", "l3_code", "ts_code", "in_date", "out_date"),
        date_column=None,
        columns=(
            "l1_code",
            "l1_name",
            "l2_code",
            "l2_name",
            "l3_code",
            "l3_name",
            "ts_code",
            "name",
            "in_date",
            "out_date",
            "is_new",
        ),
    ),
    "index_classify": ApiSpec(
        table="sw_industry_classify",
        primary_keys=("index_code", "level"),
        date_column=None,
        columns=(
            "index_code",
            "industry_name",
            "level",
            "industry_code",
            "is_pub",
            "parent_code",
            "src",
        ),
    ),
    "jq_industry_classify": ApiSpec(
        table="jq_industry_classify",
        primary_keys=("industry_code", "start_date"),
        date_column=None,
        columns=(
            "industry_code",
            "industry_name",
            "level",
            "start_date",
            "end_date",
            "parent_code",
            "source_sha256",
        ),
    ),
    "jq_industry_member": ApiSpec(
        table="jq_industry_member",
        primary_keys=("security", "in_date", "out_date"),
        date_column=None,
        columns=(
            "security",
            "sw_l1_code",
            "sw_l2_code",
            "sw_l3_code",
            "in_date",
            "out_date",
            "source_sha256",
        ),
    ),
}

_DEFAULT_UPDATE_APIS = (
    "stock_basic",
    "trade_cal",
    "stock_st",
    "stk_limit",
    "namechange",
    "bse_mapping",
    "daily",
    "daily_basic",
    "adj_factor",
    "dividend",
    "income",
    "index_daily",
    "index_weight",
    "index_member_all",
)
_DEFAULT_INDEX_CODES = (
    "399006.SZ",
    "000688.SH",
    "000001.SH",
    "399001.SZ",
    "000300.SH",
    "000905.SH",
    "000852.SH",
    "000985.CSI",
)
_DEFAULT_SW_INDEX_CODES = (
    "801010.SI", "801030.SI", "801040.SI", "801050.SI", "801080.SI",
    "801880.SI", "801110.SI", "801120.SI", "801130.SI", "801140.SI",
    "801150.SI", "801160.SI", "801170.SI", "801180.SI", "801200.SI",
    "801210.SI", "801780.SI", "801790.SI", "801230.SI", "801710.SI",
    "801720.SI", "801730.SI", "801890.SI", "801740.SI", "801750.SI",
    "801760.SI", "801770.SI", "801950.SI", "801960.SI", "801970.SI",
    "801980.SI",
)
_STOCK_BASIC_LIST_STATUSES = ("L", "D", "P")
_QUARTER_ENDS = {"q1": "0331", "q2": "0630", "q3": "0930", "q4": "1231"}
_INCOME_UPDATE_FIELDS = ",".join(_API_SPECS["income"].columns)


class TushareCacheBackend:
    def __init__(
        self,
        cache_db: str,
        token: str | None = None,
        cache_mode: str = "strict_local",
        package_path: str | None = None,
        request_interval: float = 0.2,
        *,
        strict_pit: bool = False,
        financial_mode: str = "cached_snapshot",
    ):
        self.cache_db = str(cache_db)
        if financial_mode not in {"cached_snapshot", "research_reconstructed", "strict_pit"}:
            raise ValueError("financial_mode must be cached_snapshot, research_reconstructed, or strict_pit")
        self.strict_pit = bool(strict_pit) or financial_mode == "strict_pit"
        self.financial_mode = "strict_pit" if self.strict_pit else financial_mode
        self.token = token
        self.cache_mode = cache_mode
        self.package_path = package_path
        self.request_interval = float(request_interval)
        self._pro = None
        self._last_request_ts = 0.0
        self._read_conn = None
        self._read_lock = threading.RLock()
        self.read_only = os.environ.get("JQTS_SIMULATION_READ_ONLY") == "1"
        if not self.read_only:
            self._ensure_schema()

    @classmethod
    def resolve_package_path(cls, start_path: str | Path | None = None, package_path: str | Path | None = None) -> Path:
        raise ImportError(
            "TushareCacheBackend is now self-contained and does not resolve external "
            "join_tushare packages. Use cache_db inside this project instead."
        )

    def fetch(self, api_name: str, **params) -> pd.DataFrame:
        if api_name == "financial_revision":
            return self.financial_revisions(**params)
        spec = self._spec(api_name)
        where_sql, sql_params = self._where_clause(api_name, spec, params)
        latest_per_code = bool(params.get("latest_per_code", False))
        positive_volume = bool(params.get("positive_volume", False))
        limit_per_code = params.get("limit_per_code")
        if positive_volume and "vol" in spec.columns:
            where_sql += " AND vol > 0" if where_sql else " WHERE vol > 0"
        row_limit = 1 if latest_per_code else int(limit_per_code) if limit_per_code else None
        requested_fields = [
            field.strip()
            for field in str(params.get("fields") or "").split(",")
            if field.strip() in spec.columns
        ]
        select_columns = ", ".join(requested_fields) if requested_fields else "*"
        if row_limit and "ts_code" in spec.primary_keys and spec.date_column:
            sql = (
                "SELECT * FROM ("
                f"SELECT *, ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY {spec.date_column} DESC) AS _row_number "
                f"FROM {spec.table}{where_sql}"
                ") WHERE _row_number <= ?"
            )
            sql_params = [*sql_params, row_limit]
        else:
            sql = f"SELECT {select_columns} FROM {spec.table}{where_sql}"
        order_columns = []
        if spec.date_column:
            order_columns.append(spec.date_column)
        if "ts_code" in spec.primary_keys:
            order_columns.append("ts_code")
        elif "index_code" in spec.primary_keys:
            order_columns.append("index_code")
            if "con_code" in spec.columns:
                order_columns.append("con_code")
        if order_columns and not bool(params.get("unordered", False)):
            sql += " ORDER BY " + ", ".join(order_columns)
        frame = self._read_sql(sql, sql_params)
        frame = frame.drop(columns=["_row_number"], errors="ignore")
        return self._project_fields(frame, params.get("fields"))

    def daily_basic_asof(self, date) -> pd.DataFrame:
        """Latest observed per-security snapshot, without a suspension-day cap.

        Selection stays in indexed SQLite; callers receive at most one row per
        security and must check each actual trade_date for availability.
        """
        return self.fetch("daily_basic", end_date=normalize_date(date), latest_per_code=True)

    def status(self, api_name: str) -> dict:
        spec = self._spec(api_name)
        exists = self._table_exists(spec.table)
        payload = {"api_name": api_name, "table": spec.table, "exists": exists}
        if not exists:
            payload["record_count"] = 0
            return payload

        with self._connect() as conn:
            record_count = conn.execute(f"SELECT COUNT(*) FROM {spec.table}").fetchone()[0]
            payload["record_count"] = int(record_count)
            if spec.date_column:
                row = conn.execute(
                    f"SELECT MIN({spec.date_column}), MAX({spec.date_column}) FROM {spec.table}"
                ).fetchone()
                payload["min_date"] = row[0]
                payload["max_date"] = row[1]
        return payload

    def security_state_gaps(self, start: str, end: str) -> dict:
        """SQL preflight on actual cached dates/rows, not just min/max bounds."""
        gaps = {}
        with self._connect() as conn:
            for api in ("stock_st", "stk_limit"):
                rows = conn.execute(
                    f"SELECT c.cal_date FROM trade_calendar c LEFT JOIN security_state_coverage v "
                    f"ON v.trade_date=c.cal_date AND v.api_name=? "
                    f"LEFT JOIN (SELECT trade_date,COUNT(*) AS n FROM {api} "
                    "WHERE trade_date BETWEEN ? AND ? GROUP BY trade_date) a ON a.trade_date=c.cal_date "
                    "WHERE c.exchange='SSE' AND c.is_open=1 AND c.cal_date BETWEEN ? AND ? "
                    "AND (v.row_count IS NULL OR a.n IS NULL OR v.row_count!=a.n) ORDER BY c.cal_date",
                    (api, start, end, start, end),
                ).fetchall()
                if rows:
                    gaps[api] = [r[0] for r in rows]
            if gaps:
                return gaps
            missing = conn.execute(
                "SELECT q.ts_code,q.trade_date FROM daily_quote q LEFT JOIN stk_limit l "
                "ON l.ts_code=q.ts_code AND l.trade_date=q.trade_date "
                "WHERE q.trade_date BETWEEN ? AND ? AND q.vol>0 "
                "AND (l.up_limit IS NULL OR l.down_limit IS NULL OR l.up_limit<=0 OR l.down_limit<0) LIMIT 10",
                (start, end),
            ).fetchall()
            if missing:
                gaps['stk_limit_rows'] = [list(r) for r in missing]
            missing = conn.execute(
                "SELECT q.ts_code,MIN(q.trade_date) FROM daily_quote q "
                "WHERE q.trade_date BETWEEN ? AND ? AND q.vol>0 AND NOT EXISTS "
                "(SELECT 1 FROM security_name_history n WHERE n.ts_code=q.ts_code "
                "AND n.start_date<=q.trade_date AND (n.end_date IS NULL OR n.end_date='' OR n.end_date>=q.trade_date)) "
                "GROUP BY q.ts_code LIMIT 20", (start, end),
            ).fetchall()
            if missing:
                gaps['namechange'] = [list(r) for r in missing]
        return gaps

    def cache_data(self, api_name: str, data: pd.DataFrame) -> int:
        if data is None or data.empty:
            return 0
        spec = self._spec(api_name)
        frame = self._normalize_frame(api_name, data, spec)
        if frame.empty:
            return 0

        columns = list(frame.columns)
        placeholders = ", ".join("?" for _ in columns)
        column_names = ", ".join(columns)
        conflict_columns = ", ".join(spec.primary_keys)
        update_columns = [column for column in columns if column not in spec.primary_keys]
        if update_columns:
            update_sql = "DO UPDATE SET " + ", ".join(
                f"{column}=excluded.{column}" for column in update_columns
            )
        else:
            update_sql = "DO NOTHING"
        sql = (
            f"INSERT INTO {spec.table} ({column_names}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_columns}) {update_sql}"
        )
        rows = [tuple(row) for row in frame[columns].itertuples(index=False, name=None)]
        with self._write_connect() as conn:
            if api_name in {"income", "forecast"}:
                self._append_financial_revisions(conn, api_name, data)
            if api_name in {"stock_st", "stk_limit"}:
                conn.executemany(
                    "DELETE FROM security_state_coverage WHERE api_name=? AND trade_date=?",
                    [(api_name, day) for day in frame["trade_date"].unique()],
                )
            conn.executemany(sql, rows)
            conn.commit()
        return len(rows)

    def update_data(self, api_name: str, start_date: str | None = None, end_date: str | None = None, **params) -> int:
        if self.read_only:
            raise ValueError("Simulation workers cannot update the market-data cache")
        if self.cache_mode == "strict_local" and not self.token:
            raise ValueError("update_data requires TUSHARE_TOKEN or token even when backtest reads are strict_local")

        if api_name in {"stock_st", "stk_limit"}:
            if params.get("ts_code"):
                data = self._security_pages(api_name, self._api_params(
                    api_name, start_date=start_date, end_date=end_date, **params))
                return self.cache_data(api_name, data)
            days = ([normalize_date(params["trade_date"])] if params.get("trade_date")
                    else self._trade_dates_for_update(start_date, end_date))
            return sum(self._update_security_day(api_name, day) for day in days)

        if api_name == "forecast":
            announcement = params.pop("ann_date", None)
            if announcement:
                start_date = start_date or announcement
                end_date = end_date or announcement
            if not start_date or not end_date:
                raise ValueError("forecast requires explicit announcement start_date/end_date or ann_date for coverage")
            start, end = normalize_date(start_date), normalize_date(end_date)
            if start > end:
                raise ValueError("forecast announcement start_date is after end_date")
            scopes = [item.strip() for item in str(params.get("ts_code") or "").split(',') if item.strip()]
            total = 0
            # Official forecast endpoint requires ann_date or ts_code. A
            # start/end pair alone is not an all-market query.
            requests = [(start, end, self._api_params(api_name, start_date=start, end_date=end, **params))] if scopes else [
                (day.strftime("%Y%m%d"), day.strftime("%Y%m%d"), {**params, "ann_date": day.strftime("%Y%m%d")})
                for day in self._calendar_days(start, end)
            ]
            for first, last, api_params in requests:
                data = self._security_pages(api_name, api_params)
                if not data.empty:
                    dates = data["ann_date"].fillna('').astype(str).str.replace('-', '', regex=False)
                    if (~dates.between(first, last)).any():
                        raise ValueError("forecast returned announcement rows outside requested date; coverage not established")
                    if scopes and not set(data["ts_code"].astype(str)).issubset(set(scopes)):
                        raise ValueError("forecast returned unrequested securities; coverage not established")
                count = self.cache_data(api_name, data)
                total += count
                self.cache_data("forecast_coverage", pd.DataFrame([{
                    "scope": scope, "start_date": first, "end_date": last, "row_count": count,
                    "observed_at": capture_timestamp(), "source": "tushare.forecast",
                } for scope in scopes or ['all']]))
            if not scopes:
                self.cache_data("forecast_coverage", pd.DataFrame([{
                    "scope": "all", "start_date": start, "end_date": end, "row_count": total,
                    "observed_at": capture_timestamp(), "source": "tushare.forecast",
                }]))
            return total

        if api_name == "namechange":
            # API start/end filter ANNOUNCEMENTS, not effective intervals. Fetch
            # the full timeline so states established before the window survive.
            data = self._security_pages(api_name, {k: v for k, v in params.items()
                                                   if k not in {"limit", "offset"}})
            return self.cache_data(api_name, data)

        if api_name == "stock_basic" and "list_status" not in params:
            return sum(
                self.update_data(api_name, list_status=status)
                for status in _STOCK_BASIC_LIST_STATUSES
            )

        if api_name in {"daily", "daily_basic", "adj_factor", "fund_adj"} and "trade_date" not in params and "ts_code" not in params:
            total = 0
            for trade_date in self._trade_dates_for_update(start_date, end_date):
                total += self.update_data(api_name, trade_date=trade_date)
            return total

        if api_name == "index_daily" and "ts_code" not in params:
            return sum(
                self.update_data(api_name, start_date=start_date, end_date=end_date, ts_code=code)
                for code in _DEFAULT_INDEX_CODES
            )

        if api_name == "sw_daily" and "ts_code" not in params:
            return sum(
                self.update_data(api_name, start_date=start_date, end_date=end_date, ts_code=code)
                for code in _DEFAULT_SW_INDEX_CODES
            )

        if api_name == "index_classify":
            return self._update_sw_classify(params)

        if api_name == "index_member":
            if "index_code" not in params and "ts_code" not in params:
                return sum(
                    self.update_data(api_name, index_code=code)
                    for code in self._fetch_sw_l1_codes()
                )
            return self._update_sw_members(params)

        if api_name == "index_member_all":
            if not any(params.get(key) for key in ("l1_code", "l2_code", "l3_code", "ts_code")):
                return sum(
                    self.update_data(api_name, l1_code=code)
                    for code in self._fetch_sw_l1_codes()
                )
            return self._update_sw_members_all(params)

        if api_name == "index_weight" and "index_code" not in params:
            return sum(
                self.update_data(api_name, start_date=start_date, end_date=end_date, index_code=code)
                for code in ("399006.SZ", "000688.SH", "000905.SH", "000852.SH", "000300.SH")
            )

        if api_name == "income" and "period" not in params:
            return sum(
                self.update_data("income", period=period)
                for period in self._income_periods_for_range(start_date, end_date)
            )

        if api_name == "income" and "period" in params and "ts_code" not in params:
            return self._update_income_period(params["period"])

        api_params = self._api_params(api_name, start_date=start_date, end_date=end_date, **params)
        method = getattr(self._pro_api(), api_name)
        self._wait_for_rate_limit()
        data = method(**api_params)
        if (
            data is not None
            and api_name == "stock_basic"
            and "list_status" in api_params
            and "list_status" not in data.columns
        ):
            data = data.copy()
            data["list_status"] = api_params["list_status"]
        return self.cache_data(api_name, data)

    def _security_pages(self, api_name: str, params: dict) -> pd.DataFrame:
        page_size = 1000 if api_name == "stock_st" else 5000
        payload = {**params, "fields": ",".join(self._spec(api_name).columns)}
        payload.pop("offset", None)
        payload.pop("limit", None)
        frames, seen = [], set()
        offset = 0
        while True:
            self._wait_for_rate_limit()
            page = getattr(self._pro_api(), api_name)(**payload, limit=page_size, offset=offset)
            if page is None:
                raise ValueError(f"{api_name} returned no response; coverage not established")
            if page.empty:
                break
            keys = set(map(tuple, page[list(self._spec(api_name).primary_keys)].values.tolist()))
            if keys.issubset(seen) or (api_name != "namechange" and seen.intersection(keys)):
                raise ValueError(f"{api_name} returned overlapping pages; refusing partial history")
            seen.update(keys)
            frames.append(page)
            offset += len(page)
            if len(page) < page_size:
                break
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=self._spec(api_name).columns)

    def _update_security_day(self, api_name: str, day: str) -> int:
        frame = self._security_pages(api_name, {"trade_date": day})
        # An empty historical ST response can mean provider history is missing.
        # Do not silently interpret that as a market with no ST securities.
        if frame.empty or set(frame["trade_date"].astype(str)) != {day}:
            raise ValueError(f"{api_name} has no valid complete snapshot for {day}")
        spec = self._spec(api_name)
        frame = self._normalize_frame(api_name, frame, spec)
        columns = list(frame.columns)
        rows = list(frame.where(pd.notna(frame), None).itertuples(index=False, name=None))
        with self._write_connect() as conn:
            # Replace exactly one completed snapshot atomically, retaining other days.
            conn.execute(f"DELETE FROM {spec.table} WHERE trade_date=?", (day,))
            conn.executemany(
                f"INSERT INTO {spec.table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", rows)
            conn.execute(
                "INSERT OR REPLACE INTO security_state_coverage(api_name,trade_date,row_count) VALUES (?,?,?)",
                (api_name, day, len(rows)),
            )
            conn.commit()
        return len(rows)

    def _update_income_period(self, period: str) -> int:
        api_params = self._api_params("income", period=period, fields=_INCOME_UPDATE_FIELDS)
        pro = self._pro_api()
        method = getattr(pro, "income_vip", None)
        if method is None:
            method = getattr(pro, "income")
        self._wait_for_rate_limit()
        data = method(**api_params)
        return self.cache_data("income", data)

    def _update_sw_classify(self, params: dict) -> int:
        level = str(params.get("level") or "L1")
        src = str(params.get("src") or "SW2021")
        pro = self._pro_api()
        self._wait_for_rate_limit()
        data = pro.index_classify(level=level, src=src)
        if data is None or data.empty:
            return 0
        frame = data.copy()
        if "src" not in frame.columns:
            frame["src"] = src
        return self.cache_data("index_classify", frame)

    def _update_sw_members(self, params: dict) -> int:
        """Fetch SW2021 members through Tushare's index_member_all API."""
        payload = {}
        if params.get("index_code"):
            payload["l1_code"] = str(params["index_code"])
        if params.get("ts_code"):
            payload["ts_code"] = str(params["ts_code"])
        statuses = [str(params["is_new"])] if params.get("is_new") else ["Y", "N"]
        pro = self._pro_api()
        total = 0
        for status in statuses:
            self._wait_for_rate_limit()
            data = pro.index_member_all(**payload, is_new=status)
            if data is None or data.empty:
                continue
            frame = data.rename(
                columns={"l1_code": "index_code", "ts_code": "con_code"}
            ).copy()
            frame["is_new"] = status
            total += self.cache_data("index_member", frame)
        return total

    def _update_sw_members_all(self, params: dict) -> int:
        """Cache the complete SW2021 L1/L2/L3 membership payload."""
        payload = {
            key: str(params[key])
            for key in ("l1_code", "l2_code", "l3_code", "ts_code")
            if params.get(key)
        }
        statuses = [str(params["is_new"])] if params.get("is_new") else ["Y", "N"]
        pro = self._pro_api()
        total = 0
        for status in statuses:
            self._wait_for_rate_limit()
            data = pro.index_member_all(**payload, is_new=status)
            if data is None or data.empty:
                continue
            frame = data.copy()
            frame["is_new"] = status
            text_columns = [
                "l1_code",
                "l1_name",
                "l2_code",
                "l2_name",
                "l3_code",
                "l3_name",
                "ts_code",
                "name",
                "in_date",
                "out_date",
                "is_new",
            ]
            for column in text_columns:
                if column in frame.columns:
                    frame[column] = frame[column].fillna("").astype(str)
            total += self.cache_data("index_member_all", frame)
        return total

    def _fetch_sw_l1_codes(self) -> list[str]:
        """申万一级行业指数代码（SW2021），从本地缓存读取，缺失则拉取。"""
        try:
            spec = self._spec("index_classify")
            rows = self._read_sql(
                f"SELECT DISTINCT index_code FROM {spec.table} WHERE level='L1' ORDER BY index_code",
                [],
            )
            codes = [str(value) for value in rows["index_code"].tolist() if value]
            if codes:
                return codes
        except Exception:
            pass
        self.update_data("index_classify", level="L1", src="SW2021")
        spec = self._spec("index_classify")
        rows = self._read_sql(
            f"SELECT DISTINCT index_code FROM {spec.table} WHERE level='L1' ORDER BY index_code",
            [],
        )
        return [str(value) for value in rows["index_code"].tolist() if value]

    def update_range(
        self,
        start_date: str,
        end_date: str,
        apis: Iterable[str] | None = None,
    ) -> dict[str, int]:
        requested = list(apis or _DEFAULT_UPDATE_APIS)
        counts: dict[str, int] = {}
        for api_name in requested:
            counts[api_name] = self.update_data(
                api_name,
                start_date=normalize_date(start_date),
                end_date=normalize_date(end_date),
            )
        return counts

    def _ensure_schema(self) -> None:
        db_path = Path(self.cache_db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_connect() as conn:
            for spec in _API_SPECS.values():
                conn.execute(self._create_table_sql(spec))
                if spec.date_column:
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{spec.table}_{spec.date_column} "
                        f"ON {spec.table}({spec.date_column})"
                    )
                for key in spec.primary_keys:
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{spec.table}_{key} ON {spec.table}({key})"
                    )
                table_columns = self._table_columns(conn, spec.table)
                if {"trade_date", "ts_code"}.issubset(table_columns):
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{spec.table}_trade_date_ts_code "
                        f"ON {spec.table}(trade_date, ts_code)"
                    )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_basic_code_date ON daily_basic(ts_code,trade_date)")
            conn.execute("""CREATE TABLE IF NOT EXISTS financial_revision (
                api_name TEXT NOT NULL, ts_code TEXT NOT NULL, end_date TEXT NOT NULL,
                report_type TEXT NOT NULL, announcement_date TEXT, observed_at TEXT NOT NULL,
                available_at TEXT NOT NULL, source_version_id TEXT, provenance_kind TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(api_name, ts_code, end_date, report_type, payload_sha256))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_financial_revision_asof ON financial_revision(api_name,available_at,ts_code,end_date)")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS financial_revision_no_update
                BEFORE UPDATE ON financial_revision BEGIN SELECT RAISE(ABORT,'financial revisions are append-only'); END""")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS financial_revision_no_delete
                BEFORE DELETE ON financial_revision BEGIN SELECT RAISE(ABORT,'financial revisions are append-only'); END""")
            # Evidence upgrades never rewrite an earlier captured version.
            # Separate append-only storage safely extends older cache schemas.
            conn.execute("""CREATE TABLE IF NOT EXISTS financial_revision_evidence (
                api_name TEXT NOT NULL, ts_code TEXT NOT NULL, end_date TEXT NOT NULL,
                report_type TEXT NOT NULL, announcement_date TEXT, observed_at TEXT NOT NULL,
                available_at TEXT NOT NULL, source_version_id TEXT NOT NULL, provenance_kind TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(api_name,ts_code,end_date,report_type,payload_sha256,source_version_id,available_at,provenance_kind))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_financial_revision_evidence_asof ON financial_revision_evidence(api_name,available_at,ts_code,end_date)")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS financial_revision_evidence_no_update
                BEFORE UPDATE ON financial_revision_evidence BEGIN SELECT RAISE(ABORT,'financial evidence is append-only'); END""")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS financial_revision_evidence_no_delete
                BEFORE DELETE ON financial_revision_evidence BEGIN SELECT RAISE(ABORT,'financial evidence is append-only'); END""")
            conn.commit()

    def _append_financial_revisions(self, conn, api_name, data):
        spec = self._spec(api_name)
        captured_at = capture_timestamp()
        for raw in data.to_dict("records"):
            normalized = self._normalize_frame(api_name, pd.DataFrame([raw]), spec)
            if normalized.empty:
                continue
            row = normalized.iloc[0].to_dict()
            payload, digest = normalized_payload(row)
            announcement = row.get("f_ann_date") or row.get("ann_date")
            if announcement is None or pd.isna(announcement) or not str(announcement).strip():
                announcement = None
            publication_at = asof_timestamp(str(announcement)) if announcement else captured_at
            explicit_at = raw.get("_version_available_at")
            explicit_verified = raw.get("_version_verified") is True and bool(raw.get("_source_version_id"))
            if explicit_verified and explicit_at:
                available_at = max(asof_timestamp(explicit_at), publication_at)
                provenance = "source_version_verified"
            else:
                available_at = max(captured_at, publication_at)
                provenance = "captured_revision"
            values = (
                api_name, str(row["ts_code"]), str(row.get("end_date") or ""),
                str(row.get("report_type") or row.get("type") or ""), announcement,
                captured_at, available_at, str(raw.get("_source_version_id") or ""), provenance, digest, payload)
            columns = "api_name,ts_code,end_date,report_type,announcement_date,observed_at,available_at,source_version_id,provenance_kind,payload_sha256,payload_json"
            inserted = conn.execute(f"INSERT OR IGNORE INTO financial_revision ({columns}) VALUES(?,?,?,?,?,?,?,?,?,?,?)", values).rowcount
            if not inserted and provenance == "source_version_verified":
                old = conn.execute("""SELECT source_version_id,available_at,provenance_kind FROM financial_revision
                    WHERE api_name=? AND ts_code=? AND end_date=? AND report_type=? AND payload_sha256=?""", (*values[:4], digest)).fetchone()
                if old != (values[7], values[6], values[8]):
                    conn.execute(f"INSERT OR IGNORE INTO financial_revision_evidence ({columns}) VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)

    def financial_revisions(self, api_name="income", as_of=None, period=None, ts_code=None, **_):
        clauses = ["api_name=?"]
        parameters = [api_name]
        if as_of is not None:
            clauses.append("available_at<=?")
            parameters.append(asof_timestamp(as_of))
        if period is not None:
            clauses.append("end_date=?")
            parameters.append(self._period_to_end_date(period))
        if ts_code is not None:
            codes = [item.strip() for item in str(ts_code).split(',') if item.strip()]
            clauses.append("ts_code IN (" + ','.join('?' for _ in codes) + ")")
            parameters.extend(codes)
        if not self._table_exists("financial_revision"):
            result = pd.DataFrame(columns=list(self._spec(api_name).columns))
        else:
            with self._connect() as conn:
                selection = "SELECT payload_json,available_at,observed_at,payload_sha256,provenance_kind FROM "
                rows = conn.execute(selection + "financial_revision WHERE " + ' AND '.join(clauses), parameters).fetchall()
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_revision_evidence'").fetchone():
                    rows.extend(conn.execute(selection + "financial_revision_evidence WHERE " + ' AND '.join(clauses), parameters).fetchall())
            result = pd.DataFrame([{**json.loads(row[0]), "_available_at": row[1], "_observed_at": row[2], "_payload_sha256": row[3], "_provenance_kind": row[4]} for row in rows])
            if result.empty:
                result = pd.DataFrame(columns=list(self._spec(api_name).columns))
        result.attrs.update(provenance_state="revisions_verified", as_of=str(as_of), source="tushare.financial_revision", historical_coverage_complete=False)
        return result

    def financial_readiness(self, as_of, api_name="income"):
        """Readiness is independent of strict_local (which only controls network use)."""
        if as_of is None:
            return {'ready': False, 'reason': 'financial_asof_missing', 'legacy_cache_historical_vintages_recovered': False}
        legacy = self.fetch(api_name)
        revisions = self.financial_revisions(api_name=api_name, as_of=as_of)
        key_columns = ['ts_code', 'end_date', 'report_type'] if api_name == 'income' else ['ts_code', 'end_date', 'type']
        if legacy.empty:
            return {'ready': False, 'reason': 'financial_source_coverage_missing', 'legacy_rows': 0, 'visible_revision_rows': len(revisions)}
        visible_legacy = legacy.copy()
        announcement_columns = [key for key in ('f_ann_date', 'ann_date') if key in visible_legacy]
        unknown_announcement_keys = []
        if announcement_columns:
            announcements = visible_legacy[announcement_columns].fillna('').astype(str).max(axis=1).str.replace('-', '', regex=False).str[:8]
            unknown = visible_legacy[announcements == '']
            unknown_announcement_keys = [tuple(str(row.get(key, '')) for key in key_columns) for row in unknown.to_dict('records')]
            visible_legacy = visible_legacy[(announcements != '') & (announcements <= asof_timestamp(as_of)[:10].replace('-', ''))]
        else:
            unknown_announcement_keys = [tuple(str(row.get(key, '')) for key in key_columns) for row in visible_legacy.to_dict('records')]
        if api_name == 'income':
            # A latest correction's future announcement cannot prove that no
            # earlier version was published. Require every historically ended
            # period to have a visible vintage or explicit completeness proof.
            cutoff_day = asof_timestamp(as_of)[:10].replace('-', '')
            periods = legacy['end_date'].fillna('').astype(str).str.replace('-', '', regex=False).str[:8]
            visible_legacy = legacy[(periods != '') & (periods <= cutoff_day)]
        required = {tuple(str(row.get(key, '')) for key in key_columns) for row in visible_legacy.to_dict('records')}
        available = {tuple(str(row.get(key, '')) for key in key_columns) for row in revisions.to_dict('records')}
        missing = sorted((required - available) | set(unknown_announcement_keys))
        return {'ready': not missing and bool(required or len(revisions)), 'reason': 'financial_announcement_missing' if unknown_announcement_keys else 'financial_vintage_missing' if missing else 'ready' if required or len(revisions) else 'financial_source_coverage_missing',
                'legacy_rows': len(visible_legacy), 'visible_revision_rows': len(revisions), 'unknown_announcement_key_count': len(unknown_announcement_keys), 'missing_key_count': len(missing), 'missing_keys': [list(key) for key in missing[:20]],
                'legacy_cache_historical_vintages_recovered': False}

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _create_table_sql(self, spec: ApiSpec) -> str:
        column_defs = []
        for column in spec.columns:
            if column.endswith("_date") or column.endswith("_code") or column.endswith("_name") or column in {"type", "change_reason", "ts_code", "index_code", "con_code", "security", "source_sha256", "scope", "observed_at", "source", "exchange", "symbol", "name", "industry", "market", "list_status", "report_type", "comp_type", "is_hs", "area", "fullname", "enname", "cnspell", "curr_type", "delist_date", "act_name", "act_ent_type", "is_new", "src", "level"}:
                sql_type = "TEXT"
            elif column == "is_open":
                sql_type = "INTEGER"
            else:
                sql_type = "REAL"
            column_defs.append(f"{column} {sql_type}")
        primary_key = ", ".join(spec.primary_keys)
        return (
            f"CREATE TABLE IF NOT EXISTS {spec.table} ("
            f"{', '.join(column_defs)}, update_time TEXT DEFAULT CURRENT_TIMESTAMP, "
            f"PRIMARY KEY ({primary_key}))"
        )

    def _where_clause(self, api_name: str, spec: ApiSpec, params: dict) -> tuple[str, list]:
        clauses = []
        sql_params = []
        normalized_params = dict(params)

        for key, value in normalized_params.items():
            if value is None or key in {"fields", "offset", "limit"} or value == "":
                continue
            if api_name == "income" and key == "period":
                clauses.append("end_date = ?")
                sql_params.append(self._period_to_end_date(value))
            elif key == "start_date" and spec.date_column:
                clauses.append(f"{spec.date_column} >= ?")
                sql_params.append(normalize_date(value))
            elif key == "end_date" and spec.date_column:
                clauses.append(f"{spec.date_column} <= ?")
                sql_params.append(normalize_date(value))
            elif key == "trade_date" and spec.date_column:
                clauses.append(f"{spec.date_column} = ?")
                sql_params.append(normalize_date(value))
            elif key == "period" and spec.date_column == "end_date":
                clauses.append("end_date = ?")
                sql_params.append(self._period_to_end_date(value))
            elif key in spec.columns:
                values = [item.strip() for item in str(value).split(",") if item.strip()]
                if len(values) > 1:
                    placeholders = ", ".join("?" for _ in values)
                    clauses.append(f"{key} IN ({placeholders})")
                    sql_params.extend(values)
                else:
                    clauses.append(f"{key} = ?")
                    sql_params.append(value)
        if not clauses:
            return "", []
        return " WHERE " + " AND ".join(clauses), sql_params

    def _normalize_frame(self, api_name: str, data: pd.DataFrame, spec: ApiSpec) -> pd.DataFrame:
        frame = data.copy()
        if api_name == "sw_daily" and "pct_change" in frame.columns and "pct_chg" not in frame.columns:
            frame = frame.rename(columns={"pct_change": "pct_chg"})
        if api_name == "income" and "period" in frame.columns and "end_date" not in frame.columns:
            frame["end_date"] = [self._period_to_end_date(value) for value in frame["period"].tolist()]
        if api_name == "trade_cal" and "exchange" not in frame.columns:
            frame["exchange"] = "SSE"
        if api_name == "stock_basic" and "list_status" not in frame.columns:
            frame["list_status"] = "L"
        for key in spec.primary_keys:
            if key not in frame.columns:
                raise ValueError(f"{api_name} data missing primary key column: {key}")
        columns = [column for column in spec.columns if column in frame.columns]
        frame = frame[columns].copy()
        for column in spec.columns:
            if column in frame.columns and (column.endswith("_date") or column in spec.primary_keys):
                frame[column] = frame[column].fillna("").astype(str)
        return frame.drop_duplicates(subset=list(spec.primary_keys), keep="last").reset_index(drop=True)

    def _project_fields(self, frame: pd.DataFrame, fields) -> pd.DataFrame:
        if frame.empty or not fields:
            return frame
        requested = [field.strip() for field in str(fields).split(",") if field.strip()]
        available = [field for field in requested if field in frame.columns]
        return frame[available].copy() if available else frame

    def _api_params(self, api_name: str, start_date: str | None = None, end_date: str | None = None, **params) -> dict:
        payload = {key: value for key, value in params.items() if value is not None}
        if start_date:
            payload["start_date"] = normalize_date(start_date)
        if end_date:
            payload["end_date"] = normalize_date(end_date)
        if api_name == "trade_cal":
            payload.setdefault("exchange", "SSE")
        if api_name == "stock_basic":
            payload.setdefault("list_status", "L")
            payload.setdefault("fields", ",".join(self._spec(api_name).columns))
        if api_name == "income" and "period" in payload:
            payload["period"] = self._period_to_end_date(payload["period"])
        return payload

    def _trade_dates_for_update(self, start_date: str | None, end_date: str | None) -> list[str]:
        start = normalize_date(start_date or end_date or datetime.now().strftime("%Y%m%d"))
        end = normalize_date(end_date or start_date or start)
        calendar = self.fetch("trade_cal", exchange="SSE", start_date=start, end_date=end)
        if not calendar.empty and "is_open" in calendar.columns:
            open_days = calendar[calendar["is_open"].astype(int) == 1]
            return [str(value) for value in open_days["cal_date"].tolist()]
        return [
            day.strftime("%Y%m%d")
            for day in self._calendar_days(start, end)
            if day.weekday() < 5
        ]

    def _income_periods_for_range(
        self,
        start_date: str | None,
        end_date: str | None,
    ) -> list[str]:
        start = start_date or end_date or datetime.now().strftime("%Y%m%d")
        end = end_date or start_date or start
        return [
            income_period_end(period)
            for period in required_income_periods(start, end)
        ]

    def _period_to_end_date(self, value) -> str:
        text = str(value).strip().lower()
        if len(text) == 6 and text[:4].isdigit() and text[4:] in _QUARTER_ENDS:
            return f"{text[:4]}{_QUARTER_ENDS[text[4:]]}"
        return normalize_date(text)

    def _calendar_days(self, start: str, end: str):
        current = datetime.strptime(start, "%Y%m%d")
        last = datetime.strptime(end, "%Y%m%d")
        while current <= last:
            yield current
            current += timedelta(days=1)

    def _pro_api(self):
        if self._pro is None:
            if not self.token:
                raise ValueError("TUSHARE_TOKEN is required to update local cache data")
            import tushare as ts

            self._pro = ts.pro_api(self.token)
        return self._pro

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_ts
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_ts = time.time()

    def _read_sql(self, sql: str, params: list) -> pd.DataFrame:
        with self._read_lock:
            conn = self._reader()
            return pd.read_sql_query(sql, conn, params=params)

    def _reader(self):
        with self._read_lock:
            if self._read_conn is None:
                uri = Path(self.cache_db).resolve().as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                cache_mb = self._sqlite_memory_mb("JQTS_SQLITE_CACHE_MB", 512)
                mmap_mb = self._sqlite_memory_mb("JQTS_SQLITE_MMAP_MB", 2048)
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute(f"PRAGMA cache_size=-{cache_mb * 1024}")
                conn.execute(f"PRAGMA mmap_size={mmap_mb * 1024 * 1024}")
                self._read_conn = conn
            return self._read_conn

    def _sqlite_memory_mb(self, env_name: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(env_name, default)))
        except (TypeError, ValueError):
            return int(default)

    def close(self) -> None:
        with self._read_lock:
            if self._read_conn is not None:
                self._read_conn.close()
                self._read_conn = None

    def _table_exists(self, table: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        return row is not None

    @contextmanager
    def _connect(self):
        if self.read_only:
            conn = sqlite3.connect(Path(self.cache_db).resolve().as_uri() + "?mode=ro", uri=True)
            conn.execute("PRAGMA query_only=ON")
        else:
            conn = sqlite3.connect(self.cache_db)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_connect(self):
        if self.read_only:
            raise ValueError("Simulation workers cannot modify the market-data cache")
        with cache_write_lock(self.cache_db):
            with self._connect() as conn:
                yield conn

    def _spec(self, api_name: str) -> ApiSpec:
        try:
            return _API_SPECS[api_name]
        except KeyError as exc:
            raise NotImplementedError(f"Tushare cache API is not registered: {api_name}") from exc
