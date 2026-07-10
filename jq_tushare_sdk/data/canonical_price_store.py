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
    _RAW_CODE_CHUNK_SIZE = 400

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
        self._raw: dict[str, dict[str, _ApiFrame]] = {}
        self._adjusted: OrderedDict[tuple, dict[str, float]] = OrderedDict()
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
            requested_codes = [str(code) for code in ts_codes]
            requested_start = normalize_date(start_date) if start_date is not None else self.start_date
            requested_end = normalize_date(end_date)
            source = self._raw_source(
                api_name,
                requested_codes,
                requested_start,
                requested_end,
            )
            result = source[
                (source["trade_date"] >= requested_start)
                & (source["trade_date"] <= requested_end)
            ]
            result = self._select_codes(result, requested_codes)
            if count is not None:
                if int(count) <= 0:
                    result = result.iloc[0:0].copy()
                else:
                    result = (
                        result.sort_values(["ts_code", "trade_date"], kind="mergesort")
                        .groupby("ts_code", sort=False, group_keys=False)
                        .tail(int(count))
                    )
            if fq == "pre" and factor_api_name is not None and not result.empty:
                result = self._adjusted_source(
                    api_name,
                    factor_api_name,
                    requested_codes,
                    result,
                    requested_start,
                    requested_end,
                )
            return result.copy().reset_index(drop=True)
        finally:
            self._stats.slice_calls += 1
            self._stats.slice_seconds += perf_counter() - started

    def snapshot(self) -> dict:
        return asdict(self._stats)

    def _ensure_raw(
        self,
        api_name: str,
        ts_codes: list[str],
        requested_start,
        requested_end,
    ) -> None:
        load_start = min(self.start_date, normalize_date(requested_start))
        load_end = normalize_date(requested_end)
        requested_codes = self._unique_codes(ts_codes)
        states = self._raw.setdefault(api_name, {})
        loads: dict[tuple[str, str], list[str]] = {}
        has_extension = False

        for code in requested_codes:
            current = states.get(code)
            if current is None:
                loads.setdefault((load_start, load_end), []).append(code)
                continue
            if load_start < current.start_date:
                loads.setdefault((load_start, current.start_date), []).append(code)
                has_extension = True
            if load_end > current.end_date:
                loads.setdefault((current.end_date, load_end), []).append(code)
                has_extension = True

        for (chunk_start, chunk_end), codes in loads.items():
            for code_chunk in self._chunked(codes):
                started = perf_counter()
                frame = self.fetch(
                    api_name,
                    ts_code=",".join(code_chunk),
                    start_date=chunk_start,
                    end_date=chunk_end,
                )
                normalized = self._normalize_frame(frame)
                normalized = normalized[normalized["ts_code"].isin(code_chunk)].copy()
                self._store_raw_chunk(
                    api_name,
                    code_chunk,
                    normalized,
                    chunk_start,
                    chunk_end,
                )
                self._stats.loads += 1
                self._stats.loaded_rows += len(normalized)
                self._stats.load_seconds += perf_counter() - started

        if loads:
            if has_extension:
                self._stats.extensions += 1
            self._adjusted.clear()

    def _store_raw_chunk(
        self,
        api_name: str,
        ts_codes: list[str],
        frame: pd.DataFrame,
        start_date: str,
        end_date: str,
    ) -> None:
        states = self._raw.setdefault(api_name, {})
        for code in ts_codes:
            incoming = frame[frame["ts_code"] == code].copy()
            current = states.get(code)
            if current is None:
                states[code] = self._state(incoming, start_date, end_date)
                continue
            merged = pd.concat([current.frame, incoming], ignore_index=True)
            merged = merged.drop_duplicates(["ts_code", "trade_date"], keep="last")
            states[code] = self._state(
                merged,
                min(current.start_date, start_date),
                max(current.end_date, end_date),
            )

    def _state(self, frame: pd.DataFrame, start_date: str, end_date: str) -> _ApiFrame:
        result = self._normalize_frame(frame)
        result = result.sort_values(["trade_date", "ts_code"], kind="mergesort").reset_index(drop=True)
        return _ApiFrame(frame=result, start_date=start_date, end_date=end_date)

    def _normalize_frame(self, frame: pd.DataFrame | None) -> pd.DataFrame:
        result = pd.DataFrame() if frame is None else frame.copy()
        missing = [column for column in ("ts_code", "trade_date") if column not in result.columns]
        if missing and not result.empty:
            names = ", ".join(missing)
            raise CanonicalPriceStoreError(f"Canonical price data is missing required columns: {names}")
        for column in missing:
            result[column] = pd.Series(dtype="object")
        result["ts_code"] = result["ts_code"].astype(str)
        result["trade_date"] = result["trade_date"].astype(str)
        return result

    def _raw_source(
        self,
        api_name: str,
        ts_codes: list[str],
        requested_start,
        requested_end,
    ) -> pd.DataFrame:
        start = normalize_date(requested_start)
        end = normalize_date(requested_end)
        requested_codes = self._unique_codes(ts_codes)
        self._ensure_raw(api_name, requested_codes, start, end)
        states = self._raw.get(api_name, {})
        parts = []
        template = None
        for code in requested_codes:
            current = states.get(code)
            if current is None:
                continue
            template = current.frame
            sliced = current.frame[
                (current.frame["trade_date"] >= start)
                & (current.frame["trade_date"] <= end)
            ]
            if not sliced.empty:
                parts.append(sliced)
        if not parts:
            if template is not None:
                return template.iloc[0:0].copy()
            return pd.DataFrame(columns=["ts_code", "trade_date"])
        return pd.concat(parts, ignore_index=True).copy()

    def _adjusted_source(
        self,
        api_name: str,
        factor_api_name: str,
        ts_codes: list[str],
        price: pd.DataFrame,
        requested_start,
        end_date,
    ) -> pd.DataFrame:
        normalized_start = normalize_date(requested_start)
        normalized_end = normalize_date(end_date)
        requested_codes = self._unique_codes(ts_codes)
        started = perf_counter()
        try:
            factors = self._raw_source(
                factor_api_name,
                requested_codes,
                normalized_start,
                normalized_end,
            )
            if factors.empty or "adj_factor" not in factors.columns:
                return price

            factor_frame = factors[["ts_code", "trade_date", "adj_factor"]].copy()
            factor_frame["adj_factor"] = pd.to_numeric(factor_frame["adj_factor"], errors="coerce")
            cache_key = (
                api_name,
                factor_api_name,
                normalized_start,
                normalized_end,
                tuple(sorted(requested_codes)),
            )
            latest_by_code = self._adjusted.get(cache_key)
            if latest_by_code is None:
                self._stats.adjusted_misses += 1
                latest = (
                    factor_frame.dropna(subset=["adj_factor"])
                    .sort_values(["ts_code", "trade_date"], kind="mergesort")
                    .groupby("ts_code", sort=False)["adj_factor"]
                    .tail(1)
                )
                latest_by_code = {
                    str(code): float(value)
                    for code, value in factor_frame.loc[latest.index].set_index("ts_code")["adj_factor"].items()
                }
                self._adjusted[cache_key] = latest_by_code
                self._adjusted.move_to_end(cache_key)
                while len(self._adjusted) > self.adjusted_capacity:
                    self._adjusted.popitem(last=False)
            else:
                self._stats.adjusted_hits += 1
                self._adjusted.move_to_end(cache_key)

            result = price.merge(factor_frame, on=["ts_code", "trade_date"], how="left", sort=False)
            scale = result["adj_factor"] / result["ts_code"].map(latest_by_code)
            scale = scale.where(scale.notna() & (scale > 0), 1.0)
            for column in ("open", "high", "low", "close", "pre_close"):
                if column in result.columns:
                    result[column] = pd.to_numeric(result[column], errors="coerce") * scale
            return result.drop(columns=["adj_factor"], errors="ignore")
        finally:
            self._stats.adjustment_seconds += perf_counter() - started

    def _chunked(self, values: list[str]) -> list[list[str]]:
        return [
            values[index : index + self._RAW_CODE_CHUNK_SIZE]
            for index in range(0, len(values), self._RAW_CODE_CHUNK_SIZE)
        ]

    def _unique_codes(self, ts_codes: list[str]) -> list[str]:
        return list(dict.fromkeys(str(code) for code in ts_codes))

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
