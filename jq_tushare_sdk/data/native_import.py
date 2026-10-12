"""Public import of complete JoinQuant log exports into an explicit local cache."""
from datetime import datetime, timedelta
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit
import zipfile

from .native_forecast import (decode_logged_snapshots, import_native_forecast_snapshot,
                              validate_native_forecast_snapshot)
from .native_valuation import import_native_valuation, validate_native_valuation_snapshot


def _source_reference(value):
    url = urlsplit(value)
    query = parse_qs(url.query, keep_blank_values=True)
    if (url.scheme != 'https' or url.netloc not in {'www.joinquant.com', 'joinquant.com'}
            or url.path != '/algorithm/backtest/detail' or url.fragment
            or set(query) != {'backtestId'} or len(query['backtestId']) != 1
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', query['backtestId'][0])):
        raise ValueError('source-reference must be the JoinQuant backtest detail URL, without credentials or extra parameters')
    return value


def _read_log(path):
    raw = path.read_bytes()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            logs = [item for item in archive.infolist() if not item.is_dir()
                    and '__MACOSX' not in Path(item.filename).parts
                    and Path(item.filename).suffix.lower() in {'.txt', '.log'}]
            if not logs:
                raise ValueError('ZIP contains no complete .txt or .log export')
            # Read members directly; never extract archive-controlled paths.
            text = '\n'.join(archive.read(item).decode('utf-8-sig') for item in logs)
    else:
        text = raw.decode('utf-8-sig')
    return text, hashlib.sha256(raw).hexdigest()


def _stored_receipt(cache_db, table, key, digest):
    path = Path(cache_db).resolve()
    if not path.exists():
        return None
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return None
        row = connection.execute(f'SELECT imported_at FROM {table} WHERE {key}=?', (digest,)).fetchone()
    return row[0] if row else None


def import_native_log(cache_db, input_path, *, data_date, source_reference,
                      lookback_days=90, validate_only=False):
    """Validate both complete snapshots before any append; identical hashes are reusable.

    Input may be the full downloaded log ZIP or its UTF-8 log. The explicit
    data date is checked against both manifests, never used to relabel rows.
    """
    if os.environ.get('JQTS_SIMULATION_READ_ONLY') == '1' and not validate_only:
        raise PermissionError('native import is disabled in read-only simulation')
    source_reference = _source_reference(source_reference)
    day = datetime.strptime(data_date, '%Y-%m-%d').date()
    if day.isoformat() != data_date or not isinstance(lookback_days, int) or lookback_days < 1:
        raise ValueError('Use an exact YYYY-MM-DD data date and a positive lookback-days value')
    first = (day - timedelta(days=lookback_days)).isoformat()
    text, input_hash = _read_log(Path(input_path).expanduser().resolve())
    documents = {}
    for prefix in ('JQNF1', 'JQVAL1'):
        decoded = decode_logged_snapshots(text, prefix)
        if len(decoded) != 1:
            raise ValueError('Input must contain exactly one complete forecast and one complete valuation snapshot')
        documents[prefix] = decoded[0]
    forecast, valuation = documents['JQNF1'], documents['JQVAL1']
    validate_native_forecast_snapshot(forecast)
    validate_native_valuation_snapshot(valuation)
    fm, vm = forecast['manifest'], valuation['manifest']
    if (fm['query_date'] != data_date or fm['publication_end'] != data_date
            or fm['publication_start'] != first or fm['scope'] != {'kind': 'all'}
            or vm['query_date'] != data_date):
        raise ValueError('Export data date/window does not match the requested date; export this data day again, never relabel an older snapshot')
    receipt = {'schema': 'jq-native-log-import/1', 'status': 'validated' if validate_only else 'imported',
               'data_date': data_date, 'publication_start': first, 'publication_end': data_date,
               'input_sha256': input_hash, 'source_reference': source_reference,
               'historical_vintage_verified': False, 'snapshots': {}}
    if validate_only:
        for label, manifest in [('forecast', fm), ('valuation', vm)]:
            receipt['snapshots'][label] = {'row_count': manifest['row_count'],
                'observed_at': manifest['observed_at'], 'rows_sha256': manifest['rows_sha256']}
        return receipt
    # Both envelopes, schemas, dates, scopes and page hashes passed above.
    # Each append is immutable and idempotent; an interrupted import can be rerun.
    cache = Path(cache_db).expanduser().resolve()
    cache.parent.mkdir(parents=True, exist_ok=True)
    result = import_native_forecast_snapshot(cache, forecast, source_path=source_reference)
    result['imported_at'] = _stored_receipt(cache, 'native_forecast_snapshots', 'snapshot_hash', result['snapshot_hash'])
    receipt['snapshots']['forecast'] = result
    result = import_native_valuation(cache, valuation, source_reference=source_reference)
    result['imported_at'] = _stored_receipt(cache, 'native_valuation_observations', 'document_sha256', result['document_sha256'])
    receipt['snapshots']['valuation'] = result
    return receipt
