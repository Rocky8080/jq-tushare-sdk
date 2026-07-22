import inspect
from datetime import datetime, time, timedelta

import pandas as pd

from jq_tushare_sdk.api.query import FilterExpr, Query
from jq_tushare_sdk.data.code_map import normalize_date

_STATE = None


def set_runtime_state(state):
    global _STATE
    _STATE = state


def runtime_state():
    if _STATE is None:
        raise RuntimeError("jq_tushare_sdk runtime state is not initialized")
    return _STATE


def get_price(
    security,
    start_date=None,
    end_date=None,
    count=None,
    frequency="daily",
    fields=None,
    skip_paused=False,
    fq="pre",
    panel=False,
    fill_paused=True,
):
    _validate_price_compat_kwargs(
        skip_paused=skip_paused,
        fq=fq,
        fill_paused=fill_paused,
    )
    partial_bar_date = _partial_daily_bar_date(end_date, frequency)
    effective_end_date = partial_bar_date or _context_data_date(end_date)
    portal_fields = _price_fields_with_open(fields) if partial_bar_date is not None else fields
    frame = runtime_state().data_portal.get_price(
        security,
        start_date=start_date,
        end_date=effective_end_date,
        count=count,
        frequency=frequency,
        fields=portal_fields,
        panel=panel,
        fq=fq,
        skip_paused=skip_paused,
        fill_paused=fill_paused,
    )
    if partial_bar_date is None:
        return frame
    return _mask_partial_daily_bar(frame, partial_bar_date, fields)


def attribute_history(
    security,
    count,
    unit="1d",
    fields=("open", "close", "high", "low", "volume", "money"),
    skip_paused=True,
    df=True,
    fq="pre",
):
    end_date = _context_data_date()
    frame = get_price(
        security,
        count=count,
        end_date=end_date,
        frequency=unit,
        fields=list(fields),
        skip_paused=skip_paused,
        fq=fq,
        panel=False,
    )
    if df:
        return frame
    return {field: frame[field].tolist() for field in fields if field in frame.columns}


def history(
    count,
    unit="1d",
    field="close",
    security_list=None,
    df=True,
    skip_paused=True,
    fq="pre",
):
    end_date = _context_data_date()
    securities = security_list or []
    if isinstance(securities, str):
        securities = [securities]
    frames = [
        get_price(
            code,
            count=count,
            end_date=end_date,
            frequency=unit,
            fields=[field],
            skip_paused=skip_paused,
            fq=fq,
            panel=False,
        ).assign(code=code)
        for code in securities
    ]
    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return result if df else result.to_dict("list")


def get_trade_days(start_date=None, end_date=None, count=None):
    portal = runtime_state().data_portal
    signature = inspect.signature(portal.get_trade_days)
    if "count" in signature.parameters:
        return portal.get_trade_days(start_date=start_date, end_date=end_date, count=count)

    if count is not None:
        if start_date is not None:
            raise NotImplementedError("Fallback get_trade_days(count=...) only supports end_date/count.")
        days = portal.get_trade_days("1900-01-01", end_date)
        if int(count) <= 0:
            return []
        return days[-int(count):]

    return portal.get_trade_days(start_date, end_date)


def get_index_stocks(index_symbol, date=None):
    effective_date = _context_data_date(date)
    return runtime_state().data_portal.get_index_stocks(index_symbol, date=effective_date)


def get_all_securities(types=None, date=None):
    return runtime_state().data_portal.get_all_securities(
        types=types,
        date=_context_data_date(date) if date is not None else date,
    )


def get_security_info(security):
    portal = runtime_state().data_portal
    if hasattr(portal, "get_security_info"):
        return portal.get_security_info(security)
    raise NotImplementedError("JoinQuant API is not implemented locally: get_security_info")


def get_current_data(securities=None):
    return runtime_state().data_portal.get_current_data(
        securities,
        date=_context_current_data_date(),
    )


def get_industry(securities, date=None):
    portal = runtime_state().data_portal
    if hasattr(portal, "get_industry"):
        return portal.get_industry(securities, date=_context_data_date(date))
    raise NotImplementedError("JoinQuant API is not implemented locally: get_industry")


def get_fundamentals(q: Query, date=None, statDate=None):
    effective_date = _context_data_date(date)
    df = runtime_state().data_portal.get_fundamentals(q, date=effective_date, statDate=statDate)
    _require_query_columns(df, [field.name for field in q.fields], "selected")
    _require_query_columns(df, [expr.field.name for expr in q.filters], "filter")
    _require_query_columns(df, [ordering.field.name for ordering in q.ordering], "ordering")
    for expr in q.filters:
        df = _apply_filter(df, expr)
    for ordering in reversed(q.ordering):
        if ordering.field.name in df.columns:
            df = df.sort_values(
                ordering.field.name,
                ascending=(ordering.direction == "asc"),
            )
    selected = [field.name for field in q.fields if field.name in df.columns]
    if selected:
        return df[selected].reset_index(drop=True)
    return df.reset_index(drop=True)


def get_fundamentals_continuously(q: Query, end_date=None, count=1):
    return runtime_state().data_portal.get_fundamentals_continuously(
        q,
        end_date=_context_data_date(end_date),
        count=count,
    )


def _normalize_securities(securities):
    if isinstance(securities, str):
        return [securities]
    return list(securities or [])


def _apply_filter(df, expr: FilterExpr):
    name = expr.field.name
    if name not in df.columns:
        return df
    if expr.operator == ">=":
        return df[df[name] >= expr.value]
    if expr.operator == "<=":
        return df[df[name] <= expr.value]
    if expr.operator == ">":
        return df[df[name] > expr.value]
    if expr.operator == "<":
        return df[df[name] < expr.value]
    if expr.operator == "==":
        return df[df[name] == expr.value]
    if expr.operator == "!=":
        return df[df[name] != expr.value]
    if expr.operator == "in":
        return df[df[name].isin(expr.value)]
    raise NotImplementedError(f"Unsupported query filter operator: {expr.operator}")


def _validate_price_compat_kwargs(skip_paused, fq, fill_paused):
    if not isinstance(skip_paused, bool):
        raise NotImplementedError(
            f"Unsupported JoinQuant get_price skip_paused value: {skip_paused!r}"
        )
    if fq != "pre":
        raise NotImplementedError(f"Unsupported JoinQuant get_price fq value: {fq!r}")
    if fill_paused is not True:
        raise NotImplementedError(
            f"Unsupported JoinQuant get_price fill_paused value: {fill_paused!r}"
        )


def _require_query_columns(df, column_names, label):
    missing = sorted({name for name in column_names if name not in df.columns})
    if missing:
        names = ", ".join(missing)
        raise NotImplementedError(
            f"Fundamentals result is missing required {label} fields: {names}"
        )


def _context_current_date():
    state = runtime_state()
    context = getattr(state, "context", None)
    if context is None:
        return None
    return getattr(context, "current_dt", None)


def _context_current_data_date():
    state = runtime_state()
    context = getattr(state, "context", None)
    if context is None:
        return None
    current_dt = getattr(context, "current_dt", None)
    if current_dt is None:
        return None
    time_method = getattr(current_dt, "time", None)
    if callable(time_method) and time_method() < time(9, 30):
        return _context_visible_data_date(state)
    return current_dt


def _partial_daily_bar_date(requested_date, frequency):
    if requested_date is None or frequency not in {"daily", "1d"}:
        return None
    current_dt = _context_current_date()
    if current_dt is None or not hasattr(current_dt, "time"):
        return None
    current_time = current_dt.time()
    if current_time < time(9, 30) or current_time >= time(9, 31):
        return None
    if _date_key(requested_date) < _date_key(current_dt):
        return None
    return current_dt


def _price_fields_with_open(fields):
    if fields is None:
        return None
    requested = [fields] if isinstance(fields, str) else list(fields)
    return requested if "open" in requested else [*requested, "open"]


def _mask_partial_daily_bar(frame, partial_bar_date, requested_fields):
    if frame.empty or "time" not in frame.columns or "open" not in frame.columns:
        return frame
    result = frame.copy()
    current_key = _date_key(partial_bar_date)
    partial_rows = result["time"].map(_date_key) == current_key
    if partial_rows.any():
        opening_prices = result.loc[partial_rows, "open"]
        for field in ("close", "high", "low"):
            if field in result.columns:
                result.loc[partial_rows, field] = opening_prices
        for field in ("volume", "money"):
            if field in result.columns:
                result.loc[partial_rows, field] = 0.0
    if requested_fields is not None:
        requested = [requested_fields] if isinstance(requested_fields, str) else list(requested_fields)
        keep = [name for name in ("time", "code", *requested) if name in result.columns]
        result = result[keep]
    return result


def _context_data_date(requested_date=None):
    state = runtime_state()
    visible_date = _context_visible_data_date(state)
    if visible_date is None:
        return requested_date
    if requested_date is None:
        return visible_date
    return visible_date if _date_key(requested_date) > _date_key(visible_date) else requested_date


def _context_visible_data_date(state):
    context = getattr(state, "context", None)
    if context is None:
        return None
    current_dt = getattr(context, "current_dt", None)
    if current_dt is None:
        return None
    if not _uses_previous_close_boundary(current_dt):
        return current_dt
    previous_date = getattr(context, "previous_date", None)
    if previous_date is not None:
        return previous_date
    portal_previous = _previous_trade_day_from_portal(getattr(state, "data_portal", None), current_dt)
    if portal_previous is not None:
        return portal_previous
    return _previous_calendar_day(current_dt)


def _uses_previous_close_boundary(current_dt) -> bool:
    time_method = getattr(current_dt, "time", None)
    if not callable(time_method):
        return False
    return time_method() <= time(9, 30)


def _previous_trade_day_from_portal(portal, current_dt):
    if portal is None or not hasattr(portal, "get_trade_days"):
        return None
    current_key = _date_key(current_dt)
    try:
        signature = inspect.signature(portal.get_trade_days)
        if "count" in signature.parameters:
            days = portal.get_trade_days(end_date=current_dt, count=2)
        else:
            start_date = (
                datetime.strptime(current_key, "%Y%m%d") - timedelta(days=14)
            ).strftime("%Y-%m-%d")
            days = portal.get_trade_days(start_date, _joinquant_date_key(current_key))
    except (NotImplementedError, TypeError, ValueError):
        return None
    previous_days = [day for day in days if _date_key(day) < current_key]
    return previous_days[-1] if previous_days else None


def _previous_calendar_day(current_dt):
    current_key = _date_key(current_dt)
    return datetime.strptime(current_key, "%Y%m%d") - timedelta(days=1)


def _date_key(value) -> str:
    return normalize_date(value)


def _joinquant_date_key(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
