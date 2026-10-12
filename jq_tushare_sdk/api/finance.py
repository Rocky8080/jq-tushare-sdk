"""Forecast APIs with explicit source and observation boundaries.

TUSHARE_FORECAST retains the Tushare provider schema. STK_FIN_FORCAST reads
complete native JoinQuant exports; Tushare text is never substituted.
"""
from __future__ import annotations

import math

import pandas as pd

from jq_tushare_sdk.api.query import FieldRef, Query
from jq_tushare_sdk.data.code_map import normalize_date, to_joinquant_code, to_tushare_code
from jq_tushare_sdk.data.financial_provenance import FinancialVintageUnavailable
from jq_tushare_sdk.data.native_forecast import NATIVE_FIELDS, run_native_forecast_query


class FinanceCapabilityUnavailable(NotImplementedError):
    pass


class ForecastTable:
    def __init__(self, name, fields):
        self._table_name = name
        self._fields = frozenset(fields)

    def __getattr__(self, name):
        if name not in self._fields:
            raise FinanceCapabilityUnavailable(f'{self._table_name}.{name}: unsupported/unverified field')
        return FieldRef(self._table_name, name)


_FIELDS = ('code', 'pub_date', 'end_date', 'type', 'p_change_min', 'p_change_max',
           'net_profit_min', 'net_profit_max', 'last_parent_net', 'first_ann_date', 'summary', 'change_reason')
TUSHARE_FORECAST = ForecastTable('TUSHARE_FORECAST', _FIELDS)
STK_FIN_FORCAST = ForecastTable('STK_FIN_FORCAST', NATIVE_FIELDS)
_NUMERIC_FIELDS = ('p_change_min', 'p_change_max', 'net_profit_min', 'net_profit_max', 'last_parent_net')
_FIELD_UNITS = {'p_change_min': 'percent', 'p_change_max': 'percent',
                'net_profit_min': 'ten_thousand_cny', 'net_profit_max': 'ten_thousand_cny',
                'last_parent_net': 'provider_native_unit_not_documented'}


def capabilities():
    return {
        'STK_FIN_FORCAST': {'supported': True, 'supported_fields': list(NATIVE_FIELDS),
                            'source': 'joinquant.finance.STK_FIN_FORCAST',
                            'coverage': 'requires_complete_native_snapshot_observed_before_decision',
                            'tushare_text_substitution': False,
                            'native_reproduction_eligible': False,
                            'historical_vintage_coverage': 'not_certified_by_observation_export'},
        'TUSHARE_FORECAST': {'supported': True, 'source': 'tushare.forecast', 'schema': list(_FIELDS),
                             'profit_unit': 'ten_thousand_cny', 'growth_unit': 'percent',
                             'field_units': dict(_FIELD_UNITS),
                             'native_compatibility_verified': False, 'native_reproduction_eligible': False,
                             'historical_vintage_coverage': 'requires_revision_preflight'},
    }


def _day(value):
    """Parse a calendar date without treating YYYYMMDD numbers as nanoseconds."""
    return pd.to_datetime(normalize_date(value), format='%Y%m%d', errors='raise').normalize()


def _continuous_coverage(intervals, start, end):
    cursor = start
    for first, last in sorted(intervals):
        if last < cursor:
            continue
        if first > cursor:
            return False
        cursor = last + pd.Timedelta(days=1)
        if cursor > end:
            return True
    return False


def forecast_coverage(backend, start, end, codes=None):
    """Certify every publication calendar day, including no-announcement days.

    Coverage proves the provider was exhaustively queried; it does not prove
    that today's snapshot was available in the historical publication window.
    A whole-market preflight must pass ``codes=None``. Code-scoped queries can
    use whole-market and per-code certificates, but cannot certify the market.
    """
    first, last = _day(start), _day(end)
    if first > last:
        raise ValueError('forecast publication start is after end')
    requested = None if codes is None else sorted({to_tushare_code(code) for code in codes})
    coverage = backend.fetch('forecast_coverage')
    intervals = {}
    records = []
    required = {'scope', 'start_date', 'end_date', 'source', 'observed_at', 'row_count'}
    if not coverage.empty and required.issubset(coverage.columns):
        for record in coverage.to_dict('records'):
            if record['source'] != 'tushare.forecast':
                continue
            try:
                begin, finish = _day(record['start_date']), _day(record['end_date'])
                observation = pd.Timestamp(record['observed_at'])
                row_count = float(record['row_count'])
            except (TypeError, ValueError):
                continue
            if (pd.isna(observation) or not math.isfinite(row_count) or row_count < 0
                    or not row_count.is_integer() or begin > finish or finish < first or begin > last):
                continue
            scope = 'all' if record['scope'] == 'all' else to_tushare_code(record['scope'])
            if requested is not None and scope != 'all' and scope not in requested:
                continue
            intervals.setdefault(scope, []).append((begin, finish))
            records.append({'scope': scope, 'start_date': begin.strftime('%Y%m%d'),
                            'end_date': finish.strftime('%Y%m%d'), 'source': record['source'],
                            'observed_at': str(record['observed_at']), 'row_count': int(row_count)})
    market = intervals.get('all', [])
    all_complete = _continuous_coverage(market, first, last)
    complete = all_complete if requested is None else all(
        _continuous_coverage(market + intervals.get(code, []), first, last) for code in requested)
    return {'complete': bool(complete), 'publication_start': first.strftime('%Y%m%d'),
            'publication_end': last.strftime('%Y%m%d'),
            'scope': 'all' if requested is None else [to_joinquant_code(code) for code in requested],
            'all_market_complete': bool(all_complete), 'source': 'tushare.forecast',
            'coverage_basis': 'exhaustive_publication_calendar_queries', 'records': records,
            'historical_vintage_verified': False}


def _publication_cutoff(state):
    context = getattr(state, 'context', None)
    current = getattr(context, 'current_dt', None)
    if current is None:
        raise FinancialVintageUnavailable('forecast_asof_missing: historical runtime context required')
    today = _day(current)
    previous = getattr(context, 'previous_date', None)
    if previous is None:
        dates = state.data_portal.get_trade_days(end_date=today - pd.Timedelta(days=1), count=1)
        if len(dates) == 0:
            raise FinancialVintageUnavailable('forecast_calendar_missing: previous trading date is required')
        previous = dates[-1]
    cutoff = _day(previous)
    if cutoff >= today:
        raise FinancialVintageUnavailable('forecast_calendar_invalid: previous trading date is not before current date')
    return cutoff.strftime('%Y%m%d')


def run_query(q: Query):
    from jq_tushare_sdk.api.jqdata import runtime_state
    if not isinstance(q, Query):
        raise TypeError('finance.run_query expects query(...)')
    tables = {field._table_name if isinstance(field, ForecastTable) else field.table for field in q.fields}
    tables.update(expr.field.table for expr in q.filters)
    tables.update(item.field.table for item in q.ordering)
    if tables == {'STK_FIN_FORCAST'}:
        state = runtime_state()
        return run_native_forecast_query(q, state, _publication_cutoff(state))
    if tables != {'TUSHARE_FORECAST'}:
        raise FinanceCapabilityUnavailable(f'Unsupported finance tables: {sorted(str(item) for item in tables)}')
    selected = list(_FIELDS) if any(isinstance(field, ForecastTable) for field in q.fields) else [field.name for field in q.fields]
    requested_fields = set(selected) | {expr.field.name for expr in q.filters} | {item.field.name for item in q.ordering}
    supported = _FIELDS
    if requested_fields - set(supported):
        raise FinanceCapabilityUnavailable('Unsupported forecast fields: ' + ', '.join(sorted(requested_fields - set(supported))))
    starts = [(_day(expr.value) + pd.Timedelta(days=int(expr.operator == '>'))).strftime('%Y%m%d')
              for expr in q.filters if expr.field.name == 'pub_date' and expr.operator in {'>=', '>', '=='}]
    ends = [(_day(expr.value) - pd.Timedelta(days=int(expr.operator == '<'))).strftime('%Y%m%d')
            for expr in q.filters if expr.field.name == 'pub_date' and expr.operator in {'<=', '<', '=='}]
    if not starts or not ends:
        raise FinanceCapabilityUnavailable('forecast_coverage_unknown: explicit publication lower and upper bounds required')
    start, end = max(starts), min(ends)
    if start > end:
        raise ValueError('forecast publication start is after end')
    state = runtime_state()
    backend = state.data_portal.backend
    cutoff = _publication_cutoff(state)
    if end > cutoff:
        raise FinancialVintageUnavailable('forecast_unavailable_at: publication upper bound exceeds previous trading date ' + cutoff)
    codes = None
    for expr in q.filters:
        if expr.field.name == 'code' and expr.operator in {'in', '=='}:
            values = {to_joinquant_code(code) for code in (expr.value if expr.operator == 'in' else [expr.value])}
            codes = values if codes is None else codes & values
    coverage = forecast_coverage(backend, start, end, codes=codes)
    if not coverage['complete']:
        raise FinanceCapabilityUnavailable('forecast_coverage_missing: absence of rows is not evidence of an empty forecast universe')
    if getattr(backend, 'strict_pit', False):
        readiness = backend.financial_readiness(end, api_name='forecast')
        if not readiness.get('ready'):
            raise FinancialVintageUnavailable(readiness.get('reason', 'financial_vintage_missing'))
        frame = backend.financial_revisions(api_name='forecast', as_of=end)
        if not frame.empty:
            frame = frame.sort_values(['_available_at', '_observed_at', '_payload_sha256']).drop_duplicates(
                ['ts_code', 'ann_date', 'end_date', 'type'], keep='last')
    else:
        frame = backend.fetch('forecast', start_date=start, end_date=end)
    frame = frame.rename(columns={'ts_code': 'code', 'ann_date': 'pub_date'})
    for name in _FIELDS:
        if name not in frame:
            frame[name] = pd.Series(dtype='object')
    if not frame.empty:
        if frame['code'].isna().any() or frame['code'].astype(str).str.strip().eq('').any():
            raise FinanceCapabilityUnavailable('forecast_security_code_invalid')
        frame['code'] = frame['code'].map(to_joinquant_code)
        for field in ('pub_date', 'end_date', 'first_ann_date'):
            dates = frame[field].map(lambda value: normalize_date(value) if pd.notna(value) else None)
            frame[field] = pd.to_datetime(dates, format='%Y%m%d', errors='coerce').dt.strftime('%Y-%m-%d')
        if frame['pub_date'].isna().any():
            raise FinanceCapabilityUnavailable('forecast_publication_date_invalid')
        if frame['end_date'].isna().any():
            raise FinanceCapabilityUnavailable('forecast_report_date_invalid')
        for field in _NUMERIC_FIELDS:
            try:
                frame[field] = pd.to_numeric(frame[field], errors='raise')
            except (TypeError, ValueError) as exc:
                raise FinanceCapabilityUnavailable('forecast_numeric_field_invalid: ' + field) from exc
            if frame[field].isin([float('inf'), float('-inf')]).any():
                raise FinanceCapabilityUnavailable('forecast_numeric_field_invalid: ' + field)
    for expr in q.filters:
        value = expr.value
        column = frame[expr.field.name]
        if expr.field.name == 'code':
            value = [to_joinquant_code(item) for item in value] if expr.operator == 'in' else to_joinquant_code(value)
        if expr.field.name in {'pub_date', 'end_date', 'first_ann_date'}:
            column = pd.to_datetime(column, errors='coerce')
            value = [_day(item) for item in value] if expr.operator == 'in' else _day(value)
        if expr.operator == 'in': mask = column.isin(value)
        elif expr.operator == '==': mask = column == value
        elif expr.operator == '!=': mask = column != value
        elif expr.operator == '>=': mask = column >= value
        elif expr.operator == '<=': mask = column <= value
        elif expr.operator == '>': mask = column > value
        elif expr.operator == '<': mask = column < value
        else: raise FinanceCapabilityUnavailable('Unsupported forecast filter: ' + expr.operator)
        frame = frame[mask]
    for item in reversed(q.ordering):
        frame = frame.sort_values(item.field.name, ascending=item.direction != 'desc')
    result = frame[selected].reset_index(drop=True)
    result.attrs.update(source='tushare.forecast', native_compatibility_verified=False,
                        requested_table='TUSHARE_FORECAST',
                        complete_native_table_supported=False,
                        native_reproduction_eligible=False,
                        financial_mode=getattr(backend, 'financial_mode', 'cached_snapshot'),
                        pit_verified=bool(getattr(backend, 'strict_pit', False)),
                        field_units=dict(_FIELD_UNITS), publication_cutoff=cutoff,
                        coverage_complete=True, coverage=coverage)
    return result
