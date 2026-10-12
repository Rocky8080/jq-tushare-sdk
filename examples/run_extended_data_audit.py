"""Audit real cached prices and observed native forecasts over a wider basket.

No synthetic market data, network fallback, orders or source-value rewriting.
This is local validation, not a substitute for a paired native JoinQuant run.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

SDK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK))
from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.data.portal import DataPortal
from jq_tushare_sdk.data.code_map import to_joinquant_code, to_tushare_code
from jq_tushare_sdk.data.market_reference import _read, _capital_row_quality
from jq_tushare_sdk.data.native_forecast import native_forecast_readiness, inspect_native_forecast_archive

DEFAULT_CODES = ['000001.SZ', '000002.SZ', '000333.SZ', '000651.SZ',
    '002274.SZ', '002475.SZ', '002594.SZ', '002050.SZ', '002460.SZ',
    '300750.SZ', '300059.SZ', '300502.SZ', '300014.SZ', '300122.SZ', '300015.SZ',
    '600000.SH', '600519.SH', '600036.SH', '600030.SH', '600276.SH', '600900.SH',
    '601318.SH', '601166.SH', '601288.SH', '601398.SH',
    '688001.SH', '688981.SH', '688012.SH', '688041.SH', '688008.SH', '688256.SH', '600111.SH']


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(piece)
    return digest.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def ordered(frame):
    return frame.sort_values(['time', 'code']).reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--codes', default=','.join(DEFAULT_CODES))
    parser.add_argument('--sources', default='tushare,sina_hfq,daily_ex_reference')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if pd.Timestamp(args.start) > pd.Timestamp(args.end):
        parser.error('--start must not be after --end')
    cache = args.cache.resolve(strict=True)
    out = args.out.resolve()
    if not out.is_relative_to((SDK / 'artifacts').resolve()):
        parser.error('--out must be a new directory under this SDK artifacts directory')
    out.mkdir(parents=True, exist_ok=False)
    codes = sorted(set(to_joinquant_code(code.strip()) for code in args.codes.split(',') if code.strip()))
    sources = list(dict.fromkeys(args.sources.split(',')))
    if not codes or any(s not in {'tushare', 'sina_hfq', 'daily_ex_reference'} for s in sources):
        parser.error('explicit valid codes and sources are required')
    os.environ['JQTS_SIMULATION_READ_ONLY'] = '1'
    before = file_sha(cache)
    runtime = {str(p.relative_to(SDK)): file_sha(p) for p in sorted((SDK / 'jq_tushare_sdk').rglob('*.py'))}
    results, frames, failures = [], {}, []
    fields = ['open', 'high', 'low', 'close', 'volume', 'money', 'factor']
    raw = None
    expected = None
    for source in sources:
        pair = []
        for optimized in (False, True):
            backend = TushareCacheBackend(str(cache), cache_mode='strict_local', adjustment_source=source)
            portal = DataPortal(backend, optimize_data=optimized)
            days = [str(day) for day in portal.get_trade_days(start_date=args.start, end_date=args.end)]
            expected = len(days) * len(codes)
            try:
                if not days:
                    raise ValueError('No trading sessions in the requested window; no observations verified')
                if raw is None:
                    raw = ordered(portal.get_price(codes, start_date=args.start, end_date=args.end,
                        fields=fields, fq=None, fill_paused=True, panel=False))
                    raw.to_csv(out / 'raw.csv', index=False)
                frame = ordered(portal.get_price(codes, start_date=args.start, end_date=args.end,
                    fields=fields, fq='pre', fill_paused=True, panel=False))
                numeric = frame[fields].to_numpy(dtype=float)
                quality = {'source': source, 'optimized': optimized, 'rows': len(frame),
                    'expected_rows': expected, 'unique_rows': not frame.duplicated(['time', 'code']).any(),
                    'all_expected_identities_present':
                        set(zip(frame['time'].astype(str).str[:10], frame['code'])) ==
                        {(str(day)[:10], code) for day in days for code in codes},
                    'all_fields_finite': bool(np.isfinite(numeric).all()),
                    'positive_prices_factors': bool((frame[['open','high','low','close','factor']] > 0).all().all()),
                    'nonnegative_volume_money': bool((frame[['volume','money']] >= 0).all().all()),
                    'ohlc_order_valid': bool(((frame.high >= frame[['open','close']].max(axis=1)) &
                                             (frame.low <= frame[['open','close']].min(axis=1))).all()),
                    'confirmed_no_trade_rows': int((frame.volume == 0).sum())}
                quality = {k: (v.item() if hasattr(v, 'item') else v) for k, v in quality.items()}
                frame.to_csv(out / f'{source}-{optimized}.csv', index=False)
                save(out / f'{source}-{optimized}-provenance.json', frame.attrs)
                pair.append(frame)
                results.append(quality)
                if len(frame) != expected or not all(quality[k] for k in ('unique_rows','all_expected_identities_present','all_fields_finite',
                        'positive_prices_factors','nonnegative_volume_money','ohlc_order_valid')):
                    failures.append({'source':source,'optimized':optimized,'quality':quality})
            except Exception as error:
                failures.append({'source':source,'optimized':optimized,'error':str(error),'type':type(error).__name__})
            finally:
                backend.close()
        equal = len(pair) == 2 and pair[0].equals(pair[1])
        results.append({'source':source,'ordinary_optimized_equal':equal})
        if not equal:
            failures.append({'source':source,'reason':'ordinary_optimized_differ_or_incomplete'})
        if pair:
            frames[source] = pair[0]
        print(json.dumps({'source':source,'ordinary_optimized_equal':equal,'failures_so_far':len(failures)}), flush=True)
    differences = []
    for source, frame in frames.items():
        base = frames.get('tushare')
        if base is None or source == 'tushare':
            continue
        if not frame[['time','code']].equals(base[['time','code']]):
            differences.append({'source':source,'coverage_mismatch':True})
            continue
        delta = abs(frame[fields] - base[fields])
        differences.append({'source':source, 'versus':'tushare',
            'different_numeric_observations':{k:int((delta[k] > 1e-6).sum()) for k in fields},
            'maximum_absolute_difference':{k:float(delta[k].max()) for k in fields},
            'interpretation':'Different provider/rule observations; no native correctness verdict.'})
    capital = []
    for code in codes:
        history = _read(cache, 'capital_history', to_tushare_code(code))
        if not history:
            capital.append({'code':code,'status':'source_not_prepared'})
            continue
        receipt = history[-1]
        relevant = [row for row in receipt['rows'] if args.start <= row['END_DATE'][:10] <= args.end]
        capital.append({'code':code,'captured_at':receipt['captured_at'],'rows':len(receipt['rows']),
            'unqualified_rows':len(receipt.get('unqualified_rows',[])),
            'window_rows':[{'date':row['END_DATE'][:10], 'notice_date':row.get('NOTICE_DATE'),
                'total_shares':row.get('TOTAL_SHARES'),'circulating_a_shares':row.get('LISTED_A_SHARES'),
                'quality':_capital_row_quality(row),'individual_registration_date':None} for row in relevant]})
    now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    native_end = pd.Timestamp(args.end).date().isoformat()
    native_start = (pd.Timestamp(args.end) - pd.Timedelta(days=90)).date().isoformat()
    native = native_forecast_readiness(cache, native_start, native_end, codes=codes, decision_at=now)
    inspected = inspect_native_forecast_archive(cache, native_start, native_end, codes=codes) if native['ready'] else None
    after = file_sha(cache)
    current_runtime = {str(p.relative_to(SDK)): file_sha(p) for p in sorted((SDK / 'jq_tushare_sdk').rglob('*.py'))}
    summary = {'interval':[args.start,args.end],'stocks':len(codes),'codes':codes,
        'trade_days':expected // len(codes) if expected is not None else None,
        'expected_price_rows_per_view':expected,'checks':results,'failures':failures,
        'source_differences':differences,'native_current_readiness':native,
        'native_archive_sample_rows':len(inspected) if inspected is not None else None,
        'native_historical_parity_verified':False,'historical_vintages_verified':False,
        'capital_history':capital,'cache_before_sha256':before,'cache_after_sha256':after,
        'cache_unchanged':before == after,'runtime_unchanged':runtime == current_runtime,
        'runtime_hashes':runtime,'orders':0,'scope':'Read-only real SDK API probes; native expanded pairing remains required.'}
    save(out / 'summary.json', summary)
    print(json.dumps({'summary':str(out/'summary.json'),'stocks':len(codes),'trade_days':summary['trade_days'],
        'failures':len(failures),'cache_unchanged':before == after}), flush=True)
    return 0 if not failures and before == after and runtime == current_runtime else 2


if __name__ == '__main__':
    raise SystemExit(main())
