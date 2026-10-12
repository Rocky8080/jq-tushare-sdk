"""Decode and compare real JQALIGN1 logs; truncated evidence cannot pass.

No provider mapping, unit rescaling, forward filling or zero filling is used.
Outputs are immutable per invocation and include original input hashes.
"""
from __future__ import annotations
import argparse
import base64
from collections import Counter
import csv
import hashlib
from html import escape, unescape
import json
import math
from pathlib import Path
import sys

PROTOCOL = 'jq-data-alignment/1'
MARKER = 'JQALIGN1 '
BLOCKED = {'error', 'unsupported', 'partial', 'blocked', 'empty', 'disabled'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _key(snapshot):
    m = snapshot['metadata']
    return m['trade_date'], m['phase'], m['config_sha256']


def _decode_envelope(line):
    candidate = line.split(MARKER, 1)[1].lstrip()
    try:
        return json.JSONDecoder().raw_decode(candidate)[0]
    except json.JSONDecodeError:
        # JoinQuant's page export can HTML-escape the JSON container. Decode
        # one layer only after plain JSON fails; payload hashes still validate.
        if '&quot;' in candidate or '&#' in candidate:
            try:
                return json.JSONDecoder().raw_decode(unescape(candidate))[0]
            except json.JSONDecodeError:
                pass
        # Native CSV exports may double embedded quotes. Parse the CSV container,
        # rather than replacing quotes inside data indiscriminately.
        for cell in next(csv.reader([line])):
            if MARKER in cell:
                return json.JSONDecoder().raw_decode(cell.split(MARKER, 1)[1].lstrip())[0]
        raise ValueError('malformed JSON envelope')


def read_log(path):
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) > 128 * 1024 * 1024:
        raise ValueError('log exceeds 128 MiB; split exported periods first')
    encoding = 'utf-8-sig'
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = 'gb18030'
        text = raw.decode(encoding)
    frames, problems = {}, []
    for number, line in enumerate(text.splitlines(), 1):
        if MARKER not in line:
            continue
        try:
            r = _decode_envelope(line)
            if not isinstance(r, dict) or r.get('protocol') != PROTOCOL:
                raise ValueError('unknown log protocol')
            sid = r['snapshot_id']
            if not isinstance(sid, str) or len(sid) > 128:
                raise ValueError('invalid snapshot id')
            kind = r['kind']
            frame = frames.setdefault(sid, {'begin': None, 'end': None, 'parts': {}})
            if kind in ('begin', 'end'):
                if frame[kind] is not None and frame[kind] != r:
                    raise ValueError('conflicting repeated ' + kind)
                frame[kind] = r
            elif kind == 'part':
                if not isinstance(r['part'], int) or not isinstance(r['parts'], int):
                    raise ValueError('fragment indexes must be integers')
                if not 0 <= r['part'] < r['parts'] <= 10000:
                    raise ValueError('invalid fragment count')
                name = r['section']
                if not isinstance(name, str) or not isinstance(r['payload'], str):
                    raise ValueError('invalid fragment body')
                parts = frame['parts'].setdefault(name, {})
                if r['part'] in parts and parts[r['part']] != r:
                    raise ValueError('conflicting fragment bytes')
                parts[r['part']] = r
            else:
                raise ValueError('unknown envelope kind')
        except (ValueError, KeyError, TypeError, StopIteration) as exc:
            problems.append({'line': number, 'problem': str(exc)})
    snapshots = []
    for sid, f in frames.items():
        try:
            if f['begin'] is None or f['end'] is None:
                raise ValueError('incomplete snapshot: begin/end absent')
            meta = f['begin']['metadata']
            names = meta['sections']
            if len(set(names)) != len(names) or set(f['parts']) != set(names):
                raise ValueError('section manifest missing, duplicated or has extra sections')
            sections, digests = {}, {}
            for name in names:
                pieces = f['parts'][name]
                first = next(iter(pieces.values()))
                total = first['parts']
                if set(pieces) != set(range(total)):
                    raise ValueError('missing fragment in ' + name)
                if any(r['parts'] != total or r['sha256'] != first['sha256'] for r in pieces.values()):
                    raise ValueError('fragment manifest conflicts in ' + name)
                joined = ''.join(pieces[i]['payload'] for i in range(total))
                body = base64.b64decode(joined, validate=True).decode('utf-8')
                if sha(body) != first['sha256']:
                    raise ValueError('section digest mismatch: ' + name)
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get('rows'), list):
                    raise ValueError('invalid section schema: ' + name)
                if not isinstance(payload.get('keys'), list) or payload.get('status') not in BLOCKED | {'ok'}:
                    raise ValueError('unknown section status or keys: ' + name)
                sections[name], digests[name] = payload, first['sha256']
            if f['end']['section_count'] != len(names):
                raise ValueError('end section count does not match')
            if sha(canonical({'metadata': meta, 'sections': digests})) != f['end']['sha256']:
                raise ValueError('snapshot digest mismatch')
            cfg = sections['configuration']['config']
            if sha(canonical(cfg)) != meta['config_sha256']:
                raise ValueError('configuration digest mismatch')
            for field in ['trade_date', 'phase', 'config_sha256', 'provider']:
                if not isinstance(meta.get(field), str):
                    raise ValueError('invalid metadata ' + field)
            snapshots.append({'snapshot_id': sid, 'metadata': meta, 'sections': sections,
                              'section_digests': digests})
        except (ValueError, KeyError, TypeError, StopIteration, UnicodeDecodeError) as exc:
            problems.append({'snapshot_id': sid, 'problem': str(exc)})
    # Identical exports/replayed lines may repeat evidence. Changing observations
    # under the same comparison key are ambiguous and are never picked silently.
    grouped = {}
    for snapshot in snapshots:
        key = _key(snapshot)
        if key in grouped and grouped[key]['section_digests'] != snapshot['section_digests']:
            problems.append({'alignment_key': list(key), 'problem': 'conflicting observations; export one run per provider/date/phase'})
            grouped[key]['ambiguous'] = True
        else:
            grouped.setdefault(key, snapshot)
    snapshots = [s for s in grouped.values() if not s.get('ambiguous')]
    if not frames:
        problems.append({'problem': 'no JQALIGN1 evidence found'})
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
            'encoding': encoding, 'snapshots': snapshots, 'integrity_problems': problems}


def _rows(payload):
    keys = payload['keys']
    result = {}
    for row in payload['rows']:
        key = tuple(row.get(k) for k in keys)
        if not keys or None in key or key in result:
            raise ValueError('missing or duplicate row identity: ' + str(key))
        result[key] = row
    return result


def _equal(a, b, field, units, abs_tol, rel_tol):
    if a is None or b is None:
        # Absence on both sides is missing evidence, not equal market values.
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if not math.isfinite(a) or not math.isfinite(b):
            return False
        absolute = abs_tol
        if units.get(field) in {'shares', 'CNY'} and field in {'volume', 'money'}:
            absolute = max(abs_tol, 1.0)
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=absolute)
    return type(a) is type(b) and a == b


def compare_section(name, left, right, abs_tol, rel_tol):
    result = {'section': name, 'local_status': left['status'], 'reference_status': right['status'],
              'local_rows': len(left['rows']), 'reference_rows': len(right['rows']),
              'differences': [], 'matched_fields': 0}
    if not left.get('comparable', True) or not right.get('comparable', True):
        result.update(status='context_only', reason='provider metadata or quote domains are informational')
        return result
    if left['status'] == 'disabled' or right['status'] == 'disabled':
        result.update(status='not_enabled', reason='minute probes are opt-in; not certified')
        return result
    if left['status'] in {'unsupported', 'error', 'blocked'} or right['status'] in {'unsupported', 'error', 'blocked'}:
        result.update(status='blocked', reason={'local': left.get('error', left.get('reason')),
                                               'reference': right.get('error', right.get('reason'))})
        return result
    for field in ['params', 'keys', 'units']:
        if left.get(field) != right.get(field):
            result['differences'].append({'field': field, 'local': left.get(field), 'reference': right.get(field), 'kind': 'contract_mismatch'})
    if result['differences']:
        result['status'] = 'incompatible'
        return result
    try:
        a, b = _rows(left), _rows(right)
    except ValueError as exc:
        result.update(status='incompatible', reason=str(exc))
        return result
    for key in sorted(set(a) | set(b), key=str):
        if key not in a or key not in b:
            result['differences'].append({'identity': list(key), 'kind': 'missing_row',
                                          'local': a.get(key), 'reference': b.get(key)})
            continue
        for field in sorted(set(a[key]) | set(b[key])):
            if field in left['keys']:
                continue
            av, bv = a[key].get(field), b[key].get(field)
            if _equal(av, bv, field, left['units'], abs_tol, rel_tol):
                result['matched_fields'] += 1
            else:
                row = {'identity': list(key), 'field': field, 'local': av, 'reference': bv,
                       'kind': 'missing_value' if av is None or bv is None else 'value_difference'}
                if isinstance(av, (int, float)) and isinstance(bv, (int, float)) and not isinstance(av, bool) and not isinstance(bv, bool):
                    row['absolute_difference'] = abs(av - bv)
                result['differences'].append(row)
    for field in ['universe_count', 'universe_codes_sha256', 'context_previous_date',
                  'calendar_previous_date', 'hypothetical_ranked_codes', 'orders_submitted']:
        if left.get(field) != right.get(field):
            result['differences'].append({'field': field, 'local': left.get(field),
                                         'reference': right.get(field), 'kind': 'summary_difference'})
    if left['status'] in {'partial', 'empty'} or right['status'] in {'partial', 'empty'}:
        result['status'] = 'coverage_gap'
    elif result['differences']:
        result['status'] = 'different'
    elif not a and not b:
        result['status'] = 'no_observations'
    else:
        result['status'] = 'matched'
    return result


def compare_logs(local, reference=None, abs_tol=1e-6, rel_tol=1e-6):
    rows, unpaired, identity_problems = [], [], []
    integrity = list(local['integrity_problems'])
    if reference is None:
        status = 'awaiting_joinquant_log'
        for s in local['snapshots']:
            for name, payload in s['sections'].items():
                rows.append({'date': s['metadata']['trade_date'], 'phase': s['metadata']['phase'],
                             'section': name, 'status': 'local_only',
                             'local_status': payload['status'], 'local_rows': len(payload['rows']),
                             'reason': payload.get('error', payload.get('reason')), 'differences': []})
        if integrity:
            status = 'invalid_evidence'
    else:
        integrity.extend(reference['integrity_problems'])
        a, b = {_key(s): s for s in local['snapshots']}, {_key(s): s for s in reference['snapshots']}
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                unpaired.append({'date': key[0], 'phase': key[1], 'config_sha256': key[2],
                                 'missing_side': 'local' if key not in a else 'joinquant'})
                continue
            lm, rm = a[key]['metadata'], b[key]['metadata']
            if lm['provider'] != 'jq-tushare-sdk' or rm['provider'] != 'joinquant':
                identity_problems.append({'date': key[0], 'phase': key[1],
                    'problem': 'expected independently captured jq-tushare-sdk and joinquant logs',
                    'local_provider': lm['provider'], 'reference_provider': rm['provider']})
            if any(lm.get(f) != rm.get(f) for f in ['data_date', 'previous_trade_date', 'context_time', 'timezone', 'version', 'read_only']):
                identity_problems.append({'date': key[0], 'phase': key[1], 'problem': 'date/version/cutoff mismatch'})
            for name in sorted(set(a[key]['sections']) | set(b[key]['sections'])):
                if name not in a[key]['sections'] or name not in b[key]['sections']:
                    row = {'section': name, 'status': 'incompatible', 'differences': [], 'reason': 'section absent'}
                else:
                    row = compare_section(name, a[key]['sections'][name], b[key]['sections'][name], abs_tol, rel_tol)
                row.update(date=key[0], phase=key[1])
                rows.append(row)
        if integrity or identity_problems:
            status = 'invalid_evidence'
        elif unpaired or any(r['status'] in {'blocked', 'coverage_gap', 'incompatible', 'no_observations'} for r in rows):
            status = 'incomplete'
        elif any(r['status'] == 'different' for r in rows):
            status = 'differences_found'
        elif rows and any(r['status'] == 'matched' for r in rows):
            status = 'matched_enabled_observations'
        else:
            status = 'insufficient_evidence'
    return {'protocol': PROTOCOL, 'status': status,
            'tolerances': {'absolute': abs_tol, 'relative': rel_tol,
                           'volume_money_absolute_floor': 1.0},
            'inputs': [{'path': x['path'], 'sha256': x['sha256'], 'encoding': x['encoding']}
                       for x in [local, reference] if x is not None],
            'local_snapshots': len(local['snapshots']),
            'joinquant_snapshots': len(reference['snapshots']) if reference else 0,
            'integrity_problems': integrity, 'identity_problems': identity_problems,
            'unpaired_snapshots': unpaired, 'section_counts': dict(Counter(r['status'] for r in rows)),
            'sections': rows, 'limits': ['A matched sample does not establish full-market or PIT/native engine equivalence.',
                'Disabled minute probes and contextual quote domains are not certified.',
                'No fill, return, account or production strategy equivalence is asserted.']}


def validate_expected_protocol(report, local, reference, protocol_path):
    """Use independent expected sessions so whole-snapshot omissions are visible."""
    path = Path(protocol_path)
    protocol = json.loads(path.read_text(encoding='utf-8'))
    days = protocol.get('actual_trade_days', [])
    if not days:
        raise ValueError('protocol must contain actual_trade_days from the frozen local run')
    expected = {(str(day)[:4] + '-' + str(day)[4:6] + '-' + str(day)[6:8], phase)
                for day in days for phase in ('before_open', 'after_close')}
    problems = []
    for label, evidence in [('local', local), ('joinquant', reference)]:
        if evidence is None:
            continue
        actual = [(s['metadata']['trade_date'], s['metadata']['phase']) for s in evidence['snapshots']]
        for kind, keys in [('missing_expected_snapshot', expected - set(actual)),
                           ('unexpected_snapshot', set(actual) - expected)]:
            problems.extend({'input': label, 'problem': kind, 'date': day, 'phase': phase}
                            for day, phase in sorted(keys))
        problems.extend({'input': label, 'problem': 'duplicate_expected_snapshot',
                         'date': day, 'phase': phase} for day, phase in sorted(set(actual))
                        if actual.count((day, phase)) > 1)
        if protocol.get('strategy_version') and any(s['metadata']['version'] != protocol['strategy_version']
                                                   for s in evidence['snapshots']):
            problems.append({'input': label, 'problem': 'protocol_strategy_version_mismatch'})
    report['expected_protocol'] = {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                   'expected_snapshots': len(expected), 'problems': problems}
    report['integrity_problems'].extend(problems)
    if problems:
        report['status'] = 'invalid_evidence'
    return report


def write_report(report, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    headers = ['date', 'phase', 'section', 'status', 'identity', 'field', 'kind', 'local', 'reference', 'absolute_difference']
    with (out / 'differences.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, headers)
        writer.writeheader()
        for section in report['sections']:
            for diff in section.get('differences', []):
                row = {k: section.get(k, '') for k in headers[:4]}
                row.update({k: json.dumps(diff[k], ensure_ascii=False) if isinstance(diff.get(k), (dict, list)) else diff.get(k, '') for k in headers[4:]})
                writer.writerow(row)
    labels = {'awaiting_joinquant_log': '等待聚宽日志；尚未做跨平台对账',
              'invalid_evidence': '证据不完整或来源不独立，不能判定对齐',
              'incomplete': '存在数据或接口覆盖缺口', 'differences_found': '已发现数据差异',
              'matched_enabled_observations': '已启用的样本观测在容差内一致',
              'insufficient_evidence': '证据不足'}
    phase_labels = {'before_open': '盘前 09:00', 'after_close': '收盘后 15:30'}
    state_labels = {'local_only': '等待聚宽记录', 'matched': '容差内一致',
        'context_only': '仅作环境参考', 'not_enabled': '未启用', 'blocked': '接口阻塞',
        'incompatible': '口径不兼容', 'coverage_gap': '覆盖不足', 'different': '有差异',
        'no_observations': '缺少观测', 'ok': '已返回', 'partial': '部分缺失',
        'unsupported': '暂不支持', 'error': '调用异常', 'empty': '无记录', 'disabled': '未启用'}
    section_labels = {'configuration': '版本和环境', 'calendar': '交易日历', 'universe': '股票列表和基础信息',
        'industry': '申万行业', 'valuation': 'PE、PB、总市值', 'circulating_cap': '流通市值',
        'capital_structure': '总股本和流通股本',
        'income_visible': '按日期查询可见财报', 'income_report_dates': '财报发布日和报告期',
        'daily_pre': '前复权日线', 'daily_raw': '不复权日线', 'price_factors': '复权因子', 'index_daily': '指数日线',
        'security_state': 'ST 和停牌状态', 'quote_context': '当前行情环境参考',
        'native_forecast': '原生业绩预告', 'minute_open': '开盘分钟行情', 'minute_close': '尾盘分钟行情',
        'native_forecast_detail': '原生预告完整字段（诊断，不认证映射）',
        'decision_trace': '样本虚拟选股决策'}
    for r in report['sections']:
        if r['section'].startswith('income_detail_'):
            section_labels[r['section']] = '收入/净利润明细 · ' + r['section'][len('income_detail_'):]
        if r['section'].startswith('native_income_'):
            section_labels[r['section']] = '原生累计财报/修订（诊断） · ' + r['section'][len('native_income_'):]
    e = lambda x: escape(str(x))
    table = ''.join('<tr><td>' + e(r.get('date', '')) + '</td><td>' + e(phase_labels.get(r.get('phase'), r.get('phase', ''))) +
                    '</td><td>' + e(section_labels.get(r['section'], r['section'])) + '</td><td>' + e(state_labels.get(r['status'], r['status'])) +
                    '</td><td>' + e(state_labels.get(r.get('local_status'), r.get('local_status', ''))) + ' / ' + e(state_labels.get(r.get('reference_status'), r.get('reference_status', '未提供'))) +
                    '</td><td>' + e(r.get('local_rows', '')) + ' / ' + e(r.get('reference_rows', '')) +
                    '</td><td>' + e(r.get('reason', '')) + '</td></tr>' for r in report['sections'])
    details = ''.join('<details><summary>' + e(r.get('date', '')) + ' ' + e(r.get('phase', '')) +
                      ' · ' + e(r['section']) + ' · ' + str(len(r['differences'])) +
                      ' 个差异</summary><pre>' + e(json.dumps(r['differences'], ensure_ascii=False, indent=2)) +
                      '</pre></details>' for r in report['sections'] if r.get('differences'))
    problems = report['integrity_problems'] + report['identity_problems'] + report['unpaired_snapshots']
    body = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>两平台每日数据对账</title><style>body{background:#f3f5f7;color:#172535;font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:0}main{max-width:1250px;margin:30px auto;padding:0 20px}header{background:#10283f;color:white;padding:28px;border-top:4px solid #18a0cf}h1{font-size:25px;margin:0}h2{font-size:19px;margin-top:28px}table{border-collapse:collapse;background:white;width:100%}td,th{padding:9px 12px;border:1px solid #d1dbe4;text-align:left;vertical-align:top}th{background:#e5edf3}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}details{background:white;padding:12px;margin:10px 0}summary{cursor:pointer}.scroll{overflow:auto}a{color:#136797}</style><main><header><h1>每日数据对账 · ' + e(labels[report['status']]) + '</h1><p>本地 ' + str(report['local_snapshots']) + ' 组 · 聚宽 ' + str(report['joinquant_snapshots']) + ' 组快照</p></header><p>按交易日、盘前/收盘阶段、配置校验值配对。缺失数据不补零，单位不自动换算；日志缺片、校验失败和同来源自比较均不能通过跨平台验收。</p><h2>接口与数据覆盖</h2><div class="scroll"><table><tr><th>交易日</th><th>阶段</th><th>内容</th><th>对账状态</th><th>本地/聚宽接口状态</th><th>行数</th><th>说明</th></tr>' + table + '</table></div><h2>具体差异</h2>' + (details or '<p>暂无双端字段比较结果。</p>') + '<h2>日志完整性与配对问题</h2><pre>' + e(json.dumps(problems, ensure_ascii=False, indent=2)) + '</pre><h2>口径与证据</h2><p>仅记录数据及样本虚拟决策，无真实下单、Redis 或通知。样本一致不能证明全市场、历史 PIT、真实成交或原生产策略收益一致。</p><pre>' + e(json.dumps({'inputs': report['inputs'], 'tolerances': report['tolerances']}, ensure_ascii=False, indent=2)) + '</pre><p><a href="comparison.json">结构化结果</a> · <a href="differences.csv">字段差异 CSV</a></p></main></html>'
    (out / 'report.html').write_text(body, encoding='utf-8')
    return out / 'report.html'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', type=Path, required=True)
    parser.add_argument('--joinquant', type=Path)
    parser.add_argument('--out', type=Path, required=True, help='new directory; never overwrite prior evidence')
    parser.add_argument('--abs-tol', type=float, default=1e-6)
    parser.add_argument('--rel-tol', type=float, default=1e-6)
    parser.add_argument('--protocol', type=Path, help='frozen local protocol.json; checks each expected day and phase')
    args = parser.parse_args()
    if any(not math.isfinite(x) or x < 0 for x in [args.abs_tol, args.rel_tol]):
        parser.error('tolerances must be finite and nonnegative')
    try:
        local = read_log(args.local)
        reference = read_log(args.joinquant) if args.joinquant else None
        result = compare_logs(local, reference, args.abs_tol, args.rel_tol)
        if args.protocol:
            result = validate_expected_protocol(result, local, reference, args.protocol)
        path = write_report(result, args.out)
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps({'status': result['status'], 'report': str(path.resolve()),
                      'section_counts': result['section_counts']}, ensure_ascii=False))
    return 0 if result['status'] in {'matched_enabled_observations', 'awaiting_joinquant_log'} else 2


if __name__ == '__main__':
    sys.exit(main())
