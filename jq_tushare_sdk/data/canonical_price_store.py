from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from time import perf_counter
from typing import Callable

import numpy as np
import pandas as pd

from jq_tushare_sdk.data.code_map import normalize_date


class CanonicalPriceStoreError(RuntimeError):
    pass


@dataclass
class _ApiFrame:
    frame: pd.DataFrame
    dates: np.ndarray
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
    factor_rows_scanned: int = 0
    factor_change_nodes: int = 0


@dataclass
class _FactorCodeView:
    factor_api_name: str
    code: str
    dates: np.ndarray
    values: np.ndarray
    coverage: tuple[str, str]
    change_nodes: int
    latest_end: str | None = None
    latest_value: float | None = None


class CanonicalPriceStore:
    _RAW_CODE_CHUNK_SIZE = 400
    _FORWARD_PREFETCH_DAYS = 45

    def __init__(
        self,
        fetch: Callable[..., pd.DataFrame],
        start_date,
        end_date,
        adjusted_capacity: int = 8,
    ):
        self.fetch = fetch
        self.start_date = normalize_date(start_date)
        self.end_date = normalize_date(end_date)
        self.adjusted_capacity = max(1, int(adjusted_capacity))
        self._raw: dict[str, dict[str, _ApiFrame]] = {}
        self._factor_code_views: dict[tuple[str, str], _FactorCodeView] = {}
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
            requested_end = normalize_date(end_date)
            if count is not None and start_date is None:
                result = self._count_source(
                    api_name,
                    requested_codes,
                    requested_end,
                    int(count),
                )
            else:
                requested_start = (
                    normalize_date(start_date)
                    if start_date is not None
                    else self.start_date
                )
                result = self._range_source(
                    api_name,
                    requested_codes,
                    requested_start,
                    requested_end,
                    int(count) if count is not None else None,
                )
            if fq == "pre" and factor_api_name is not None and not result.empty:
                result = self._adjusted_source(api_name, factor_api_name, result)
            return result.reset_index(drop=True)
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
        *,
        use_configured_start: bool = True,
    ) -> None:
        normalized_start = normalize_date(requested_start)
        load_start = min(self.start_date, normalized_start) if use_configured_start else normalized_start
        normalized_end = normalize_date(requested_end)
        load_end = max(self.end_date, normalized_end) if use_configured_start else normalized_end
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
                # Forward extensions typically advance one trading day per query
                # (the prefetch boundary follows the requested end date). Fetch a
                # full prefetch-size block ahead instead so the same code is not
                # re-concatenated/refreshed every single day.
                extended_end = max(
                    load_end,
                    self._prefetch_end(current.end_date, self._FORWARD_PREFETCH_DAYS),
                )
                loads.setdefault((current.end_date, extended_end), []).append(code)
                has_extension = True

        for (chunk_start, chunk_end), codes in loads.items():
            for code_chunk in self._chunked(codes):
                started = perf_counter()
                frame = self.fetch(
                    api_name,
                    ts_code=",".join(code_chunk),
                    start_date=chunk_start,
                    end_date=chunk_end,
                    fields=self._canonical_fields(api_name),
                    unordered=True,
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

        if loads and has_extension:
            self._stats.extensions += 1

    def _store_raw_chunk(
        self,
        api_name: str,
        ts_codes: list[str],
        frame: pd.DataFrame,
        start_date: str,
        end_date: str,
    ) -> None:
        states = self._raw.setdefault(api_name, {})
        incoming_by_code = {
            str(code): group.copy()
            for code, group in frame.groupby("ts_code", sort=False)
        }
        for code in ts_codes:
            incoming = incoming_by_code.get(code)
            if incoming is None:
                incoming = frame.iloc[0:0].copy()
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
        return _ApiFrame(
            frame=result,
            dates=result["trade_date"].to_numpy(copy=True),
            start_date=start_date,
            end_date=end_date,
        )

    def _canonical_fields(self, api_name: str) -> str:
        if api_name in {"adj_factor", "fund_adj"}:
            return "ts_code,trade_date,adj_factor"
        return "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"

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

    def _range_source(
        self,
        api_name: str,
        ts_codes: list[str],
        requested_start: str,
        requested_end: str,
        count: int | None,
    ) -> pd.DataFrame:
        if count is not None and int(count) <= 0:
            return pd.DataFrame(columns=["ts_code", "trade_date"])
        load_start = min(self.start_date, requested_start)
        load_end = max(self.end_date, requested_end)
        requested_codes = self._unique_codes(ts_codes)
        self._ensure_raw(api_name, requested_codes, load_start, load_end)
        states = self._raw.get(api_name, {})
        parts = []
        for code in requested_codes:
            state = states.get(code)
            if state is None or state.frame.empty:
                continue
            left = int(np.searchsorted(state.dates, requested_start, side="left"))
            right = int(np.searchsorted(state.dates, requested_end, side="right"))
            if count is not None:
                left = max(left, right - int(count))
            if right > left:
                parts.append(state.frame.iloc[left:right])
        return self._merge_slices(parts)

    def _count_source(
        self,
        api_name: str,
        ts_codes: list[str],
        requested_end: str,
        count: int,
    ) -> pd.DataFrame:
        if count <= 0:
            return pd.DataFrame(columns=["ts_code", "trade_date"])
        requested_codes = self._unique_codes(ts_codes)
        window_start = self._count_window_start(requested_end, count)
        load_end = self._prefetch_end(requested_end, self._FORWARD_PREFETCH_DAYS)
        self._ensure_raw(
            api_name,
            requested_codes,
            window_start,
            load_end,
            use_configured_start=False,
        )

        states = self._raw.get(api_name, {})
        incomplete = [
            code
            for code in requested_codes
            if self._count_available_rows(states.get(code), requested_end) < count
            and states.get(code) is not None
            and states[code].start_date > self.start_date
        ]
        if incomplete:
            self._ensure_raw(
                api_name,
                incomplete,
                self.start_date,
                load_end,
                use_configured_start=False,
            )

        states = self._raw.get(api_name, {})
        parts = []
        for code in requested_codes:
            state = states.get(code)
            if state is None or state.frame.empty:
                continue
            right = int(np.searchsorted(state.dates, requested_end, side="right"))
            left = max(0, right - int(count))
            if right > left:
                parts.append(state.frame.iloc[left:right])
        return self._merge_slices(parts)

    def _merge_slices(self, parts: list[pd.DataFrame]) -> pd.DataFrame:
        if not parts:
            return pd.DataFrame(columns=["ts_code", "trade_date"])
        if len(parts) == 1:
            return parts[0].reset_index(drop=True).copy()
        return (
            pd.concat(parts, ignore_index=True)
            .sort_values(["trade_date", "ts_code"], kind="mergesort")
            .reset_index(drop=True)
        )

    def _adjusted_source(
        self,
        api_name: str,
        factor_api_name: str,
        result: pd.DataFrame,
    ) -> pd.DataFrame:
        started = perf_counter()
        try:
            factor_start = normalize_date(result["trade_date"].min())
            factor_end = normalize_date(result["trade_date"].max())
            factor_codes = self._unique_codes(result["ts_code"].tolist())
            self._ensure_raw(
                factor_api_name,
                factor_codes,
                factor_start,
                self._prefetch_end(factor_end, self._FORWARD_PREFETCH_DAYS),
                use_configured_start=False,
            )
            factor_views, cache_hit = self._factor_code_views_for(
                factor_api_name,
                factor_codes,
                factor_end,
            )
            if not factor_views:
                return result
            if cache_hit:
                self._stats.adjusted_hits += 1
            else:
                self._stats.adjusted_misses += 1
            scale_values = self._factor_scales(result, factor_views)

            adjusted = result.copy()
            for column in ("open", "high", "low", "close", "pre_close"):
                if column in adjusted.columns:
                    adjusted[column] = (
                        pd.to_numeric(adjusted[column], errors="coerce").to_numpy() * scale_values
                    )
            return adjusted.drop(columns=["adj_factor"], errors="ignore")
        finally:
            self._stats.adjustment_seconds += perf_counter() - started

    def _factor_code_views_for(
        self,
        factor_api_name: str,
        ts_codes: list[str],
        served_end: str,
    ) -> tuple[dict[str, _FactorCodeView], bool]:
        states = self._raw.get(factor_api_name, {})
        views: dict[str, _FactorCodeView] = {}
        cache_hit = True
        for code in self._unique_codes(ts_codes):
            state = states.get(code)
            if state is None or state.frame.empty:
                continue
            if "adj_factor" not in state.frame.columns:
                continue
            key = (str(factor_api_name), str(code))
            coverage = (state.start_date, state.end_date)
            view = self._factor_code_views.get(key)
            if view is None or view.coverage != coverage:
                view = self._build_factor_code_view(
                    factor_api_name,
                    code,
                    state,
                )
                self._factor_code_views[key] = view
                cache_hit = False
            if not len(view.dates):
                continue
            if view.latest_end != served_end:
                position = int(np.searchsorted(view.dates, int(served_end), side="right")) - 1
                view.latest_value = float(view.values[position]) if position >= 0 else None
                view.latest_end = served_end
                cache_hit = False
            views[code] = view
        return views, cache_hit

    def _build_factor_code_view(
        self,
        factor_api_name: str,
        code: str,
        state: _ApiFrame,
    ) -> _FactorCodeView:
        values = pd.to_numeric(state.frame["adj_factor"], errors="coerce").to_numpy(
            dtype=float,
            copy=True,
        )
        valid = np.isfinite(values)
        dates = state.dates[valid].astype(np.int64, copy=True)
        values = values[valid]
        change_nodes = 0
        if len(values):
            changed = np.empty(len(values), dtype=bool)
            changed[0] = True
            changed[1:] = values[1:] != values[:-1]
            change_nodes = int(changed.sum())
        self._stats.factor_rows_scanned += len(values)
        self._stats.factor_change_nodes += change_nodes
        return _FactorCodeView(
            factor_api_name=factor_api_name,
            code=code,
            dates=dates,
            values=values,
            coverage=(state.start_date, state.end_date),
            change_nodes=change_nodes,
            latest_end=None,
            latest_value=None,
        )

    def _factor_scales(
        self,
        result: pd.DataFrame,
        views: dict[str, _FactorCodeView],
    ) -> np.ndarray:
        scales = np.ones(len(result), dtype=float)
        dates = result["trade_date"].astype(np.int64).to_numpy()
        groups = result.groupby("ts_code", sort=False).indices
        for code, indices in groups.items():
            view = views.get(str(code))
            if view is None or not len(view.dates):
                continue
            latest = view.latest_value
            if latest is None or not np.isfinite(latest) or latest <= 0:
                continue
            row_dates = dates[indices]
            positions = np.searchsorted(view.dates, row_dates, side="left")
            valid = positions < len(view.dates)
            valid_indices = np.flatnonzero(valid)
            valid[valid_indices] &= view.dates[positions[valid_indices]] == row_dates[valid_indices]
            if not valid.any():
                continue
            computed = view.values[positions[valid]] / latest
            target = np.asarray(indices)[valid]
            scales[target] = np.where(
                np.isfinite(computed) & (computed > 0), computed, 1.0
            )
        return scales

    def _count_window_start(self, requested_end: str, count: int) -> str:
        lookback_days = max(int(count) * 3 + 7, 14)
        end = datetime.strptime(normalize_date(requested_end), "%Y%m%d")
        inferred = (end - timedelta(days=lookback_days)).strftime("%Y%m%d")
        return max(self.start_date, inferred)

    def _prefetch_end(self, requested_end: str, days: int) -> str:
        end = datetime.strptime(normalize_date(requested_end), "%Y%m%d")
        inferred = (end + timedelta(days=max(0, int(days)))).strftime("%Y%m%d")
        return min(self.end_date, inferred)

    def _count_available_rows(self, state: _ApiFrame | None, requested_end: str) -> int:
        if state is None or state.frame.empty:
            return 0
        return int(
            np.searchsorted(
                state.dates,
                normalize_date(requested_end),
                side="right",
            )
        )

    def _chunked(self, values: list[str]) -> list[list[str]]:
        return [
            values[index : index + self._RAW_CODE_CHUNK_SIZE]
            for index in range(0, len(values), self._RAW_CODE_CHUNK_SIZE)
        ]

    def _unique_codes(self, ts_codes: list[str]) -> list[str]:
        return list(dict.fromkeys(str(code) for code in ts_codes))
