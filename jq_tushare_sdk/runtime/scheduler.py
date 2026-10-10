from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable


@dataclass(frozen=True)
class ScheduledEntry:
    func: Callable[..., Any]
    kind: str
    time: str
    weekday: int | None = None
    monthday: int | None = None


class Scheduler:
    def __init__(self):
        self.entries: list[ScheduledEntry] = []
        self._callback_wrapper: Callable[[Callable[..., Any]], Callable[..., Any]] = lambda func: func
        self._week_key = None
        self._week_trade_days = []
        self._calendar_days = None
        self._calendar_weeks = {}
        self._calendar_months = {}

    def bind_calendar(self, trade_days):
        """Bind complete exchange weeks/months, independent of the replay window."""
        days = sorted({day.date() if isinstance(day, datetime) else
                       day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
                       for day in trade_days})
        weeks, months = {}, {}
        for day in days:
            weeks.setdefault(day.isocalendar()[:2], []).append(day)
            months.setdefault((day.year, day.month), []).append(day)
        self._calendar_days = frozenset(days)
        self._calendar_weeks = weeks
        self._calendar_months = months

    @staticmethod
    def _ordinal_index(ordinal, length):
        ordinal = int(ordinal)
        if ordinal == 0:
            raise ValueError("Trading-day ordinal cannot be zero")
        index = ordinal - 1 if ordinal > 0 else length + ordinal
        return min(max(index, 0), length - 1)

    def _calendar_period(self, current_dt, kind):
        if self._calendar_days is None:
            raise NotImplementedError("Weekly/monthly scheduling requires a complete exchange calendar")
        day = current_dt.date()
        if day not in self._calendar_days:
            raise NotImplementedError("Scheduled date is absent from the bound exchange calendar")
        if kind == "weekly":
            return self._calendar_weeks[day.isocalendar()[:2]]
        return self._calendar_months[(day.year, day.month)]

    def set_callback_wrapper(
        self, wrapper: Callable[[Callable[..., Any]], Callable[..., Any]] | None
    ):
        self._callback_wrapper = wrapper or (lambda func: func)

    def _raise_unsupported_kwargs(self, kwargs):
        if not kwargs:
            return
        unsupported = ", ".join(sorted(kwargs))
        raise TypeError(f"Unsupported scheduler kwargs: {unsupported}")

    def run_daily(self, func, time="open", **kwargs):
        self._raise_unsupported_kwargs(kwargs)
        self.entries.append(
            ScheduledEntry(func=self._callback_wrapper(func), kind="daily", time=time)
        )

    def run_weekly(self, func, weekday=1, time="open", **kwargs):
        self._raise_unsupported_kwargs(kwargs)
        if int(weekday) == 0:
            raise ValueError("Trading-day ordinal cannot be zero")
        self.entries.append(
            ScheduledEntry(
                func=self._callback_wrapper(func),
                kind="weekly",
                time=time,
                weekday=int(weekday),
            )
        )

    def run_monthly(self, func, monthday=1, time="open", **kwargs):
        self._raise_unsupported_kwargs(kwargs)
        if int(monthday) == 0:
            raise ValueError("Trading-day ordinal cannot be zero")
        self.entries.append(
            ScheduledEntry(
                func=self._callback_wrapper(func),
                kind="monthly",
                time=time,
                monthday=int(monthday),
            )
        )

    def unschedule_all(self):
        self.entries.clear()
        self._week_key = None
        self._week_trade_days.clear()

    def callbacks_for(self, current_dt: datetime, time_label: str, previous_dt=None):
        callbacks = []
        has_weekly_entries = any(entry.kind == "weekly" for entry in self.entries)
        week_trade_day = (
            self._week_trade_day(current_dt)
            if has_weekly_entries and current_dt is not None
            else None
        )
        for entry in self.entries:
            if entry.time != time_label:
                continue
            if entry.kind == "weekly":
                if week_trade_day is None:
                    continue
                if not self._matches_weekly_entry(
                    entry,
                    current_dt,
                    previous_dt,
                    week_trade_day,
                ):
                    continue
            if entry.kind == "monthly":
                days = self._calendar_period(current_dt, "monthly")
                if current_dt.date() != days[self._ordinal_index(entry.monthday, len(days))]:
                    continue
            callbacks.append(entry.func)
        return callbacks

    def _week_trade_day(self, current_dt: datetime) -> int:
        current_date = current_dt.date()
        return self._calendar_period(current_dt, "weekly").index(current_date) + 1

    def _matches_weekly_entry(
        self,
        entry: ScheduledEntry,
        current_dt: datetime,
        previous_dt,
        week_trade_day: int,
    ) -> bool:
        days = self._calendar_period(current_dt, "weekly")
        return current_dt.date() == days[self._ordinal_index(entry.weekday, len(days))]
