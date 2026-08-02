from __future__ import annotations

from collections import OrderedDict
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
class _PriceView:
    """Merged raw price rows for one (api, code-group) tuple.

    The frame is kept sorted by (ts_code, trade_date) so each code's rows are
    contiguous; ``code_ranges`` maps ts_code -> (start_idx, end_idx) so a per
    code tail slice can be gathered with a single positional iloc instead of
    concatenating one DataFrame per symbol on every query.

    ``state_coverage`` records each code's raw-state (start, end) so the view
    is rebuilt whenever any individual code's coverage changes, even if the
    aggregate min/max stays the same.
    """

    api_name: str
    codes: tuple[str, ...]
    frame: pd.DataFrame
    start_date: str
    end_date: str
    code_ranges: dict[str, tuple[int, int]] | None = None
    state_coverage: dict[str, tuple[str, str]] | None = None


@dataclass
class _CompressedFactorSeries:
    """One security's factor change points."""

    change_dates: np.ndarray
    change_values: np.ndarray


@dataclass
class _FactorView:
    """Compressed factor series plus the latest factor as-of ``latest_end``.

    ``state_coverage`` records each code's raw-state (start, end) so the view
    can be rebuilt whenever any individual code's coverage changes, even if the
    aggregate min/max stays the same.
    """

    factor_api_name: str
    codes: tuple[str, ...]
    series_by_code: dict[str, _CompressedFactorSeries]
    code_ids: dict[str, int]
    lookup_keys: np.ndarray
    lookup_values: np.ndarray
    start_date: str
    end_date: str
    latest_by_code: dict[str, float] | None = None
    latest_end: str | None = None
    state_coverage: dict[str, tuple[str, str]] | None = None


class CanonicalPriceStore:
    _RAW_CODE_CHUNK_SIZE = 400
    _FORWARD_PREFETCH_DAYS = 45
    _VIEW_CAPACITY = 8

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
        self._price_views: OrderedDict[tuple, _PriceView] = OrderedDict()
        self._factor_views: OrderedDict[tuple, _FactorView] = OrderedDict()
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
        view = self._price_view(api_name, requested_codes)
        self._extend_price_view(view, load_start, load_end)
        frame = view.frame[
            (view.frame["trade_date"] >= requested_start)
            & (view.frame["trade_date"] <= requested_end)
        ]
        result = self._select_codes(frame, requested_codes)
        if count is not None:
            result = result.sort_values(["ts_code", "trade_date"], kind="mergesort")
            result = result.groupby("ts_code", sort=False, group_keys=False).tail(int(count))
        return (
            result.sort_values(["trade_date", "ts_code"], kind="mergesort")
            .reset_index(drop=True)
        )

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

        view = self._price_view(api_name, requested_codes)
        self._extend_price_view(view, window_start, load_end)
        result = self._tail_from_view(view, requested_end, count)
        return (
            result.sort_values(["trade_date", "ts_code"], kind="mergesort")
            .reset_index(drop=True)
        )

    def _tail_from_view(
        self,
        view: _PriceView,
        requested_end: str,
        count: int,
    ) -> pd.DataFrame:
        if view.frame.empty:
            return view.frame
        ranges = view.code_ranges or {}
        dates = view.frame["trade_date"].to_numpy()
        positions = []
        for code in view.codes:
            code_range = ranges.get(code)
            if code_range is None:
                continue
            start_idx, end_idx = code_range
            boundary = start_idx + int(
                np.searchsorted(dates[start_idx:end_idx], requested_end, side="right")
            )
            if boundary <= start_idx:
                continue
            low = max(start_idx, boundary - int(count))
            positions.append(np.arange(low, boundary))
        if not positions:
            return view.frame.iloc[0:0].copy()
        return view.frame.iloc[np.concatenate(positions)].copy()

    def _price_view(self, api_name: str, ts_codes: list[str]) -> _PriceView:
        key = (str(api_name), tuple(sorted(set(ts_codes))))
        view = self._price_views.get(key)
        if view is None:
            view = self._build_price_view(api_name, key[1])
            self._price_views[key] = view
            self._price_views.move_to_end(key)
            while len(self._price_views) > self._VIEW_CAPACITY:
                self._price_views.popitem(last=False)
        else:
            self._price_views.move_to_end(key)
        return view

    def _build_price_view(self, api_name: str, codes: tuple[str, ...]) -> _PriceView:
        states = self._raw.get(api_name, {})
        parts = []
        start_date = None
        end_date = None
        for code in codes:
            state = states.get(code)
            if state is None or state.frame.empty:
                continue
            parts.append(state.frame)
            if start_date is None or state.start_date < start_date:
                start_date = state.start_date
            if end_date is None or state.end_date > end_date:
                end_date = state.end_date
        frame = (
            pd.concat(parts, ignore_index=True)
            .sort_values(["ts_code", "trade_date"], kind="mergesort")
            .reset_index(drop=True)
            if parts
            else pd.DataFrame(columns=["ts_code", "trade_date"])
        )
        view = _PriceView(
            api_name=api_name,
            codes=codes,
            frame=frame,
            start_date=start_date or "",
            end_date=end_date or "",
            state_coverage=self._raw_coverage(api_name, codes),
        )
        return self._refresh_view_ranges(view)

    def _extend_price_view(
        self,
        view: _PriceView,
        needed_start: str,
        needed_end: str,
    ) -> None:
        if (
            view.start_date
            and needed_start >= view.start_date
            and needed_end <= view.end_date
            and self._raw_coverage(view.api_name, view.codes) == view.state_coverage
        ):
            return
        self._rebuild_price_view(view, needed_start, needed_end)

    def _rebuild_price_view(
        self,
        view: _PriceView,
        needed_start: str,
        needed_end: str,
    ) -> None:
        states = self._raw.get(view.api_name, {})
        parts = []
        for code in view.codes:
            state = states.get(code)
            if state is None or state.frame.empty:
                continue
            sliced = state.frame[
                (state.frame["trade_date"] >= needed_start)
                & (state.frame["trade_date"] <= needed_end)
            ]
            if not sliced.empty:
                parts.append(sliced)
        frame = (
            pd.concat(parts, ignore_index=True)
            .sort_values(["ts_code", "trade_date"], kind="mergesort")
            .reset_index(drop=True)
            if parts
            else pd.DataFrame(columns=["ts_code", "trade_date"])
        )
        view.frame = frame
        view.start_date = needed_start
        view.end_date = needed_end
        view.state_coverage = self._raw_coverage(view.api_name, view.codes)
        self._refresh_view_ranges(view)

    def _raw_coverage(
        self,
        api_name: str,
        codes: tuple[str, ...] | list[str],
    ) -> dict[str, tuple[str, str]]:
        states = self._raw.get(api_name, {})
        return {
            code: (states[code].start_date, states[code].end_date)
            for code in codes
            if states.get(code) is not None and not states[code].frame.empty
        }

    def _refresh_view_ranges(self, view: _PriceView) -> _PriceView:
        ranges: dict[str, tuple[int, int]] = {}
        if not view.frame.empty and "ts_code" in view.frame.columns:
            codes = view.frame["ts_code"].to_numpy()
            starts: dict[str, int] = {}
            previous = None
            for index, code in enumerate(codes):
                if code != previous:
                    starts[code] = index
                    previous = code
            ordered = list(starts)
            for index, code in enumerate(ordered):
                end = starts[ordered[index + 1]] if index + 1 < len(ordered) else len(codes)
                ranges[code] = (starts[code], end)
        view.code_ranges = ranges
        return view

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
            factor_view = self._factor_view(factor_api_name, factor_codes, factor_end)
            if not factor_view.series_by_code:
                return result
            latest_by_code = factor_view.latest_by_code or {}
            scale_values = self._factor_scales(result, factor_view, latest_by_code)

            adjusted = result.copy()
            for column in ("open", "high", "low", "close", "pre_close"):
                if column in adjusted.columns:
                    adjusted[column] = (
                        pd.to_numeric(adjusted[column], errors="coerce").to_numpy() * scale_values
                    )
            return adjusted.drop(columns=["adj_factor"], errors="ignore")
        finally:
            self._stats.adjustment_seconds += perf_counter() - started

    def _factor_view(
        self,
        factor_api_name: str,
        ts_codes: list[str],
        served_end: str,
    ) -> _FactorView:
        key = (str(factor_api_name), tuple(sorted(set(ts_codes))))
        view = self._factor_views.get(key)
        states = self._raw.get(factor_api_name, {})
        state_coverage = {
            code: (states[code].start_date, states[code].end_date)
            for code in key[1]
            if states.get(code) is not None and not states[code].frame.empty
        }
        stored = view.state_coverage if view is not None else None
        rebuild = view is None or state_coverage != stored
        if rebuild:
            view = self._build_factor_view(factor_api_name, key[1], state_coverage)
            self._factor_views[key] = view
            self._factor_views.move_to_end(key)
            while len(self._factor_views) > self.adjusted_capacity:
                self._factor_views.popitem(last=False)
        else:
            self._factor_views.move_to_end(key)

        if view.latest_end == served_end:
            self._stats.adjusted_hits += 1
        else:
            self._stats.adjusted_misses += 1
            view.latest_by_code = self._latest_factors_asof(view, served_end)
            view.latest_end = served_end
        return view

    def _build_factor_view(
        self,
        factor_api_name: str,
        codes: tuple[str, ...],
        state_coverage: dict[str, tuple[str, str]] | None,
    ) -> _FactorView:
        states = self._raw.get(factor_api_name, {})
        series_by_code: dict[str, _CompressedFactorSeries] = {}
        code_ids = {code: index for index, code in enumerate(codes)}
        lookup_keys = []
        lookup_values = []
        start_date = None
        end_date = None
        for code in codes:
            state = states.get(code)
            if state is None or state.frame.empty:
                continue
            if "adj_factor" not in state.frame.columns:
                continue
            factor_frame = state.frame[["trade_date", "adj_factor"]].copy()
            factor_frame["adj_factor"] = pd.to_numeric(
                factor_frame["adj_factor"], errors="coerce"
            )
            factor_frame = factor_frame.dropna(subset=["adj_factor"])
            if factor_frame.empty:
                continue
            dates = factor_frame["trade_date"].to_numpy(copy=True)
            values = factor_frame["adj_factor"].to_numpy(dtype=float, copy=True)
            changed = np.empty(len(values), dtype=bool)
            changed[0] = True
            changed[1:] = values[1:] != values[:-1]
            series_by_code[code] = _CompressedFactorSeries(
                change_dates=dates[changed],
                change_values=values[changed],
            )
            lookup_keys.append(
                np.full(len(dates), code_ids[code], dtype=np.int64) * 100_000_000
                + dates.astype(np.int64)
            )
            lookup_values.append(values)
            self._stats.factor_rows_scanned += len(values)
            self._stats.factor_change_nodes += int(changed.sum())
            if start_date is None or state.start_date < start_date:
                start_date = state.start_date
            if end_date is None or state.end_date > end_date:
                end_date = state.end_date
        return _FactorView(
            factor_api_name=factor_api_name,
            codes=codes,
            series_by_code=series_by_code,
            code_ids=code_ids,
            lookup_keys=(
                np.concatenate(lookup_keys)
                if lookup_keys
                else np.array([], dtype=np.int64)
            ),
            lookup_values=(
                np.concatenate(lookup_values)
                if lookup_values
                else np.array([], dtype=float)
            ),
            start_date=start_date or "",
            end_date=end_date or "",
            latest_by_code={},
            latest_end=None,
            state_coverage=dict(state_coverage) if state_coverage is not None else {},
        )

    def _latest_factors_asof(
        self,
        view: _FactorView,
        end: str,
    ) -> dict[str, float]:
        latest: dict[str, float] = {}
        for code, series in view.series_by_code.items():
            position = int(np.searchsorted(series.change_dates, end, side="right")) - 1
            if position >= 0:
                latest[code] = float(series.change_values[position])
        return latest

    def _factor_scales(
        self,
        result: pd.DataFrame,
        view: _FactorView,
        latest_by_code: dict[str, float],
    ) -> np.ndarray:
        scales = np.ones(len(result), dtype=float)
        if not len(view.lookup_keys):
            return scales
        code_ids = result["ts_code"].map(view.code_ids).fillna(-1).to_numpy(dtype=np.int64)
        dates = result["trade_date"].astype(np.int64).to_numpy()
        keys = code_ids * 100_000_000 + dates
        positions = np.searchsorted(view.lookup_keys, keys, side="left")
        valid = (code_ids >= 0) & (positions < len(view.lookup_keys))
        valid_indices = np.flatnonzero(valid)
        valid[valid_indices] &= view.lookup_keys[positions[valid_indices]] == keys[valid_indices]
        latest = result["ts_code"].map(latest_by_code).to_numpy(dtype=float)
        valid &= np.isfinite(latest) & (latest > 0)
        if valid.any():
            computed = view.lookup_values[positions[valid]] / latest[valid]
            scales[valid] = np.where(
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
        dates = state.frame["trade_date"]
        return int(dates.searchsorted(normalize_date(requested_end), side="right"))

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
