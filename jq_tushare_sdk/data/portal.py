import time
from datetime import date, datetime
from types import SimpleNamespace
from typing import Iterable

import pandas as pd

from jq_tushare_sdk.data.canonical_price_store import (
    CanonicalPriceStore,
    CanonicalPriceStoreError,
)
from jq_tushare_sdk.data.code_map import (
    is_tushare_fund_code,
    is_tushare_index_code,
    is_tushare_sw_index_code,
    joinquant_date,
    normalize_date,
    to_joinquant_code,
    to_tushare_code,
)
from jq_tushare_sdk.data.joinquant_fields import PRICE_FIELD_MAP


class JoinQuantDateStr(str):
    def strftime(self, fmt: str) -> str:
        return datetime.strptime(str(self), "%Y-%m-%d").strftime(fmt)


class ContinuousFundamentalsResult:
    def __init__(self, frame: pd.DataFrame, selected_fields: list[str]):
        self._frame = frame.reset_index(drop=True)
        self._selected_fields = list(selected_fields)

    def minor_xs(self, security: str) -> pd.DataFrame:
        subset = self._frame[self._frame["code"] == str(security)].copy()
        if subset.empty:
            columns = [name for name in self._selected_fields if name != "code"]
            return pd.DataFrame(columns=columns, index=pd.Index([], name="time"))
        subset = subset.sort_values("time").set_index("time")
        columns = [name for name in self._selected_fields if name != "code"]
        return subset[columns]

    def to_frame(self) -> pd.DataFrame:
        subset = self._frame.copy().sort_values(["time", "code"])
        columns = [name for name in self._selected_fields if name != "code"]
        return subset.set_index(["time", "code"])[columns]


class DataPortal:
    def __init__(
        self,
        backend,
        optimize_data: bool = True,
        *,
        price_cache_start: str | None = None,
        price_cache_end: str | None = None,
    ):
        self.backend = backend
        self.optimize_data = bool(optimize_data)
        self._price_count_cache = {}
        self._price_count_group_cache = {}
        self._date_range_cache = {}
        self._date_range_group_cache = {}
        self._price_result_cache = {}
        self._fetch_cache = {}
        self._security_metadata_cache = None
        self._industry_cache = None
        self._valuation_cache = {}
        self._portal_calls = {"get_price": {"count": 0, "seconds": 0.0}}
        self._result_cache_hits = 0
        self._result_cache_misses = 0
        self._canonical_fallbacks = 0
        self._format_seconds = 0.0
        self._canonical_price_store = (
            CanonicalPriceStore(self.backend.fetch, price_cache_start, price_cache_end)
            if self.optimize_data
            and price_cache_start is not None
            and price_cache_end is not None
            else None
        )

    def get_price(
        self,
        security,
        start_date=None,
        end_date=None,
        count=None,
        fields=None,
        frequency="daily",
        panel=False,
        fq=None,
        **kwargs,
    ):
        started = time.perf_counter()
        try:
            return self._get_price_impl(
                security,
                start_date=start_date,
                end_date=end_date,
                count=count,
                fields=fields,
                frequency=frequency,
                panel=panel,
                fq=fq,
                **kwargs,
            )
        finally:
            payload = self._portal_calls["get_price"]
            payload["count"] += 1
            payload["seconds"] += time.perf_counter() - started

    def _get_price_impl(
        self,
        security,
        start_date=None,
        end_date=None,
        count=None,
        fields=None,
        frequency="daily",
        panel=False,
        fq=None,
        **kwargs,
    ):
        skip_paused = bool(kwargs.pop("skip_paused", False))
        fill_paused = bool(kwargs.pop("fill_paused", False))
        self._validate_price_options(frequency=frequency, panel=panel, fq=fq, kwargs=kwargs)
        fields = self._normalize_fields(fields)
        securities = self._normalize_securities(security)
        api_name = self._price_api_name(securities)
        source_start_date = start_date
        source_count = count
        direct_active_count = (
            skip_paused
            and count is not None
            and start_date is None
            and end_date is not None
        )
        if direct_active_count:
            source_count = None
        result_cache_key = self._price_result_cache_key(
            api_name=api_name,
            securities=securities,
            start_date=start_date,
            end_date=end_date,
            count=count,
            fields=fields,
            frequency=frequency,
            fq=fq,
            skip_paused=skip_paused,
            fill_paused=fill_paused,
        )
        cached_result = self._price_result_cache.get(result_cache_key) if self.optimize_data else None
        if cached_result is not None:
            self._result_cache_hits += 1
            return cached_result.copy()
        if self.optimize_data:
            self._result_cache_misses += 1

        canonical_selected = False
        already_adjusted = False
        if direct_active_count:
            df = self._fetch(
                api_name,
                ts_code=",".join(to_tushare_code(item) for item in securities),
                end_date=normalize_date(end_date),
                positive_volume=True,
                limit_per_code=int(count),
            )
        elif self._canonical_price_store is not None and end_date is not None:
            factor_api = (
                "fund_adj"
                if api_name == "fund_daily"
                else "adj_factor"
                if api_name == "daily"
                else None
            )
            try:
                df = self._canonical_price_store.select(
                    api_name,
                    ts_codes=[to_tushare_code(item) for item in securities],
                    start_date=(
                        normalize_date(source_start_date)
                        if source_start_date is not None
                        else None
                    ),
                    end_date=normalize_date(end_date),
                    count=int(source_count) if source_count is not None else None,
                    fq=fq,
                    factor_api_name=factor_api,
                )
                canonical_selected = True
                already_adjusted = fq == "pre" and factor_api is not None
            except (MemoryError, CanonicalPriceStoreError):
                self._canonical_fallbacks += 1
                self._canonical_price_store = None
                df = self._legacy_price_frame(
                    api_name,
                    securities,
                    source_start_date,
                    end_date,
                    source_count,
                )
        else:
            df = self._legacy_price_frame(
                api_name,
                securities,
                source_start_date,
                end_date,
                source_count,
            )
        if fill_paused and not skip_paused:
            df = self._ensure_paused_price_seed(
                df,
                api_name=api_name,
                securities=securities,
                start_date=start_date,
                end_date=end_date,
                count=count,
                fq=fq,
                adjust_seed=already_adjusted,
            )
        if df.empty:
            return pd.DataFrame(columns=["time", "code"] + fields)
        self._validate_price_frame(df)
        if source_count is not None and not canonical_selected:
            df = self._tail_per_security(df, int(source_count))
        if not already_adjusted:
            df = self._apply_price_adjustment(
                df,
                api_name=api_name,
                fq=fq,
                adjustment_end_date=end_date,
            )
        if skip_paused and "vol" in df.columns:
            volume = pd.to_numeric(df["vol"], errors="coerce").fillna(0.0)
            df = df[volume > 0].reset_index(drop=True)
            if count is not None:
                df = self._tail_per_security(df, int(count))
        if fill_paused and not skip_paused:
            df = self._fill_paused_trade_days(
                df,
                start_date=start_date,
                end_date=end_date,
                count=count,
            )
        format_started = time.perf_counter()
        try:
            result = self._format_price_frame(df, fields)
        finally:
            self._format_seconds += time.perf_counter() - format_started
        if self.optimize_data:
            self._price_result_cache[result_cache_key] = result.copy()
        return result

    def _legacy_price_frame(
        self,
        api_name: str,
        securities: list[str],
        start_date,
        end_date,
        count,
    ) -> pd.DataFrame:
        if count is not None and start_date is None and self.optimize_data and (
            end_date is not None or len(securities) == 1
        ):
            df = self._cached_count_price_frame_for_securities(
                api_name,
                end_date=end_date,
                count=int(count),
                securities=securities,
            )
        elif count is None and self._can_use_range_price_cache(start_date, end_date):
            df = self._cached_date_range_frame_for_ts_codes(
                api_name,
                start_date=normalize_date(start_date),
                end_date=normalize_date(end_date),
                ts_codes=[to_tushare_code(item) for item in securities],
            )
        else:
            params = {"ts_code": ",".join(to_tushare_code(item) for item in securities)}
            if start_date:
                params["start_date"] = normalize_date(start_date)
            if end_date:
                params["end_date"] = normalize_date(end_date)
            return self._fetch(api_name, **params)
        return df

    def performance_snapshot(self) -> dict:
        return {
            "public_calls": [
                {"api": name, "count": value["count"], "seconds": round(value["seconds"], 6)}
                for name, value in sorted(self._portal_calls.items())
            ],
            "result_cache": {
                "hits": self._result_cache_hits,
                "misses": self._result_cache_misses,
            },
            "canonical_fallbacks": self._canonical_fallbacks,
            "canonical_cache": (
                self._canonical_price_store.snapshot()
                if self._canonical_price_store
                else {}
            ),
            "format_seconds": round(self._format_seconds, 6),
        }

    def get_trade_days(self, start_date=None, end_date=None, count=None):
        params = {"exchange": "SSE"}
        if start_date is not None:
            params["start_date"] = normalize_date(start_date)
        if end_date is not None:
            params["end_date"] = normalize_date(end_date)
        if not params.get("start_date") and not params.get("end_date"):
            raise NotImplementedError("get_trade_days requires start_date/end_date or end_date/count.")
        df = self._fetch("trade_cal", **params)
        if df.empty:
            return []
        open_df = df[df["is_open"].astype(int) == 1].copy()
        open_df = open_df.sort_values("cal_date")
        if count is not None:
            if int(count) <= 0:
                return []
            open_df = open_df.tail(int(count))
        return [JoinQuantDateStr(joinquant_date(item)) for item in open_df["cal_date"].tolist()]

    def get_index_stocks(self, index_symbol, date=None):
        params = {"index_code": to_tushare_code(index_symbol)}
        if date:
            params["end_date"] = normalize_date(date)
        df = self._fetch("index_weight", **params)
        if df.empty:
            return []
        if "con_code" not in df.columns:
            raise NotImplementedError("index_weight backend data must include con_code.")
        if "trade_date" in df.columns:
            latest = df["trade_date"].max()
            df = df[df["trade_date"] == latest]
        return [to_joinquant_code(code) for code in df["con_code"].tolist()]

    def get_all_securities(self, types=None, date=None):
        df = self._fetch("stock_basic")
        if df.empty:
            return pd.DataFrame()
        result = df.copy()
        if "ts_code" in result.columns:
            result.index = [to_joinquant_code(code) for code in result["ts_code"].tolist()]
        return result

    def get_security_info(self, security):
        code = to_joinquant_code(security)
        metadata_by_code = self._security_metadata_map()
        if code not in metadata_by_code:
            raise NotImplementedError(
                f"stock_basic backend data does not include requested security: {code}"
            )
        metadata = metadata_by_code[code]
        name = str(metadata.get("name") or code)
        return SimpleNamespace(
            code=code,
            display_name=name,
            name=name,
            start_date=self._security_start_date(metadata, code),
            end_date=date(2200, 1, 1),
            type="stock",
        )

    def get_current_data(self, securities=None, date=None):
        metadata_by_code = self._security_metadata_map()
        codes = securities or list(metadata_by_code)
        if isinstance(codes, str):
            codes = [codes]
        else:
            codes = list(codes)
        for code in codes:
            if code not in metadata_by_code:
                if not is_tushare_fund_code(code):
                    raise NotImplementedError(
                        f"stock_basic backend data does not include requested security: {code}"
                    )
                metadata_by_code[code] = {
                    "ts_code": to_tushare_code(code),
                    "name": code,
                }

        price_by_code = {}
        if self.optimize_data and codes:
            price_frame = self.get_price(
                codes,
                count=1,
                end_date=date,
                fields=["open", "close", "volume"],
                panel=False,
            )
            if not price_frame.empty:
                price_by_code = {
                    str(row["code"]): row
                    for _, row in price_frame.iterrows()
                }

        current = {}
        for code in codes:
            metadata = metadata_by_code[code]
            name = str(metadata.get("name") or "")
            if self.optimize_data:
                row = price_by_code.get(code)
            else:
                price = self.get_price(code, count=1, end_date=date, fields=["open", "close", "volume"], panel=False)
                row = None if price.empty else price.iloc[-1]
            last_price = 0.0
            day_open = 0.0
            paused = True
            if row is not None:
                last_price = self._float_value(row.get("close"), 0.0)
                day_open = self._float_value(row.get("open"), last_price)
                volume = self._float_value(row.get("volume"), 0.0)
                row_date = normalize_date(row.get("time")) if row.get("time") else ""
                requested_date = normalize_date(date) if date is not None else row_date
                paused = row_date != requested_date or volume <= 0
            is_st = self._is_st_name(name)
            limit_ratio = self._limit_ratio(code, is_st)
            current[code] = SimpleNamespace(
                is_st=is_st,
                paused=paused,
                last_price=last_price,
                high_limit=round(last_price * (1 + limit_ratio), 2) if last_price else 0.0,
                low_limit=round(last_price * (1 - limit_ratio), 2) if last_price else 0.0,
                day_open=day_open,
                name=name,
            )
        return current

    def get_industry(self, securities, date=None):
        industry_by_code = self._industry_map()
        codes = self._normalize_securities(securities)
        missing_codes = [code for code in codes if code not in industry_by_code]
        if missing_codes:
            names = ", ".join(missing_codes)
            raise NotImplementedError(
                f"stock_basic backend data does not include requested security industry: {names}"
            )
        return {
            code: {
                "sw_l1": {
                    "industry_code": industry_by_code[code],
                    "industry_name": industry_by_code[code],
                },
                "industry_name": industry_by_code[code],
            }
            for code in codes
        }

    def get_fundamentals(self, q, date=None, statDate=None):
        frames = []
        table_names = (
            {field.table for field in q.fields}
            | {expr.field.table for expr in q.filters}
            | {ordering.field.table for ordering in q.ordering}
        )
        unsupported_tables = sorted(table_names - {"valuation", "income"})
        if unsupported_tables:
            names = ", ".join(unsupported_tables)
            raise NotImplementedError(
                f"Fundamentals tables are not implemented locally: {names}"
            )
        if "valuation" in table_names:
            daily_basic = self._valuation_frame(date)
            if not daily_basic.empty:
                frames.append(daily_basic.copy())
        if "income" in table_names:
            income_df, income_stat_date = self._income_frame_for_stat_date(statDate, date)
            if not income_df.empty:
                income_df = self._income_single_quarter_frame(income_df, income_stat_date, date=date)
                income_df = income_df.rename(
                    columns={
                        "ts_code": "code",
                        "n_income_attr_p": "np_parent_company_owners",
                        "total_revenue": "total_operating_revenue",
                    }
                )
                income_df["code"] = [to_joinquant_code(code) for code in income_df["code"].tolist()]
                frames.append(income_df)
        if not frames:
            return pd.DataFrame(columns=[self._fundamental_column_name(field) for field in q.fields])
        result = frames[0]
        for frame in frames[1:]:
            result = result.merge(frame, on="code", how="outer")
        result = self._ensure_fundamental_columns(result, q)
        result = self._apply_query_filters(result, q.filters)
        result = self._apply_query_ordering(result, q.ordering)
        return self._project_fundamental_fields(result, q.fields)

    def get_fundamentals_continuously(self, q, end_date=None, count=1):
        table_names = (
            {field.table for field in q.fields}
            | {expr.field.table for expr in q.filters}
            | {ordering.field.table for ordering in q.ordering}
        )
        unsupported_tables = sorted(table_names - {"valuation"})
        if unsupported_tables:
            names = ", ".join(unsupported_tables)
            raise NotImplementedError(
                f"Continuous fundamentals tables are not implemented locally: {names}"
            )
        if q.ordering:
            raise NotImplementedError("Continuous fundamentals ordering is unsupported locally")

        selected_fields = [field.name for field in q.fields]
        unsupported_fields = sorted(set(selected_fields) - {"code", "turnover_ratio"})
        if unsupported_fields:
            names = ", ".join(unsupported_fields)
            raise NotImplementedError(
                f"Continuous fundamentals fields are not implemented locally: {names}"
            )

        filtered_codes = self._continuous_fundamentals_codes(q.filters)
        params = {}
        if filtered_codes:
            params["ts_code"] = ",".join(to_tushare_code(code) for code in filtered_codes)
        if end_date is not None:
            params["end_date"] = normalize_date(end_date)
        daily_basic = self._fetch("daily_basic", **params)
        if daily_basic.empty:
            return ContinuousFundamentalsResult(
                pd.DataFrame(columns=["time", "code", *[name for name in selected_fields if name != "code"]]),
                selected_fields,
            )

        required_columns = {"ts_code", "trade_date", "turnover_rate"}
        missing_columns = sorted(required_columns - set(daily_basic.columns))
        if missing_columns:
            names = ", ".join(missing_columns)
            raise NotImplementedError(
                f"daily_basic backend data is missing required continuous fundamentals columns: {names}"
            )

        daily_basic = daily_basic.sort_values(["ts_code", "trade_date"])
        if int(count) > 0:
            daily_basic = self._tail_per_security(daily_basic, int(count))
        else:
            daily_basic = daily_basic.iloc[0:0].copy()
        daily_basic["code"] = [to_joinquant_code(code) for code in daily_basic["ts_code"].tolist()]
        daily_basic["time"] = [joinquant_date(value) for value in daily_basic["trade_date"].tolist()]
        daily_basic["turnover_ratio"] = daily_basic["turnover_rate"].astype(float)
        frame = daily_basic[["time", "code", "turnover_ratio"]].reset_index(drop=True)
        if filtered_codes:
            frame = frame[frame["code"].isin(filtered_codes)].reset_index(drop=True)
        return ContinuousFundamentalsResult(frame, selected_fields)

    def _validate_price_options(self, frequency, panel, fq, kwargs) -> None:
        if frequency not in {"daily", "1d"}:
            raise NotImplementedError(f"Unsupported frequency: {frequency}")
        if panel is not False:
            raise NotImplementedError("Only panel=False is supported.")
        if fq not in {None, "pre"}:
            raise NotImplementedError(f"Unsupported fq: {fq}")
        if kwargs:
            names = ", ".join(sorted(kwargs))
            raise NotImplementedError(f"Unsupported get_price kwargs: {names}")

    def _normalize_securities(self, security) -> list[str]:
        if isinstance(security, pd.Index):
            values = security.tolist()
        elif isinstance(security, (list, tuple, set)):
            values = list(security)
        else:
            values = [security]
        if not values:
            raise NotImplementedError("security must not be empty.")
        return [str(item) for item in values]

    def _price_api_name(self, securities) -> str:
        api_names = {self._single_price_api_name(security) for security in securities}
        if len(api_names) != 1:
            raise NotImplementedError("Mixed stock/index security batches are not supported.")
        return api_names.pop()

    def _single_price_api_name(self, security) -> str:
        code = to_tushare_code(security)
        if is_tushare_sw_index_code(code):
            return "sw_daily"
        if is_tushare_index_code(code):
            return "index_daily"
        if is_tushare_fund_code(code):
            return "fund_daily"
        return "daily"

    def _normalize_fields(self, fields) -> list[str]:
        if fields is None:
            return ["open", "close", "high", "low", "volume", "money"]
        if isinstance(fields, str):
            return [fields]
        return list(fields)

    def _valuation_date_params(self, date) -> dict:
        if date is None:
            return {}
        end = normalize_date(date)
        start_dt = datetime.strptime(end, "%Y%m%d") - pd.Timedelta(days=14)
        return {"start_date": start_dt.strftime("%Y%m%d"), "end_date": end}

    def _fetch(self, api_name: str, **params) -> pd.DataFrame:
        cache_key = (str(api_name), self._freeze_params(params))
        cached = self._fetch_cache.get(cache_key)
        if cached is not None:
            return cached.copy()
        frame = self.backend.fetch(api_name, **params)
        if frame is None:
            frame = pd.DataFrame()
        self._fetch_cache[cache_key] = frame.copy()
        return frame.copy()

    def _cached_date_range_frame(self, api_name: str, *, start_date: str, end_date: str) -> pd.DataFrame:
        cache_key = self._ensure_date_range_cache(api_name, start_date=start_date, end_date=end_date)
        return self._date_range_cache[cache_key].copy()

    def _cached_date_range_frame_for_ts_codes(
        self,
        api_name: str,
        *,
        start_date: str,
        end_date: str,
        ts_codes: list[str],
    ) -> pd.DataFrame:
        cache_key = self._ensure_date_range_cache(api_name, start_date=start_date, end_date=end_date)
        frame = self._date_range_cache[cache_key]
        groups = self._date_range_group_cache.get(cache_key)
        return self._frame_for_ts_codes(frame, groups, ts_codes)

    def _ensure_date_range_cache(self, api_name: str, *, start_date: str, end_date: str) -> tuple:
        cache_key = (str(api_name), str(start_date), str(end_date))
        if cache_key not in self._date_range_cache:
            frame = self._fetch(api_name, start_date=start_date, end_date=end_date)
            self._date_range_cache[cache_key] = frame.copy()
            self._date_range_group_cache[cache_key] = self._group_by_ts_code(frame)
        return cache_key

    def _can_use_range_price_cache(self, start_date, end_date) -> bool:
        return self.optimize_data and start_date is not None and end_date is not None

    def _freeze_params(self, params: dict) -> tuple:
        return tuple(
            (str(key), self._freeze_value(value))
            for key, value in sorted(params.items(), key=lambda item: str(item[0]))
        )

    def _freeze_value(self, value):
        if isinstance(value, (list, tuple)):
            return tuple(self._freeze_value(item) for item in value)
        if isinstance(value, set):
            return tuple(sorted(self._freeze_value(item) for item in value))
        return str(value)

    def _price_result_cache_key(
        self,
        *,
        api_name: str,
        securities: list[str],
        start_date,
        end_date,
        count,
        fields: list[str],
        frequency: str,
        fq,
        skip_paused: bool,
        fill_paused: bool,
    ) -> tuple:
        return (
            str(api_name),
            tuple(str(item) for item in securities),
            normalize_date(start_date) if start_date is not None else "",
            normalize_date(end_date) if end_date is not None else "",
            "" if count is None else int(count),
            tuple(str(field) for field in fields),
            str(frequency),
            "" if fq is None else str(fq),
            bool(skip_paused),
            bool(fill_paused),
        )

    def _price_trade_calendar(self, *, start_date, end_date, count):
        if end_date is None:
            return []
        if start_date is not None:
            days = self.get_trade_days(start_date=start_date, end_date=end_date)
        elif count is not None:
            days = self.get_trade_days(end_date=end_date, count=int(count))
        else:
            return []
        return [normalize_date(day) for day in days]

    def _ensure_paused_price_seed(
        self,
        df,
        *,
        api_name,
        securities,
        start_date,
        end_date,
        count,
        fq,
        adjust_seed,
    ):
        calendar = self._price_trade_calendar(
            start_date=start_date,
            end_date=end_date,
            count=count,
        )
        if not calendar:
            return df
        boundary = calendar[0]
        requested_codes = [to_tushare_code(code) for code in securities]
        available_codes = set()
        if not df.empty and {"ts_code", "trade_date"}.issubset(df.columns):
            visible = df[df["trade_date"].map(normalize_date) <= boundary]
            available_codes = set(visible["ts_code"].astype(str).tolist())
        missing_codes = [code for code in requested_codes if code not in available_codes]
        if not missing_codes:
            return df
        seed_end = (
            datetime.strptime(boundary, "%Y%m%d") - pd.Timedelta(days=1)
        ).strftime("%Y%m%d")
        seed = self._fetch(
            api_name,
            ts_code=",".join(missing_codes),
            end_date=seed_end,
            latest_per_code=True,
        )
        if seed.empty:
            return df
        seed = self._tail_per_security(seed, 1)
        if adjust_seed and fq == "pre":
            seed = self._adjust_price_seed_to_end(seed, api_name=api_name, end_date=end_date)
        return pd.concat([seed, df], ignore_index=True).drop_duplicates(
            ["ts_code", "trade_date"],
            keep="last",
        )

    def _adjust_price_seed_to_end(self, seed, *, api_name, end_date):
        factor_api = (
            "fund_adj"
            if api_name == "fund_daily"
            else "adj_factor"
            if api_name == "daily"
            else None
        )
        if factor_api is None or seed.empty or end_date is None:
            return seed
        codes = seed["ts_code"].astype(str).drop_duplicates().tolist()
        latest = self._fetch(
            factor_api,
            ts_code=",".join(codes),
            end_date=normalize_date(end_date),
            latest_per_code=True,
        )
        latest = self._tail_per_security(latest, 1)
        seed_factor_parts = []
        for seed_date, rows in seed.groupby("trade_date", sort=False):
            seed_codes = rows["ts_code"].astype(str).drop_duplicates().tolist()
            factors = self._fetch(
                factor_api,
                ts_code=",".join(seed_codes),
                end_date=normalize_date(seed_date),
                latest_per_code=True,
            )
            if not factors.empty:
                seed_factor_parts.append(self._tail_per_security(factors, 1))
        at_seed = (
            pd.concat(seed_factor_parts, ignore_index=True)
            if seed_factor_parts
            else pd.DataFrame()
        )
        if latest.empty or at_seed.empty or "adj_factor" not in latest.columns:
            return seed
        latest_by_code = latest.set_index("ts_code")["adj_factor"]
        seed_by_code = at_seed.set_index("ts_code")["adj_factor"]
        scale = seed["ts_code"].map(seed_by_code) / seed["ts_code"].map(latest_by_code)
        scale = pd.to_numeric(scale, errors="coerce").where(lambda values: values > 0, 1.0)
        result = seed.copy()
        for column in ("open", "high", "low", "close", "pre_close"):
            if column in result.columns:
                result[column] = pd.to_numeric(result[column], errors="coerce") * scale
        return result

    def _fill_paused_trade_days(self, df, *, start_date, end_date, count):
        if df.empty or "trade_date" not in df.columns or "ts_code" not in df.columns:
            return df
        effective_end = end_date or df["trade_date"].max()
        calendar = self._price_trade_calendar(
            start_date=start_date,
            end_date=effective_end,
            count=count,
        )
        if not calendar:
            trade_days = self.get_trade_days(
                start_date=df["trade_date"].min(),
                end_date=effective_end,
            )
            calendar = [normalize_date(day) for day in trade_days]
        if not calendar:
            return df

        ordered = df.copy()
        ordered["trade_date"] = ordered["trade_date"].map(normalize_date)
        ordered = ordered.sort_values(["ts_code", "trade_date"]).drop_duplicates(
            ["ts_code", "trade_date"],
            keep="last",
        )
        target_rows = ordered[ordered["trade_date"].isin(calendar)].copy()
        observed_counts = target_rows.groupby("ts_code")["trade_date"].nunique()
        complete_codes = set(observed_counts[observed_counts == len(calendar)].index)
        complete = target_rows[target_rows["ts_code"].isin(complete_codes)]
        incomplete = ordered[~ordered["ts_code"].isin(complete_codes)].copy()
        if incomplete.empty:
            result = complete
            if count is not None:
                result = self._tail_per_security(result, int(count))
            return result.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

        ordered = incomplete
        ordered["_observed"] = True
        codes = ordered["ts_code"].astype(str).drop_duplicates().tolist()
        all_days = sorted(set(calendar) | set(ordered["trade_date"].tolist()))
        grid = pd.MultiIndex.from_product(
            [codes, all_days],
            names=["ts_code", "trade_date"],
        )
        indexed = ordered.set_index(["ts_code", "trade_date"]).reindex(grid)
        missing = indexed["_observed"].isna()
        previous_close = pd.to_numeric(indexed["close"], errors="coerce").groupby(level=0).ffill()
        for column in ("open", "high", "low", "close"):
            if column in indexed.columns:
                indexed.loc[missing, column] = previous_close.loc[missing]
        for column in ("vol", "amount", "change", "pct_chg"):
            if column in indexed.columns:
                indexed.loc[missing, column] = 0.0
        if "pre_close" in indexed.columns:
            indexed.loc[missing, "pre_close"] = previous_close.loc[missing]
        filled = indexed.reset_index()
        filled = filled[filled["trade_date"].isin(calendar)]
        filled = filled[filled["close"].notna()].drop(columns=["_observed"], errors="ignore")
        result = pd.concat([complete, filled], ignore_index=True)
        if count is not None:
            result = self._tail_per_security(result, int(count))
        return result.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)

    def _valuation_frame(self, date) -> pd.DataFrame:
        cache_key = normalize_date(date) if date is not None else ""
        cached = self._valuation_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        daily_basic = self._fetch("daily_basic", **self._valuation_date_params(date))
        if not daily_basic.empty:
            daily_basic = self._latest_per_security(daily_basic)
            daily_basic = self._filter_a_share_rows(daily_basic)
            daily_basic = daily_basic.rename(
                columns={
                    "ts_code": "code",
                    "total_mv": "market_cap",
                    "pe": "pe_ratio",
                    "pb": "pb_ratio",
                    "turnover_rate": "turnover_ratio",
                }
            )
            if "market_cap" in daily_basic.columns:
                daily_basic["market_cap"] = daily_basic["market_cap"].astype(float) / 10000.0
            if "pe_ratio" in daily_basic.columns and "pe_ttm" in daily_basic.columns:
                pe_ratio = pd.to_numeric(daily_basic["pe_ratio"], errors="coerce")
                pe_ttm = pd.to_numeric(daily_basic["pe_ttm"], errors="coerce")
                daily_basic["pe_ratio"] = pe_ratio.fillna(pe_ttm)
            elif "pe_ratio" not in daily_basic.columns and "pe_ttm" in daily_basic.columns:
                daily_basic["pe_ratio"] = pd.to_numeric(daily_basic["pe_ttm"], errors="coerce")
            if "pe_ratio" in daily_basic.columns:
                daily_basic["pe_ratio"] = pd.to_numeric(daily_basic["pe_ratio"], errors="coerce").fillna(0.0)
            daily_basic["code"] = [to_joinquant_code(code) for code in daily_basic["code"].tolist()]
        self._valuation_cache[cache_key] = daily_basic.copy()
        return daily_basic.copy()

    def _income_frame_for_stat_date(self, stat_date, date=None) -> tuple[pd.DataFrame, str | None]:
        if stat_date is None:
            return self._filter_income_asof(self._fetch("income"), date), None

        period = self._normalize_income_period(stat_date)
        if period is None:
            return self._filter_income_asof(self._fetch("income", period=stat_date), date), stat_date

        frame = self._filter_income_asof(self._fetch("income", period=period), date)
        return frame, period

    def _filter_income_asof(self, income_df: pd.DataFrame, date=None) -> pd.DataFrame:
        if income_df.empty or date is None:
            return income_df
        date_columns = [column for column in ("f_ann_date", "ann_date") if column in income_df.columns]
        if not date_columns:
            return income_df
        asof = normalize_date(date)
        result = income_df.copy()
        visible = pd.Series(True, index=result.index)
        has_announcement_date = pd.Series(False, index=result.index)
        for column in date_columns:
            values = result[column].map(self._normalize_income_announcement_date)
            has_value = values.notna()
            has_announcement_date = has_announcement_date | has_value
            visible = visible & (~has_value | (values <= asof))
        return result[visible & has_announcement_date].reset_index(drop=True)

    def _normalize_income_announcement_date(self, value) -> str | None:
        if pd.isna(value):
            return None
        text = str(value).strip()
        if not text:
            return None
        digits = "".join(character for character in text if character.isdigit())
        if len(digits) >= 8:
            return digits[:8]
        return normalize_date(text)

    def _income_single_quarter_frame(self, income_df: pd.DataFrame, stat_date, date=None) -> pd.DataFrame:
        result = self._latest_income_per_security(income_df)
        prev_period = self._previous_income_period(stat_date)
        if prev_period is None:
            return result

        prev_df = self._fetch("income", period=prev_period)
        prev_df = self._filter_income_asof(prev_df, date)
        if prev_df.empty:
            return result
        prev_df = self._latest_income_per_security(prev_df)
        previous_by_code = prev_df.set_index("ts_code")
        for column in ("total_revenue", "revenue", "n_income", "n_income_attr_p"):
            if column not in result.columns or column not in previous_by_code.columns:
                continue
            previous_values = result["ts_code"].map(previous_by_code[column])
            result[column] = result[column].astype(float) - previous_values.fillna(0.0).astype(float)
        return result

    def _latest_income_per_security(self, income_df: pd.DataFrame) -> pd.DataFrame:
        if "ts_code" not in income_df.columns:
            return income_df
        sort_columns = [column for column in ("ts_code", "f_ann_date", "ann_date", "report_type") if column in income_df.columns]
        if sort_columns:
            income_df = income_df.sort_values(sort_columns)
        return income_df.groupby("ts_code", group_keys=False).tail(1).reset_index(drop=True)

    def _previous_income_period(self, stat_date) -> str | None:
        parsed = self._parse_income_period(stat_date)
        if parsed is None:
            return None
        year, quarter = parsed
        if quarter <= 1:
            return None
        return f"{year}q{quarter - 1}"

    def _normalize_income_period(self, stat_date) -> str | None:
        parsed = self._parse_income_period(stat_date)
        if parsed is None:
            return None
        year, quarter = parsed
        return f"{year}q{quarter}"

    def _parse_income_period(self, stat_date) -> tuple[int, int] | None:
        text = str(stat_date or "").strip().lower()
        if len(text) == 6 and text[:4].isdigit() and text[4] == "q" and text[5].isdigit():
            quarter = int(text[5])
            if 1 <= quarter <= 4:
                return int(text[:4]), quarter
            return None
        if len(text) == 8 and text[:4].isdigit():
            quarter_by_end = {"0331": 1, "0630": 2, "0930": 3, "1231": 4}
            quarter = quarter_by_end.get(text[4:])
            if quarter is not None:
                return int(text[:4]), quarter
        return None

    def _latest_per_security(self, df: pd.DataFrame) -> pd.DataFrame:
        if "ts_code" not in df.columns or "trade_date" not in df.columns:
            return df
        ordered = df.sort_values(["ts_code", "trade_date"])
        return ordered.groupby("ts_code", group_keys=False).tail(1).reset_index(drop=True)

    def _filter_a_share_rows(self, df: pd.DataFrame) -> pd.DataFrame:
        if "ts_code" not in df.columns:
            return df
        return df[df["ts_code"].map(self._is_a_share_tushare_code)].reset_index(drop=True)

    def _is_a_share_tushare_code(self, code: str) -> bool:
        raw = str(code).split(".", 1)[0]
        return raw.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689"))

    def _apply_query_filters(self, df: pd.DataFrame, filters) -> pd.DataFrame:
        result = df.copy()
        for expr in filters:
            column = self._fundamental_column_name(expr.field)
            if column not in result.columns:
                raise NotImplementedError(f"Fundamentals filter field is not available locally: {column}")
            value = expr.value
            if expr.operator == "==":
                result = result[result[column] == value]
            elif expr.operator == "!=":
                result = result[result[column] != value]
            elif expr.operator == ">=":
                result = result[result[column] >= value]
            elif expr.operator == "<=":
                result = result[result[column] <= value]
            elif expr.operator == ">":
                result = result[result[column] > value]
            elif expr.operator == "<":
                result = result[result[column] < value]
            elif expr.operator == "in":
                result = result[result[column].isin(list(value))]
            else:
                raise NotImplementedError(f"Unsupported fundamentals filter operator: {expr.operator}")
        return result.reset_index(drop=True)

    def _ensure_fundamental_columns(self, df: pd.DataFrame, q) -> pd.DataFrame:
        result = df.copy()
        fields = (
            [self._fundamental_column_name(field) for field in q.fields]
            + [self._fundamental_column_name(expr.field) for expr in q.filters]
            + [self._fundamental_column_name(ordering.field) for ordering in q.ordering]
        )
        for column in fields:
            if column in result.columns:
                continue
            result[column] = "" if column == "code" else float("nan")
        return result

    def _apply_query_ordering(self, df: pd.DataFrame, ordering) -> pd.DataFrame:
        result = df.copy()
        for item in reversed(ordering):
            column = self._fundamental_column_name(item.field)
            if column not in result.columns:
                raise NotImplementedError(f"Fundamentals ordering field is not available locally: {column}")
            result = result.sort_values(column, ascending=item.direction != "desc")
        return result.reset_index(drop=True)

    def _project_fundamental_fields(self, df: pd.DataFrame, fields) -> pd.DataFrame:
        columns = [self._fundamental_column_name(field) for field in fields]
        missing = [column for column in columns if column not in df.columns]
        if missing:
            names = ", ".join(missing)
            raise NotImplementedError(f"Fundamentals fields are not available locally: {names}")
        return df[columns].reset_index(drop=True)

    def _fundamental_column_name(self, field) -> str:
        table = getattr(field, "table", "")
        name = getattr(field, "name", "")
        if table == "valuation":
            return {
                "code": "code",
                "market_cap": "market_cap",
                "pe_ratio": "pe_ratio",
                "pb_ratio": "pb_ratio",
                "turnover_ratio": "turnover_ratio",
            }.get(name, name)
        if table == "income":
            return {
                "code": "code",
                "np_parent_company_owners": "np_parent_company_owners",
                "total_operating_revenue": "total_operating_revenue",
            }.get(name, name)
        return name

    def _tail_per_security(self, df: pd.DataFrame, count: int) -> pd.DataFrame:
        if count <= 0:
            return df.iloc[0:0].copy()
        ordered = df.sort_values(["ts_code", "trade_date"])
        return ordered.groupby("ts_code", group_keys=False).tail(count).reset_index(drop=True)

    def _cached_count_price_frame(self, api_name: str, end_date, count: int) -> pd.DataFrame:
        normalized_end = normalize_date(end_date) if end_date is not None else ""
        cache_key = self._ensure_count_price_cache(api_name, normalized_end, count, end_date=end_date)
        return self._price_count_cache[cache_key].copy()

    def _cached_count_price_frame_for_securities(
        self,
        api_name: str,
        *,
        end_date,
        count: int,
        securities: list[str],
    ) -> pd.DataFrame:
        normalized_end = normalize_date(end_date) if end_date is not None else ""
        cache_key = self._ensure_count_price_cache(api_name, normalized_end, count, end_date=end_date)
        frame = self._price_count_cache[cache_key]
        groups = self._price_count_group_cache.get(cache_key)
        return self._frame_for_ts_codes(frame, groups, [to_tushare_code(item) for item in securities])

    def _count_price_cache_key(self, api_name: str, normalized_end: str, count: int) -> tuple:
        return (str(api_name), str(normalized_end), int(count))

    def _ensure_count_price_cache(self, api_name: str, normalized_end: str, count: int, *, end_date) -> tuple:
        cache_key = self._count_price_cache_key(api_name, normalized_end, count)
        if cache_key not in self._price_count_cache:
            params = {}
            if end_date is not None:
                params["end_date"] = normalized_end
                params["start_date"] = self._count_start_date(normalized_end, count)
            df = self._fetch(api_name, **params)
            if not df.empty:
                self._validate_price_frame(df)
                df = self._tail_per_security(df, int(count))
            self._price_count_cache[cache_key] = df.copy()
            self._price_count_group_cache[cache_key] = self._group_by_ts_code(df)
        return cache_key

    def _filter_price_securities(self, df: pd.DataFrame, securities: list[str]) -> pd.DataFrame:
        if df.empty or "ts_code" not in df.columns:
            return df.copy()
        ts_codes = [to_tushare_code(item) for item in securities]
        return df[df["ts_code"].isin(ts_codes)].reset_index(drop=True)

    def _group_by_ts_code(self, df: pd.DataFrame) -> dict[str, pd.DataFrame]:
        if df.empty or "ts_code" not in df.columns:
            return {}
        return {
            str(code): group.reset_index(drop=True)
            for code, group in df.groupby("ts_code", sort=False)
        }

    def _frame_for_ts_codes(
        self,
        frame: pd.DataFrame,
        groups: dict[str, pd.DataFrame] | None,
        ts_codes: list[str],
    ) -> pd.DataFrame:
        if frame.empty or "ts_code" not in frame.columns:
            return frame.copy()
        if len(ts_codes) > 100:
            return frame[frame["ts_code"].isin(ts_codes)].reset_index(drop=True)
        if not groups:
            return frame[frame["ts_code"].isin(ts_codes)].reset_index(drop=True)
        parts = [groups[code] for code in ts_codes if code in groups]
        if not parts:
            return frame.iloc[0:0].copy()
        return pd.concat(parts, ignore_index=True).copy()

    def _count_start_date(self, normalized_end: str, count: int) -> str:
        lookback_days = max(int(count) * 3 + 7, 14)
        start_dt = datetime.strptime(normalized_end, "%Y%m%d") - pd.Timedelta(days=lookback_days)
        return start_dt.strftime("%Y%m%d")

    def _validate_price_frame(self, df: pd.DataFrame) -> None:
        missing_columns = [
            column for column in ("trade_date", "ts_code") if column not in df.columns
        ]
        if missing_columns:
            names = ", ".join(missing_columns)
            raise NotImplementedError(
                f"Price backend data is missing required columns: {names}"
            )

    def _format_price_frame(self, df: pd.DataFrame, fields: Iterable[str]) -> pd.DataFrame:
        self._validate_price_frame(df)
        result = pd.DataFrame()
        result["time"] = [joinquant_date(value) for value in df["trade_date"].tolist()]
        result["code"] = [to_joinquant_code(value) for value in df["ts_code"].tolist()]
        for field in fields:
            source = PRICE_FIELD_MAP.get(field, field)
            if source not in df.columns:
                raise NotImplementedError(f"Requested field is not available from backend data: {field}")
            if field == "money":
                result[field] = pd.to_numeric(df[source], errors="coerce").values * 1000.0
            else:
                result[field] = df[source].values
        return result.reset_index(drop=True)

    def _apply_price_adjustment(
        self,
        df: pd.DataFrame,
        api_name: str,
        fq,
        adjustment_end_date=None,
    ) -> pd.DataFrame:
        if fq != "pre" or api_name not in {"daily", "fund_daily"} or df.empty:
            return df
        if "ts_code" not in df.columns or "trade_date" not in df.columns:
            return df
        ts_codes = ",".join(sorted(str(code) for code in df["ts_code"].dropna().unique()))
        if not ts_codes:
            return df
        start_date = str(df["trade_date"].min())
        end_date = (
            normalize_date(adjustment_end_date)
            if adjustment_end_date is not None
            else str(df["trade_date"].max())
        )
        factor_api_name = "fund_adj" if api_name == "fund_daily" else "adj_factor"
        try:
            factors = self._fetch_price_adjustment_factors(
                factor_api_name,
                ts_codes=ts_codes,
                start_date=start_date,
                end_date=end_date,
            )
        except NotImplementedError:
            return df
        if factors.empty or "adj_factor" not in factors.columns:
            return df

        factor_frame = factors[["ts_code", "trade_date", "adj_factor"]].copy()
        factor_frame["adj_factor"] = pd.to_numeric(factor_frame["adj_factor"], errors="coerce")
        latest_factor = (
            factor_frame.dropna(subset=["adj_factor"])
            .sort_values(["ts_code", "trade_date"])
            .groupby("ts_code")["adj_factor"]
            .tail(1)
        )
        latest_by_code = factor_frame.loc[latest_factor.index].set_index("ts_code")["adj_factor"]

        result = df.merge(factor_frame, on=["ts_code", "trade_date"], how="left")
        result["_latest_adj_factor"] = result["ts_code"].map(latest_by_code)
        scale = result["adj_factor"] / result["_latest_adj_factor"]
        scale = scale.where(scale.notna() & (scale > 0), 1.0)
        for column in ("open", "high", "low", "close", "pre_close"):
            if column in result.columns:
                result[column] = pd.to_numeric(result[column], errors="coerce") * scale
        return result.drop(columns=["adj_factor", "_latest_adj_factor"], errors="ignore")

    def _fetch_price_adjustment_factors(
        self,
        factor_api_name: str,
        *,
        ts_codes: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        if self.optimize_data and start_date and end_date:
            return self._cached_date_range_frame_for_ts_codes(
                factor_api_name,
                start_date=start_date,
                end_date=end_date,
                ts_codes=str(ts_codes).split(","),
            )
        return self._fetch(
            factor_api_name,
            ts_code=ts_codes,
            start_date=start_date,
            end_date=end_date,
        )

    def _security_name_map(self) -> dict[str, str]:
        return {code: str(payload.get("name") or "") for code, payload in self._security_metadata_map().items()}

    def _security_metadata_map(self) -> dict[str, dict]:
        if self._security_metadata_cache is not None:
            return self._security_metadata_cache
        df = self._fetch("stock_basic")
        if df.empty:
            self._security_metadata_cache = {}
            return self._security_metadata_cache
        missing_columns = [column for column in ("ts_code", "name") if column not in df.columns]
        if missing_columns:
            names = ", ".join(missing_columns)
            raise NotImplementedError(
                f"stock_basic backend data is missing required columns: {names}"
            )
        self._security_metadata_cache = {
            to_joinquant_code(row["ts_code"]): dict(row)
            for _, row in df.iterrows()
        }
        return self._security_metadata_cache

    def _security_start_date(self, metadata: dict, code: str) -> date:
        raw_value = metadata.get("list_date")
        if raw_value is None or pd.isna(raw_value) or str(raw_value).strip() == "":
            raise NotImplementedError(
                f"stock_basic backend data does not include list_date for requested security: {code}"
            )
        text = str(raw_value).strip()
        if text.endswith(".0") and text[:-2].isdigit():
            text = text[:-2]
        text = normalize_date(text)
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError as exc:
            raise NotImplementedError(
                f"stock_basic backend data has invalid list_date for requested security: {code}"
            ) from exc

    def _is_st_name(self, name: str) -> bool:
        normalized = str(name or "").upper().replace("＊", "*")
        return "ST" in normalized

    def _limit_ratio(self, code: str, is_st: bool) -> float:
        if is_st:
            return 0.05
        raw_code = str(code).split(".", 1)[0]
        if raw_code.startswith(("300", "301", "688", "689")):
            return 0.2
        return 0.1

    def _float_value(self, value, default: float) -> float:
        if value is None or pd.isna(value):
            return float(default)
        return float(value)

    def _industry_map(self) -> dict[str, str]:
        if self._industry_cache is not None:
            return self._industry_cache
        df = self._fetch("stock_basic")
        if df.empty:
            self._industry_cache = {}
            return self._industry_cache
        missing_columns = [column for column in ("ts_code", "industry") if column not in df.columns]
        if missing_columns:
            names = ", ".join(missing_columns)
            raise NotImplementedError(
                f"stock_basic backend data is missing required columns: {names}"
            )
        self._industry_cache = {
            to_joinquant_code(row["ts_code"]): str(row["industry"])
            for _, row in df.iterrows()
        }
        return self._industry_cache

    def _continuous_fundamentals_codes(self, filters) -> list[str]:
        codes = None
        for expr in filters:
            if expr.field.table != "valuation":
                raise NotImplementedError(
                    f"Continuous fundamentals filter table is unsupported: {expr.field.table}"
                )
            if expr.field.name != "code":
                raise NotImplementedError(
                    f"Continuous fundamentals filter field is unsupported: {expr.field.name}"
                )
            if expr.operator == "==":
                next_codes = [str(expr.value)]
            elif expr.operator == "in":
                next_codes = [str(item) for item in expr.value]
            else:
                raise NotImplementedError(
                    f"Continuous fundamentals filter operator is unsupported: {expr.operator}"
                )
            codes = next_codes if codes is None else [code for code in codes if code in next_codes]
        return codes or []
