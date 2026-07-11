from dataclasses import dataclass
from datetime import datetime
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
            if entry.kind == "monthly" and current_dt.day != entry.monthday:
                continue
            callbacks.append(entry.func)
        return callbacks

    def _week_trade_day(self, current_dt: datetime) -> int:
        current_date = current_dt.date()
        week_key = current_date.isocalendar()[:2]
        if week_key != self._week_key:
            self._week_key = week_key
            self._week_trade_days = [current_date]
        elif current_date not in self._week_trade_days:
            self._week_trade_days.append(current_date)
        return self._week_trade_days.index(current_date) + 1

    def _matches_weekly_entry(
        self,
        entry: ScheduledEntry,
        current_dt: datetime,
        previous_dt,
        week_trade_day: int,
    ) -> bool:
        return week_trade_day == entry.weekday
