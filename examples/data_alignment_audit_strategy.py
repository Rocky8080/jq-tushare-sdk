"""Paste this entire file into JoinQuant or load it with jq-tushare-sdk.

Read-only data probes and hypothetical decisions; no orders, Redis or network
notifications. Machine-readable log fragments retain missing/unsupported data.
"""
from jqdata import *
from jqdata import finance
import builtins as _python
_IMPORTED_REDUCTIONS = {name: {'module': getattr(globals().get(name), '__module__', 'builtins'),
                              'name': getattr(globals().get(name), '__name__', name)}
                        for name in ['any', 'all', 'sum', 'min', 'max']}
from builtins import (any, all, sum, min, max, round, abs, bool, int, float, str,
                      type, list, tuple, set, dict, len, sorted, isinstance, range,
                      enumerate, getattr, hasattr, callable, globals)
import base64
import hashlib
import json
import math
import time as _wall_time
import traceback as _traceback
from datetime import date, datetime, timedelta
import pandas as pd

VERSION = '0.4.0'
UPDATE_DATE = '2026-10-12'
PROTOCOL = 'jq-data-alignment/1'
LOG_MARKER = 'JQALIGN1 '

# Keep these identical on both platforms. Change the basket to include actual
# production candidates/holdings when investigating a particular trading day.
SAMPLE_CODES = ['000001.XSHE', '600000.XSHG', '600519.XSHG',
                '300750.XSHE', '688001.XSHG', '688981.XSHG']
INDEX_CODES = ['399006.XSHE', '000688.XSHG', '000300.XSHG']
# 002274 has a real 2026-07-09 forecast in the inspected Tushare source.
# A matching nonempty native observation was received in v0.3.0 log 9.
FORECAST_SAMPLE_CODES = SAMPLE_CODES + ['002274.XSHE']
INCOME_DIAGNOSTIC_PERIODS = ['2025q3', '2025q4', '2025']
NATIVE_INCOME_PERIOD_ENDS = ['2025-09-30', '2025-12-31']
INCOME_DETAIL_FIELDS = ['code', 'total_operating_revenue', 'operating_revenue',
                        'net_profit', 'np_parent_company_owners', 'pubDate', 'statDate']
HISTORY_DAYS = 21
FORECAST_LOOKBACK_DAYS = 90
EXPORT_FULL_UNIVERSE = False
ENABLE_MINUTE_PROBES = False
# 'prices' collects daily/raw/factor/calendar/decision evidence for a larger
# continuous replay. It does not certify omitted financial/minute interfaces.
AUDIT_PROFILE = 'full'
PHASE_TIMES = {'before_open': '09:00', 'after_close': '15:30'}
PRICE_FIELDS = ['open', 'high', 'low', 'close', 'volume', 'money']
VALUATION_FIELDS = ['code', 'pe_ratio', 'pb_ratio', 'market_cap']
PAYLOAD_CHARS = 1100


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _day(value):
    if value is None:
        return None
    text = str(value)
    if text in ['NaT', 'nan', 'None', '']:
        return None
    if len(text) == 8 and text.isdigit():
        return text[:4] + '-' + text[4:6] + '-' + text[6:]
    return text[:10]


def _value(value):
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return _day(value)
    if hasattr(value, 'item'):
        value = value.item()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    return str(value)


def _config():
    return {'protocol': PROTOCOL, 'version': VERSION,
            'audit_profile': AUDIT_PROFILE,
            'sample_codes': sorted(set(SAMPLE_CODES)),
            'index_codes': sorted(set(INDEX_CODES)),
            'history_days': HISTORY_DAYS,
            'forecast_lookback_days': FORECAST_LOOKBACK_DAYS,
            'forecast_sample_codes': sorted(set(FORECAST_SAMPLE_CODES)),
            'income_diagnostic_periods': INCOME_DIAGNOSTIC_PERIODS,
            'income_detail_fields': INCOME_DETAIL_FIELDS,
            'native_income_period_ends': NATIVE_INCOME_PERIOD_ENDS,
            'native_forecast_detail': 'native full-row schema observation; diagnostic only',
            'full_universe': EXPORT_FULL_UNIVERSE,
            'minute_probes': ENABLE_MINUTE_PROBES,
            'phase_times': PHASE_TIMES, 'price_fields': PRICE_FIELDS,
            'valuation_fields': VALUATION_FIELDS,
            'use_real_price': True, 'snapshot_only': True,
            'fundamentals_date': 'previous_trade_date',
            'decision_rule': 'sample-only MA20 + 5-session return; no orders'}


def _namespace():
    return {'imported_reductions': _IMPORTED_REDUCTIONS,
            'effective_reductions': {name: {'module': getattr(globals()[name], '__module__', None),
                                          'is_python_builtin': globals()[name] is getattr(_python, name)}
                                     for name in ['any', 'all', 'sum', 'min', 'max']},
            'note': 'Reducers are explicitly pinned after jqdata wildcard import.'}


def _contract():
    getter = globals().get('get_data_contract')
    if callable(getter):
        return getter()
    # Native provenance is an environment label, not a PIT certificate.
    return {'provider': 'joinquant', 'financial_mode': 'native',
            'historical_vintages_verified': None,
            'environment_identification': 'jqdata API without local contract getter'}


def _envelope(kind, sid, **values):
    values.update({'protocol': PROTOCOL, 'kind': kind, 'snapshot_id': sid})
    log.info(LOG_MARKER + _json(values))


def _section(sid, name, payload):
    body = _json(payload)
    digest = _sha(body)
    encoded = base64.b64encode(body.encode('utf-8')).decode('ascii')
    pieces = [encoded[i:i + PAYLOAD_CHARS]
              for i in range(0, len(encoded), PAYLOAD_CHARS)]
    for i, piece in enumerate(pieces):
        _envelope('part', sid, section=name, part=i, parts=len(pieces),
                  sha256=digest, payload=piece)
    return digest


def _empty_payload(params, keys, units=None, comparable=True):
    return {'status': 'ok', 'params': params, 'keys': keys,
            'units': units or {}, 'comparable': comparable, 'rows': []}


def _frame(frame, payload, required_fields, expected_keys=None, time_field=False,
           nullable_fields=None):
    payload['columns'] = sorted(str(c) for c in frame.columns)
    payload['dtypes'] = {str(c): str(frame[c].dtype) for c in frame.columns}
    attrs = {str(k): str(v) for k, v in getattr(frame, 'attrs', {}).items()}
    payload['source_attrs'] = {k: v[:12000] for k, v in attrs.items()}
    payload['source_attrs_truncation'] = {k: {'original_characters': len(v),
        'retained_characters': 12000, 'original_sha256': _sha(v)}
        for k, v in attrs.items() if len(v) > 12000}
    # Preserve critical provenance outside potentially huge per-market status
    # dictionaries. Native runs without these SDK attrs legitimately emit {}.
    original_attrs = getattr(frame, 'attrs', {})
    source_evidence = {}
    price = original_attrs.get('price_provenance')
    if isinstance(price, dict):
        source_evidence['price'] = {k: price[k] for k in (
            'source_api', 'fq', 'adjustment_source', 'factor_precision_policy',
            'adjustment_historical_vintages_verified', 'native_factor_equivalence_verified',
            'factor_receipts') if k in price}
    capital = original_attrs.get('valuation_provenance')
    if isinstance(capital, dict):
        source_evidence['capital'] = {k: capital[k] for k in (
            'requested_date', 'share_capital_source', 'share_capital_date_basis',
            'share_capital_effective_date_verified', 'published_capital_evidence',
            'capital_evidence_policy', 'disclosed_capital_receipts') if k in capital}
    payload['source_evidence'] = source_evidence
    payload['index_names'] = [str(x) for x in frame.index.names]
    for idx, row in frame.iterrows():
        record = {field: _value(row.get(field)) for field in required_fields}
        if time_field:
            value = row.get('time', idx)
            if time_field == 'timestamp':
                record['timestamp'] = str(value).replace('T', ' ')[:19]
            else:
                record['date'] = _day(value)
        if 'code' in required_fields and not record.get('code'):
            record['code'] = str(idx) if isinstance(idx, str) else None
        payload['rows'].append(record)
    payload['rows'].sort(key=lambda row: _json([row.get(k) for k in payload['keys']]))
    missing = sorted(set(required_fields) - set(frame.columns))
    # A string security index is also a legitimate code column in native data.
    if 'code' in missing and all(r.get('code') for r in payload['rows']):
        missing.remove('code')
    payload['missing_columns'] = missing
    present = {tuple(r.get(k) for k in payload['keys']) for r in payload['rows']}
    absent = sorted(set(expected_keys or []) - present)
    payload['missing_keys'] = [list(x) for x in absent]
    payload['null_fields'] = [dict(identity={k: r.get(k) for k in payload['keys']},
                                   field=f) for r in payload['rows']
                              for f in required_fields if r.get(f) is None]
    nullable = set(nullable_fields or []) - set(payload['keys'])
    payload['nullable_fields'] = sorted(nullable)
    invalid_nulls = [item for item in payload['null_fields'] if item['field'] not in nullable]
    if frame.empty:
        payload['status'] = 'empty'
    elif missing or absent or invalid_nulls:
        payload['status'] = 'partial'
    return payload


def _price(codes, end, fq, frequency='daily', start=None, count=HISTORY_DAYS):
    params = {'securities': list(codes), 'end_date': end, 'frequency': frequency,
              'fields': PRICE_FIELDS, 'skip_paused': False, 'fq': fq, 'panel': False}
    if start is None:
        params['count'] = count
    else:
        params['start_date'] = start
    call = dict(params)
    call['security'] = call.pop('securities')
    frame = get_price(**call)
    minute = frequency in ['1m', 'minute']
    payload = _empty_payload(params, ['code', 'timestamp' if minute else 'date'],
                             dict(open='CNY' if frequency != 'index' else 'index_points',
                                  high='CNY', low='CNY', close='CNY',
                                  volume='shares', money='CNY'))
    # All calls are deliberately list calls, ensuring the native long-frame API.
    expected = None
    if not minute and start is None:
        days = [_day(d) for d in get_trade_days(end_date=end, count=count)]
        expected = [(code, day) for code in codes for day in days]
        payload['expected_sessions'] = count
    result = _frame(frame, payload, ['code'] + PRICE_FIELDS, expected,
                    time_field='timestamp' if minute else True)
    if not minute and start is None and len(days) != count:
        result['status'] = 'partial'
    return result


def _valuation(previous):
    fields = VALUATION_FIELDS
    frame = get_fundamentals(query(*[getattr(valuation, f) for f in fields])
                             .filter(valuation.code.in_(SAMPLE_CODES)), date=previous)
    payload = _empty_payload({'date': previous, 'securities': SAMPLE_CODES,
                              'fields': fields}, ['code'],
                             {'market_cap': '1e8_CNY', 'circulating_market_cap': '1e8_CNY',
                              'pe_ratio': 'ratio_provider_definition', 'pb_ratio': 'ratio'})
    return _frame(frame, payload, fields, [(c,) for c in SAMPLE_CODES])


def _price_factors(end):
    params = {'securities': SAMPLE_CODES, 'end_date': end, 'count': HISTORY_DAYS,
              'frequency': 'daily', 'fields': ['factor'], 'fq': 'pre',
              'skip_paused': False, 'panel': False}
    call = dict(params)
    call['security'] = call.pop('securities')
    frame = get_price(**call)
    days = [_day(d) for d in get_trade_days(end_date=end, count=HISTORY_DAYS)]
    return _frame(frame, _empty_payload(params, ['code', 'date'], {'factor': 'provider_adjustment_factor'}),
                  ['code', 'factor'], [(code, day) for code in SAMPLE_CODES for day in days], time_field=True)


def _circulating_cap(previous):
    fields = ['code', 'circulating_market_cap']
    frame = get_fundamentals(query(valuation.code, valuation.circulating_market_cap)
                             .filter(valuation.code.in_(SAMPLE_CODES)), date=previous)
    return _frame(frame, _empty_payload({'date': previous, 'fields': fields,
                  'securities': SAMPLE_CODES}, ['code'],
                  {'circulating_market_cap': '1e8_CNY'}), fields, [(c,) for c in SAMPLE_CODES])


def _capital_structure(previous):
    fields = ['code', 'capitalization', 'circulating_cap']
    frame = get_fundamentals(query(*[getattr(valuation, f) for f in fields])
                             .filter(valuation.code.in_(SAMPLE_CODES)), date=previous)
    return _frame(frame, _empty_payload({'date': previous, 'fields': fields,
                  'securities': SAMPLE_CODES}, ['code'],
                  {'capitalization': '1e4_shares', 'circulating_cap': '1e4_shares'}),
                  fields, [(c,) for c in SAMPLE_CODES])


def _income_detail(period):
    fields = INCOME_DETAIL_FIELDS
    frame = get_fundamentals(query(*[getattr(income, f) for f in fields])
                             .filter(income.code.in_(SAMPLE_CODES)), statDate=period)
    annual = len(period) == 4
    units = {f: 'CNY_annual_cumulative' if annual else 'CNY_single_quarter'
             for f in fields if f not in ['code', 'pubDate', 'statDate']}
    p = _frame(frame, _empty_payload({'statDate': period, 'fields': fields,
                'securities': SAMPLE_CODES, 'visibility': 'retrospective_statDate_probe_not_PIT'},
                ['code'], units), fields, [(c,) for c in SAMPLE_CODES])
    for row in p['rows']:
        row['pubDate'], row['statDate'] = _day(row['pubDate']), _day(row['statDate'])
    return p


def _forecast_schema():
    """Retain native table definitions; this is not a guessed SDK mapping."""
    try:
        table = getattr(finance.STK_FIN_FORCAST, '__table__', None)
        if table is None:
            return {'status': 'unavailable'}
        return {'status': 'observed', 'columns': [dict(name=str(c.name),
                type=str(c.type), description=str(getattr(c, 'doc', None))) for c in table.columns]}
    except Exception as exc:
        return {'status': 'unavailable', 'error_type': type(exc).__name__, 'error': str(exc)}


def _native_income_detail(previous, period):
    # The native report-period table exposes cumulative values and revision
    # rows. Keep every observed row; do not guess report_type/source_id mapping.
    t = finance.STK_INCOME_STATEMENT
    frame = finance.run_query(query(t).filter(t.code == '000001.XSHE',
                t.end_date == period, t.pub_date <= previous))
    fields = [str(c) for c in frame.columns]
    p = _frame(frame, _empty_payload({'table': 'STK_INCOME_STATEMENT',
                'securities': ['000001.XSHE'], 'end_date': period,
                'publication_end': previous, 'fields': 'native_table_all_columns',
                'visibility': 'native_observed_report_rows; historical_vintage_not_certified'},
                ['code', 'pub_date', 'end_date'], comparable=False), fields,
                nullable_fields=fields)
    p['observed_schema'] = {'basis': 'returned_frame_not_table_metadata',
        'columns': p['columns'], 'dtypes': p['dtypes']}
    p['note'] = 'Native cumulative/revision evidence only; no SDK table mapping or PIT certification assumed.'
    return p


def _quarter_end(day):
    day = datetime.strptime(day, '%Y-%m-%d').date()
    ends = [date(y, m, d) for y in [day.year - 1, day.year]
            for m, d in [(3, 31), (6, 30), (9, 30), (12, 31)] if date(y, m, d) <= day]
    return sorted(ends)[-3:]


def _income(previous, period=None):
    fields = ['code', 'total_operating_revenue', 'np_parent_company_owners']
    q = query(valuation.code, income.total_operating_revenue,
              income.np_parent_company_owners).filter(valuation.code.in_(SAMPLE_CODES))
    params = {'securities': SAMPLE_CODES, 'fields': fields}
    if period:
        params.update(statDate=period, visibility='retrospective_statDate_probe_not_PIT')
        frame = get_fundamentals(q, statDate=period)
    else:
        params.update(date=previous, visibility='requested_date_cutoff; vintage_unverified')
        frame = get_fundamentals(q, date=previous)
    return _frame(frame, _empty_payload(params, ['code'],
                  {f: 'CNY_single_quarter' for f in fields if f != 'code'}),
                  fields, [(c,) for c in SAMPLE_CODES])


def _income_dates(previous):
    fields = ['code', 'pubDate', 'statDate']
    frame = get_fundamentals(query(valuation.code, income.pubDate, income.statDate)
                             .filter(valuation.code.in_(SAMPLE_CODES)), date=previous)
    p = _frame(frame, _empty_payload({'date': previous, 'fields': fields,
                    'securities': SAMPLE_CODES}, ['code']), fields,
                    [(c,) for c in SAMPLE_CODES])
    for r in p['rows']:
        r['pubDate'], r['statDate'] = _day(r['pubDate']), _day(r['statDate'])
    return p


def _industry(previous):
    data = get_industry(SAMPLE_CODES, date=previous)
    p = _empty_payload({'date': previous, 'securities': SAMPLE_CODES,
                         'taxonomies': ['sw_l1', 'sw_l2']}, ['code', 'taxonomy'])
    for code in SAMPLE_CODES:
        for taxonomy in ['sw_l1', 'sw_l2']:
            item = data.get(code, {}).get(taxonomy, {})
            p['rows'].append({'code': code, 'taxonomy': taxonomy,
                             'industry_code': _value(item.get('industry_code')),
                             'industry_name': _value(item.get('industry_name'))})
    if any(r['industry_code'] is None or r['industry_name'] is None for r in p['rows']):
        p['status'] = 'partial'
    return p


def _membership(index, previous):
    members = sorted(set(get_index_stocks(index, date=previous)))
    p = _empty_payload({'index': index, 'date': previous}, ['code'])
    p['rows'] = [{'code': code} for code in members]
    if not members:
        p['status'] = 'empty'
    return p


def _universe(previous):
    frame = get_all_securities(types=['stock'], date=previous)
    codes = sorted(str(c) for c in frame.index)
    p = _empty_payload({'date': previous, 'types': ['stock'],
                         'export_all_codes': EXPORT_FULL_UNIVERSE}, ['code'])
    p['universe_count'] = len(codes)
    p['universe_codes_sha256'] = _sha(_json(codes))
    if EXPORT_FULL_UNIVERSE:
        p['rows'] = [{'code': c} for c in codes]
    else:
        fields = ['code', 'display_name', 'start_date', 'end_date']
        p = _frame(frame.loc[[c for c in SAMPLE_CODES if c in frame.index]], p,
                   fields, [(c,) for c in SAMPLE_CODES])
    return p


def _current(state_only):
    current = get_current_data()
    fields = ['is_st', 'paused'] if state_only else ['last_price', 'day_open', 'high_limit', 'low_limit']
    p = _empty_payload({'securities': SAMPLE_CODES, 'fields': fields,
                         'time_domain': 'callback_context'}, ['code'],
                        comparable=state_only)
    p['rows'] = [dict(code=code, **{f: _value(getattr(current[code], f, None))
                                   for f in fields}) for code in SAMPLE_CODES]
    if any(r[f] is None for r in p['rows'] for f in fields):
        p['status'] = 'partial'
    if not state_only:
        p['note'] = 'Contextual quotes; daily reference is not a verified minute quote.'
    return p


def _forecast(previous):
    start = _day(datetime.strptime(previous, '%Y-%m-%d') - timedelta(days=FORECAST_LOOKBACK_DAYS))
    t = finance.STK_FIN_FORCAST
    frame = finance.run_query(query(t.code, t.pub_date).filter(
        t.code.in_(FORECAST_SAMPLE_CODES), t.pub_date >= start, t.pub_date <= previous))
    p = _frame(frame, _empty_payload({'table': 'STK_FIN_FORCAST', 'securities': FORECAST_SAMPLE_CODES,
                'publication_start': start, 'publication_end': previous,
                'fields': ['code', 'pub_date']}, ['code', 'pub_date']), ['code', 'pub_date'])
    for row in p['rows']:
        row['pub_date'] = _day(row['pub_date'])
    identities = [(row['code'], row['pub_date']) for row in p['rows']]
    p['duplicate_projection_keys'] = [list(key) for key in sorted(set(identities))
                                      if identities.count(key) > 1]
    if p['duplicate_projection_keys']:
        p['status'] = 'partial'
    p['note'] = 'Presence/publication-date probe only; native forecast numeric schemas not assumed.'
    return p


def _forecast_detail(previous):
    start = _day(datetime.strptime(previous, '%Y-%m-%d') - timedelta(days=FORECAST_LOOKBACK_DAYS))
    t = finance.STK_FIN_FORCAST
    frame = finance.run_query(query(t).filter(
        t.code.in_(FORECAST_SAMPLE_CODES), t.pub_date >= start, t.pub_date <= previous))
    fields = [str(c) for c in frame.columns]
    p = _frame(frame, _empty_payload({'table': 'STK_FIN_FORCAST',
                'securities': FORECAST_SAMPLE_CODES, 'publication_start': start,
                'publication_end': previous, 'fields': 'native_table_all_columns'},
                ['id'] if 'id' in fields else ['code', 'pub_date'], comparable=False), fields,
                nullable_fields=fields)
    p['observed_schema'] = {'basis': 'returned_frame_not_table_metadata',
        'columns': p['columns'], 'dtypes': p['dtypes']}
    p['note'] = 'Native full-row/schema evidence only; no SDK field or numeric unit mapping assumed.'
    return p


def _decision(prices, end):
    p = _empty_payload({'data_date': end, 'basket': SAMPLE_CODES,
                         'rule': _config()['decision_rule']}, ['code'],
                        {'return_5': 'ratio', 'ma20': 'CNY', 'close': 'CNY'})
    if prices.get('status') != 'ok':
        p.update(status='blocked', reason='price probe not complete')
        return p
    for code in sorted(SAMPLE_CODES):
        rows = sorted([r for r in prices['rows'] if r['code'] == code], key=lambda r: r['date'])
        closes = [r['close'] for r in rows]
        if len(closes) < HISTORY_DAYS or rows[-1]['date'] != end or any(c is None or c <= 0 for c in closes):
            p['status'] = 'partial'
            p['rows'].append({'code': code, 'eligible': None, 'reason': 'missing price window'})
            continue
        ma = sum(closes[-20:]) / 20.0
        ret = closes[-1] / closes[-6] - 1.0
        p['rows'].append({'code': code, 'close': closes[-1], 'ma20': ma,
                         'return_5': ret, 'eligible': closes[-1] > ma and ret > 0})
    eligible = sorted([r for r in p['rows'] if r.get('eligible')],
                      key=lambda r: (-r['return_5'], r['code']))
    p['hypothetical_ranked_codes'] = [r['code'] for r in eligible]
    p['orders_submitted'] = 0
    return p


def _audit(context, phase):
    config = _config()
    config_hash = _sha(_json(config))
    today, previous = _day(context.current_dt), _day(context.previous_date)
    end = previous if phase == 'before_open' else today
    key = today + ':' + phase + ':' + config_hash
    done = list(getattr(g, 'alignment_completed', []))
    if key in done:
        return
    periods = [str(d.year) + 'q' + str((d.month - 1) // 3 + 1) for d in _quarter_end(previous)]
    names = ['configuration', 'calendar', 'universe', 'industry', 'valuation', 'circulating_cap',
             'income_visible', 'income_report_dates'] + ['income_' + x for x in periods]
    names += ['capital_structure'] + ['income_detail_' + x for x in INCOME_DIAGNOSTIC_PERIODS]
    names += ['native_income_' + x for x in NATIVE_INCOME_PERIOD_ENDS]
    names += ['members_' + x for x in INDEX_CODES]
    names += ['daily_pre', 'daily_raw', 'price_factors', 'index_daily', 'security_state',
              'quote_context', 'native_forecast', 'native_forecast_detail',
              'minute_open', 'minute_close', 'decision_trace']
    if AUDIT_PROFILE == 'prices':
        names = ['configuration', 'calendar', 'daily_pre', 'daily_raw',
                 'price_factors', 'decision_trace']
    contract = _contract()
    meta = {'version': VERSION, 'trade_date': today, 'phase': phase,
            'data_date': end, 'previous_trade_date': previous,
            'context_time': str(context.current_dt), 'timezone': 'Asia/Shanghai',
            'config_sha256': config_hash, 'provider': contract.get('provider', 'unknown'),
            'sections': names, 'read_only': True}
    sid = _sha(_json(meta) + ':' + str(_wall_time.time()))[:32]
    _envelope('begin', sid, metadata=meta)
    digests, captured = {}, {}
    # Retain requested parameters even when an API raises before returning.
    requests = {'valuation': {'date': previous, 'securities': SAMPLE_CODES, 'fields': VALUATION_FIELDS},
                'circulating_cap': {'date': previous, 'securities': SAMPLE_CODES,
                                    'fields': ['code', 'circulating_market_cap']},
                'income_visible': {'date': previous, 'securities': SAMPLE_CODES,
                                   'fields': ['code', 'total_operating_revenue', 'np_parent_company_owners']},
                'income_report_dates': {'date': previous, 'securities': SAMPLE_CODES,
                                        'fields': ['code', 'pubDate', 'statDate']},
                'native_forecast': {'table': 'STK_FIN_FORCAST', 'securities': FORECAST_SAMPLE_CODES,
                    'fields': ['code', 'pub_date'], 'publication_end': previous,
                    'publication_start': _day(datetime.strptime(previous, '%Y-%m-%d') - timedelta(days=FORECAST_LOOKBACK_DAYS))}}
    requests['native_forecast_detail'] = dict(requests['native_forecast'], fields='native_table_all_columns')
    for fq_name, fq in [('daily_pre', 'pre'), ('daily_raw', None), ('index_daily', 'pre')]:
        requests[fq_name] = {'securities': INDEX_CODES if fq_name == 'index_daily' else SAMPLE_CODES,
            'end_date': end, 'count': HISTORY_DAYS, 'frequency': 'daily',
            'fields': PRICE_FIELDS, 'skip_paused': False, 'fq': fq, 'panel': False}
    for period in periods:
        requests['income_' + period] = {'statDate': period, 'securities': SAMPLE_CODES,
            'fields': ['code', 'total_operating_revenue', 'np_parent_company_owners']}
    requests['capital_structure'] = {'date': previous, 'securities': SAMPLE_CODES,
                                     'fields': ['code', 'capitalization', 'circulating_cap']}
    for period in INCOME_DIAGNOSTIC_PERIODS:
        requests['income_detail_' + period] = {'statDate': period, 'securities': SAMPLE_CODES,
                                               'fields': INCOME_DETAIL_FIELDS}
    for period in NATIVE_INCOME_PERIOD_ENDS:
        requests['native_income_' + period] = {'table': 'STK_INCOME_STATEMENT',
            'securities': ['000001.XSHE'], 'end_date': period, 'publication_end': previous,
            'fields': 'native_table_all_columns'}
    requests['price_factors'] = {'securities': SAMPLE_CODES, 'end_date': end, 'count': HISTORY_DAYS,
        'frequency': 'daily', 'fields': ['factor'], 'fq': 'pre', 'skip_paused': False, 'panel': False}
    requests.update(calendar={'end_date': end, 'count': HISTORY_DAYS},
                    universe={'date': previous, 'types': ['stock'], 'export_all_codes': EXPORT_FULL_UNIVERSE},
                    industry={'date': previous, 'securities': SAMPLE_CODES, 'taxonomies': ['sw_l1', 'sw_l2']},
                    decision_trace={'data_date': end, 'basket': SAMPLE_CODES, 'rule': config['decision_rule']})
    for index in INDEX_CODES:
        requests['members_' + index] = {'index': index, 'date': previous}
    for name, fields in [('security_state', ['is_st', 'paused']),
                         ('quote_context', ['last_price', 'day_open', 'high_limit', 'low_limit'])]:
        requests[name] = {'securities': SAMPLE_CODES, 'fields': fields, 'time_domain': 'callback_context'}
    for name, lower, upper in [('minute_open', '09:30:00', '09:35:00'),
                               ('minute_close', '14:55:00', '15:00:00')]:
        requests[name] = {'securities': SAMPLE_CODES, 'start_date': end + ' ' + lower,
                         'end_date': end + ' ' + upper, 'frequency': '1m',
                         'fields': PRICE_FIELDS, 'skip_paused': False, 'fq': None, 'panel': False}

    def capture(name, func):
        if name not in names:
            return
        started = _wall_time.time()
        try:
            payload = func()
        except Exception as exc:
            unsupported = isinstance(exc, (NotImplementedError, AttributeError))
            payload = {'status': 'unsupported' if unsupported else 'error', 'rows': [],
                       'keys': [], 'params': requests.get(name, {'data_date': end, 'securities': SAMPLE_CODES}),
                       'units': {}, 'comparable': True,
                       'error_type': type(exc).__name__, 'error': str(exc),
                       'traceback': _traceback.format_exc()}
        payload['collection'] = {'started_at_unix': started, 'finished_at_unix': _wall_time.time(),
                                  'elapsed_seconds': _wall_time.time() - started,
                                  'callback_context': str(context.current_dt), 'case_id': name}
        captured[name] = payload
        digests[name] = _section(sid, name, payload)

    capture('configuration', lambda: dict(status='ok', config=config, data_contract=contract,
                                         namespace=_namespace(),
                                         native_forecast_schema=_forecast_schema(),
                                         rows=[], keys=[], params={}, units={}, comparable=False))

    def calendar():
        days = [_day(d) for d in get_trade_days(end_date=end, count=HISTORY_DAYS)]
        p = _empty_payload({'end_date': end, 'count': HISTORY_DAYS}, ['date'])
        p['rows'] = [{'date': d} for d in days]
        p['context_previous_date'] = previous
        prev = [_day(d) for d in get_trade_days(end_date=_day(datetime.strptime(today, '%Y-%m-%d') - timedelta(days=1)), count=1)]
        p['calendar_previous_date'] = prev[-1] if prev else None
        if len(days) != HISTORY_DAYS or not prev or prev[-1] != previous:
            p['status'] = 'partial'
        return p

    capture('calendar', calendar)
    capture('universe', lambda: _universe(previous))
    capture('industry', lambda: _industry(previous))
    capture('valuation', lambda: _valuation(previous))
    capture('circulating_cap', lambda: _circulating_cap(previous))
    capture('capital_structure', lambda: _capital_structure(previous))
    capture('income_visible', lambda: _income(previous))
    capture('income_report_dates', lambda: _income_dates(previous))
    for period in periods:
        capture('income_' + period, lambda period=period: _income(previous, period))
    for period in INCOME_DIAGNOSTIC_PERIODS:
        capture('income_detail_' + period, lambda period=period: _income_detail(period))
    for period in NATIVE_INCOME_PERIOD_ENDS:
        capture('native_income_' + period, lambda period=period: _native_income_detail(previous, period))
    for index in INDEX_CODES:
        capture('members_' + index, lambda index=index: _membership(index, previous))
    capture('daily_pre', lambda: _price(SAMPLE_CODES, end, 'pre'))
    capture('daily_raw', lambda: _price(SAMPLE_CODES, end, None))
    capture('price_factors', lambda: _price_factors(end))

    def index_prices():
        p = _price(INDEX_CODES, end, 'pre')
        p['units'] = {f: 'index_points' for f in ['open', 'high', 'low', 'close']}
        p['units'].update(volume='provider_index_volume_unit', money='CNY')
        return p

    capture('index_daily', index_prices)
    capture('security_state', lambda: _current(True))
    capture('quote_context', lambda: _current(False))
    capture('native_forecast', lambda: _forecast(previous))
    capture('native_forecast_detail', lambda: _forecast_detail(previous))
    for name, first, last in [('minute_open', '09:30:00', '09:35:00'),
                               ('minute_close', '14:55:00', '15:00:00')]:
        if ENABLE_MINUTE_PROBES:
            capture(name, lambda first=first, last=last: _price(
                SAMPLE_CODES, end + ' ' + last, None, frequency='1m', start=end + ' ' + first))
        else:
            capture(name, lambda: dict(status='disabled', reason='minute probes opt-in',
                                      rows=[], keys=[], units={}, params={}, comparable=True))
    capture('decision_trace', lambda: _decision(captured['daily_pre'], end))
    digest = _sha(_json({'metadata': meta, 'sections': digests}))
    _envelope('end', sid, sha256=digest, section_count=len(digests))
    done.append(key)
    g.alignment_completed = done[-120:]
    log.info('数据对账完成 %s %s；%s；仅记录，不下单' %
             (today, phase, ', '.join(n + '=' + captured[n]['status'] for n in names)))


def audit_before_open(context):
    _audit(context, 'before_open')


def audit_after_close(context):
    _audit(context, 'after_close')


def _schedule():
    unschedule_all()
    run_daily(audit_before_open, time=PHASE_TIMES['before_open'])
    run_daily(audit_after_close, time=PHASE_TIMES['after_close'])


def initialize(context):
    if AUDIT_PROFILE not in {'full', 'prices'}:
        raise ValueError('AUDIT_PROFILE must be full or prices')
    set_option('use_real_price', True)
    set_benchmark('399006.XSHE')
    g.alignment_completed = []
    _schedule()


def after_code_changed(context):
    if not hasattr(g, 'alignment_completed'):
        g.alignment_completed = []
    _schedule()


def process_initialize(context):
    if not hasattr(g, 'alignment_completed'):
        g.alignment_completed = []
