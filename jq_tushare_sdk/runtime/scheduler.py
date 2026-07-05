from dataclasses import dataclass
from datetime import datetime, timedelta
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

    def callbacks_for(self, current_dt: datetime, time_label: str, previous_dt=None):
        callbacks = []
        for entry in self.entries:
            if entry.time != time_label:
                continue
            if entry.kind == "weekly" and not self._matches_weekly_entry(
                entry,
                current_dt,
                previous_dt,
            ):
                continue
            if entry.kind == "monthly" and current_dt.day != entry.monthday:
                continue
            callbacks.append(entry.func)
        return callbacks

    def _matches_weekly_entry(self, entry: ScheduledEntry, current_dt: datetime, previous_dt) -> bool:
        if current_dt.weekday() + 1 == entry.weekday:
            return True
        if previous_dt is None:
            return current_dt.isoweekday() > entry.weekday

        previous_date = previous_dt.date() if hasattr(previous_dt, "date") else previous_dt
        current_date = current_dt.date()
        if previous_date >= current_date:
            return False

        day = previous_date + timedelta(days=1)
        while day <= current_date:
            if day.isoweekday() == entry.weekday:
                return True
            day += timedelta(days=1)
        return False
