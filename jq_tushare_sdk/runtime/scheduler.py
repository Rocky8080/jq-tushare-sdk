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

    def callbacks_for(self, current_dt: datetime, time_label: str):
        callbacks = []
        for entry in self.entries:
            if entry.time != time_label:
                continue
            if entry.kind == "weekly" and current_dt.weekday() + 1 != entry.weekday:
                continue
            if entry.kind == "monthly" and current_dt.day != entry.monthday:
                continue
            callbacks.append(entry.func)
        return callbacks
