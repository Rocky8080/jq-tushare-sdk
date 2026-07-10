from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Callable

import pandas as pd

from jq_tushare_sdk.data.code_map import normalize_date


class CanonicalPriceStoreError(RuntimeError):
    pass


@dataclass
class _ApiFrame:
    frame: pd.DataFrame
    start_date: str
    end_date: str


@dataclass
class CanonicalPriceStats:
    loads: int = 0
    extensions: int = 0
    loaded_rows: int = 0
    load_seconds: float = 0.0
    slice_calls: int = 0
    slice_seconds: float = 0.0
    adjusted_hits: int = 0
    adjusted_misses: int = 0
    adjustment_seconds: float = 0.0


class CanonicalPriceStore:
    def __init__(
        self,
        fetch: Callable[..., pd.DataFrame],
        start_date,
        end_date,
        adjusted_capacity: int = 2,
    ):
        self.fetch = fetch
        self.start_date = normalize_date(start_date)
        self.end_date = normalize_date(end_date)
        self.adjusted_capacity = int(adjusted_capacity)
        self._raw: dict[str, _ApiFrame] = {}
        self._adjusted: OrderedDict[tuple[str, str, str], pd.DataFrame] = OrderedDict()
        self._stats = CanonicalPriceStats()

    def select(
        self,
        api_name: str,
        *,
        ts_codes: list[str],
        start_date: str | None,
        end_date: str,
        count: int | None,
        fq: str | None,
        factor_api_name: str | None,
    ) -> pd.DataFrame:
        started = perf_counter()
        try:
            requested_start = normalize_date(start_date) if start_date is not None else self.start_date
            requested_end = normalize_date(end_date)
            if fq == "pre" and factor_api_name is not None:
                source = self._adjusted_source(
                    api_name,
                    factor_api_name,
                    requested_start,
                    requested_end,
                )
            else:
                source = self._raw_source(api_name, requested_start, requested_end)

            result = source[
                (source["trade_date"] >= requested_start)
                & (source["trade_date"] <= requested_end)
            ]
            if count is not None:
                if int(count) <= 0:
                    result = result.iloc[0:0]
                else:
                    result = (
                        result.sort_values(["ts_code", "trade_date"], kind="mergesort")
                        .groupby("ts_code", sort=False, group_keys=False)
                        .tail(int(count))
                    )
            return self._select_codes(result, ts_codes)
        finally:
            self._stats.slice_calls += 1
            self._stats.slice_seconds += perf_counter() - started

    def snapshot(self) -> dict:
        return asdict(self._stats)

    def _ensure_raw(self, api_name: str, requested_start, requested_end) -> None:
        load_start = min(self.start_date, normalize_date(requested_start))
        load_end = max(self.end_date, normalize_date(requested_end))
        current = self._raw.get(api_name)
        if current is None:
            started = perf_counter()
            frame = self.fetch(api_name, start_date=load_start, end_date=load_end)
            self._raw[api_name] = self._state(frame, load_start, load_end)
            self._stats.load_seconds += perf_counter() - started
        elif load_start < current.start_date or load_end > current.end_date:
            started = perf_counter()
            frame = self.fetch(api_name, start_date=load_start, end_date=load_end)
            merged = pd.concat([current.frame, frame], ignore_index=True)
            merged = merged.drop_duplicates(["ts_code", "trade_date"], keep="last")
            self._raw[api_name] = self._state(merged, load_start, load_end)
            self._stats.extensions += 1
            self._stats.load_seconds += perf_counter() - started
            self._adjusted.clear()

    def _state(self, frame: pd.DataFrame, start_date: str, end_date: str) -> _ApiFrame:
        missing = [column for column in ("ts_code", "trade_date") if column not in frame.columns]
        if missing:
            names = ", ".join(missing)
            raise CanonicalPriceStoreError(f"Canonical price data is missing required columns: {names}")
        result = frame.copy()
        result["ts_code"] = result["ts_code"].astype(str)
        result["trade_date"] = result["trade_date"].astype(str)
        result = result.sort_values(["trade_date", "ts_code"], kind="mergesort").reset_index(drop=True)
        self._stats.loads += 1
        self._stats.loaded_rows += len(result)
        return _ApiFrame(frame=result, start_date=start_date, end_date=end_date)

    def _raw_source(self, api_name: str, requested_start, requested_end) -> pd.DataFrame:
        start = normalize_date(requested_start)
        end = normalize_date(requested_end)
        self._ensure_raw(api_name, start, end)
        frame = self._raw[api_name].frame
        return frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)]

    def _adjusted_source(
        self,
        api_name: str,
        factor_api_name: str,
        requested_start,
        end_date,
    ) -> pd.DataFrame:
        normalized_end = normalize_date(end_date)
        self._ensure_raw(api_name, requested_start, normalized_end)
        self._ensure_raw(factor_api_name, requested_start, normalized_end)
        cache_key = (api_name, factor_api_name, normalized_end)
        cached = self._adjusted.get(cache_key)
        if cached is not None:
            self._stats.adjusted_hits += 1
            self._adjusted.move_to_end(cache_key)
            return cached

        started = perf_counter()
        self._stats.adjusted_misses += 1
        try:
            snapshot_start = min(
                self._raw[api_name].start_date,
                self._raw[factor_api_name].start_date,
            )
            price = self._raw_source(api_name, snapshot_start, normalized_end)
            factors = self._raw_source(factor_api_name, snapshot_start, normalized_end)
            if "adj_factor" not in factors.columns:
                raise CanonicalPriceStoreError(
                    "Canonical adjustment data is missing required column: adj_factor"
                )
            factor_frame = factors[["ts_code", "trade_date", "adj_factor"]].copy()
            factor_frame["adj_factor"] = pd.to_numeric(factor_frame["adj_factor"], errors="coerce")
            latest = (
                factor_frame.dropna(subset=["adj_factor"])
                .sort_values(["ts_code", "trade_date"], kind="mergesort")
                .groupby("ts_code", sort=False)["adj_factor"]
                .tail(1)
            )
            latest_by_code = factor_frame.loc[latest.index].set_index("ts_code")["adj_factor"]
            result = price.merge(factor_frame, on=["ts_code", "trade_date"], how="left", sort=False)
            result["_latest_adj_factor"] = result["ts_code"].map(latest_by_code)
            scale = result["adj_factor"] / result["_latest_adj_factor"]
            scale = scale.where(scale.notna() & (scale > 0), 1.0)
            for column in ("open", "high", "low", "close", "pre_close"):
                if column in result.columns:
                    result[column] = pd.to_numeric(result[column], errors="coerce") * scale
            result = result.drop(columns=["adj_factor", "_latest_adj_factor"], errors="ignore")
        finally:
            self._stats.adjustment_seconds += perf_counter() - started

        self._adjusted[cache_key] = result
        self._adjusted.move_to_end(cache_key)
        while len(self._adjusted) > self.adjusted_capacity:
            self._adjusted.popitem(last=False)
        return result

    def _select_codes(self, frame: pd.DataFrame, ts_codes: list[str]) -> pd.DataFrame:
        if len(ts_codes) > 100:
            return frame[frame["ts_code"].isin(ts_codes)].copy().reset_index(drop=True)
        groups = {
            str(code): group
            for code, group in frame.groupby("ts_code", sort=False)
        }
        parts = [groups[code] for code in ts_codes if code in groups]
        if not parts:
            return frame.iloc[0:0].copy().reset_index(drop=True)
        return pd.concat(parts, ignore_index=True).copy()
