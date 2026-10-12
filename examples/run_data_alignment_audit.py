"""Run the shared read-only strategy against a real local SDK cache.

Example: .venv/bin/python examples/run_data_alignment_audit.py
    --start 2026-10-08 --end 2026-10-09
All generated outputs stay in the SDK's ignored artifacts/ directory.
"""
from __future__ import annotations
import argparse
import ast
import csv
from datetime import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import sys

EXAMPLES = Path(__file__).resolve().parent
SDK = EXAMPLES.parent
SOURCE = EXAMPLES / 'data_alignment_audit_strategy.py'
OUTPUT_ROOT = SDK / 'artifacts'
sys.path.insert(0, str(SDK))
sys.path.insert(0, str(EXAMPLES))


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def cache_identity(path):
    result = {}
    for suffix in ['', '-wal']:
        target = Path(str(path) + suffix)
        result['database' if not suffix else 'wal'] = ({'sha256': file_sha(target),
            'size': target.stat().st_size} if target.exists() else None)
    return result


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def ledger_counts(directory):
    result = {}
    for name in ['orders.csv', 'transactions.csv']:
        path = Path(directory) / name
        if path.exists():
            with path.open(encoding='utf-8-sig') as handle:
                result[name] = sum(1 for _ in csv.DictReader(handle))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--strategy', type=Path, default=SOURCE,
                        help='shared audit strategy; defaults to examples/data_alignment_audit_strategy.py')
    parser.add_argument('--cache', type=Path, default=SDK / 'data/jq_tushare_cache.db')
    parser.add_argument('--financial-mode', choices=['cached_snapshot', 'research_reconstructed', 'strict_pit'], default='cached_snapshot')
    parser.add_argument('--adjustment-source', choices=['tushare', 'sina_hfq', 'daily_ex_reference'],
                        default=os.environ.get('JQTS_ADJUSTMENT_SOURCE', 'tushare'))
    parser.add_argument('--capital-source', choices=['tushare', 'disclosed_reference'],
                        default=os.environ.get('JQTS_CAPITAL_SOURCE', 'tushare'))
    parser.add_argument('--out', type=Path, help='new directory under the SDK artifacts/ directory')
    parser.add_argument('--prepare', action='store_true', help='automatically prepare permitted source data before the read-only replay')
    args = parser.parse_args()
    os.environ['JQTS_ADJUSTMENT_SOURCE'] = args.adjustment_source
    os.environ['JQTS_CAPITAL_SOURCE'] = args.capital_source
    start, end = [datetime.strptime(x, '%Y-%m-%d').date() for x in [args.start, args.end]]
    if start > end:
        parser.error('start must be on or before end')
    source = args.strategy.resolve(strict=True)
    cache = args.cache.resolve(strict=True)
    out = (args.out or OUTPUT_ROOT / ('data_alignment_' +
            datetime.now().strftime('%Y%m%dT%H%M%S') + '_' + str(os.getpid()))).resolve()
    if not out.is_relative_to(OUTPUT_ROOT.resolve()):
        parser.error('generated outputs must be under the SDK artifacts/ directory')
    out.mkdir(parents=True, exist_ok=False)
    body = source.read_bytes()
    tree = ast.parse(body)
    forbidden = {'order', 'order_target', 'order_value', 'order_target_value',
                 'cancel_order', 'send_redis_signal', 'sendmail', 'send_message'}
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    if forbidden & calls:
        raise ValueError('audit source contains a trading/notification call')
    frozen = out / source.name
    frozen.write_bytes(body)
    source_version = next((n.value.value for n in tree.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in n.targets)
        and isinstance(n.value, ast.Constant)), 'unknown')
    if args.prepare:
        from jq_tushare_sdk.data.daily_sync import DailyDataSync
        sync = DailyDataSync(cache, SDK)
        try:
            preparation = sync.prepare(args.start, args.end, source=body.decode('utf-8'),
                progress=lambda p: print(json.dumps(p,ensure_ascii=False),flush=True))
            write_json(out / 'data-preparation.json', preparation)
        finally:
            sync.close()
    before = cache_identity(cache)
    runtime = {str(p.relative_to(SDK)): file_sha(p)
               for p in sorted((SDK / 'jq_tushare_sdk').rglob('*.py'))}
    import jq_tushare_sdk
    versions = {'runtime_source': jq_tushare_sdk.__version__,
                'installed_distribution': importlib.metadata.version('jq-tushare-sdk'),
                'declared_version_file': (SDK / 'VERSION').read_text().strip()}
    with sqlite3.connect(cache.as_uri() + '?mode=ro', uri=True) as conn:
        days = [r[0] for r in conn.execute('SELECT cal_date FROM trade_calendar '
            'WHERE exchange=? AND is_open=1 AND cal_date BETWEEN ? AND ? ORDER BY cal_date',
            ('SSE', start.strftime('%Y%m%d'), end.strftime('%Y%m%d')))]
    protocol = {'start': args.start, 'end': args.end, 'actual_trade_days': days,
        'financial_mode': args.financial_mode, 'strategy_version': source_version,
        'adjustment_source': args.adjustment_source,
        'capital_source': args.capital_source,
        'capital_historical_vintages_verified': False,
        'adjustment_historical_vintages_verified': False,
        'source': str(source), 'source_sha256': file_sha(source), 'cache': str(cache),
        'cache_before': before, 'runtime_hashes': runtime,
        'sdk_version': versions['runtime_source'], 'sdk_versions': versions,
        'sdk_distribution_matches_runtime': versions['installed_distribution'] == versions['runtime_source'],
        'safety': 'strict_local read-only cache, audit source has zero order calls, SDK backtest Redis guard',
        'scope': 'real historical daily observations; no native JoinQuant log yet'}
    write_json(out / 'protocol.json', protocol)
    if not days:
        parser.exit(2, 'No local exchange sessions in the requested window; protocol saved.\n')
    os.environ['JQTS_SIMULATION_READ_ONLY'] = '1'
    from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
    from jq_tushare_sdk.config import BacktestConfig
    from jq_tushare_sdk.runtime.engine import BacktestEngine
    from compare_data_alignment_logs import MARKER, read_log, compare_logs, write_report
    backend = TushareCacheBackend(str(cache), cache_mode='strict_local', financial_mode=args.financial_mode)
    assert backend.read_only
    config = BacktestConfig(str(frozen), args.start, args.end, 1_000_000, str(cache),
        output_dir=str(out / 'runs'), strategy_version=source_version, financial_mode=args.financial_mode)
    engine = BacktestEngine(config, backend,
        progress_callback=lambda p, label, detail=None: print(label, detail or '', flush=True))
    try:
        manifest = engine.run()
    finally:
        backend.close()
    full_log = manifest.logs_dir / 'backtest.log'
    audit_log = out / 'alignment.log'
    audit_log.write_text(''.join(line for line in full_log.read_text(encoding='utf-8').splitlines(keepends=True)
                               if MARKER in line), encoding='utf-8')
    ledgers = ledger_counts(manifest.trades_dir)
    evidence = read_log(audit_log)
    after = cache_identity(cache)
    result = compare_logs(evidence)
    report = write_report(result, out / 'local_inventory')
    validation = {'log_integrity_problems': evidence['integrity_problems'],
        'expected_snapshots': len(days) * 2, 'actual_snapshots': len(evidence['snapshots']),
        'snapshot_keys': [s['metadata'] for s in evidence['snapshots']],
        'source_unchanged': file_sha(source) == protocol['source_sha256'],
        'source_copy_identical': frozen.read_bytes() == source.read_bytes(),
        'cache_before_after_equal': before == after, 'cache_after': after,
        'runtime_before_after_equal': runtime == {str(p.relative_to(SDK)): file_sha(p)
                        for p in sorted((SDK / 'jq_tushare_sdk').rglob('*.py'))},
        'declared_version_before_after_equal': versions['declared_version_file'] == (SDK / 'VERSION').read_text().strip(),
        'actual_ledger_row_counts': ledgers,
        'section_status_counts': dict(__import__('collections').Counter(p['status']
                for s in evidence['snapshots'] for p in s['sections'].values())),
        'cross_platform_status': result['status'], 'run_manifest': str(manifest.run_dir)}
    expected_keys = {(datetime.strptime(day, '%Y%m%d').date().isoformat(), phase)
                     for day in days for phase in ('before_open', 'after_close')}
    actual_keys = [(s['metadata']['trade_date'], s['metadata']['phase']) for s in evidence['snapshots']]
    validation['missing_expected_snapshots'] = [list(key) for key in sorted(expected_keys - set(actual_keys))]
    validation['unexpected_snapshots'] = [list(key) for key in sorted(set(actual_keys) - expected_keys)]
    validation['duplicate_snapshot_keys'] = [list(key) for key in sorted(set(actual_keys)) if actual_keys.count(key) > 1]
    write_json(out / 'local-validation.json', validation)
    checks = [not evidence['integrity_problems'], len(evidence['snapshots']) == len(days) * 2,
              not validation['missing_expected_snapshots'], not validation['unexpected_snapshots'],
              not validation['duplicate_snapshot_keys'],
              validation['source_unchanged'], validation['cache_before_after_equal'],
              validation['runtime_before_after_equal'], ledgers.get('orders.csv') == 0,
              validation['declared_version_before_after_equal'],
              ledgers.get('transactions.csv') == 0]
    print(json.dumps({'out': str(out), 'alignment_log': str(audit_log), 'report': str(report),
        'snapshots': len(evidence['snapshots']), 'status': result['status'],
        'local_collection_valid': all(checks), 'section_status_counts': validation['section_status_counts']}, ensure_ascii=False), flush=True)
    return 0 if all(checks) else 2


if __name__ == '__main__':
    sys.exit(main())
