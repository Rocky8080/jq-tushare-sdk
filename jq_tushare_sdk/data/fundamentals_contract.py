"""Request and visibility errors for fundamentals, separate from price clamping."""

from datetime import date, datetime, time, timedelta

from jq_tushare_sdk.data.code_map import normalize_date


class FundamentalsUnavailableError(RuntimeError):
    def __init__(self, reason: str, **details):
        self.reason = reason
        self.details = {"reason": reason, **details}
        super().__init__(f"Fundamentals unavailable: {reason}; {details}")

    def to_dict(self):
        return dict(self.details)


def validated_date(value) -> str:
    text = normalize_date(value)
    try:
        datetime.strptime(text, "%Y%m%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid fundamentals date: {value!r}") from exc
    return text


def previous_calendar_date(value) -> str:
    return (datetime.strptime(validated_date(value), "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


def validate_daily_snapshot_visibility(effective_date, as_of):
    """Absent a verified intraday publication time, a daily snapshot is EOD.

    A date request preserves its meaning; an intraday replay cannot authorize
    reading that day's completed daily_basic merely because it is cached.
    """
    if as_of is None or effective_date is None:
        return
    observed_at = datetime.fromisoformat(str(as_of)) if not isinstance(as_of, datetime) else as_of
    if isinstance(as_of, date) and not isinstance(as_of, datetime):
        observed_at = datetime.combine(as_of, time.min)
    available_at = datetime.combine(datetime.strptime(validated_date(effective_date), "%Y%m%d").date(), time.max)
    if observed_at.tzinfo is not None:
        available_at = available_at.replace(tzinfo=observed_at.tzinfo)
    if available_at > observed_at:
        raise FundamentalsUnavailableError(
            "daily_snapshot_not_yet_available",
            effective_date=validated_date(effective_date),
            available_at=available_at.isoformat(),
            as_of=observed_at.isoformat(),
            visibility_policy="daily_snapshot_end_of_day",
        )
