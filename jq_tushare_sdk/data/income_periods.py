from __future__ import annotations

from jq_tushare_sdk.data.code_map import normalize_date


_QUARTER_ENDS = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}


def required_income_periods(
    start_date,
    end_date,
) -> list[str]:
    """Return fully reportable income periods needed by a backtest window.

    The latest mandatory period follows A-share reporting deadlines. Its prior
    quarter is included because single-quarter growth calculations need both the
    selected report and its comparison period.
    """

    start = normalize_date(start_date)
    end = normalize_date(end_date)
    if end < start:
        raise ValueError("income period range end must not precede start")

    earliest = _latest_mandatory_period_index(start) - 1
    latest = _latest_mandatory_period_index(end)
    return [
        _period_from_index(index)
        for index in range(latest, earliest - 1, -1)
    ]


def income_period_end(period: str) -> str:
    text = str(period).strip().lower()
    if len(text) != 6 or not text[:4].isdigit() or text[4] != "q":
        raise ValueError(f"Unsupported income period: {period}")
    quarter = int(text[5])
    if quarter not in _QUARTER_ENDS:
        raise ValueError(f"Unsupported income period: {period}")
    return f"{text[:4]}{_QUARTER_ENDS[quarter]}"


def _latest_mandatory_period_index(value) -> int:
    normalized = normalize_date(value)
    year = int(normalized[:4])
    month_day = int(normalized[4:])
    # Backtests execute during the trading day. A report whose legal deadline
    # is today may still be published after the simulated callback, so only
    # advance the mandatory period on the following calendar day.
    if month_day > 1031:
        quarter = 3
    elif month_day > 831:
        quarter = 2
    elif month_day > 430:
        quarter = 1
    else:
        year -= 1
        quarter = 3
    return year * 4 + quarter - 1


def _period_from_index(index: int) -> str:
    year, offset = divmod(int(index), 4)
    return f"{year}q{offset + 1}"
