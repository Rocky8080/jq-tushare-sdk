"""Read exact JoinQuant forecast exports without synthesizing vendor text."""
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import zlib

import pandas as pd

from .cache_lock import cache_write_lock
from .code_map import normalize_date, to_joinquant_code
from .financial_provenance import FinancialVintageUnavailable
from zoneinfo import ZoneInfo


def stamp(value=None):
    """Normalize real observation time without depending on minute runtime."""
    china = ZoneInfo("Asia/Shanghai")
    value = datetime.now(china) if value is None else pd.Timestamp(value).to_pydatetime()
    return value.replace(tzinfo=china) if value.tzinfo is None else value.astimezone(china)


NATIVE_FIELDS = ('id', 'code', 'name', 'company_id', 'pub_date', 'end_date',
                 'report_type_id', 'report_type', 'type_id', 'type', 'profit_min',
                 'profit_max', 'profit_last', 'profit_ratio_min', 'profit_ratio_max', 'content')
CONTRACT = 'jq-native-forecast-snapshot/1'
_DATE_FIELDS = {'pub_date', 'end_date'}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _day(value):
    return datetime.strptime(normalize_date(value), '%Y%m%d').date().isoformat()


def _observed(value):
    observed = datetime.fromisoformat(str(value))
    if observed.tzinfo is None:
        raise ValueError('Native forecast observation must include a time zone')
    return observed.astimezone(timezone.utc)


def decode_logged_snapshots(log_text, prefix='JQNF1'):
    """Decode complete hash-checked log envelopes emitted by the read-only exporter."""
    groups, order = {}, []
    pattern = re.compile(re.escape(prefix) + r'\|(BEGIN|CHUNK|END)\|([^\r\n]+)')
    for match in pattern.finditer(log_text):
        kind, payload = match.groups()
        parts = payload.strip().split('|')
        digest = parts[0]
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid native export hash')
        if kind == 'BEGIN':
            if len(parts) != 4:
                raise ValueError('Invalid native export header')
            if digest not in groups:
                groups[digest] = {'count': int(parts[1]), 'raw_size': int(parts[2]),
                                  'compressed_size': int(parts[3]), 'chunks': {}, 'ended': False}
                order.append(digest)
        elif digest not in groups:
            raise ValueError('Native export chunk appeared without its header')
        elif kind == 'END':
            groups[digest]['ended'] = True
        else:
            if len(parts) != 4:
                raise ValueError('Invalid native export chunk')
            index, count, chunk = int(parts[1]), int(parts[2]), parts[3]
            group = groups[digest]
            if count != group['count'] or index < 0 or index >= count:
                raise ValueError('Native export chunk index mismatch')
            if index in group['chunks'] and group['chunks'][index] != chunk:
                raise ValueError('Conflicting native export chunk')
            group['chunks'][index] = chunk
    documents = []
    for digest in order:
        group = groups[digest]
        if not group['ended'] or set(group['chunks']) != set(range(group['count'])):
            raise ValueError('Native export log is incomplete; missing chunks or end marker')
        compressed = base64.b64decode(''.join(group['chunks'][i] for i in range(group['count'])), validate=True)
        if len(compressed) != group['compressed_size']:
            raise ValueError('Native export compressed size mismatch')
        raw = zlib.decompress(compressed)
        if len(raw) != group['raw_size'] or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Native export payload hash or size mismatch')
        documents.append(json.loads(raw))
    if not documents:
        raise ValueError('No native export envelopes found')
    return documents


def validate_native_forecast_snapshot(document):
    """Require complete native schema, explicit scope, pagination and actual observation."""
    manifest, rows = document['manifest'], document['rows']
    if (manifest.get('schema') != CONTRACT or manifest.get('provider') != 'joinquant'
            or manifest.get('table') != 'STK_FIN_FORCAST'
            or manifest.get('complete') is not True or manifest.get('terminal_empty_page') is not True
            or manifest.get('pagination') != 'id_gt_ascending'
            or manifest.get('historical_vintage_verified') is not False):
        raise ValueError('Native forecast manifest does not establish a complete observed query')
    start, end = _day(manifest['publication_start']), _day(manifest['publication_end'])
    if start > end or _day(manifest['query_date']) != end:
        raise ValueError('Native forecast publication window or query date is invalid')
    observed, started = _observed(manifest['observed_at']), _observed(manifest['query_started_at'])
    if started > observed or observed > datetime.now(timezone.utc) + timedelta(seconds=1):
        raise ValueError('Native forecast observation is invalid or in the future')
    if end > stamp(observed).date().isoformat():
        raise ValueError('Native forecast publication window reaches beyond its observation')
    columns, dtypes = manifest['columns'], manifest['dtypes']
    if (len(columns) != len(set(columns)) or set(columns) != set(NATIVE_FIELDS)
            or set(dtypes) != set(columns)
            or {field['name'] for field in manifest['native_table_schema']} != set(columns)):
        raise ValueError('Native forecast full-table schema does not match the verified fields')
    if any(dtype not in {'object', 'float64', 'int64', 'int32', 'float32'} for dtype in dtypes.values()):
        raise ValueError('Native forecast dtype is not supported without an explicit conversion')
    if any(dtypes[name] != 'object' for name in ('code', 'name', 'pub_date', 'end_date', 'report_type', 'type', 'content')):
        raise ValueError('Native forecast text/date dtypes must retain their native object semantics')
    scope = manifest['scope']
    if scope == {'kind': 'all'}:
        codes = None
    elif scope.get('kind') == 'codes' and isinstance(scope.get('codes'), list) and scope['codes']:
        codes = {to_joinquant_code(code) for code in scope['codes']}
    else:
        raise ValueError('Native forecast requires explicit whole-market or security-list coverage')
    if not isinstance(rows, list) or len(rows) != manifest['row_count'] or _hash(rows) != manifest['rows_sha256']:
        raise ValueError('Native forecast rows/hash do not match the manifest')
    ids = []
    for row in rows:
        if set(row) != set(columns) or not isinstance(row['id'], int) or isinstance(row['id'], bool):
            raise ValueError('Native forecast row schema or identifier is invalid')
        code = to_joinquant_code(row['code'])
        if code != row['code'] or codes is not None and code not in codes:
            raise ValueError('Native forecast row is outside its security scope')
        if not start <= _day(row['pub_date']) <= end:
            raise ValueError('Native forecast row is outside its publication window')
        _day(row['end_date'])
        ids.append(row['id'])
    if ids != sorted(set(ids)):
        raise ValueError('Native forecast rows are not unique and ordered by native id')
    offset, cursor = 0, 0
    pages = manifest['pages']
    if not pages or pages[-1]['row_count'] != 0:
        raise ValueError('Native forecast pagination has no empty terminal page')
    for index, page in enumerate(pages):
        count = page['row_count']
        if not isinstance(count, int) or count < 0 or page['after_id'] != cursor:
            raise ValueError('Native forecast page count or cursor is invalid')
        if not started <= _observed(page['observed_at']) <= observed:
            raise ValueError('Native forecast page observation is outside the capture interval')
        batch = rows[offset:offset + count]
        if len(batch) != count or _hash(batch) != page['rows_sha256']:
            raise ValueError('Native forecast page payload hash mismatch')
        if count:
            if any(row['id'] <= cursor for row in batch):
                raise ValueError('Native forecast page does not follow its exclusive cursor')
            cursor = batch[-1]['id']
        elif index != len(pages) - 1:
            raise ValueError('Native forecast contains pages after its empty terminal page')
        offset += count
    if offset != len(rows):
        raise ValueError('Native forecast pagination did not cover every row')
    return manifest


def import_native_forecast_snapshot(cache_db, document, *, source_path=None):
    """Append a verified export, preserving its native rows and observation time."""
    if os.environ.get('JQTS_SIMULATION_READ_ONLY') == '1':
        raise PermissionError('native forecast import is disabled in read-only simulation')
    manifest = validate_native_forecast_snapshot(document)
    digest, payload = _hash(document), _canonical(document).decode('utf-8')
    path = Path(cache_db).resolve()
    with cache_write_lock(path):
        with sqlite3.connect(str(path)) as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute("""CREATE TABLE IF NOT EXISTS native_forecast_snapshots (
                snapshot_hash TEXT PRIMARY KEY, publication_start TEXT NOT NULL,
                publication_end TEXT NOT NULL, observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL, source_path TEXT, imported_at TEXT NOT NULL)""")
            for operation in ('UPDATE', 'DELETE'):
                connection.execute(f"""CREATE TRIGGER IF NOT EXISTS native_forecast_snapshots_no_{operation.lower()}
                    BEFORE {operation} ON native_forecast_snapshots BEGIN
                    SELECT RAISE(ABORT, 'native forecast snapshots are immutable'); END""")
            connection.execute('INSERT OR IGNORE INTO native_forecast_snapshots VALUES (?,?,?,?,?,?,?)',
                (digest, _day(manifest['publication_start']), _day(manifest['publication_end']),
                 _observed(manifest['observed_at']).isoformat(), payload,
                 str(source_path) if source_path else None, datetime.now(timezone.utc).isoformat()))
    return {'snapshot_hash': digest, 'row_count': manifest['row_count'],
            'publication_start': manifest['publication_start'], 'publication_end': manifest['publication_end'],
            'observed_at': manifest['observed_at'], 'historical_vintage_verified': False}


def _select_native_forecast_snapshot(cache_db, start, end, codes, decision_at):
    """Check window, scope and real observation separately, without fallback."""
    requested = None if codes is None else {to_joinquant_code(code) for code in codes}
    decision = _observed(decision_at)
    diagnostics = {'source': 'joinquant.finance.STK_FIN_FORCAST',
        'publication_start': start, 'publication_end': end,
        'requested_scope': 'all' if requested is None else sorted(requested),
        'decision_at': decision.isoformat(), 'historical_vintage_verified': False,
        'coverage_complete': False, 'ready': False, 'receipts': []}
    path = Path(cache_db).resolve()
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_forecast_snapshots'").fetchone():
            diagnostics['reason'] = 'native_forecast_source_missing'
            return None, None, diagnostics
        rows = connection.execute('SELECT snapshot_hash,payload_json,imported_at FROM native_forecast_snapshots '
                                  'ORDER BY observed_at DESC, imported_at DESC').fetchall()
    window_matches, scope_matches = 0, 0
    for snapshot_hash, payload, imported_at in rows:
        document = json.loads(payload)
        if _hash(document) != snapshot_hash:
            raise FinancialVintageUnavailable('native_forecast_snapshot_hash_mismatch')
        manifest = validate_native_forecast_snapshot(document)
        window_ok = _day(manifest['publication_start']) <= start and end <= _day(manifest['publication_end'])
        scope = manifest['scope']
        scope_ok = scope == {'kind': 'all'} or requested is not None and requested.issubset(set(scope.get('codes', [])))
        observed = _observed(manifest['observed_at'])
        imported = _observed(imported_at)
        visible = observed <= decision and imported <= decision
        diagnostics['receipts'].append({'snapshot_hash': snapshot_hash,
            'publication_start': manifest['publication_start'], 'publication_end': manifest['publication_end'],
            'scope': scope, 'observed_at': manifest['observed_at'], 'imported_at': imported_at,
            'window_complete': window_ok, 'scope_complete': scope_ok, 'visible_before_decision': visible})
        window_matches += bool(window_ok)
        scope_matches += bool(window_ok and scope_ok)
        if window_ok and scope_ok and visible:
            diagnostics.update(ready=True, coverage_complete=True, reason=None,
                               selected_snapshot_hash=snapshot_hash)
            return document, imported_at, diagnostics
    diagnostics['reason'] = ('native_forecast_window_missing' if not window_matches
                             else 'native_forecast_scope_missing' if not scope_matches
                             else 'native_forecast_not_observed_before_decision')
    return None, None, diagnostics


def native_forecast_readiness(cache_db, publication_start, publication_end, *, codes=None,
                              decision_at, strict_pit=False):
    """Read-only source readiness. Empty rows never stand in for coverage."""
    start, end = _day(publication_start), _day(publication_end)
    if start > end or end > stamp(_observed(decision_at)).date().isoformat():
        raise ValueError('Native forecast requested window is reversed or after the decision date')
    _, _, result = _select_native_forecast_snapshot(cache_db, start, end, codes, decision_at)
    if strict_pit and result['ready']:
        result.update(ready=False, reason='native_forecast_historical_vintage_unverified')
    return result


def inspect_native_forecast_archive(cache_db, publication_start, publication_end, *, codes=None):
    """Inspect an observed archive now, outside any historical strategy runtime.

    Publication dates select content, not past visibility. This function does
    not unlock finance.run_query or certify historical backtests.
    """
    start, end = _day(publication_start), _day(publication_end)
    observed_now = datetime.now(timezone.utc).isoformat()
    if start > end or end > stamp(_observed(observed_now)).date().isoformat():
        raise ValueError('Native forecast archive window is reversed or future')
    document, imported_at, diagnostic = _select_native_forecast_snapshot(
        cache_db, start, end, codes, observed_now)
    if document is None:
        raise FinancialVintageUnavailable(diagnostic['reason'] + ': ' + json.dumps(diagnostic, ensure_ascii=False))
    manifest = document['manifest']
    frame = pd.DataFrame(document['rows'], columns=manifest['columns'])
    for name, dtype in manifest['dtypes'].items():
        frame[name] = frame[name].astype(dtype)
    published = pd.to_datetime(frame['pub_date'], errors='raise')
    frame = frame[(published >= pd.Timestamp(start)) & (published <= pd.Timestamp(end))]
    if codes is not None:
        frame = frame[frame['code'].isin({to_joinquant_code(code) for code in codes})]
    frame = frame.reset_index(drop=True)
    frame.attrs.update(source='joinquant.finance.STK_FIN_FORCAST',
        purpose='observed_archive_inspection_only', inspected_at=observed_now,
        observed_at=manifest['observed_at'], imported_at=imported_at,
        native_snapshot_hash=_hash(document), coverage_complete=True,
        historical_vintage_verified=False, historical_decision_eligible=False,
        native_reproduction_eligible=False, publication_start=start, publication_end=end)
    return frame


def run_native_forecast_query(q, state, publication_cutoff):
    """Evaluate native queries solely against complete, already observed native rows."""
    fields = {field.name for field in q.fields if not hasattr(field, '_table_name')}
    fields.update(expr.field.name for expr in q.filters)
    fields.update(item.field.name for item in q.ordering)
    if fields - set(NATIVE_FIELDS):
        raise ValueError('Unsupported native forecast fields: ' + ', '.join(sorted(fields - set(NATIVE_FIELDS))))
    starts = [(_observed(_day(expr.value) + 'T00:00:00+00:00') + timedelta(days=expr.operator == '>')).date().isoformat()
              for expr in q.filters if expr.field.name == 'pub_date' and expr.operator in {'>=', '>', '=='}]
    ends = [(_observed(_day(expr.value) + 'T00:00:00+00:00') - timedelta(days=expr.operator == '<')).date().isoformat()
            for expr in q.filters if expr.field.name == 'pub_date' and expr.operator in {'<=', '<', '=='}]
    if not starts or not ends:
        raise FinancialVintageUnavailable('native_forecast_coverage_unknown: explicit publication window required')
    start, end = max(starts), min(ends)
    if start > end or end > _day(publication_cutoff):
        raise FinancialVintageUnavailable('native_forecast_unavailable_at: publication window exceeds the runtime cutoff')
    codes = None
    for expr in q.filters:
        if expr.field.name == 'code' and expr.operator in {'in', '=='}:
            values = expr.value if expr.operator == 'in' else [expr.value]
            selected = {to_joinquant_code(code) for code in values}
            codes = selected if codes is None else codes & selected
    observed_at = stamp(state.context.current_dt).astimezone(timezone.utc)
    document, imported_at, diagnostic = _select_native_forecast_snapshot(
        state.data_portal.backend.cache_db, start, end, codes, observed_at.isoformat())
    if document is None:
        raise FinancialVintageUnavailable(diagnostic['reason'] + ': ' + json.dumps(diagnostic, ensure_ascii=False))
    if getattr(state.data_portal.backend, 'strict_pit', False):
        raise FinancialVintageUnavailable('native_forecast_historical_vintage_unverified: observation exports do not certify historical revisions')
    manifest = document['manifest']
    frame = pd.DataFrame(document['rows'], columns=manifest['columns'])
    for name, dtype in manifest['dtypes'].items():
        frame[name] = frame[name].astype(dtype)
    for expr in q.filters:
        value, column = expr.value, frame[expr.field.name]
        if expr.field.name == 'code':
            value = [to_joinquant_code(item) for item in value] if expr.operator == 'in' else to_joinquant_code(value)
        elif expr.field.name in _DATE_FIELDS:
            column = pd.to_datetime(column, errors='raise')
            value = [pd.Timestamp(_day(item)) for item in value] if expr.operator == 'in' else pd.Timestamp(_day(value))
        if expr.operator == 'in': mask = column.isin(value)
        elif expr.operator == '==': mask = column == value
        elif expr.operator == '!=': mask = column != value
        elif expr.operator == '>=': mask = column >= value
        elif expr.operator == '<=': mask = column <= value
        elif expr.operator == '>': mask = column > value
        elif expr.operator == '<': mask = column < value
        else: raise ValueError('Unsupported native forecast operator: ' + expr.operator)
        frame = frame[mask]
    for item in reversed(q.ordering):
        frame = frame.sort_values(item.field.name, ascending=item.direction != 'desc', kind='stable')
    selected = manifest['columns'] if any(hasattr(field, '_table_name') for field in q.fields) else [field.name for field in q.fields]
    result = frame[selected].reset_index(drop=True)
    result.attrs.update(source='joinquant.finance.STK_FIN_FORCAST', requested_table='STK_FIN_FORCAST',
        complete_native_table_supported=True, native_compatibility_verified=True,
        native_reproduction_eligible=False, pit_verified=False, historical_vintage_verified=False,
        native_snapshot_hash=_hash(document), observed_at=manifest['observed_at'], imported_at=imported_at,
        decision_at=observed_at.isoformat(), publication_cutoff=_day(publication_cutoff),
        coverage_complete=True, coverage={'scope': manifest['scope'], 'publication_start': manifest['publication_start'],
            'publication_end': manifest['publication_end'], 'all_market_complete': manifest['scope'] == {'kind': 'all'}},
        financial_mode=getattr(state.data_portal.backend, 'financial_mode', 'cached_snapshot'))
    return result
