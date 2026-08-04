from __future__ import annotations

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
from jq_tushare_sdk.data.income_periods import income_period_end
from jq_tushare_sdk.data.income_periods import required_income_periods


DEFAULT_PACKAGE_ENV = "JQ_TUSHARE_SDK_TUSHARE_PACKAGE"


@dataclass(frozen=True)
class ApiSpec:
    table: str
    primary_keys: tuple[str, ...]
    date_column: str | None
    columns: tuple[str, ...]


_API_SPECS: dict[str, ApiSpec] = {
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
}

_DEFAULT_UPDATE_APIS = (
    "stock_basic",
    "trade_cal",
    "daily",
    "daily_basic",
    "adj_factor",
    "dividend",
    "income",
    "index_daily",
    "index_weight",
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
    ):
        self.cache_db = str(cache_db)
        self.token = token
        self.cache_mode = cache_mode
        self.package_path = package_path
        self.request_interval = float(request_interval)
        self._pro = None
        self._last_request_ts = 0.0
        self._read_conn = None
        self._read_lock = threading.RLock()
        self._ensure_schema()

    @classmethod
    def resolve_package_path(cls, start_path: str | Path | None = None, package_path: str | Path | None = None) -> Path:
        raise ImportError(
            "TushareCacheBackend is now self-contained and does not resolve external "
            "join_tushare packages. Use cache_db inside this project instead."
        )

    def fetch(self, api_name: str, **params) -> pd.DataFrame:
        spec = self._spec(api_name)
        where_sql, sql_params = self._where_clause(api_name, spec, params)
        latest_per_code = bool(params.get("latest_per_code", False))
        positive_volume = bool(params.get("positive_volume", False))
        limit_per_code = params.get("limit_per_code")
        if positive_volume and "vol" in spec.columns:
            where_sql += " AND vol > 0" if where_sql else " WHERE vol > 0"
        row_limit = 1 if latest_per_code else int(limit_per_code) if limit_per_code else None
        if row_limit and "ts_code" in spec.primary_keys and spec.date_column:
            sql = (
                "SELECT * FROM ("
                f"SELECT *, ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY {spec.date_column} DESC) AS _row_number "
                f"FROM {spec.table}{where_sql}"
                ") WHERE _row_number <= ?"
            )
            sql_params = [*sql_params, row_limit]
        else:
            sql = f"SELECT * FROM {spec.table}{where_sql}"
        order_columns = []
        if spec.date_column:
            order_columns.append(spec.date_column)
        if "ts_code" in spec.primary_keys:
            order_columns.append("ts_code")
        elif "index_code" in spec.primary_keys:
            order_columns.extend(["index_code", "con_code"])
        if order_columns:
            sql += " ORDER BY " + ", ".join(order_columns)
        frame = self._read_sql(sql, sql_params)
        frame = frame.drop(columns=["_row_number"], errors="ignore")
        return self._project_fields(frame, params.get("fields"))

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
        with self._connect() as conn:
            conn.executemany(sql, rows)
            conn.commit()
        return len(rows)

    def update_data(self, api_name: str, start_date: str | None = None, end_date: str | None = None, **params) -> int:
        if self.cache_mode == "strict_local" and not self.token:
            raise ValueError("update_data requires TUSHARE_TOKEN or token even when backtest reads are strict_local")

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

    def _update_income_period(self, period: str) -> int:
        api_params = self._api_params("income", period=period, fields=_INCOME_UPDATE_FIELDS)
        pro = self._pro_api()
        method = getattr(pro, "income_vip", None)
        if method is None:
            method = getattr(pro, "income")
        self._wait_for_rate_limit()
        data = method(**api_params)
        return self.cache_data("income", data)

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
        with self._connect() as conn:
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
            conn.commit()

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _create_table_sql(self, spec: ApiSpec) -> str:
        column_defs = []
        for column in spec.columns:
            if column.endswith("_date") or column in {"ts_code", "index_code", "con_code", "exchange", "symbol", "name", "industry", "market", "list_status", "report_type", "comp_type", "is_hs", "area", "fullname", "enname", "cnspell", "curr_type", "delist_date", "act_name", "act_ent_type"}:
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
                frame[column] = frame[column].astype(str)
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
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA cache_size=-131072")
                conn.execute("PRAGMA mmap_size=268435456")
                self._read_conn = conn
            return self._read_conn

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
        conn = sqlite3.connect(self.cache_db)
        try:
            yield conn
        finally:
            conn.close()

    def _spec(self, api_name: str) -> ApiSpec:
        try:
            return _API_SPECS[api_name]
        except KeyError as exc:
            raise NotImplementedError(f"Tushare cache API is not registered: {api_name}") from exc
