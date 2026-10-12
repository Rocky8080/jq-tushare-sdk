"""Immutable supplementary market evidence, separate from Tushare source rows.

Precise arithmetic does not establish native equivalence or historical vintages.
Issuer period-end statements are bounds, not individual registration dates.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .cache_lock import cache_write_lock
from .code_map import normalize_date, to_tushare_code
from .financial_provenance import asof_timestamp, capture_timestamp


class MarketReferenceUnavailable(RuntimeError):
    pass


def _digest(body):
    return hashlib.sha256(body).hexdigest()


def _read(cache, kind, code=None):
    with sqlite3.connect(Path(cache).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_reference_evidence'").fetchone():
            return []
        sql = 'SELECT captured_at,payload_json,evidence_sha256 FROM market_reference_evidence WHERE kind=?'
        values = [kind]
        if code is not None:
            sql += ' AND ts_code=?'
            values.append(code)
        rows = conn.execute(sql + ' ORDER BY captured_at,evidence_sha256', values).fetchall()
    result = []
    for stamp, payload, expected_hash in rows:
        if _digest(payload.encode('utf-8')) != expected_hash:
            raise MarketReferenceUnavailable('market_reference_evidence_hash_mismatch: ' + kind)
        result.append(dict(json.loads(payload), captured_at=stamp))
    return result


def record_evidence(cache, kind, code, payload):
    if os.environ.get('JQTS_SIMULATION_READ_ONLY') == '1':
        raise MarketReferenceUnavailable('read-only replay cannot prepare market references')
    code = to_tushare_code(code)
    captured = capture_timestamp()
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))
    with cache_write_lock(cache), sqlite3.connect(str(cache), timeout=30) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS market_reference_evidence (
            kind TEXT NOT NULL, ts_code TEXT NOT NULL, captured_at TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(kind,ts_code,captured_at,evidence_sha256))''')
        for operation in ('UPDATE', 'DELETE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS market_reference_no_{operation.lower()}
                BEFORE {operation} ON market_reference_evidence BEGIN
                SELECT RAISE(ABORT, 'market reference evidence is immutable'); END''')
        conn.execute('INSERT OR IGNORE INTO market_reference_evidence VALUES (?,?,?,?,?)',
                     (kind, code, captured, _digest(body.encode()), body))
    return dict(payload, captured_at=captured)


def _sina_nodes(body, symbol, direction):
    # Parse JSON only. Never evaluate the provider's JavaScript container.
    match = re.match(r'\s*var\s+' + re.escape(symbol + direction) + r'\s*=\s*(\{.*?\})\s*(?:;|/\*|$)',
                     body.decode('utf-8'), re.S)
    if not match:
        raise MarketReferenceUnavailable('factor_source_schema_invalid: unexpected Sina container')
    data = json.loads(match.group(1))
    rows = data.get('data', [])
    if not rows or int(data.get('total', -1)) != len(rows):
        raise MarketReferenceUnavailable('factor_source_incomplete: node count differs')
    seen = set()
    for row in rows:
        day = datetime.strptime(row['d'], '%Y-%m-%d').date()
        factor = Decimal(row['f'])
        if row['d'] in seen or not factor.is_finite() or factor <= 0 or day > datetime.now(ZoneInfo('Asia/Shanghai')).date():
            raise MarketReferenceUnavailable('factor_source_schema_invalid: duplicate, future or invalid node')
        seen.add(row['d'])
    return sorted(rows, key=lambda row: row['d'])


def refresh_precise_factors(cache, codes, *, cancelled=None, progress=None):
    """Explicit preparation only; strategies never access the network here.

    Archive both independent endpoint responses and check their reciprocal
    relationship. Original decimal strings remain in the evidence table.
    """
    import requests
    result = []
    with requests.Session() as session:
        for code in sorted({to_tushare_code(code) for code in codes}):
            if cancelled and cancelled():
                raise InterruptedError('factor preparation cancelled')
            number, market = code.split('.')
            if market not in {'SH', 'SZ'} or len(number) != 6 or not number.isdigit():
                raise MarketReferenceUnavailable('Sina stock factors support SH/SZ stocks only')
            symbol = market.lower() + number
            payload = {'source': 'sina.hfq', 'historical_vintages_verified': False,
                       'native_factor_equivalence_verified': False, 'responses': {}}
            nodes = {}
            for direction in ('hfq', 'qfq'):
                if cancelled and cancelled():
                    raise InterruptedError('factor preparation cancelled')
                url = f'https://finance.sina.com.cn/realstock/company/{symbol}/{direction}.js'
                response = session.get(url, timeout=15)
                response.raise_for_status()
                if len(response.content) > 1024 * 1024:
                    raise MarketReferenceUnavailable('factor response exceeds size limit')
                nodes[direction] = _sina_nodes(response.content, symbol, direction)
                payload['responses'][direction] = {'url': url, 'raw_sha256': _digest(response.content),
                    'raw_text': response.content.decode('utf-8'), 'nodes': nodes[direction]}
            if [row['d'] for row in nodes['hfq']] != [row['d'] for row in nodes['qfq']]:
                raise MarketReferenceUnavailable('factor endpoint dates disagree')
            with localcontext() as ctx:
                ctx.prec = 40
                anchor = Decimal(nodes['hfq'][-1]['f'])
                for hfq, qfq in zip(nodes['hfq'], nodes['qfq']):
                    if abs(Decimal(hfq['f']) * Decimal(qfq['f']) / anchor - 1) > Decimal('1e-10'):
                        raise MarketReferenceUnavailable('factor endpoint reciprocal check failed')
            evidence = record_evidence(cache, 'precise_factor', code, payload)
            result.append({'code': code, 'source': payload['source'], 'nodes': len(nodes['hfq']),
                           'captured_at': evidence['captured_at'], 'hfq_raw_sha256': payload['responses']['hfq']['raw_sha256']})
            if progress:
                progress(result[-1])
    return result


def precise_factor_frame(cache, frame, *, as_of=None, strict_pit=False, source='sina_hfq'):
    """Replace only factors on existing source dates; no price/date fabrication."""
    if frame.empty:
        return frame
    if not {'ts_code', 'trade_date', 'adj_factor'}.issubset(frame):
        raise MarketReferenceUnavailable('precise factor resolution requires code/date/value columns')
    result = frame.copy()
    provenance = {}
    cutoff = asof_timestamp(as_of) if as_of is not None else None
    for code, indices in result.groupby('ts_code').groups.items():
        kind = 'reference_price_factor' if source == 'daily_ex_reference' else 'precise_factor'
        records = _read(cache, kind, str(code))
        if strict_pit:
            if cutoff is None:
                raise MarketReferenceUnavailable('factor_vintage_missing: strict PIT requires as_of')
            records = [row for row in records if row['captured_at'] <= cutoff]
            if not records:
                raise MarketReferenceUnavailable(f'factor_vintage_missing: {code}; no capture visible at {cutoff}')
        if not records:
            raise MarketReferenceUnavailable(f'precise_factor_missing: {code}; prepare source before running')
        requested_days = [normalize_date(result.at[index, 'trade_date']) for index in indices]
        requested_first, requested_last = min(requested_days), max(requested_days)
        # A later preparation may cover a smaller interval. It must not hide
        # an older immutable receipt that still covers the entire request.
        # Select one complete receipt per security; do not splice absolute
        # factor units from separately seeded preparations.
        covering = []
        for candidate in records:
            candidate_nodes = candidate['responses']['hfq']['nodes']
            first = candidate.get('coverage_start', candidate_nodes[0]['d']).replace('-', '')
            through = candidate.get('coverage_through', candidate['captured_at'][:10]).replace('-', '')
            if first <= requested_first and requested_last <= through:
                covering.append(candidate)
        if not covering:
            raise MarketReferenceUnavailable(
                f'precise_factor_coverage_missing: {code} {requested_first}..{requested_last}; '
                'no single visible receipt covers the requested interval')
        record = covering[-1]
        nodes = record['responses']['hfq']['nodes']
        first = record.get('coverage_start', nodes[0]['d']).replace('-', '')
        through = record.get('coverage_through', record['captured_at'][:10]).replace('-', '')
        for index in indices:
            day = normalize_date(result.at[index, 'trade_date'])
            if not first <= day <= through:
                raise MarketReferenceUnavailable(f'precise_factor_coverage_missing: {code} {day}')
            # A change node applies on its date; no next-event or future anchor.
            value = next((row['f'] for row in reversed(nodes) if row['d'].replace('-', '') <= day), None)
            if value is None:
                raise MarketReferenceUnavailable(f'precise_factor_coverage_missing: {code} {day}')
            result.at[index, 'adj_factor'] = float(Decimal(value))
        provenance[str(code)] = {'source': record['source'], 'captured_at': record['captured_at'],
            'raw_sha256': record['responses']['hfq']['raw_sha256'],
            'coverage_start': record.get('coverage_start', nodes[0]['d']),
            'coverage_through': record.get('coverage_through', record['captured_at'][:10]),
            'receipt_selection': 'single_visible_receipt_covers_entire_requested_interval',
            'historical_vintages_verified': False, 'native_factor_equivalence_verified': False,
            'calculation_rule': record.get('calculation_rule', 'provider_hfq_change_nodes'),
            'input_precision': record.get('input_precision', 'original_provider_decimal_strings')}
    result.attrs.update(frame.attrs, precise_factor_provenance=provenance)
    return result


def prepare_reference_price_factors(cache, codes, start, end):
    """Explicit alternative rule from observed ex-day reference quotations.

    Tushare documents daily.pre_close as the ex-adjusted prior close. Use
    previous raw close / ex-day pre_close at source-detected factor changes.
    Decimal arithmetic retains the ratio; two-decimal quotation inputs do not
    establish an exact native factor. No comparison logs enter this function.
    """
    lower, upper = normalize_date(start), normalize_date(end)
    if lower > upper:
        raise ValueError('factor source interval reversed')
    output = []
    for code in sorted({to_tushare_code(code) for code in codes}):
        with sqlite3.connect(Path(cache).resolve().as_uri() + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            factors = [dict(row) for row in conn.execute('SELECT ts_code,trade_date,adj_factor FROM adj_factor '
                'WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date', (code, lower, upper))]
            quotes = [dict(row) for row in conn.execute('SELECT ts_code,trade_date,close,pre_close FROM daily_quote '
                'WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date', (code, lower, upper))]
        if not factors or not quotes or factors[0]['trade_date'] != quotes[0]['trade_date']:
            raise MarketReferenceUnavailable(f'reference_price_factor_anchor_missing: {code}')
        by_day = {row['trade_date']: row for row in quotes}
        nodes, events = [], []
        last_source, last_quote = None, None
        with localcontext() as ctx:
            ctx.prec = 40
            # Absolute unit is source-seeded; pre normalization cancels it.
            factor = Decimal(str(factors[0]['adj_factor']))
            for row in factors:
                source_value = Decimal(str(row['adj_factor']))
                if not source_value.is_finite() or source_value <= 0:
                    raise MarketReferenceUnavailable('reference_price_source_factor_invalid')
                quote = by_day.get(row['trade_date'])
                if last_source is not None and source_value != last_source:
                    if quote is None or last_quote is None:
                        raise MarketReferenceUnavailable(f'reference_price_event_quotes_missing: {code} {row["trade_date"]}')
                    previous, reference = Decimal(str(last_quote['close'])), Decimal(str(quote['pre_close']))
                    if not all(v.is_finite() and v > 0 for v in (previous, reference)) or previous == reference:
                        raise MarketReferenceUnavailable('reference_price_event_precision_missing')
                    factor *= previous / reference
                    events.append({'date': row['trade_date'], 'previous_observed_quote_date': last_quote['trade_date'],
                        'previous_raw_close': str(previous), 'ex_day_pre_close': str(reference),
                        'pre_scale': str(reference / previous), 'exact_native_factor_verified': False})
                nodes.append({'d': pd.Timestamp(row['trade_date']).strftime('%Y-%m-%d'), 'f': str(factor)})
                last_source = source_value
                if quote is not None:
                    last_quote = quote
        raw = {'raw_factors': factors, 'raw_quotes': quotes}
        raw_text = json.dumps(raw, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        payload = {'source': 'tushare.daily_ex_reference', 'coverage_start': nodes[0]['d'],
            'coverage_through': nodes[-1]['d'], 'calculation_rule': 'factor_next=factor_previous*previous_raw_close/ex_day_pre_close',
            'input_precision': 'observed rounded quotation inputs; not exact native reconstruction',
            'historical_vintages_verified': False, 'native_factor_equivalence_verified': False,
            'source_documentation': 'https://tushare.pro/document/2?doc_id=27',
            'events': events, 'responses': {'hfq': {'nodes': nodes, 'raw_text': raw_text,
                'raw_sha256': _digest(raw_text.encode()), 'source': 'readonly local Tushare source tables'}}}
        evidence = record_evidence(cache, 'reference_price_factor', code, payload)
        output.append({'code': code, 'nodes': len(nodes), 'events': events,
                       'captured_at': evidence['captured_at'], 'raw_sha256': payload['responses']['hfq']['raw_sha256']})
    return output


def sync_szse_announcements(cache, codes, start, end, *, cancelled=None):
    """Archive the exchange's exact publication times, including seconds."""
    import requests
    output = []
    url = 'https://www.szse.cn/api/disc/announcement/annList'
    with requests.Session() as session:
        for code in sorted({to_tushare_code(code) for code in codes}):
            if not re.fullmatch(r'\d{6}\.SZ', code):
                raise ValueError('SZSE announcements require a Shenzhen stock code')
            found, ids = [], set()
            for page in range(1, 1001):
                if cancelled and cancelled():
                    raise InterruptedError('announcement preparation cancelled')
                params = {'seDate': [pd.Timestamp(start).strftime('%Y-%m-%d'), pd.Timestamp(end).strftime('%Y-%m-%d')],
                          'stock': [code[:6]], 'channelCode': ['listedNotice_disc'], 'pageSize': 100, 'pageNum': page}
                response = session.post(url, json=params, timeout=20)
                response.raise_for_status()
                data = response.json()
                count, rows = int(data['announceCount']), data['data']
                for row in rows:
                    if row['id'] in ids or code[:6] not in row.get('secCode', []):
                        raise MarketReferenceUnavailable('announcement pagination repeats or wrong security')
                    stamp = asof_timestamp(row['publishTime'])
                    if not params['seDate'][0] <= stamp[:10] <= params['seDate'][1]:
                        raise MarketReferenceUnavailable('announcement date outside requested interval')
                    if not str(row['attachPath']).startswith('/disc/'):
                        raise MarketReferenceUnavailable('unexpected exchange attachment path')
                    ids.add(row['id'])
                payload = {'source': 'szse.announcement', 'url': url, 'request': params,
                           'raw_sha256': _digest(response.content), 'raw_text': response.text,
                           'rows': rows, 'publication_market': 'SZSE', 'earliest_global_visibility_verified': False}
                output.append(record_evidence(cache, 'announcements', code, payload))
                found.extend(rows)
                if len(found) == count:
                    break
                if not rows or len(found) > count:
                    raise MarketReferenceUnavailable('announcement response incomplete')
            else:
                raise MarketReferenceUnavailable('announcement page limit exceeded')
    return output


def import_capital_statement(cache, code, statement, pdf_path):
    """Attach a reviewed issuer statement to independently archived publication.

    A monthly boundary is deliberately not promoted to an exact effective date.
    Float capital, registration dates and dividend precision are not inferred.
    """
    code = to_tushare_code(code)
    matches = [row for receipt in _read(cache, 'announcements', code) for row in receipt['rows']
               if row['id'] == statement['announcement_id']]
    if not matches:
        raise MarketReferenceUnavailable('official announcement metadata missing')
    hashes = {_digest(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()) for row in matches}
    if len(hashes) != 1:
        raise MarketReferenceUnavailable('official announcement versions disagree')
    metadata = matches[0]
    pdf = Path(pdf_path).read_bytes()
    if not pdf.startswith(b'%PDF') or _digest(pdf) != statement['pdf_sha256']:
        raise ValueError('capital statement PDF hash mismatch')
    if statement['date_basis'] not in {'period_end_bound', 'exact_effective_date'}:
        raise ValueError('unknown capital date basis')
    bound = pd.Timestamp(statement['effective_on_or_before']).strftime('%Y-%m-%d')
    exact = statement.get('economic_effective_date')
    if (statement['date_basis'] == 'exact_effective_date') != bool(exact):
        raise ValueError('exact effective dates require explicit evidence, never a monthly bound')
    if exact and pd.Timestamp(exact).strftime('%Y-%m-%d') > bound:
        raise ValueError('effective date exceeds statement boundary')
    for column in ('total_shares', 'a_shares', 'h_shares'):
        value = statement[column]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('capital statement requires original integer share counts')
    if statement['total_shares'] != statement['a_shares'] + statement['h_shares']:
        raise ValueError('capital statement share classes do not reconcile')
    publication = asof_timestamp(metadata['publishTime'])
    if publication[:10] < bound:
        raise ValueError('completed capital statement cannot precede its period end')
    payload = dict(statement, ts_code=code, effective_on_or_before=bound,
                   economic_effective_date=pd.Timestamp(exact).strftime('%Y-%m-%d') if exact else None,
                   source='issuer_filing', publication_at=publication,
                   publication_time_verified=True, economic_effective_date_verified=bool(exact),
                   publication_market='SZSE', earliest_global_visibility_verified=False,
                   source_url='https://disc.static.szse.cn' + metadata['attachPath'],
                   announcement_metadata=metadata, numeric_override_enabled=False,
                   extraction='reviewed_original_filing', historical_capture_verified=False)
    return record_evidence(cache, 'capital_statement', code, payload)


def capital_evidence_asof(cache, effective_date, knowledge_asof):
    if knowledge_asof is None:
        return []
    day = pd.Timestamp(effective_date).strftime('%Y-%m-%d')
    cutoff = asof_timestamp(knowledge_asof)
    result = []
    for row in _read(cache, 'capital_statement'):
        if row['effective_on_or_before'] <= day and row['publication_at'] <= cutoff:
            # Unknown float capital or individual dates cannot modify valuation.
            result.append({key: value for key, value in row.items() if key != 'announcement_metadata'})
    for row in _read(cache, 'capital_registration_notice'):
        if row['available_at'] <= cutoff:
            result.append({**row, 'event_active_as_of_query_date':
                           row.get('listing_date') is not None and row['listing_date'] <= day})
    return result


def import_capital_registration_notice(cache, code, notice, pdf_path):
    """Keep registration, pre-registration and announced listing distinct.

    An issuer's pre-registration statement is not final CSDC registration.
    This evidence augments provenance only and never overrides daily capital.
    """
    code = to_tushare_code(code)
    rows = {json.dumps(row, sort_keys=True, ensure_ascii=False): row
            for receipt in _read(cache, 'announcements', code) for row in receipt['rows']
            if row['id'] == notice['announcement_id']}
    if len(rows) != 1:
        raise MarketReferenceUnavailable('registration notice requires one archived exchange announcement')
    metadata = next(iter(rows.values()))
    body = Path(pdf_path).read_bytes()
    if not body.startswith(b'%PDF') or _digest(body) != notice['pdf_sha256']:
        raise ValueError('registration notice PDF hash mismatch')
    status = notice['registration_status']
    if status not in {'pre_registered', 'registered'}:
        raise ValueError('unknown capital registration status')
    count = notice['share_count']
    pages = notice['source_pages']
    if type(count) is not int or count <= 0 or not pages or any(type(p) is not int or p <= 0 for p in pages):
        raise ValueError('registration notice requires original integer shares and reviewed pages')
    publication = asof_timestamp(metadata['publishTime'])
    available = publication[:10] + 'T23:59:59' if publication.endswith('T00:00:00') else publication
    exact = notice.get('registration_date')
    bound = notice.get('preregistration_completed_on_or_before')
    if status == 'pre_registered':
        if exact is not None or bound is None:
            raise ValueError('pre-registration cannot establish a final registration date')
        bound = pd.Timestamp(bound).strftime('%Y-%m-%d')
        if bound > publication[:10]:
            raise ValueError('pre-registration completion bound cannot follow its announcement')
    elif exact is None:
        raise ValueError('registered status requires a reviewed final registration date')
    if exact is not None:
        exact = pd.Timestamp(exact).strftime('%Y-%m-%d')
        if exact > publication[:10]:
            raise ValueError('completed registration date cannot follow its announcement')
    listing = notice.get('listing_date')
    listing = pd.Timestamp(listing).strftime('%Y-%m-%d') if listing else None
    if listing is not None and exact is not None and listing < exact:
        raise ValueError('listing date precedes final registration')
    if notice.get('listing_date_basis') not in {'announced_expected', 'completed_listing'}:
        raise ValueError('listing date requires an explicit announced/completed basis')
    if notice['listing_date_basis'] == 'completed_listing' and (listing is None or listing > publication[:10]):
        raise ValueError('completed listing requires a reviewed date no later than publication')
    return record_evidence(cache, 'capital_registration_notice', code, {
        **notice, 'ts_code': code, 'record_type': 'capital_registration_notice',
        'registration_date': exact, 'preregistration_completed_on_or_before': bound,
        'listing_date': listing, 'publication_at': publication, 'available_at': available,
        'source_url': 'https://disc.static.szse.cn' + metadata['attachPath'],
        'extraction': 'reviewed_original_issuer_notice',
        'historical_vintages_verified': False, 'numeric_override_enabled': False,
        'economic_effective_date_verified': False})


def _capital_row_quality(row):
    """Retain legacy non-free shares as a distinct class, not modern limits."""
    counts = ('TOTAL_SHARES', 'LISTED_A_SHARES', 'LIMITED_A_SHARES',
              'UNLIMITED_SHARES', 'LIMITED_SHARES', 'NON_FREE_SHARES')
    invalid = [name for name in counts if row.get(name) is not None
               and (type(row[name]) is not int or row[name] < 0)]
    if invalid:
        return {'qualified': False, 'reason': 'original_integer_precision_invalid', 'fields': invalid}
    total = row.get('TOTAL_SHARES')
    listed, restricted = row.get('LISTED_A_SHARES'), row.get('LIMITED_A_SHARES')
    if total is not None and listed is not None and restricted is not None and listed + restricted > total:
        return {'qualified': False, 'reason': 'a_share_classes_exceed_total'}
    unlimited, limited, nonfree = (row.get(name) for name in ('UNLIMITED_SHARES', 'LIMITED_SHARES', 'NON_FREE_SHARES'))
    if total is not None and unlimited is not None:
        known = unlimited + (limited if limited is not None else 0)
        if nonfree is not None:
            known += nonfree
        if known == total:
            return {'qualified': True, 'basis': 'provider_share_classes_reconcile',
                    'legacy_non_free_shares': nonfree}
        return {'qualified': False, 'reason': 'share_class_totals_incomplete_or_inconsistent',
                'known_class_sum': known, 'total_shares': total}
    return {'qualified': False, 'reason': 'share_class_coverage_unknown'}


def refresh_capital_history(cache, codes, *, cancelled=None):
    """Archive provider-dated share snapshots at original integer precision.

    END_DATE/LISTING_DATE can be monthly aggregates. They are never labelled
    individual registration dates. NOTICE_DATE has no intraday timestamp.
    """
    import requests
    output = []
    url = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
    with requests.Session() as session:
        for code in sorted({to_tushare_code(code) for code in codes}):
            rows, seen, responses, unqualified = [], set(), [], []
            for page in range(1, 101):
                if cancelled and cancelled():
                    raise InterruptedError('capital history preparation cancelled')
                params = {'reportName': 'RPT_F10_EH_EQUITY', 'columns': 'ALL',
                    'filter': f'(SECUCODE="{code}")', 'pageSize': '200', 'pageNumber': str(page),
                    'sortColumns': 'END_DATE', 'sortTypes': '-1', 'source': 'HSF10', 'client': 'PC'}
                response = session.get(url, params=params, timeout=20)
                response.raise_for_status()
                data = response.json()
                if data.get('success') is not True or not isinstance(data.get('result'), dict):
                    raise MarketReferenceUnavailable('capital_history_source_failed')
                batch = data['result']['data']
                for row in batch:
                    key = (row['END_DATE'], row.get('CHANGE_REASON'))
                    if row['SECUCODE'] != code or key in seen:
                        raise MarketReferenceUnavailable('capital_history_identity_invalid')
                    quality = _capital_row_quality(row)
                    if not quality['qualified']:
                        # Archive the original response and isolate the row.
                        # One old incomplete classification must not discard
                        # otherwise valid recent snapshots for the security.
                        unqualified.append({'end_date': row['END_DATE'],
                                            'change_reason': row.get('CHANGE_REASON'), **quality})
                    seen.add(key)
                rows.extend(batch)
                responses.append({'url': response.url, 'request': params,
                    'raw_sha256': _digest(response.content), 'raw_text': response.text})
                if page == int(data['result']['pages']):
                    if len(rows) != int(data['result']['count']):
                        raise MarketReferenceUnavailable('capital_history_coverage_incomplete')
                    break
                if not batch:
                    raise MarketReferenceUnavailable('capital_history_pagination_incomplete')
            else:
                raise MarketReferenceUnavailable('capital_history_page_limit_exceeded')
            evidence = record_evidence(cache, 'capital_history', code, {
                'source': 'eastmoney.RPT_F10_EH_EQUITY', 'responses': responses, 'rows': rows,
                'unqualified_rows': unqualified,
                'share_class_policy': 'legacy_NON_FREE_SHARES_distinct_from_modern_LIMITED_SHARES',
                'historical_vintages_verified': False, 'individual_registration_dates_verified': False,
                'publication_time_verified': False, 'float_definition': 'LISTED_A_SHARES; excludes H shares'})
            output.append({'code': code, 'rows': len(rows), 'captured_at': evidence['captured_at'],
                           'unqualified_rows': len(unqualified),
                           'raw_sha256': responses[0]['raw_sha256']})
    return output


def import_disclosed_capital_reference(cache, code, document, *, document_directory):
    """Bind a reviewed capital/valuation extraction to real independent sources.

    This is an explicit research view of latest disclosed capital, not a daily
    CSDC registration feed. All original source tables remain unchanged. PDFs
    and archived exchange metadata are mandatory; financial values are retained
    exactly as extracted from original consolidated statements.
    """
    code = to_tushare_code(code)
    day = pd.Timestamp(document['snapshot_date']).strftime('%Y-%m-%d')
    histories = _read(cache, 'capital_history', code)
    matches = []
    for receipt in histories:
        for response in receipt['responses']:
            if response['raw_sha256'] != document['capital_history_sha256']:
                continue
            if _digest(response['raw_text'].encode('utf-8')) != response['raw_sha256']:
                raise MarketReferenceUnavailable('capital history original response hash mismatch')
            # Bind the dated row to the specific response page, including
            # multi-page histories. A page-one hash does not bind later pages.
            page_rows = json.loads(response['raw_text'])['result']['data']
            for row in page_rows:
                if row['END_DATE'][:10] == day and row in receipt['rows']:
                    matches.append((receipt, row))
    identities = {json.dumps(row, ensure_ascii=False, sort_keys=True) for _, row in matches}
    if len(identities) != 1:
        raise MarketReferenceUnavailable('reviewed capital requires one exact original response and unambiguous dated row')
    # Repeated authentic captures of identical content are not a conflict.
    # Use the latest receipt; retain all actual observations in the archive.
    history, shares = max(matches, key=lambda item: item[0]['captured_at'])
    quality = _capital_row_quality(shares)
    if not quality['qualified']:
        raise MarketReferenceUnavailable('selected capital snapshot is unqualified: ' + quality['reason'])
    if any(type(shares.get(field)) is not int for field in ('TOTAL_SHARES', 'LISTED_A_SHARES', 'LIMITED_A_SHARES')):
        raise MarketReferenceUnavailable('selected capital snapshot lacks exact integer share counts')
    publications, sources = [], []
    roles = [source['role'] for source in document['filings']]
    if len(roles) != len(set(roles)) or not {'capital', 'current_financial'}.issubset(roles):
        raise ValueError('reviewed filings require distinct roles and capital/current financial sources')
    metadata = [row for receipt in _read(cache, 'announcements', code) for row in receipt['rows']]
    for source in document['filings']:
        found = {json.dumps(row, ensure_ascii=False, sort_keys=True): row for row in metadata
                 if row['id'] == source['announcement_id']}
        if len(found) != 1:
            raise MarketReferenceUnavailable('reviewed financial/capital filing metadata missing or conflicting')
        row = next(iter(found.values()))
        body = (Path(document_directory) / source['pdf_path']).read_bytes()
        if not body.startswith(b'%PDF') or _digest(body) != source['pdf_sha256']:
            raise ValueError('reviewed original filing PDF hash mismatch')
        publication = asof_timestamp(row['publishTime'])
        # Legacy exchange metadata at midnight establishes a date only.
        precision = 'date' if publication.endswith('T00:00:00') else 'second'
        available = publication[:10] + 'T23:59:59' if precision == 'date' else publication
        publications.append(available)
        sources.append(dict(source, source_url='https://disc.static.szse.cn' + row['attachPath'],
            publication_at=publication, publication_precision=precision, available_at=available))
    capital_source = next(source for source in sources if source['role'] == 'capital')
    capital_statements = [row for row in _read(cache, 'capital_statement', code)
        if row['announcement_id'] == capital_source['announcement_id']
        and row['pdf_sha256'] == capital_source['pdf_sha256'] and row['effective_on_or_before'] == day]
    if not capital_statements or any(row['total_shares'] != shares['TOTAL_SHARES'] for row in capital_statements):
        raise MarketReferenceUnavailable('independent capital count does not match reviewed issuer period-end statement')
    # A provider notice date must also have elapsed; a midnight value does not
    # prove the data could be read at the start of that day.
    publications.append(shares['NOTICE_DATE'][:10] + 'T23:59:59')
    profits = []
    for item in document['income_inputs']:
        if (item['filing_role'] not in {source['role'] for source in sources}
                or type(item.get('source_page')) is not int or item['source_page'] <= 0
                or item['source_unit_cny'] not in {1, 1000, 10000, 1000000, 100000000}):
            raise ValueError('income extraction must identify an original filing and page')
        value = Decimal(str(item['source_value'])) * Decimal(str(item['source_unit_cny']))
        if not value.is_finite() or value != value.to_integral_value():
            raise ValueError('reviewed financial extraction must preserve original currency units')
        profits.append({**item, 'end_date': normalize_date(item['end_date']), 'cny': str(value)})
    periods = {row['end_date']: Decimal(row['cny']) for row in profits}
    if len(periods) != len(profits) or not periods:
        raise ValueError('reviewed TTM periods are duplicate or missing')
    latest = max(periods)
    year, suffix = int(latest[:4]), latest[4:]
    required = [latest] if suffix == '1231' else [latest, f'{year-1}1231', f'{year-1}{suffix}']
    if set(periods) != set(required):
        raise ValueError('reviewed TTM requires exact cumulative period bridge')
    with sqlite3.connect(Path(cache).resolve().as_uri() + '?mode=ro', uri=True) as conn:
        for period, value in periods.items():
            rows = conn.execute('SELECT n_income_attr_p FROM income_statement '
                'WHERE ts_code=? AND end_date=? AND report_type=?', (code, period, '1')).fetchall()
            observed = {Decimal(str(row[0])) for row in rows if row[0] is not None}
            if observed != {value}:
                raise MarketReferenceUnavailable('original cumulative income does not corroborate reviewed TTM extraction')
    if any(source['available_at'][:10] < pd.Timestamp(latest).strftime('%Y-%m-%d')
           for source in sources if source['role'] == 'current_financial'):
        raise ValueError('current financial filing precedes its reporting period')
    ttm = periods[latest] if suffix == '1231' else periods[latest] + periods[required[1]] - periods[required[2]]
    equity = document['ordinary_equity']
    if (equity['filing_role'] != 'current_financial' or not equity.get('source_page')
            or equity.get('other_equity_instruments_verified_zero') is not True):
        raise ValueError('PB denominator requires reviewed common equity; missing values are not zero')
    book = Decimal(str(equity['source_value'])) * Decimal(str(equity['source_unit_cny']))
    if not book.is_finite() or book == 0 or book != book.to_integral_value():
        raise ValueError('invalid original common equity extraction')
    if latest > normalize_date(day):
        raise ValueError('financial period is later than capital snapshot')
    subsequent = [row['END_DATE'][:10] for row in history['rows'] if row['END_DATE'][:10] > day]
    through = ((pd.Timestamp(min(subsequent)) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
               if subsequent else history['captured_at'][:10])
    payload = {'source': 'disclosed_reference', 'ts_code': code, 'snapshot_date': day,
        'coverage_through': through,
        'total_shares': shares['TOTAL_SHARES'], 'circulating_a_shares': shares['LISTED_A_SHARES'],
        'provider_row': shares, 'capital_history_sha256': document['capital_history_sha256'],
        'capital_history_captured_at': history['captured_at'],
        'available_at': max(publications), 'filings': sources, 'income_inputs': profits,
        'ttm_parent_profit_cny': str(ttm), 'ordinary_equity_cny': str(book),
        'ordinary_equity': equity, 'financial_period': latest,
        'date_basis': 'reported_period_end_bound; latest_disclosed_capital_view',
        'economic_effective_date': None, 'individual_registration_dates': None,
        'economic_effective_date_verified': False, 'historical_vintages_verified': False,
        'extraction': 'reviewed_original_filings_and_independent_provider_float',
        'original_source_tables_modified': False}
    return record_evidence(cache, 'disclosed_capital_reference', code, payload)


def disclosed_capital_values(cache, date, knowledge_asof, *, strict_pit=False):
    """Return only visible reviewed snapshots, with explicit historical limits."""
    if knowledge_asof is None:
        raise MarketReferenceUnavailable('disclosed capital requires an explicit knowledge timestamp')
    day, cutoff = pd.Timestamp(date).strftime('%Y-%m-%d'), asof_timestamp(knowledge_asof)
    if day > cutoff[:10]:
        raise MarketReferenceUnavailable('capital knowledge timestamp precedes requested data date')
    selected = {}
    for row in _read(cache, 'disclosed_capital_reference'):
        if not row['snapshot_date'] <= day <= row['coverage_through'] or row['available_at'] > cutoff:
            continue
        if strict_pit and row['captured_at'] > cutoff:
            raise MarketReferenceUnavailable('capital_vintage_missing: reviewed history was captured after as_of')
        prior = selected.get(row['ts_code'])
        if prior is None or (row['snapshot_date'], row['captured_at']) > (prior['snapshot_date'], prior['captured_at']):
            selected[row['ts_code']] = row
    return selected
