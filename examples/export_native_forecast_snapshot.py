"""Run a single DATA_DATE JoinQuant backtest; only read data and emit audit logs.

No orders, Redis, messages, account identifiers, or portfolio values are read.
Actual observation timestamps are wall-clock UTC, never the backtest clock.
Decode CHUNK payloads in index order with base64 + zlib, then verify SHA-256.
"""
from jqdata import *
from jqdata import finance
from builtins import all, any, max, min, sum, sorted
import base64
from datetime import datetime
import hashlib
import json
import zlib
import pandas as pd

# REQUIRED EACH DATA DAY: change DATA_DATE and use the same single-day backtest.
# This is the completed trading data day, not the following order execution day.
DATA_DATE = '2026-10-09'
LOOKBACK_DAYS = 90
PUBLICATION_END = DATA_DATE
PUBLICATION_START = (pd.Timestamp(DATA_DATE) - pd.Timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')
PAGE_SIZE = 3000
EXPORT_VERSION = '1.1'


def _now():
    return datetime.utcnow().isoformat(timespec='microseconds') + '+00:00'


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def _rows(frame):
    # Native object/date and numeric columns keep their declared dtype in the manifest.
    rows = frame.to_dict(orient='records')
    for row in rows:
        for field, value in row.items():
            if pd.isna(value):
                row[field] = None
            elif hasattr(value, 'isoformat'):
                row[field] = value.isoformat()
            elif hasattr(value, 'item'):
                row[field] = value.item()
        for field in ('pub_date', 'end_date'):
            if row.get(field) is not None:
                row[field] = str(row[field])[:10]
    return rows


def _emit(prefix, document):
    raw = _canonical(document)
    digest = hashlib.sha256(raw).hexdigest()
    compressed = zlib.compress(raw, 9)
    encoded = base64.b64encode(compressed).decode('ascii')
    chunks = [encoded[i:i + 300] for i in range(0, len(encoded), 300)]
    log.info('%s|BEGIN|%s|%s|%s|%s' % (prefix, digest, len(chunks), len(raw), len(compressed)))
    log.info('%s|MANIFEST|%s' % (prefix, _canonical(document['manifest']).decode('utf-8')))
    for index, chunk in enumerate(chunks):
        log.info('%s|CHUNK|%s|%s|%s|%s' % (prefix, digest, index, len(chunks), chunk))
    log.info('%s|END|%s' % (prefix, digest))


def _forecast():
    started = _now()
    table = finance.STK_FIN_FORCAST
    cursor, rows, pages, columns, dtypes = 0, [], [], None, None
    for _ in range(10000):
        q = query(table).filter(table.pub_date >= PUBLICATION_START,
                                table.pub_date <= PUBLICATION_END,
                                table.id > cursor).order_by(table.id.asc()).limit(PAGE_SIZE)
        frame = finance.run_query(q)
        observed = _now()
        if columns is None:
            columns = list(frame.columns)
            dtypes = {name: str(frame[name].dtype) for name in columns}
        if list(frame.columns) != columns:
            raise ValueError('Native forecast schema changed during pagination')
        batch = _rows(frame)
        pages.append({'after_id': cursor, 'row_count': len(batch), 'observed_at': observed,
                      'rows_sha256': hashlib.sha256(_canonical(batch)).hexdigest()})
        if not batch:
            break
        ids = [row['id'] for row in batch]
        if any(not isinstance(item, int) or item <= cursor for item in ids) or ids != sorted(set(ids)):
            raise ValueError('Native forecast pagination is not strictly increasing')
        if any(not PUBLICATION_START <= row['pub_date'] <= PUBLICATION_END for row in batch):
            raise ValueError('Native forecast publication date is outside the query')
        rows.extend(batch)
        cursor = ids[-1]
    else:
        raise ValueError('Native forecast pagination did not reach an empty terminal page')
    table_schema = [{'name': name, 'dtype': dtypes[name]} for name in columns]
    manifest = {'schema': 'jq-native-forecast-snapshot/1', 'provider': 'joinquant',
                'table': 'STK_FIN_FORCAST', 'exporter_version': EXPORT_VERSION,
                'scope': {'kind': 'all'}, 'publication_start': PUBLICATION_START,
                'publication_end': PUBLICATION_END, 'query_date': PUBLICATION_END,
                'query_started_at': started, 'observed_at': _now(),
                'columns': columns, 'dtypes': dtypes, 'native_table_schema': table_schema,
                'schema_source': 'native_query_dataframe_columns_and_dtypes',
                'row_count': len(rows), 'rows_sha256': hashlib.sha256(_canonical(rows)).hexdigest(),
                'complete': True, 'terminal_empty_page': True, 'pagination': 'id_gt_ascending',
                'pages': pages, 'historical_vintage_verified': False}
    _emit('JQNF1', {'manifest': manifest, 'rows': rows})


def _valuation():
    started = _now()
    cursor, rows, pages, columns, dtypes = '', [], [], None, None
    fields = (valuation.code, valuation.pe_ratio, valuation.pb_ratio,
              valuation.market_cap, valuation.circulating_market_cap, valuation.circulating_cap)
    for _ in range(10000):
        q = query(*fields).filter(valuation.code > cursor).order_by(valuation.code.asc()).limit(PAGE_SIZE)
        frame = get_fundamentals(q, date=PUBLICATION_END)
        if columns is None:
            columns = list(frame.columns)
            dtypes = {name: str(frame[name].dtype) for name in columns}
        batch = _rows(frame)
        pages.append({'after_code': cursor, 'row_count': len(batch), 'observed_at': _now(),
                      'rows_sha256': hashlib.sha256(_canonical(batch)).hexdigest()})
        if not batch:
            break
        codes = [row['code'] for row in batch]
        if any(code <= cursor for code in codes) or codes != sorted(set(codes)):
            raise ValueError('Native valuation pagination is not strictly increasing')
        rows.extend(batch)
        cursor = codes[-1]
    else:
        raise ValueError('Native valuation pagination did not reach an empty terminal page')
    manifest = {'schema': 'jq-native-valuation-snapshot/1', 'provider': 'joinquant',
                'table': 'valuation', 'exporter_version': EXPORT_VERSION,
                'scope': {'kind': 'all'}, 'query_date': PUBLICATION_END,
                'query_started_at': started, 'observed_at': _now(),
                'columns': columns, 'dtypes': dtypes, 'row_count': len(rows),
                'rows_sha256': hashlib.sha256(_canonical(rows)).hexdigest(),
                'complete': True, 'terminal_empty_page': True, 'pagination': 'code_gt_ascending',
                'pages': pages, 'historical_vintage_verified': False}
    _emit('JQVAL1', {'manifest': manifest, 'rows': rows})


def initialize(context):
    g.native_export_finished = False
    run_daily(export_snapshots, time='after_close')


def export_snapshots(context):
    if g.native_export_finished:
        return
    if str(context.current_dt)[:10] != DATA_DATE:
        raise ValueError('Backtest day must match DATA_DATE; change both for each new completed trading day')
    g.native_export_finished = True
    for name, callback in [('JQNF1', _forecast), ('JQVAL1', _valuation)]:
        try:
            callback()
        except Exception as error:
            log.error('%s|ERROR|%s|%s' % (name, type(error).__name__, str(error)))
