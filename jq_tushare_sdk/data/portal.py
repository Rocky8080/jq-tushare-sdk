import os
import time
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from collections import OrderedDict
from datetime import date, datetime
from types import SimpleNamespace
from typing import Iterable

import numpy as np
import pandas as pd

from jq_tushare_sdk.data.canonical_price_store import (
    CanonicalPriceStore,
    CanonicalPriceStoreError,
)
from jq_tushare_sdk.data.code_map import (
    is_tushare_a_share_code,
    is_tushare_fund_code,
    is_tushare_index_code,
    is_tushare_sw_index_code,
    joinquant_date,
    normalize_date,
    to_joinquant_code,
    to_tushare_code,
)
from jq_tushare_sdk.data.joinquant_fields import PRICE_FIELD_MAP
from jq_tushare_sdk.data.joinquant_industry import normalize_industry_code
from jq_tushare_sdk.data.industry_provenance import effective_industry_provider
from jq_tushare_sdk.data.security_state import HistoricalSecurityState
from jq_tushare_sdk.data.financial_provenance import FinancialVintageUnavailable, select_visible_reports, signed_ttm_by_security
from jq_tushare_sdk.data.fundamentals_contract import (
    FundamentalsUnavailableError,
    previous_calendar_date,
    validated_date,
    validate_daily_snapshot_visibility,
)

_DEFAULT_FQ = object()


class PriceDataUnavailable(ValueError):
    """Missing observations must not become synthetic suspension prices."""


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
        default_fq: str | None = None,
    ):
        self.backend = backend
        self.optimize_data = bool(optimize_data)
        env_fq = os.environ.get("JQTS_DEFAULT_FQ")
        if default_fq is not None:
            self._default_fq = default_fq
        elif env_fq in {"pre", "none", ""}:
            self._default_fq = None if env_fq == "none" else "pre"
        else:
            self._default_fq = None
        self._price_count_cache = {}
        self._price_count_group_cache = {}
        self._date_range_cache = {}
        self._date_range_group_cache = {}
        self._price_result_cache = OrderedDict()
        try:
            self._price_result_cache_row_budget = max(
                0,
                int(os.environ.get("JQTS_PRICE_RESULT_CACHE_ROWS", "500000")),
            )
        except ValueError:
            self._price_result_cache_row_budget = 500000
        self._price_result_cache_rows = 0
        self._price_result_cache_peak_rows = 0
        self._price_result_cache_evictions = 0
        self._fetch_cache = {}
        self._security_metadata_cache = None
        self._historical_security_state = HistoricalSecurityState(backend)
        self._industry_cache = None
        self._sw_industry_cache = None
        self._sw_industry_all_cache = None
        self._sw_members_index = None
        self._sw_members_all_index = None
        self._sw_l1_name_by_code = None
        self._jq_classification_cache = {}
        self._jq_members_index = None
        self._industry_provider = effective_industry_provider()
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
        fq=_DEFAULT_FQ,
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
        fq=_DEFAULT_FQ,
        **kwargs,
    ):
        if fq is _DEFAULT_FQ:
            fq = self._default_fq
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
            self._price_result_cache.move_to_end(result_cache_key)
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
            if fill_paused and not skip_paused:
                raise PriceDataUnavailable(f"price_data_missing: {api_name} has no observations for {securities}; prepare source data first")
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
        if "factor" not in df:
            df["factor"] = 1.0
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
                api_name=api_name,
                securities=securities,
                fq=fq,
            )
        format_started = time.perf_counter()
        try:
            result = self._format_price_frame(df, fields, api_name=api_name)
        finally:
            self._format_seconds += time.perf_counter() - format_started
        result.attrs["price_provenance"] = {"source_api": api_name, "fq": fq,
            "volume_unit": "shares", "money_unit": "CNY",
            "adjustment_source": ({"sina_hfq": "sina.hfq precise change nodes", "daily_ex_reference": "Tushare observed ex-day reference price rule"}.get(getattr(self.backend, "adjustment_source", "tushare"), "Tushare dated adj_factor") if api_name == "daily" else "Tushare fund_adj") if fq == "pre" and api_name in {"daily", "fund_daily"} else "unadjusted",
            "adjustment_historical_vintages_verified": False,
            "factor_receipts": {code: receipt for code, receipt in getattr(self.backend, "precise_factor_provenance", {}).items()
                                if api_name == "daily" and fq == "pre" and code in set(df.get("ts_code", []))},
            "native_factor_equivalence_verified": False,
            "factor_precision_policy": ("decimal_ratio_from_rounded_observed_reference_quotes; not_exact_native_reconstruction" if getattr(self.backend, "adjustment_source", "tushare") == "daily_ex_reference" else "retain_provider_precision; never infer_exact_factor_from_rounded_prices"),
            "cash_dividend_note": "shareholder_cash_and_exchange_ex_dividend_cash_can_differ_when_treasury_shares_do_not_participate",
            "price_decimals": (2 if api_name == "daily" else 3) if fq == "pre" and api_name in {"daily", "fund_daily"} else None,
            "missing_data_policy": "block unless affirmative suspension/lifecycle evidence", "end_date": str(end_date)}
        if self.optimize_data:
            self._cache_price_result(result_cache_key, result)
        return result

    def _cache_price_result(self, cache_key, result: pd.DataFrame) -> None:
        row_count = len(result)
        budget = self._price_result_cache_row_budget
        if budget <= 0 or row_count > budget:
            return
        previous = self._price_result_cache.pop(cache_key, None)
        if previous is not None:
            self._price_result_cache_rows -= len(previous)
        self._price_result_cache[cache_key] = result.copy()
        self._price_result_cache_rows += row_count
        self._price_result_cache_peak_rows = max(
            self._price_result_cache_peak_rows,
            self._price_result_cache_rows,
        )
        while self._price_result_cache_rows > budget:
            _, evicted = self._price_result_cache.popitem(last=False)
            self._price_result_cache_rows -= len(evicted)
            self._price_result_cache_evictions += 1

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
                "entries": len(self._price_result_cache),
                "rows": self._price_result_cache_rows,
                "peak_rows": self._price_result_cache_peak_rows,
                "row_budget": self._price_result_cache_row_budget,
                "evictions": self._price_result_cache_evictions,
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
            return np.array([], dtype=object)
        open_df = df[df["is_open"].astype(int) == 1].copy()
        open_df = open_df.sort_values("cal_date")
        if count is not None:
            if int(count) <= 0:
                return np.array([], dtype=object)
            open_df = open_df.tail(int(count))
        return np.array([date.fromisoformat(joinquant_date(item)) for item in open_df["cal_date"].tolist()], dtype=object)

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
            result = result[result["ts_code"].map(is_tushare_a_share_code)].copy()
        if date is not None:
            day = normalize_date(date)
            ends = result.get("delist_date", pd.Series("", index=result.index)).fillna("")
            result = result[(result["list_date"] <= day) & ((ends == "") | (ends >= day))].copy()
            historical_names = self._historical_security_state.names_on(day)
            result["name"] = [historical_names.get(code, code) for code in result["ts_code"]]
        if "ts_code" in result.columns:
            result.index = [to_joinquant_code(code) for code in result["ts_code"].tolist()]
        result["display_name"] = result["name"]
        result["start_date"] = [datetime.strptime(normalize_date(x), "%Y%m%d").date() for x in result["list_date"]]
        result["end_date"] = [datetime.strptime(normalize_date(x), "%Y%m%d").date()
                              if pd.notna(x) and str(x).strip() else datetime(2200, 1, 1).date()
                              for x in result.get("delist_date", pd.Series("", index=result.index))]
        result["type"] = "stock"
        return result

    def get_security_info(self, security, as_of=None):
        code = to_joinquant_code(security)
        metadata_by_code = self._security_metadata_map()
        if code not in metadata_by_code:
            raise NotImplementedError(
                f"stock_basic backend data does not include requested security: {code}"
            )
        metadata = metadata_by_code[code]
        name = str(metadata.get("name") or code)
        if as_of is not None:
            name = self._historical_security_state.names_on(normalize_date(as_of)).get(to_tushare_code(code), code)
        delist = metadata.get("delist_date")
        end_date = (datetime.strptime(normalize_date(delist), "%Y%m%d").date()
                    if delist and not pd.isna(delist) else date(2200, 1, 1))
        return SimpleNamespace(
            code=code,
            display_name=name,
            name=name,
            start_date=self._security_start_date(metadata, code),
            end_date=end_date,
            type="stock",
        )

    def get_current_data(self, securities=None, date=None):
        metadata_by_code = self._security_metadata_map()
        codes = securities if securities is not None else list(self.get_all_securities(date=date).index)
        if securities is None:
            codes += [code for code in metadata_by_code if is_tushare_fund_code(code) and code not in codes]
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

        price_rows_by_code = {}
        if self.optimize_data and codes:
            price_frame = self.get_price(
                codes,
                count=2,
                end_date=date,
                fields=["open", "close", "volume"],
                panel=False,
            )
            if not price_frame.empty:
                price_rows_by_code = {
                    str(code): group.sort_values("time").reset_index(drop=True)
                    for code, group in price_frame.groupby("code", sort=False)
                }

        current = {}
        for code in codes:
            metadata = metadata_by_code[code]
            name = str(metadata.get("name") or "")
            if self.optimize_data:
                rows = price_rows_by_code.get(code)
            else:
                rows = self.get_price(
                    code,
                    count=2,
                    end_date=date,
                    fields=["open", "close", "volume"],
                    panel=False,
                )
            row = None if rows is None or rows.empty else rows.iloc[-1]
            last_price = 0.0
            day_open = 0.0
            paused = True
            limit_reference = 0.0
            if row is not None:
                closing_price = self._float_value(row.get("close"), 0.0)
                day_open = self._float_value(row.get("open"), closing_price)
                volume = self._float_value(row.get("volume"), 0.0)
                row_date = normalize_date(row.get("time")) if row.get("time") else ""
                requested_date = normalize_date(date) if date is not None else row_date
                paused = row_date != requested_date or volume <= 0
                at_open = row_date == requested_date and self._is_open_snapshot(date)
                last_price = day_open if at_open else closing_price
                if row_date == requested_date and len(rows) > 1:
                    limit_reference = self._float_value(rows.iloc[-2].get("close"), last_price)
                else:
                    limit_reference = last_price
            state_day = normalize_date(date) if date is not None else row_date if row is not None else None
            if not is_tushare_fund_code(code):
                if state_day is None:
                    raise ValueError(f"No date available for historical security state: {code}")
                name, is_st, high_limit, low_limit, name_source = self._historical_security_state.resolve(
                    to_tushare_code(code), state_day, paused=paused)
            else:
                # This repair covers A-share historical state; legacy fund limits
                # remain separate and must not infer an ST status from their names.
                is_st = False
                name_source = "fund_metadata"
                high_limit = round(limit_reference * 1.1, 2) if limit_reference else 0.0
                low_limit = round(limit_reference * .9, 2) if limit_reference else 0.0
            current[code] = SimpleNamespace(
                is_st=is_st,
                paused=paused,
                last_price=last_price,
                high_limit=high_limit,
                low_limit=low_limit,
                day_open=day_open,
                name=name,
                name_source=name_source,
            )
        return current

    def get_industry(self, securities, date=None):
        codes = self._normalize_securities(securities)
        industry_by_code = self._industry_map()
        if os.environ.get("JQTS_INDUSTRY_COMPAT") == "stock_basic_as_sw_l1":
            missing = [code for code in codes if code not in industry_by_code]
            if missing:
                raise NotImplementedError(
                    "stock_basic backend data does not include requested security industry: "
                    + ", ".join(missing)
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
        if self._industry_provider == "joinquant_full":
            sw_by_code = self._jq_industry_map(codes, date=date)
            missing_full = [code for code in codes if code not in sw_by_code]
            if missing_full:
                raise NotImplementedError(
                    "JoinQuant member cache does not include requested security/date: "
                    + ", ".join(missing_full)
                    + "; import a JoinQuant member export before using joinquant_full"
                )
        else:
            sw_by_code = self._sw_industry_map(codes, date=date)
            if self._industry_provider == "joinquant_taxonomy":
                sw_by_code = self._apply_jq_taxonomy(sw_by_code, date=date)
        missing_codes = [
            code
            for code in codes
            if code not in sw_by_code and code not in industry_by_code
        ]
        if missing_codes:
            names = ", ".join(missing_codes)
            raise NotImplementedError(
                f"stock_basic backend data does not include requested security industry: {names}"
            )
        result = {}
        for code in codes:
            sw_levels = sw_by_code.get(code) or {}
            fallback = industry_by_code.get(code, "")
            l1 = sw_levels.get("sw_l1") or {}
            industry_name = l1.get("industry_name") or fallback
            industry_code = l1.get("industry_code") or fallback
            if not l1:
                if self._industry_provider == "joinquant_full":
                    raise NotImplementedError(
                        "JoinQuant member/classification cache does not provide SW L1 for "
                        + code
                    )
                sw_levels = dict(sw_levels)
                sw_levels["sw_l1"] = {
                    "industry_code": industry_code,
                    "industry_name": industry_name,
                }
            result[code] = dict(sw_levels)
            result[code]["industry_name"] = industry_name
        return result

    def _apply_jq_taxonomy(self, industry_map, date=None):
        """Apply JoinQuant's official names to dated Tushare membership codes."""
        names = self._jq_classification_names(date)
        if not names:
            raise NotImplementedError(
                "JoinQuant classification cache is empty; run import-jq-industry first"
            )
        result = {}
        for security, levels in industry_map.items():
            mapped = {}
            for api_level, jq_level in (("sw_l1", "L1"), ("sw_l2", "L2"), ("sw_l3", "L3")):
                value = levels.get(api_level) or {}
                code = normalize_industry_code(value.get("industry_code"))
                if not code:
                    continue
                mapped[api_level] = {
                    "industry_code": code,
                    "industry_name": names.get((jq_level, code), value.get("industry_name", "")),
                    "provider_industry_name": value.get("industry_name", ""),
                }
            if mapped:
                result[security] = mapped
        return result

    def _jq_classification_names(self, date=None) -> dict[tuple[str, str], str]:
        asof = normalize_date(date) if date is not None else None
        cache_key = asof or "latest"
        if cache_key in self._jq_classification_cache:
            return self._jq_classification_cache[cache_key]
        try:
            frame = self._fetch("jq_industry_classify")
        except Exception:
            frame = None
        names: dict[tuple[str, str], str] = {}
        if frame is not None and not frame.empty:
            candidates: dict[tuple[str, str], list[dict[str, str]]] = {}
            for _, row in frame.iterrows():
                level = str(row.get("level") or "")
                code = normalize_industry_code(row.get("industry_code"))
                start = self._membership_date(row.get("start_date"))
                end = self._membership_date(row.get("end_date"))
                if not level or not code:
                    continue
                if asof is None:
                    if end:
                        continue
                elif (start and asof < start) or (end and asof > end):
                    continue
                candidates.setdefault((level, code), []).append(
                    {"name": str(row.get("industry_name") or ""), "start": start}
                )
            for key, values in candidates.items():
                selected = max(values, key=lambda item: item["start"])
                if selected["name"]:
                    names[key] = selected["name"]
        self._jq_classification_cache[cache_key] = names
        return names

    def _jq_industry_map(self, codes, date=None):
        """Return imported JoinQuant stock membership with official dated names."""
        if self._jq_members_index is None:
            try:
                frame = self._fetch("jq_industry_member")
            except Exception:
                frame = None
            index = {}
            if frame is not None and not frame.empty:
                for _, row in frame.iterrows():
                    security = str(row.get("security") or "")
                    if not security:
                        continue
                    index.setdefault(security, []).append(
                        {
                            "l1_code": normalize_industry_code(row.get("sw_l1_code")),
                            "l2_code": normalize_industry_code(row.get("sw_l2_code")),
                            "l3_code": normalize_industry_code(row.get("sw_l3_code")),
                            "in_date": self._membership_date(row.get("in_date")),
                            "out_date": self._membership_date(row.get("out_date")),
                        }
                    )
            self._jq_members_index = index

        asof = normalize_date(date) if date is not None else None
        names = self._jq_classification_names(date)
        result = {}
        for security in codes:
            candidates = []
            for entry in (self._jq_members_index or {}).get(security, []):
                in_date = entry.get("in_date", "")
                out_date = entry.get("out_date", "")
                if asof is None:
                    if out_date:
                        continue
                elif (in_date and asof < in_date) or (out_date and asof >= out_date):
                    continue
                candidates.append(entry)
            if not candidates:
                continue
            selected = max(candidates, key=lambda item: item.get("in_date", ""))
            levels = {}
            for api_level, raw_level, jq_level in (
                ("sw_l1", "l1", "L1"),
                ("sw_l2", "l2", "L2"),
                ("sw_l3", "l3", "L3"),
            ):
                code = selected.get(f"{raw_level}_code", "")
                name = names.get((jq_level, code), "")
                if code and name:
                    levels[api_level] = {"industry_code": code, "industry_name": name}
            if levels:
                result[security] = levels
        return result

    def _sw_industry_map(self, codes=None, date=None) -> dict[str, dict[str, dict[str, str]]]:
        """Return date-sensitive SW2021 L1/L2/L3 membership when cached.

        The complete Tushare ``index_member_all`` payload is used when present.
        Older caches continue to resolve L1 through ``index_member`` so adding
        L2 support does not silently change an existing L1-only backtest.
        """
        if self._sw_industry_all_cache is not None and date is None:
            return self._sw_industry_all_cache
        if self._sw_members_all_index is None:
            try:
                members = self._fetch("index_member_all")
            except Exception:
                members = None
            index: dict[str, list[dict[str, str]]] = {}
            required = {"l1_code", "l2_code", "ts_code"}
            if members is not None and not members.empty and required.issubset(members.columns):
                for _, row in members.iterrows():
                    raw_code = str(row.get("ts_code") or "")
                    if not raw_code:
                        continue
                    index.setdefault(raw_code, []).append(
                        {
                            "l1_code": str(row.get("l1_code") or ""),
                            "l1_name": str(row.get("l1_name") or ""),
                            "l2_code": str(row.get("l2_code") or ""),
                            "l2_name": str(row.get("l2_name") or ""),
                            "l3_code": str(row.get("l3_code") or ""),
                            "l3_name": str(row.get("l3_name") or ""),
                            "in_date": self._membership_date(row.get("in_date")),
                            "out_date": self._membership_date(row.get("out_date")),
                            "is_new": str(row.get("is_new") or ""),
                        }
                    )
            self._sw_members_all_index = index

        index = self._sw_members_all_index or {}
        asof = normalize_date(date) if date is not None else None
        target_codes = [to_tushare_code(code) for code in codes] if codes else list(index)
        result: dict[str, dict[str, dict[str, str]]] = {}
        for raw_code in target_codes:
            value = self._sw_levels_for(index.get(raw_code, []), asof)
            if value:
                result[to_joinquant_code(raw_code)] = value

        legacy = self._sw_l1_industry_map(codes, date=date)
        for code, value in legacy.items():
            result.setdefault(code, {"sw_l1": value})
        if date is None:
            self._sw_industry_all_cache = result
        return result

    @staticmethod
    def _sw_levels_for(
        entries: list[dict[str, str]],
        asof: str | None,
    ) -> dict[str, dict[str, str]] | None:
        candidates = []
        for entry in entries:
            in_date = entry.get("in_date", "")
            out_date = entry.get("out_date", "")
            if asof is None:
                if out_date and entry.get("is_new") != "Y":
                    continue
            else:
                if in_date and asof < in_date:
                    continue
                if out_date and asof >= out_date:
                    continue
            candidates.append(entry)
        if not candidates:
            return None
        selected = max(candidates, key=lambda item: item.get("in_date", ""))
        result = {}
        for level in ("l1", "l2", "l3"):
            code = selected.get(f"{level}_code", "")
            name = selected.get(f"{level}_name", "")
            if code and name:
                result[f"sw_{level}"] = {
                    "industry_code": code.split(".")[0],
                    "industry_name": name,
                }
        return result or None

    def _sw_l1_industry_map(self, codes=None, date=None) -> dict[str, dict[str, str]]:
        """Return JoinQuant security to SW L1 code/name as of ``date``.

        Uses the Tushare ``index_member`` / ``index_classify`` caches (SW2021).
        Only the requested ``codes`` are resolved on the date-sensitive path;
        membership rows are indexed once and kept in ``self._sw_members_index``.
        Falls back to an empty mapping when the SW tables are unavailable, in
        which case ``get_industry`` degrades to ``stock_basic.industry``.
        """
        if self._sw_industry_cache is not None and date is None:
            return self._sw_industry_cache
        if self._sw_members_index is None:
            try:
                members = self._fetch("index_member")
                classify = self._fetch("index_classify")
            except Exception:
                self._sw_members_index = {}
                self._sw_l1_name_by_code = {}
            else:
                l1_name: dict[str, str] = {}
                if classify is not None and not classify.empty and "index_code" in classify.columns:
                    l1 = (
                        classify[classify.get("level") == "L1"]
                        if "level" in classify.columns
                        else classify
                    )
                    if "industry_name" in l1.columns:
                        l1_name = {
                            str(code): str(name)
                            for code, name in zip(l1["index_code"], l1["industry_name"])
                            if code and name
                        }
                index: dict[str, list[tuple[str, str, str, str]]] = {}
                if members is not None and not members.empty and "con_code" in members.columns:
                    in_col = "in_date" in members.columns
                    out_col = "out_date" in members.columns
                    current_col = "is_new" in members.columns
                    for _, row in members.iterrows():
                        index.setdefault(str(row["con_code"]), []).append(
                            (
                                str(row.get("index_code") or ""),
                                self._membership_date(row.get("in_date")) if in_col else "",
                                self._membership_date(row.get("out_date")) if out_col else "",
                                str(row.get("is_new") or "") if current_col else "",
                            )
                        )
                self._sw_members_index = index
                self._sw_l1_name_by_code = l1_name
        index = self._sw_members_index or {}
        l1_name = self._sw_l1_name_by_code or {}
        if not l1_name or not index:
            return {}
        asof = normalize_date(date) if date is not None else None
        target_codes = [to_tushare_code(code) for code in codes] if codes else None
        result: dict[str, dict[str, str]] = {}
        if target_codes is None:
            for raw, entries in index.items():
                value = self._sw_value_for(entries, l1_name, asof)
                if value:
                    result[to_joinquant_code(raw)] = value
        else:
            for raw in target_codes:
                value = self._sw_value_for(index.get(raw, []), l1_name, asof)
                if value:
                    result[to_joinquant_code(raw)] = value
        if date is None:
            self._sw_industry_cache = result
        return result

    @staticmethod
    def _membership_date(value) -> str:
        if value is None or pd.isna(value):
            return ""
        text = str(value).strip().replace("-", "")
        return "" if text.lower() in {"", "nan", "none"} else text[:8]

    @staticmethod
    def _sw_value_for(
        entries: list[tuple[str, str, str, str]],
        l1_name: dict[str, str],
        asof: str | None,
    ) -> dict[str, str] | None:
        if not entries:
            return None
        candidates = []
        for index_code, in_date, out_date, is_new in entries:
            if asof is None:
                if out_date and is_new != "Y":
                    continue
            else:
                if in_date and asof < in_date:
                    continue
                if out_date and asof >= out_date:
                    continue
            name = l1_name.get(index_code, "")
            if name:
                candidates.append((in_date, index_code, name))
        if not candidates:
            return None
        _, index_code, name = max(candidates)
        return {
            "industry_code": index_code.split(".")[0],
            "industry_name": name,
        }

    def resolve_fundamentals_date(self, requested_date):
        day = validated_date(requested_date)
        calendar = self._fetch("trade_cal", exchange="SSE", end_date=day)
        if calendar.empty or not {"cal_date", "is_open"}.issubset(calendar.columns):
            raise FundamentalsUnavailableError("trade_calendar_unavailable", requested_date=day)
        calendar = calendar.copy()
        calendar["cal_date"] = calendar["cal_date"].map(validated_date)
        # A stale calendar's last open day is not evidence of a holiday today.
        if day not in set(calendar["cal_date"]):
            raise FundamentalsUnavailableError("trade_calendar_coverage_missing", requested_date=day)
        opened = calendar[(calendar["cal_date"] <= day) & (pd.to_numeric(calendar["is_open"], errors="coerce") == 1)]
        if opened.empty:
            raise FundamentalsUnavailableError("previous_trade_day_unavailable", requested_date=day)
        return opened["cal_date"].max()

    def validate_fundamentals_visibility(self, effective_date, as_of):
        """Default daily visibility; specialized portals must supply observation evidence."""
        validate_daily_snapshot_visibility(effective_date, as_of)

    def get_fundamentals(self, q, date=None, statDate=None, *, as_of=None, requested_date=None):
        if date is not None and statDate is not None:
            raise ValueError("get_fundamentals date and statDate are mutually exclusive")
        audit = {
            "requested_date": str(requested_date if requested_date is not None else date) if (requested_date is not None or date is not None) else None,
            "statDate": str(statDate) if statDate is not None else None,
            "as_of": str(as_of) if as_of is not None else None,
            "mode": "strict_point_in_time" if getattr(self.backend, "strict_pit", False) else "compatibility_retrospective",
            "visibility_policy": "daily_snapshot_end_of_day" if as_of is not None else "research_eod_reconstruction",
        }
        if not hasattr(self, "fundamentals_audit"):
            self.fundamentals_audit = []
        try:
            result = self._get_fundamentals_impl(q, date, statDate, as_of, audit)
            audit["row_count"] = len(result)
            audit["status"] = "empty_required_table" if audit.get("empty_tables") else "ok"
            result.attrs["query_executed"] = True
            result.attrs["fundamentals_audit"] = dict(audit)
            if "valuation_provenance" in audit:
                result.attrs["valuation_provenance"] = audit["valuation_provenance"]
            return result
        except Exception as exc:
            audit["status"] = "unavailable"
            audit["reason"] = getattr(exc, "reason", type(exc).__name__)
            audit["details"] = getattr(exc, "details", {"message": str(exc)})
            raise
        finally:
            self.fundamentals_audit.append(dict(audit))

    def _get_fundamentals_impl(self, q, date, stat_date, as_of, audit):
        frames = []
        table_names = (
            {field.table for field in q.fields}
            | {expr.field.table for expr in q.filters}
            | {ordering.field.table for ordering in q.ordering}
        )
        unsupported_tables = sorted(table_names - {"valuation", "income", "indicator"})
        if unsupported_tables:
            names = ", ".join(unsupported_tables)
            raise NotImplementedError(
                f"Fundamentals tables are not implemented locally: {names}"
            )
        report_period = self._fundamental_report_period(stat_date) if stat_date is not None else None
        valuation_date = None
        if "valuation" in table_names:
            request = report_period[0] if report_period else date
            if request is None:
                # Local research still defaults to a completed day, rather
                # than treating the newest cached row as today's snapshot.
                request = previous_calendar_date(as_of or datetime.now())
            valuation_date = self.resolve_fundamentals_date(request)
            audit["valuation_date"] = valuation_date
            audit.update(self.validate_fundamentals_visibility(valuation_date, as_of) or {})
        income_as_of = date
        if stat_date is not None:
            income_as_of = as_of if getattr(self.backend, "strict_pit", False) else None
            if getattr(self.backend, "financial_mode", None) == "research_reconstructed" and as_of is not None:
                # A quarterly query must not make later announcements visible
                # during a historical research replay. Latest-only revisions
                # remain explicitly unverified even with this cutoff.
                income_as_of = previous_calendar_date(as_of)
            audit["financial_query"] = "visible_report_period" if income_as_of is not None else "retrospective_report_period"
        elif income_as_of is None:
            income_as_of = previous_calendar_date(as_of or datetime.now())
        audit["income_as_of"] = str(income_as_of) if income_as_of is not None else None
        if report_period:
            audit["report_period_end"] = report_period[0]
            audit["income_semantics"] = "annual_cumulative" if report_period[1] else "single_quarter"
        if "valuation" in table_names:
            frames.append(("valuation", self._valuation_frame(valuation_date, as_of=as_of)))
        if "income" in table_names:
            income_df, income_stat_date = self._income_frame_for_stat_date(stat_date, income_as_of)
            if not income_df.empty:
                if not (report_period and report_period[1]):
                    income_df = self._income_single_quarter_frame(income_df, income_stat_date, date=income_as_of)
                income_df = income_df.rename(
                    columns={
                        "ts_code": "code",
                        "n_income_attr_p": "np_parent_company_owners",
                        "total_revenue": "total_operating_revenue",
                        "revenue": "operating_revenue",
                        "n_income": "net_profit",
                        "end_date": "statDate",
                    }
                )
                announcement = pd.Series(None, index=income_df.index, dtype="object")
                for column in ("ann_date", "f_ann_date"):
                    if column in income_df:
                        values = income_df[column].map(self._normalize_income_announcement_date)
                        announcement = values.where(values.notna(), announcement)
                income_df["pubDate"] = announcement.map(lambda x: joinquant_date(x) if pd.notna(x) else None)
                income_df["statDate"] = income_df["statDate"].map(lambda x: joinquant_date(x) if pd.notna(x) else None)
                income_df["code"] = [to_joinquant_code(code) for code in income_df["code"].tolist()]
            frames.append(("income", income_df))
        if "indicator" in table_names:
            frames.append(("indicator", self._income_growth_indicator_frame(income_as_of, stat_date)))
        references = list(q.fields) + [expr.field for expr in q.filters] + [item.field for item in q.ordering]
        # Required tables cannot disappear from the join merely because their
        # source frame is empty. Keep the selected/filter/order schema.
        audit["empty_tables"] = [table for table, frame in frames if frame.empty]
        prepared = []
        for table, frame in frames:
            if table == "valuation" and "valuation_provenance" in frame.attrs:
                audit["valuation_provenance"] = dict(frame.attrs["valuation_provenance"])
            if table == "income":
                audit["income_provenance"] = dict(frame.attrs)
            fields = [field for field in references if field.table == table]
            public_columns = list(dict.fromkeys(["code", *[self._fundamental_column_name(field) for field in fields]]))
            if frame.empty:
                frame = pd.DataFrame({name: pd.Series(dtype="object" if name == "code" else "float64") for name in public_columns})
            else:
                missing = sorted(set(public_columns) - set(frame.columns))
                if missing:
                    raise NotImplementedError(f"Fundamentals {table} backend is missing required fields: {', '.join(missing)}")
                frame = frame[public_columns].copy()
                if frame["code"].duplicated().any():
                    raise FundamentalsUnavailableError("duplicate_fundamental_security", table=table)
            frame = frame.rename(columns={name: f"{table}.{name}" for name in public_columns if name != "code"})
            prepared.append(frame)
        if not prepared:
            return pd.DataFrame(columns=[self._fundamental_column_name(field) for field in q.fields])
        result = prepared[0]
        for frame in prepared[1:]:
            result = result.merge(frame, on="code", how="inner", validate="one_to_one")
        result = self._apply_query_filters(result, q.filters)
        result = self._apply_query_ordering(result, q.ordering)
        audit["code_set_sha256"] = hashlib.sha256("\n".join(sorted(set(result["code"].astype(str)))).encode()).hexdigest()
        return self._project_fundamental_fields(result, q.fields)

    def _fundamental_report_period(self, value):
        text = str(value).strip().lower().replace("-", "")
        if len(text) == 4 and text.isdigit():
            return f"{text}1231", True
        parsed = self._parse_income_period(text)
        if parsed is None:
            raise ValueError(f"Unsupported fundamentals statDate: {value!r}")
        year, quarter = parsed
        return f"{year:04d}{ {1: '0331', 2: '0630', 3: '0930', 4: '1231'}[quarter]}", False

    def get_fundamentals_continuously(self, q, end_date=None, count=1):
        end_date = self.resolve_fundamentals_date(end_date or previous_calendar_date(datetime.now()))
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
        # The backend can select a per-security as-of row with an index/window
        # query. A fixed lookback window silently removes long suspensions.
        return {"end_date": end, "latest_per_code": True}

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
            raise PriceDataUnavailable(f"adjustment_factor_missing: {factor_api} for suspension seed")
        latest_by_code = latest.set_index("ts_code")["adj_factor"]
        seed_by_code = at_seed.set_index("ts_code")["adj_factor"]
        scale = seed["ts_code"].map(seed_by_code) / seed["ts_code"].map(latest_by_code)
        scale = pd.to_numeric(scale, errors="coerce")
        if not (np.isfinite(scale) & (scale > 0)).all():
            raise PriceDataUnavailable(f"adjustment_factor_missing: incomplete {factor_api} for suspension seed")
        result = seed.copy()
        result["factor"] = scale
        for column in ("open", "high", "low", "close", "pre_close"):
            if column in result.columns:
                result[column] = (pd.to_numeric(result[column], errors="coerce") * scale).round(2 if api_name == "daily" else 3)
        if "vol" in result:
            result["vol"] = pd.to_numeric(result["vol"], errors="coerce") / scale
        return result

    def _fill_paused_trade_days(self, df, *, start_date, end_date, count, api_name="daily", securities=(), fq=None):
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

        absent_codes = {to_tushare_code(code) for code in securities} - set(df["ts_code"])
        if absent_codes:
            raise PriceDataUnavailable(f"price_data_missing: {api_name} missing securities {sorted(absent_codes)}")

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
        # Seed observations can have different dates across securities. Only
        # requested sessions require complete bars or suspension evidence.
        gaps = indexed.index[missing & indexed.index.get_level_values("trade_date").isin(calendar)]
        if len(gaps):
            confirmed = set()
            if api_name == "daily":
                suspensions = self._fetch("suspend_d", start_date=min(all_days), end_date=max(all_days))
                if {"ts_code", "trade_date", "suspend_type"}.issubset(suspensions.columns):
                    confirmed = set(zip(suspensions.loc[suspensions.suspend_type == "S", "ts_code"],
                                        suspensions.loc[suspensions.suspend_type == "S", "trade_date"]))
            metadata = self._security_metadata_map() if api_name == "daily" else {}
            outside_lifecycle = set()
            for code, day in gaps:
                info = metadata.get(to_joinquant_code(code), {})
                listed, delisted = info.get("list_date"), info.get("delist_date")
                if (listed and pd.notna(listed) and day < normalize_date(listed)) or (
                    delisted and pd.notna(delisted) and day > normalize_date(delisted)):
                    outside_lifecycle.add((code, day))
            unresolved = set(gaps) - confirmed - outside_lifecycle
            if unresolved:
                examples = ", ".join(f"{code}@{day}" for code, day in sorted(unresolved)[:8])
                raise PriceDataUnavailable(f"price_data_missing: {api_name} missing {examples}; absent source bars are not confirmed suspensions")
            if outside_lifecycle:
                indexed = indexed.drop(list(outside_lifecycle))
                missing = indexed["_observed"].isna()
        factor_gaps = indexed.index[missing & indexed.index.get_level_values("trade_date").isin(calendar)]
        if "factor" in indexed.columns and len(factor_gaps):
            if fq != "pre" or api_name not in {"daily", "fund_daily"}:
                indexed.loc[factor_gaps, "factor"] = 1.0
            else:
                # Price filling follows affirmative suspension evidence.
                # Factor metadata must still use the actual dated source;
                # carrying a prior factor would miss actions during a halt.
                factor_api = "fund_adj" if api_name == "fund_daily" else "adj_factor"
                gap_codes = sorted({code for code, _ in factor_gaps})
                factors = self._fetch_price_adjustment_factors(
                    factor_api, ts_codes=",".join(gap_codes),
                    start_date=min(day for _, day in factor_gaps), end_date=normalize_date(effective_end))
                anchors = self._fetch(factor_api, ts_code=",".join(gap_codes),
                                      end_date=normalize_date(effective_end), latest_per_code=True)
                if factors.empty or anchors.empty:
                    raise PriceDataUnavailable("adjustment_factor_missing: confirmed suspension has no dated factor")
                anchors = self._tail_per_security(anchors, 1).set_index("ts_code")["adj_factor"]
                dated = factors.set_index(["ts_code", "trade_date"])["adj_factor"]
                for identity in factor_gaps:
                    value = dated.get(identity)
                    anchor = anchors.get(identity[0])
                    if (value is None or anchor is None or not np.isfinite(value)
                            or not np.isfinite(anchor) or value <= 0 or anchor <= 0):
                        raise PriceDataUnavailable(f"adjustment_factor_missing: confirmed suspension {identity}")
                    indexed.at[identity, "factor"] = float(value / anchor)
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

    def _valuation_frame(self, date, *, as_of=None) -> pd.DataFrame:
        mode = getattr(self.backend, "financial_mode", "strict_pit" if getattr(self.backend, "strict_pit", False) else "cached_snapshot")
        capital_source = getattr(self.backend, "capital_source", "tushare")
        cache_key = (normalize_date(date) if date is not None else "", mode, str(as_of), capital_source)
        cached = self._valuation_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        strict_reports = self._load_income_raw(date) if mode == "strict_pit" else None
        snapshot_method = getattr(self.backend, "daily_basic_asof", None)
        source = snapshot_method(date) if date is not None and callable(snapshot_method) else self._fetch("daily_basic", **self._valuation_date_params(date))
        diagnostics = {"mode": mode, "financial_mode": mode,
                       "requested_date": normalize_date(date) if date is not None else None,
                       "share_capital_source": "tushare.daily_basic",
                       "share_capital_date_basis": "daily_snapshot_date; not_verified_corporate_action_effective_date",
                       "share_capital_effective_date_verified": False,
                       "pit_verified": False, "stale_codes": [], "missing_codes": [],
                       "pe_status_by_code": {}, "pb_status_by_code": {}}
        capital_reader = getattr(self.backend, "capital_evidence_asof", None)
        if callable(capital_reader) and date is not None:
            diagnostics["published_capital_evidence"] = capital_reader(date, as_of)
            diagnostics["capital_evidence_policy"] = "publication_time_gate; period_end_bounds_do_not_override_snapshot_values"
        if source.empty:
            result = source.copy()
            result.attrs["valuation_provenance"] = {**diagnostics, "status": "valuation_snapshot_missing"}
            self._valuation_cache[cache_key] = result.copy()
            return result
        result = self._filter_a_share_rows(self._latest_per_security(source)).copy()
        # PE is explicitly TTM. Static pe cannot substitute for missing pe_ttm.
        result["pe_ratio"] = pd.to_numeric(result.get("pe_ttm", pd.Series(float("nan"), index=result.index)), errors="coerce")
        result["pe_ratio"] = result["pe_ratio"].replace([np.inf, -np.inf], np.nan)
        statuses = pd.Series(np.where(result["pe_ratio"].notna(), "source_pe_ttm", "ttm_source_missing"), index=result.index)
        if mode in {"research_reconstructed", "strict_pit"} and date is not None and result["pe_ratio"].isna().any():
            reports = strict_reports if strict_reports is not None else self._load_income_raw(date)
            reports = self._filter_income_asof(reports, date)
            profits = signed_ttm_by_security(reports, date)
            for index, row in result[result["pe_ratio"].isna()].iterrows():
                profit, status = profits.get(str(row["ts_code"]), (float("nan"), "ttm_inputs_missing"))
                statuses.loc[index] = status
                market_value = pd.to_numeric(row.get("total_mv"), errors="coerce")
                if np.isfinite(profit) and profit != 0 and np.isfinite(market_value) and market_value > 0:
                    result.loc[index, "pe_ratio"] = float(market_value) * 10000.0 / profit
                    statuses.loc[index] = "retrospective_reconstructed" if mode == "research_reconstructed" else "revision_ttm"
        if date is not None and "trade_date" in result:
            requested = normalize_date(date)
            metadata = self._fetch("stock_basic")
            active = pd.DataFrame()
            if not metadata.empty and "ts_code" in metadata:
                active = self._filter_a_share_rows(metadata)
                if "list_date" in active:
                    active = active[(active["list_date"].astype(str) <= requested) | active["list_date"].isna()]
                if "delist_date" in active:
                    active = active[active["delist_date"].isna() | active["delist_date"].astype(str).isin(["", "None", "nan"]) | (active["delist_date"].astype(str) >= requested)]
                # Listed/delisted membership is a lifecycle boundary. A stale
                # final quote does not keep a delisted security in today's pool.
                known = set(metadata["ts_code"])
                active_codes = set(active["ts_code"])
                result = result[~result["ts_code"].isin(known) | result["ts_code"].isin(active_codes)]
                statuses = statuses.loc[result.index]
                missing_codes = sorted(active_codes - set(result["ts_code"]))
                diagnostics["missing_codes"] = [to_joinquant_code(code) for code in missing_codes]
                if missing_codes:
                    missing = pd.DataFrame([{column: code if column == "ts_code" else float("nan") for column in result.columns} for code in missing_codes])
                    result = pd.concat([result, missing], ignore_index=True)
                    statuses = pd.Series([*statuses.tolist(), *["valuation_snapshot_missing" for _ in missing_codes]], index=result.index)
            stale = result["trade_date"].notna() & (result["trade_date"].astype(str).map(normalize_date) != requested)
            diagnostics["stale_codes"] = [to_joinquant_code(code) for code in result.loc[stale, "ts_code"]]
            diagnostics["snapshot_date_by_code"] = {to_joinquant_code(row.ts_code): str(row.trade_date) if pd.notna(row.trade_date) else None for row in result.itertuples()}
            # Old source rows remain traceable identities; their numeric values
            # cannot stand in for as-of share capital/actions/financial updates.
            invalid = stale | result["trade_date"].isna()
            result.loc[invalid, "pe_ratio"] = float("nan")
            statuses.loc[stale] = "stale_valuation_snapshot"
            for column in ("total_mv", "circ_mv", "pb", "turnover_rate", "total_share", "float_share"):
                if column in result:
                    result.loc[invalid, column] = float("nan")
        diagnostics["pe_status_by_code"] = {to_joinquant_code(result.at[index, "ts_code"]): str(statuses.at[index]) for index in result.index}
        for index, row in result.iterrows():
            code = to_joinquant_code(row["ts_code"])
            pb = pd.to_numeric(row.get("pb"), errors="coerce")
            state = diagnostics["pe_status_by_code"][code]
            diagnostics["pb_status_by_code"][code] = (
                state if state in {"stale_valuation_snapshot", "valuation_snapshot_missing"}
                else ("source_pb" if pd.notna(pb) and np.isfinite(pb) else "source_pb_missing"))
        if capital_source == "disclosed_reference" and date is not None:
            # Opt-in research view, not an inferred CSDC registration feed.
            # Recompute from original financial statements rather than scale
            # rounded source ratios or fit values to the comparison log.
            references = self.backend.disclosed_capital_values(date, as_of)
            receipts = {}
            requested = normalize_date(date)
            quote_rows = (pd.concat([self._fetch("daily", trade_date=requested, ts_code=code)
                                    for code in references], ignore_index=True)
                          if references else pd.DataFrame())
            quotes = {str(row.ts_code): row for row in quote_rows.itertuples()}
            for index, row in result.iterrows():
                code = str(row["ts_code"])
                basis = references.get(code)
                if basis is None or pd.isna(row.get("trade_date")) or normalize_date(row["trade_date"]) != requested:
                    continue
                quote = quotes.get(code)
                if (quote is None or normalize_date(quote.trade_date) != requested
                        or not np.isfinite(quote.close) or quote.close <= 0):
                    continue
                price = Decimal(str(quote.close))
                total = Decimal(basis["total_shares"])
                circulating = Decimal(basis["circulating_a_shares"])
                market_value = total * price
                result.at[index, "total_share"] = float(total / 10000)
                result.at[index, "float_share"] = float(circulating / 10000)
                result.at[index, "total_mv"] = float(market_value / 10000)
                result.at[index, "circ_mv"] = float(circulating * price / 10000)
                quantum = Decimal("0.0001")
                profit = Decimal(basis["ttm_parent_profit_cny"])
                result.at[index, "pe_ratio"] = float((market_value / profit).quantize(quantum, rounding=ROUND_HALF_UP)) if profit else float("nan")
                result.at[index, "pb"] = float((market_value / Decimal(basis["ordinary_equity_cny"])).quantize(quantum, rounding=ROUND_HALF_UP))
                if circulating and np.isfinite(quote.vol):
                    result.at[index, "turnover_rate"] = float((Decimal(str(quote.vol)) * 10000 / circulating).quantize(quantum, rounding=ROUND_HALF_UP))
                jqcode = to_joinquant_code(code)
                diagnostics["pe_status_by_code"][jqcode] = "original_filing_ttm_reconstructed"
                diagnostics["pb_status_by_code"][jqcode] = "original_filing_common_equity_reconstructed"
                receipts[jqcode] = {**basis, "price_source": "tushare.daily", "price_date": requested,
                    "close": str(price), "valuation_rounding": "PE/PB/turnover: decimal ROUND_HALF_UP 4 places"}
            diagnostics["share_capital_source"] = "per_code_disclosed_reference_with_tushare_snapshot_for_unqualified_rows"
            diagnostics["share_capital_date_basis"] = "latest_visible_disclosed_period_end_snapshot; individual_registration_dates_unknown"
            diagnostics["capital_evidence_policy"] = "explicit_research_view; publication_and_scope_gated; original_rows_preserved"
            diagnostics["disclosed_capital_receipts"] = receipts
        diagnostics["pit_verified"] = mode == "strict_pit"
        diagnostics["status"] = "incomplete_snapshot" if diagnostics["stale_codes"] or diagnostics["missing_codes"] else "snapshot_available"
        diagnostics["legacy_financial_vintages_recovered"] = False
        # Both providers publish share capital in ten thousand shares. Keep
        # the source values separate from market value (ten thousand CNY).
        result = result.rename(columns={"ts_code": "code", "total_mv": "market_cap", "circ_mv": "circulating_market_cap", "pb": "pb_ratio", "turnover_rate": "turnover_ratio", "total_share": "capitalization", "float_share": "circulating_cap"})
        for column in ("market_cap", "circulating_market_cap"):
            if column in result:
                result[column] = pd.to_numeric(result[column], errors="coerce") / 10000.0
        result["code"] = [to_joinquant_code(code) for code in result["code"]]
        result.attrs["valuation_provenance"] = diagnostics
        self._valuation_cache[cache_key] = result.copy()
        return result.copy()

    def _load_income_raw(self, date=None) -> pd.DataFrame:
        strict = bool(getattr(self.backend, "strict_pit", False))
        mode = getattr(self.backend, "financial_mode", "cached_snapshot")
        if strict:
            readiness_method = getattr(self.backend, "financial_readiness", None)
            revision_method = getattr(self.backend, "financial_revisions", None)
            if date is None or not callable(readiness_method) or not callable(revision_method):
                raise FinancialVintageUnavailable("financial_vintage_missing: strict PIT requires as_of and revision storage")
            readiness = readiness_method(date)
            if not readiness.get("ready"):
                raise FinancialVintageUnavailable(readiness.get("reason", "financial_vintage_missing"))
            frame = revision_method(api_name="income", as_of=date)
            frame.attrs["financial_readiness"] = readiness
            return frame
        frame = self._fetch("income").copy()
        frame.attrs.update(provenance_state="research_reconstructed" if mode == "research_reconstructed" else "legacy_snapshot", pit_verified=False, legacy_financial_vintages_recovered=False)
        return frame

    def _income_frame_for_stat_date(self, stat_date, date=None) -> tuple[pd.DataFrame, str | None]:
        frame = self._filter_income_asof(self._load_income_raw(date), date)
        if stat_date is None:
            return frame, None
        period_end, annual = self._fundamental_report_period(stat_date)
        period_column = "end_date" if "end_date" in frame.columns else "period"
        if frame.empty:
            return frame, period_end
        if period_column not in frame.columns:
            raise NotImplementedError("income backend data must include end_date")
        dates = frame[period_column].astype(str).str.replace("-", "", regex=False).str[:8]
        return frame[dates == period_end].reset_index(drop=True), period_end

    def _income_growth_indicator_frame(self, date=None, stat_date=None) -> pd.DataFrame:
        """Derive JoinQuant-style disclosed YoY indicators from cached income reports."""
        visible = self._filter_income_asof(self._load_income_raw(date), date)
        required = {"ts_code", "end_date", "total_revenue", "n_income_attr_p"}
        if visible.empty or not required.issubset(set(visible.columns)):
            return pd.DataFrame(
                columns=[
                    "code",
                    "inc_revenue_year_on_year",
                    "inc_net_profit_year_on_year",
                    "inc_revenue_annual",
                    "inc_net_profit_annual",
                ]
            )

        reports = visible.copy()
        reports["end_date"] = reports["end_date"].astype(str).str.replace("-", "", regex=False).str[:8]
        reports = reports[reports["end_date"].str.len() == 8]
        reports = reports.sort_values(
            [
                column
                for column in (
                    "ts_code",
                    "end_date",
                    "f_ann_date",
                    "ann_date",
                    "report_type",
                )
                if column in reports.columns
            ]
        )
        reports = reports.groupby(["ts_code", "end_date"], group_keys=False).tail(1)

        def previous_cumulative_end(value: str) -> str | None:
            year, month_day = int(value[:4]), value[4:]
            return {
                "0630": f"{year:04d}0331",
                "0930": f"{year:04d}0630",
                "1231": f"{year:04d}0930",
            }.get(month_day)

        reports["previous_cumulative_end_date"] = reports["end_date"].map(
            previous_cumulative_end
        )
        previous_cumulative = reports[
            ["ts_code", "end_date", "total_revenue", "n_income_attr_p"]
        ].rename(
            columns={
                "end_date": "previous_cumulative_end_date",
                "total_revenue": "previous_cumulative_revenue",
                "n_income_attr_p": "previous_cumulative_profit",
            }
        )
        reports = reports.merge(
            previous_cumulative,
            on=["ts_code", "previous_cumulative_end_date"],
            how="left",
        )
        previous_revenue = pd.to_numeric(reports["previous_cumulative_revenue"], errors="coerce")
        previous_profit = pd.to_numeric(reports["previous_cumulative_profit"], errors="coerce")
        first_quarter = reports["end_date"].str.endswith("0331")
        previous_revenue = previous_revenue.where(~first_quarter, 0.0)
        previous_profit = previous_profit.where(~first_quarter, 0.0)
        reports["single_quarter_revenue"] = pd.to_numeric(
            reports["total_revenue"], errors="coerce"
        ) - previous_revenue
        reports["single_quarter_profit"] = pd.to_numeric(
            reports["n_income_attr_p"], errors="coerce"
        ) - previous_profit

        if stat_date is not None:
            period_end, _annual = self._fundamental_report_period(stat_date)
            latest = reports[reports["end_date"] == period_end].copy()
        else:
            latest = reports.groupby("ts_code", group_keys=False).tail(1).copy()
        latest["prior_end_date"] = latest["end_date"].map(
            lambda value: f"{int(value[:4]) - 1:04d}{value[4:]}"
        )

        def previous_quarter_end(value: str) -> str:
            year, month_day = int(value[:4]), value[4:]
            return {
                "0331": f"{year - 1:04d}1231",
                "0630": f"{year:04d}0331",
                "0930": f"{year:04d}0630",
                "1231": f"{year:04d}0930",
            }[month_day]

        latest["previous_quarter_end_date"] = latest["end_date"].map(
            previous_quarter_end
        )

        prior = reports[
            ["ts_code", "end_date", "total_revenue", "n_income_attr_p"]
        ].rename(
            columns={
                "end_date": "prior_end_date",
                "total_revenue": "prior_total_revenue",
                "n_income_attr_p": "prior_n_income_attr_p",
            }
        )
        result = latest.merge(prior, on=["ts_code", "prior_end_date"], how="left")
        previous_quarter = reports[
            [
                "ts_code",
                "end_date",
                "single_quarter_revenue",
                "single_quarter_profit",
            ]
        ].rename(
            columns={
                "end_date": "previous_quarter_end_date",
                "single_quarter_revenue": "previous_quarter_revenue",
                "single_quarter_profit": "previous_quarter_profit",
            }
        )
        result = result.merge(
            previous_quarter,
            on=["ts_code", "previous_quarter_end_date"],
            how="left",
        )

        current_revenue = pd.to_numeric(result["total_revenue"], errors="coerce")
        prior_revenue = pd.to_numeric(result["prior_total_revenue"], errors="coerce")
        current_profit = pd.to_numeric(result["n_income_attr_p"], errors="coerce")
        prior_profit = pd.to_numeric(result["prior_n_income_attr_p"], errors="coerce")
        result["inc_revenue_year_on_year"] = (
            (current_revenue / prior_revenue.replace(0, pd.NA) - 1.0) * 100.0
        )
        result["inc_net_profit_year_on_year"] = (
            (current_profit - prior_profit) / prior_profit.abs().replace(0, pd.NA) * 100.0
        )
        single_revenue = pd.to_numeric(result["single_quarter_revenue"], errors="coerce")
        previous_quarter_revenue = pd.to_numeric(
            result["previous_quarter_revenue"], errors="coerce"
        )
        single_profit = pd.to_numeric(result["single_quarter_profit"], errors="coerce")
        previous_quarter_profit = pd.to_numeric(
            result["previous_quarter_profit"], errors="coerce"
        )
        result["inc_revenue_annual"] = (
            (single_revenue - previous_quarter_revenue)
            / previous_quarter_revenue.abs().replace(0, pd.NA)
            * 100.0
        )
        result["inc_net_profit_annual"] = (
            (single_profit - previous_quarter_profit)
            / previous_quarter_profit.abs().replace(0, pd.NA)
            * 100.0
        )
        result["code"] = [to_joinquant_code(code) for code in result["ts_code"].tolist()]
        return result[
            [
                "code",
                "inc_revenue_year_on_year",
                "inc_net_profit_year_on_year",
                "inc_revenue_annual",
                "inc_net_profit_annual",
            ]
        ].reset_index(drop=True)

    def _filter_income_asof(self, income_df: pd.DataFrame, date=None) -> pd.DataFrame:
        strict = bool(getattr(self.backend, "strict_pit", False))
        if strict and income_df.attrs.get("provenance_state") != "revisions_verified":
            raise FinancialVintageUnavailable("financial_vintage_missing: latest-only income cannot establish historical versions")
        if income_df.empty:
            return income_df.copy()
        if income_df.attrs.get("provenance_state") == "revisions_verified":
            return select_visible_reports(income_df, date)
        result = income_df.copy()
        if date is None:
            return result
        date_columns = [column for column in ("f_ann_date", "ann_date") if column in result.columns]
        if not date_columns:
            result = result.iloc[:0].copy()
            result.attrs.update(income_df.attrs, financial_gap="announcement_date_missing")
            return result
        asof = normalize_date(date)
        visible = pd.Series(True, index=result.index)
        has_announcement_date = pd.Series(False, index=result.index)
        for column in date_columns:
            values = result[column].map(self._normalize_income_announcement_date)
            has_value = values.notna()
            has_announcement_date = has_announcement_date | has_value
            visible &= ~has_value | (values <= asof)
        result = result[visible & has_announcement_date].reset_index(drop=True)
        if "report_type" in result:
            result = result[result["report_type"].astype(str).isin(["1", "1.0"])].reset_index(drop=True)
        result.attrs.update(income_df.attrs)
        return result

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
        if result.empty:
            return result
        if stat_date is not None:
            prev_period = self._previous_income_period(stat_date)
            if prev_period is None:
                return result
            previous_end = self._fundamental_report_period(prev_period)[0]
            target_previous = pd.Series(previous_end, index=result.index)
        elif "end_date" in result.columns:
            target_previous = result["end_date"].map(
                lambda value: self._previous_income_period(value)
            ).map(lambda value: self._fundamental_report_period(value)[0] if pd.notna(value) else None)
        else:
            # Legacy fixtures with no period identity cannot support a
            # cumulative-to-quarter transformation.
            return result
        prev_df = self._filter_income_asof(self._load_income_raw(date), date)
        if "end_date" not in prev_df.columns:
            if "period" in prev_df.columns:
                prev_df = prev_df.rename(columns={"period": "end_date"})
            else:
                prev_df = pd.DataFrame(columns=["ts_code", "end_date"])
        prev_df = prev_df.copy()
        prev_df["end_date"] = prev_df["end_date"].astype(str).str.replace("-", "", regex=False).str[:8]
        previous_by_period = prev_df.sort_values([name for name in ("ts_code", "end_date", "f_ann_date", "ann_date", "report_type") if name in prev_df.columns]).drop_duplicates(["ts_code", "end_date"], keep="last").set_index(["ts_code", "end_date"])
        previous_keys = pd.MultiIndex.from_arrays([result["ts_code"], target_previous])
        for column in ("total_revenue", "revenue", "n_income", "n_income_attr_p"):
            if column not in result.columns:
                continue
            previous_values = (previous_by_period[column].reindex(previous_keys).to_numpy() if column in previous_by_period.columns else np.full(len(result), np.nan))
            previous_values = pd.Series(previous_values, index=result.index).where(target_previous.notna(), 0.0)
            result[column] = pd.to_numeric(result[column], errors="coerce") - pd.to_numeric(previous_values, errors="coerce")
        return result

    def _latest_income_per_security(self, income_df: pd.DataFrame) -> pd.DataFrame:
        if "ts_code" not in income_df.columns:
            return income_df
        sort_columns = [column for column in ("ts_code", "end_date", "period", "f_ann_date", "ann_date", "report_type") if column in income_df.columns]
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
        text = str(stat_date or "").strip().lower().replace("-", "")
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
        return is_tushare_a_share_code(code)

    def _apply_query_filters(self, df: pd.DataFrame, filters) -> pd.DataFrame:
        result = df.copy()
        for expr in filters:
            column = self._fundamental_internal_column_name(expr.field)
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
            column = self._fundamental_internal_column_name(item.field)
            if column not in result.columns:
                raise NotImplementedError(f"Fundamentals ordering field is not available locally: {column}")
            result = result.sort_values(column, ascending=item.direction != "desc")
        return result.reset_index(drop=True)

    def _project_fundamental_fields(self, df: pd.DataFrame, fields) -> pd.DataFrame:
        columns = [self._fundamental_internal_column_name(field) for field in fields]
        missing = [column for column in columns if column not in df.columns]
        if missing:
            names = ", ".join(missing)
            raise NotImplementedError(f"Fundamentals fields are not available locally: {names}")
        result = df[columns].reset_index(drop=True)
        result.columns = [self._fundamental_column_name(field) for field in fields]
        return result

    def _fundamental_internal_column_name(self, field):
        name = self._fundamental_column_name(field)
        return name if name == "code" else f"{field.table}.{name}"

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
        if table == "indicator":
            return {
                "code": "code",
                "inc_revenue_year_on_year": "inc_revenue_year_on_year",
                "inc_net_profit_year_on_year": "inc_net_profit_year_on_year",
                "inc_revenue_annual": "inc_revenue_annual",
                "inc_net_profit_annual": "inc_net_profit_annual",
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

    def _format_price_frame(self, df: pd.DataFrame, fields: Iterable[str], *, api_name="daily") -> pd.DataFrame:
        self._validate_price_frame(df)
        result = pd.DataFrame()
        result["time"] = self._format_joinquant_dates(df["trade_date"])
        result["code"] = self._format_joinquant_codes(df["ts_code"])
        for field in fields:
            source = PRICE_FIELD_MAP.get(field, field)
            if source not in df.columns:
                raise NotImplementedError(f"Requested field is not available from backend data: {field}")
            if field == "money":
                multiplier = 10000.0 if api_name == "sw_daily" else 1000.0
                result[field] = pd.to_numeric(df[source], errors="coerce").values * multiplier
            elif field == "volume":
                multiplier = 10000.0 if api_name == "sw_daily" else 100.0
                result[field] = np.round(pd.to_numeric(df[source], errors="coerce").values * multiplier)
            else:
                result[field] = df[source].values
        return result.reset_index(drop=True)

    def _format_joinquant_dates(self, values: pd.Series) -> pd.Series:
        dates = values.astype(str).str[:10].str.replace("-", "", regex=False)
        return dates.str[:4] + "-" + dates.str[4:6] + "-" + dates.str[6:8]

    def _format_joinquant_codes(self, values: pd.Series) -> pd.Series:
        raw = values.astype(str)
        mapping = {code: to_joinquant_code(code) for code in raw.unique()}
        return raw.map(mapping)

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
        except NotImplementedError as error:
            raise PriceDataUnavailable(f"adjustment_factor_missing: {factor_api_name}: {error}") from error
        if factors.empty or "adj_factor" not in factors.columns:
            raise PriceDataUnavailable(f"adjustment_factor_missing: {factor_api_name} {ts_codes}")

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
        if not (np.isfinite(scale) & (scale > 0)).all():
            raise PriceDataUnavailable(f"adjustment_factor_missing: incomplete {factor_api_name} for requested prices")
        result["factor"] = scale
        for column in ("open", "high", "low", "close", "pre_close"):
            if column in result.columns:
                result[column] = (pd.to_numeric(result[column], errors="coerce") * scale).round(2 if api_name == "daily" else 3)
        if "vol" in result:
            result["vol"] = pd.to_numeric(result["vol"], errors="coerce") / scale
        return result.drop(columns=["adj_factor", "_latest_adj_factor"], errors="ignore")

    def _fetch_price_adjustment_factors(
        self,
        factor_api_name: str,
        *,
        ts_codes: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        if factor_api_name == "adj_factor" and getattr(self.backend, "adjustment_source", "tushare") != "tushare":
            # Optional receipts cover requested stocks, not the entire market.
            # Filter source identities before resolving their factor source.
            return self._fetch(factor_api_name, ts_code=ts_codes,
                               start_date=start_date, end_date=end_date)
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

    def _is_open_snapshot(self, value) -> bool:
        time_method = getattr(value, "time", None)
        if not callable(time_method):
            return False
        current_time = time_method()
        return current_time.hour == 9 and current_time.minute == 30

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
