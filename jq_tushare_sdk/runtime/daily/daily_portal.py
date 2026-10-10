"""Daily simulation data: dated source bars, with no synthetic execution prices."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

import pandas as pd

from jq_tushare_sdk.data.code_map import is_tushare_fund_code, normalize_date, to_tushare_code
from jq_tushare_sdk.data.portal import DataPortal


class DailyDataUnavailable(RuntimeError):
    """The day must remain uncommitted until its local source data is ready."""


class DailySimulationPortal(DataPortal):
    def __init__(self, backend, trade_day):
        super().__init__(backend, optimize_data=False)
        # Execution and valuation are always raw. API callers may explicitly
        # request adjusted history; an environment default cannot alter cash.
        self._default_fq = None
        self.trade_day = date.fromisoformat(str(trade_day))
        self._exact_bars = {}

    def _visible_date(self, value=None):
        if value is None:
            return self.trade_day
        if normalize_date(value) > normalize_date(self.trade_day):
            raise ValueError("Daily simulation cannot read future economic data")
        return value

    def get_index_stocks(self, index_symbol, date=None):
        return super().get_index_stocks(index_symbol, date=self._visible_date(date))

    def get_all_securities(self, types=None, date=None):
        return super().get_all_securities(types=types, date=self._visible_date(date))

    def get_security_info(self, security, as_of=None):
        return super().get_security_info(security, as_of=self._visible_date(as_of))

    def get_current_data(self, securities=None, date=None):
        return super().get_current_data(securities=securities, date=self._visible_date(date))

    def get_industry(self, securities, date=None):
        return super().get_industry(securities, date=self._visible_date(date))

    def resolve_fundamentals_date(self, requested_date):
        return super().resolve_fundamentals_date(self._visible_date(requested_date))

    def get_fundamentals(self, q, date=None, statDate=None, *, as_of=None, requested_date=None):
        if date is not None:
            self._visible_date(date)
        if requested_date is not None:
            self._visible_date(requested_date)
        if statDate is not None:
            period_end, _ = self._fundamental_report_period(statDate)
            self._visible_date(period_end)
        visible_at = datetime.combine(self.trade_day, time(15, 30))
        if as_of is None or pd.Timestamp(as_of).tz_localize(None).to_pydatetime() > visible_at:
            as_of = visible_at
        return super().get_fundamentals(q, date=date, statDate=statDate, as_of=as_of, requested_date=requested_date)

    def _load_income_raw(self, date=None):
        # Legacy statDate queries pass date=None and ordinarily permit a
        # retrospective statement. Simulation must still hide future releases.
        cutoff = self.trade_day - timedelta(days=1)
        effective = cutoff if date is None or normalize_date(date) > normalize_date(cutoff) else date
        frame = super()._load_income_raw(effective)
        return self._filter_income_asof(frame, effective)

    def get_fundamentals_continuously(self, q, end_date=None, count=1):
        return super().get_fundamentals_continuously(q, end_date=self._visible_date(end_date), count=count)

    def get_price(self, security, start_date=None, end_date=None, count=None,
                  fields=None, frequency="daily", panel=False, fq=None, **kwargs):
        if frequency not in ("daily", "1d"):
            raise NotImplementedError("Daily simulation does not support minute/tick prices")
        end_date = end_date if end_date is not None else self.trade_day
        if normalize_date(end_date) > normalize_date(self.trade_day):
            end_date = self.trade_day
        if start_date is not None and normalize_date(start_date) > normalize_date(end_date):
            raise ValueError("Daily simulation cannot read future prices")
        return super().get_price(security, start_date=start_date, end_date=end_date,
                                 count=count, fields=fields, frequency=frequency,
                                 panel=panel, fq=fq, **kwargs)

    def calendar(self, start, end):
        start, end = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        frame = self.backend.fetch("trade_cal", exchange="SSE", start_date=normalize_date(start), end_date=normalize_date(end))
        if frame.empty or not {"cal_date", "is_open"}.issubset(frame.columns):
            raise DailyDataUnavailable("Official exchange calendar is missing")
        days = {}
        for row in frame.to_dict("records"):
            day = normalize_date(row["cal_date"])
            try:
                flag = float(row["is_open"])
            except (TypeError, ValueError):
                raise DailyDataUnavailable("Official calendar contains invalid open flags") from None
            if flag not in (0., 1.) or (day in days and days[day] != flag):
                raise DailyDataUnavailable("Official calendar contains conflicting open flags")
            days[day] = flag
        expected = {normalize_date(day) for day in pd.date_range(start, end)}
        if expected - set(days):
            raise DailyDataUnavailable("Official exchange calendar is incomplete")
        return [datetime.strptime(day, "%Y%m%d").date() for day in sorted(expected) if days[day] == 1]

    def validate_day(self, previous_day=None, start_date=None):
        lower = pd.Timestamp(previous_day).date() if previous_day else pd.Timestamp(start_date or self.trade_day).date()
        if self.trade_day < lower:
            raise ValueError("Simulation date precedes account/checkpoint date")
        days = self.calendar(lower, self.trade_day)
        eligible = [day for day in days if day > lower] if previous_day else days
        if not eligible or eligible[0] != self.trade_day:
            raise ValueError("Simulation must process the next uncommitted exchange trading day")
        # Completion of a date requires actual dated market data even when the
        # account happens to hold only cash.
        frame = self.backend.fetch("daily", trade_date=normalize_date(self.trade_day))
        if frame.empty or "trade_date" not in frame or not (frame.trade_date.map(normalize_date) == normalize_date(self.trade_day)).any():
            raise DailyDataUnavailable("Completed daily market data is missing for this trading day")

    def exact_bar(self, security):
        """Require one real bar for the actual execution/valuation day."""
        code = to_tushare_code(security)
        if code in self._exact_bars:
            return dict(self._exact_bars[code])
        api = self._single_price_api_name(security)
        frame = self.backend.fetch(api, ts_code=code, trade_date=normalize_date(self.trade_day))
        if frame.empty or not {"ts_code", "trade_date", "open", "close", "vol"}.issubset(frame.columns):
            raise DailyDataUnavailable(f"Dated daily price is unavailable: {security} {self.trade_day}")
        rows = frame[(frame.ts_code.astype(str) == code) & (frame.trade_date.map(normalize_date) == normalize_date(self.trade_day))]
        if len(rows) != 1:
            raise DailyDataUnavailable(f"Dated daily price is missing or duplicated: {security} {self.trade_day}")
        row = rows.iloc[0].to_dict()
        for field in ("open", "close", "vol"):
            try:
                value = float(row[field])
            except (TypeError, ValueError):
                raise DailyDataUnavailable(f"Invalid daily {field}: {security}") from None
            if not math.isfinite(value) or value < 0 or (field == "close" and value <= 0):
                raise DailyDataUnavailable(f"Invalid daily {field}: {security}")
            row[field] = value
        if row["vol"] > 0 and row["open"] <= 0:
            raise DailyDataUnavailable(f"Actual opening price is unavailable: {security}")
        self._exact_bars[code] = row
        return dict(row)

    def validate_holdings(self, context):
        for code in context.portfolio.positions:
            self.exact_bar(code)

    def validate_adjustment_events(self, securities, previous_day, matching_securities):
        """Fail closed on unexplained factor changes in this account's scope."""
        changed = []
        for security in sorted(set(securities)):
            code = to_tushare_code(security)
            api = "fund_adj" if is_tushare_fund_code(code) else "adj_factor"
            values = []
            for day in (previous_day, self.trade_day):
                frame = self.backend.fetch(api, ts_code=code, trade_date=normalize_date(day))
                if not {"ts_code", "trade_date", "adj_factor"}.issubset(frame.columns):
                    raise DailyDataUnavailable(f"Adjustment factor is unavailable: {security} {day}")
                rows = frame[(frame.ts_code.astype(str) == code) & (frame.trade_date.map(normalize_date) == normalize_date(day))]
                if len(rows) != 1:
                    raise DailyDataUnavailable(f"Adjustment factor is missing or duplicated: {security} {day}")
                try:
                    value = float(rows.iloc[0]["adj_factor"])
                except (TypeError, ValueError):
                    raise DailyDataUnavailable(f"Invalid adjustment factor: {security} {day}") from None
                if not math.isfinite(value) or value <= 0:
                    raise DailyDataUnavailable(f"Invalid adjustment factor: {security} {day}")
                values.append(value)
            if not math.isclose(values[0], values[1], rel_tol=1e-10, abs_tol=1e-12):
                changed.append(security)
                if security not in matching_securities:
                    raise DailyDataUnavailable(f"Adjustment factor changed without a matching corporate action: {security} {self.trade_day}")
        return changed
