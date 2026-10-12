"""Explain real paired audit observations without replacing provider values.

Factor substitution is a causal diagnostic, never an independent aligned feed.
Preserves all decoded sections and unique differences with every occurrence.
"""
from __future__ import annotations
import argparse
from collections import Counter
from html import escape
import json
import math
import hashlib
import re
from pathlib import Path
import sys

from compare_data_alignment_logs import (canonical, compare_logs, read_log,
                                         validate_expected_protocol, _equal)


def provenance_diagnostics(local, reference):
    """Date transitions and cumulative bridges from actual recorded rows only.

    A later matching snapshot is evidence of provider timing, never permission
    to move that value backwards. Implied cash is algebra, not a dividend feed.
    Nullable full financial tables are usable only when bridge inputs exist.
    Multiple native revisions are deliberately left ambiguous.
    """
    capital = {'local': {}, 'reference': {}}
    events, bridges = {}, []
    native_by_context = {(s['metadata']['trade_date'], s['metadata']['phase']): s
                         for s in reference['snapshots']}
    for label, evidence in [('local', local), ('reference', reference)]:
        for snapshot in evidence['snapshots']:
            p = snapshot['sections'].get('capital_structure', {})
            day = p.get('params', {}).get('date')
            if p.get('status') == 'ok' and day:
                for row in p.get('rows', []):
                    capital[label].setdefault((day, row['code']), []).append(row)
    timing = []
    for (day, code), rows in sorted(capital['reference'].items()):
        left = capital['local'].get((day, code), [])
        fields = ['capitalization', 'circulating_cap']
        if (not left or len({canonical(r) for r in rows}) != 1
                or len({canonical(r) for r in left}) != 1):
            continue
        right, first = rows[0], left[0]
        if any(first.get(f) != right.get(f) for f in fields):
            candidates = sorted(d for (d, c), values in capital['local'].items()
                if c == code and d != day and all(values[0].get(f) == right.get(f) for f in fields))
            timing.append({'requested_date': day, 'code': code,
                'local': {f: first.get(f) for f in fields},
                'reference': {f: right.get(f) for f in fields},
                'matching_local_snapshot_dates': candidates,
                'date_shift_applied': False,
                'corporate_action_effective_date_verified': False})
    for snapshot in local['snapshots']:
        context = (snapshot['metadata']['trade_date'], snapshot['metadata']['phase'])
        other = native_by_context.get(context)
        if other is None:
            continue
        for label, item in [('local', snapshot), ('reference', other)]:
            sections = item['sections']
            factors = sections.get('price_factors', {})
            raw = sections.get('daily_raw', {})
            if factors.get('status') == raw.get('status') == 'ok':
                quotes = {(r['code'], r['date']): r for r in raw.get('rows', [])}
                codes = sorted({r['code'] for r in factors.get('rows', [])})
                for code in codes:
                    rows = sorted([r for r in factors['rows'] if r['code'] == code], key=lambda r: r['date'])
                    for before, after in zip(rows, rows[1:]):
                        a, b = before.get('factor'), after.get('factor')
                        close = quotes.get((code, before['date']), {}).get('close')
                        if (not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0
                                    for x in [a, b, close]) or a == b):
                            continue
                        ratio = a / b
                        key = canonical([label, code, before['date'], after['date'], ratio, close])
                        record = events.setdefault(key, {'provider': label, 'code': code,
                            'last_observed_pre_event_date': before['date'], 'first_observed_post_event_date': after['date'],
                            'pre_event_close': close, 'event_scale': ratio,
                            'cash_only_implied_amount': close * (1 - ratio),
                            'is_published_cash_or_verified_event': False, 'occurrences': []})
                        record['occurrences'].append({'date': context[0], 'phase': context[1]})
            for name, current in sections.items():
                if not name.startswith('native_income_') or not name.endswith('-12-31'):
                    continue
                year = name[len('native_income_'):len('native_income_') + 4]
                prior = sections.get('native_income_' + year + '-09-30', {})
                quarter = sections.get('income_detail_' + year + 'q4', {})
                allowed = {'ok', 'partial'}
                for row in quarter.get('rows', []):
                    code = row.get('code')
                    aa = [r for r in current.get('rows', []) if r.get('code') == code]
                    bb = [r for r in prior.get('rows', []) if r.get('code') == code]
                    if not aa or not bb:
                        continue
                    record = {'date': context[0], 'phase': context[1], 'provider': label,
                        'code': code, 'quarter': year + 'q4', 'status': 'ambiguous_revision_rows',
                        'annual_row_count': len(aa), 'prior_row_count': len(bb), 'fields': []}
                    if (len(aa) == len(bb) == 1 and current.get('status') in allowed
                            and prior.get('status') in allowed and quarter.get('status') in allowed):
                        record['status'] = 'checked'
                        for field in ['total_operating_revenue', 'operating_revenue', 'np_parent_company_owners']:
                            inputs = [aa[0].get(field), bb[0].get(field), row.get(field)]
                            valid = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in inputs)
                            calculated = inputs[0] - inputs[1] if valid else None
                            equal = valid and _equal(calculated, inputs[2], field, quarter.get('units', {}), 1e-6, 1e-6)
                            record['fields'].append({'field': field, 'annual_cumulative': inputs[0],
                                'prior_cumulative': inputs[1], 'observed_quarter': inputs[2],
                                'calculated_quarter': calculated, 'equal': bool(equal)})
                            if not valid:
                                record['status'] = 'inputs_missing'
                    bridges.append(record)
    return {'share_snapshot_timing': timing, 'factor_event_algebra': list(events.values()),
            'native_cumulative_to_quarter_bridge': bridges,
            'limits': ['snapshot_date does not prove effective_date or publication availability',
                'implied cash assumes cash-only event; not a verified cash feed',
                'within-provider arithmetic does not certify cross-provider values or historical vintages']}


def analyze(local, reference, report):
    params = {(s['metadata']['trade_date'], s['metadata']['phase'], name): payload.get('params', {})
              for s in local['snapshots'] for name, payload in s['sections'].items()}
    unique = {}
    for section in report['sections']:
        for difference in section.get('differences', []):
            request = params.get((section['date'], section['phase'], section['section']), {})
            query_date = request.get('date', request.get('statDate'))
            signature = canonical([section['section'], query_date, difference])
            entry = unique.setdefault(signature, {'section': section['section'],
                'query_date_or_period': query_date, 'difference': difference, 'occurrences': []})
            entry['occurrences'].append({'date': section['date'], 'phase': section['phase']})
    result = {'status': 'analyzed', 'inputs': report['inputs'],
              'section_counts': report['section_counts'], 'unique_differences': list(unique.values()),
              'limits': ['Reference factor substitution is diagnostic only; it is not a source correction.',
                         'Fixed-basket decisions are not the production strategy or execution.',
                         'Small differences can affect other strategies near their thresholds.']}
    if (report['integrity_problems'] or report['identity_problems'] or report['unpaired_snapshots']):
        result.update(status='invalid_evidence', reason='Integrity/identity/pairing must pass before causal analysis.')
        return result
    refs = {(s['metadata']['trade_date'], s['metadata']['phase'], s['metadata']['config_sha256']): s
            for s in reference['snapshots']}
    factor_checks, decisions = [], []
    for snapshot in local['snapshots']:
        meta = snapshot['metadata']
        key = meta['trade_date'], meta['phase'], meta['config_sha256']
        other = refs[key]
        context = {'date': key[0], 'phase': key[1]}
        left, right = snapshot['sections'], other['sections']
        required = ['daily_raw', 'daily_pre', 'price_factors', 'universe']
        if all(name in left and name in right and left[name]['status'] == right[name]['status'] == 'ok'
               for name in required):
            raw = {(r['code'], r['date']): r for r in left['daily_raw']['rows']}
            factors = {(r['code'], r['date']): r['factor'] for r in right['price_factors']['rows']}
            local_factors = {(r['code'], r['date']): r['factor'] for r in left['price_factors']['rows']}
            stock_codes = {r['code'] for r in left['universe']['rows']}
            record = dict(context, status='checked', field_checks=0, failures=[], changed_factor_rows=[],
                          checked_units={'price': 'CNY_round_2', 'volume': 'shares_round_0'})
            for row in right['daily_pre']['rows']:
                identity = row['code'], row['date']
                if identity not in raw or identity not in factors or row['code'] not in stock_codes:
                    record['status'] = 'incomplete'
                    record['failures'].append({'identity': list(identity), 'reason': 'missing raw/factor/stock identity'})
                    continue
                factor = factors[identity]
                if not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
                    record['status'] = 'incomplete'
                    record['failures'].append({'identity': list(identity), 'reason': 'invalid factor'})
                    continue
                if factor != local_factors.get(identity):
                    record['changed_factor_rows'].append({'identity': list(identity),
                        'local_factor': local_factors.get(identity), 'reference_factor': factor})
                for field in ['open', 'high', 'low', 'close', 'volume']:
                    value = raw[identity][field]
                    if not isinstance(value, (int, float)) or not math.isfinite(value):
                        record['status'] = 'incomplete'
                        record['failures'].append({'identity': list(identity), 'field': field, 'reason': 'invalid raw value'})
                        continue
                    calculated = round(value / factor) if field == 'volume' else round(value * factor, 2)
                    record['field_checks'] += 1
                    if not _equal(calculated, row[field], field, right['daily_pre']['units'],
                                  report['tolerances']['absolute'], report['tolerances']['relative']):
                        record['failures'].append({'identity': list(identity), 'field': field,
                            'diagnostic_value': calculated, 'reference': row[field]})
            factor_checks.append(record)
        else:
            factor_checks.append(dict(context, status='unavailable', reason='raw/pre/factor/stock coverage incomplete',
                                      field_checks=0, failures=[]))
        a, b = left.get('decision_trace', {}), right.get('decision_trace', {})
        record = dict(context, local_status=a.get('status'), reference_status=b.get('status'))
        if a.get('status') == b.get('status') == 'ok':
            aa = {r['code']: r for r in a['rows']}
            bb = {r['code']: r for r in b['rows']}
            complete = (bool(aa) and set(aa) == set(bb) and
                        all(isinstance(r.get('eligible'), bool) for r in [*aa.values(), *bb.values()]) and
                        isinstance(a.get('hypothetical_ranked_codes'), list) and
                        isinstance(b.get('hypothetical_ranked_codes'), list))
            record.update(status='checked' if complete else 'incomplete',
                eligibility_equal=complete and all(aa[c]['eligible'] == bb[c]['eligible'] for c in aa),
                ranks_equal=complete and a['hypothetical_ranked_codes'] == b['hypothetical_ranked_codes'],
                local_rank=a.get('hypothetical_ranked_codes'), reference_rank=b.get('hypothetical_ranked_codes'),
                metric_differences=[{'code': c, 'field': field, 'local': aa[c].get(field), 'reference': bb[c].get(field)}
                    for c in sorted(set(aa) & set(bb)) for field in ['close', 'ma20', 'return_5']
                    if not _equal(aa[c].get(field), bb[c].get(field), field, a.get('units', {}),
                                  report['tolerances']['absolute'], report['tolerances']['relative'])])
        else:
            record['status'] = 'unavailable'
        decisions.append(record)
    result['factor_diagnostic'] = {'contexts': factor_checks,
        'field_checks': sum(r['field_checks'] for r in factor_checks),
        'failed_fields': sum(len(r['failures']) for r in factor_checks),
        'complete': bool(factor_checks) and all(r['status'] == 'checked' for r in factor_checks),
        'feed_is_replaced': False}
    result['decision_impact'] = {'contexts': decisions,
        'complete': bool(decisions) and all(r['status'] == 'checked' for r in decisions),
        'all_eligibility_equal': bool(decisions) and all(r.get('eligibility_equal', False) for r in decisions),
        'all_ranks_equal': bool(decisions) and all(r.get('ranks_equal', False) for r in decisions)}
    result['unique_difference_counts'] = dict(Counter(r['section'] for r in unique.values()))
    result['provenance_diagnostics'] = provenance_diagnostics(local, reference)
    result['root_causes'] = group_root_causes(result, report)
    return result


def group_root_causes(result, report):
    """Group propagation and repeated observations without hiding any values.

    Group labels are causal candidates. An original-filing verdict requires
    independent evidence outside these logs. Unknown patterns stay separate.
    """
    groups = {}
    factor_codes = {d['difference'].get('identity', [None])[0]
                    for d in result['unique_differences'] if d['section'] == 'price_factors'}
    factor_codes.update(row['identity'][0]
        for context in result.get('factor_diagnostic', {}).get('contexts', [])
        for row in context.get('changed_factor_rows', []))
    factor_propagation_checked = (result.get('factor_diagnostic', {}).get('complete') is True
                                  and result['factor_diagnostic']['failed_fields'] == 0)
    for number, entry in enumerate(result['unique_differences']):
        name, diff = entry['section'], entry['difference']
        identity = diff.get('identity') or []
        code = identity[0] if identity else None
        quarter = re.fullmatch(r'income_(?:detail_)?(\d{4})q[1-4]', name)
        if code in factor_codes and (name == 'price_factors' or name == 'daily_pre' and factor_propagation_checked):
            key, label = ('adjustment_factor_rule', code), '复权因子及价格/成交量传播'
            reason = 'Reference-factor algebra reproduces adjusted fields; exact vendor rule still needs independent evidence.'
        elif name in {'capital_structure', 'circulating_cap', 'valuation'}:
            key = ('capital_and_valuation_inputs', code, entry['query_date_or_period'])
            label, reason = '股本快照及估值输入', 'Same security/query date; share date and original financial denominators must be checked independently.'
        elif quarter:
            key = ('income_cumulative_source', code, quarter[1])
            label, reason = '累计财报收入及单季差分', 'Same security/fiscal year; grouping does not decide which source is authoritative.'
        else:
            key, label, reason = ('unclassified', number), '待核查独立差异', 'No established propagation pattern.'
        group = groups.setdefault(key, {'root_id': hashlib.sha256(canonical(key).encode()).hexdigest()[:16],
            'category': key[0], 'label': label, 'code': code, 'basis': reason,
            'verdict': 'requires_independent_source_review', 'unique_field_differences': [], 'section_occurrences': set()})
        group['unique_field_differences'].append(entry)
        for occurrence in entry['occurrences']:
            group['section_occurrences'].add((occurrence['date'], occurrence['phase'], name))
    output = []
    for group in groups.values():
        contexts = sorted(group.pop('section_occurrences'))
        group['different_section_groups'] = len(contexts)
        group['unique_field_count'] = len(group['unique_field_differences'])
        group['section_occurrences'] = [{'date': d, 'phase': p, 'section': n} for d, p, n in contexts]
        output.append(group)
    observed = sum(g['different_section_groups'] for g in output)
    # Several causes could share a section in a broader audit. Reconcile the
    # UNION of occurrences; summing root counts would double count that case.
    union = {(o['date'], o['phase'], o['section']) for g in output for o in g['section_occurrences']}
    expected = {(s['date'], s['phase'], s['section']) for s in report['sections'] if s['status'] == 'different'}
    return {'root_count': len(output), 'unique_field_count': sum(g['unique_field_count'] for g in output),
        'different_section_groups': len(union), 'sum_root_section_groups': observed,
        'all_differences_accounted_for': union == expected,
        'overlapping_root_sections': observed - len(union), 'groups': output,
        'source_verdicts_are_not_inferred_from_reference_values': True}


def write_analysis(result, local, reference, out):
    out.mkdir(parents=True, exist_ok=False)
    (out / 'analysis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for label, evidence in [('local', local), ('joinquant', reference)]:
        with (out / (label + '-decoded.jsonl')).open('w', encoding='utf-8') as stream:
            for snapshot in evidence['snapshots']:
                for name, payload in snapshot['sections'].items():
                    stream.write(canonical({'input_sha256': evidence['sha256'],
                        'snapshot_id': snapshot['snapshot_id'], 'metadata': snapshot['metadata'],
                        'section': name, 'section_sha256': snapshot['section_digests'][name], 'payload': payload}) + '\n')
    factor = result.get('factor_diagnostic', {})
    decision = result.get('decision_impact', {})
    e = lambda value: escape(str(value))
    rows = ''.join('<tr><td>' + e(r['section']) + '</td><td>' + e(r['difference'].get('identity', '')) +
        '</td><td>' + e(r['difference'].get('field', '')) + '</td><td>' + e(r['difference'].get('local')) +
        '</td><td>' + e(r['difference'].get('reference')) + '</td><td>' + e(len(r['occurrences'])) + '</td></tr>'
        for r in result['unique_differences'])
    html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>数据对齐差异诊断</title><style>body{background:#f2f4f7;color:#172535;font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:0}main{max-width:1160px;padding:24px;margin:auto}header{background:#10283f;color:#fff;padding:24px;border-top:4px solid #00a7c4}h1{font-size:26px;margin:0}h2{font-size:20px}table{border-collapse:collapse;width:100%;background:#fff}th,td{border:1px solid #cbd6e1;padding:10px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.scroll{overflow:auto}a{color:#16678b}</style><main><header><h1>真实日志 · 差异与决策影响</h1><p>' + e(result['status']) + '</p></header><p>输入校验通过后，按日期、阶段、配置配对。数值差异保留原值；重复观测合并展示。</p><h2>复权传播诊断</h2><p>已检查 ' + e(factor.get('field_checks', 0)) + ' 个价格/成交量字段；失败 ' + e(factor.get('failed_fields', '未检查')) + ' 个；窗口覆盖完整：' + e(factor.get('complete', False)) + '。</p><p>仅在诊断中将聚宽记录的因子代入本地原价，定位误差来源。该结果不能作为独立行情，也没有写回 SDK。</p><h2>固定样本决策影响</h2><p>窗口完整：' + e(decision.get('complete', False)) + '；筛选结果相同：' + e(decision.get('all_eligibility_equal', False)) + '；排名相同：' + e(decision.get('all_ranks_equal', False)) + '。</p><p>此结论只适用于本次六只样本和 MA20/5 期收益规则，不能推断原生产策略或其他阈值规则。</p><h2>去重字段差异</h2><div class="scroll"><table><tr><th>接口</th><th>股票/日期</th><th>字段</th><th>本地</th><th>聚宽</th><th>观测次数</th></tr>' + rows + '</table></div><h2>证据与边界</h2><pre>' + e(json.dumps({'inputs': result['inputs'], 'limits': result['limits']}, ensure_ascii=False, indent=2)) + '</pre><p><a href="analysis.json">完整诊断 JSON</a> · <a href="local-decoded.jsonl">本地无损解码</a> · <a href="joinquant-decoded.jsonl">聚宽无损解码</a></p></main></html>'
    (out / 'report.html').write_text(html, encoding='utf-8')
    page = (out / 'report.html').read_text(encoding='utf-8')
    panel = '<h2>股本日期 · 除权事件 · 财报累计桥接</h2><p>仅依据实际日志计算；匹配的后续股本不会回填，隐含现金额不会用于复权取数。</p><details><summary>展开完整来源诊断</summary><pre>' + e(json.dumps(result.get('provenance_diagnostics', {}), ensure_ascii=False, indent=2)) + '</pre></details>'
    (out / 'report.html').write_text(page.replace('<h2>证据与边界</h2>', panel + '<h2>证据与边界</h2>'), encoding='utf-8')
    roots = result.get('root_causes', {})
    (out / 'root-causes.json').write_text(json.dumps(roots, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    root_rows = ''.join('<tr><td>' + e(g['label']) + '</td><td>' + e(g['code']) + '</td><td>'
        + e(g['different_section_groups']) + '</td><td>' + e(g['unique_field_count']) + '</td><td>'
        + e(g['verdict']) + '</td></tr>' for g in roots.get('groups', []))
    panel = '<h2>按根因去重</h2><p>' + e(roots.get('different_section_groups')) + ' 个差异分组合并为 '
    panel += e(roots.get('root_count')) + ' 类根因候选，保留 ' + e(roots.get('unique_field_count')) + ' 个独立字段差异及所有出现位置。</p>'
    panel += '<p>完整核算：' + e(roots.get('all_differences_accounted_for')) + '；来源判定需要独立公告佐证。</p><div class="scroll"><table><tr><th>根因</th><th>股票</th><th>分组数</th><th>字段数</th><th>判定</th></tr>' + root_rows + '</table></div><p><a href="root-causes.json">完整根因及逐次位置</a></p>'
    page = (out / 'report.html').read_text(encoding='utf-8')
    (out / 'report.html').write_text(page.replace('<h2>复权传播诊断</h2>', panel + '<h2>复权传播诊断</h2>'), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', type=Path, required=True)
    parser.add_argument('--joinquant', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    local, reference = read_log(args.local), read_log(args.joinquant)
    report = validate_expected_protocol(compare_logs(local, reference), local, reference, args.protocol)
    result = analyze(local, reference, report)
    write_analysis(result, local, reference, args.out)
    print(json.dumps({'status': result['status'], 'report': str((args.out / 'report.html').resolve()),
        'factor_checks': result.get('factor_diagnostic', {}).get('field_checks'),
        'decision_impact': {k: v for k, v in result.get('decision_impact', {}).items() if k != 'contexts'}}, ensure_ascii=False))
    return 2 if result['status'] == 'invalid_evidence' else 0


if __name__ == '__main__':
    sys.exit(main())
