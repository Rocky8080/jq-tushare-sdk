"""Prepare real supplementary evidence before an offline strategy replay.

Only explicit sources are fetched; raw Tushare factors/financials are retained.
Example: .venv/bin/python examples/prepare_market_reference_data.py
    --codes 300750.XSHE,000001.XSHE --precise-factors
    --announcements --start 2026-08-01 --end 2026-09-08
"""
import argparse
import json
from pathlib import Path
import sys

SDK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK))
from jq_tushare_sdk.data.market_reference import (refresh_precise_factors,
    sync_szse_announcements, import_capital_statement, refresh_capital_history,
    import_disclosed_capital_reference, prepare_reference_price_factors,
    import_capital_registration_notice)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=SDK / 'data/jq_tushare_cache.db')
    parser.add_argument('--codes', required=True)
    parser.add_argument('--precise-factors', action='store_true')
    parser.add_argument('--announcements', action='store_true')
    parser.add_argument('--capital-history', action='store_true')
    parser.add_argument('--reference-price-factors', action='store_true',
                        help='explicit research rule using actual rounded ex-day reference quotes')
    parser.add_argument('--disclosed-capital-reference', type=Path,
                        help='reviewed original financial/capital extraction JSON; never native comparison values')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--capital-statement', type=Path,
                        help='reviewed JSON containing code, statement and pdf_path')
    parser.add_argument('--capital-registration-notice', type=Path,
                        help='reviewed JSON containing code, notice and pdf_path; pre-registration is distinct')
    args = parser.parse_args()
    args.cache.resolve(strict=True)
    codes = [code.strip() for code in args.codes.split(',') if code.strip()]
    if not codes or not any([args.precise_factors, args.announcements, args.capital_statement,
                             args.capital_history, args.disclosed_capital_reference, args.reference_price_factors,
                             args.capital_registration_notice]):
        parser.error('explicit codes and at least one source action are required')
    if (args.announcements or args.reference_price_factors) and (not args.start or not args.end):
        parser.error('source preparation interval requires --start and --end')
    result = {}
    if args.reference_price_factors:
        result['reference_price_factors'] = prepare_reference_price_factors(args.cache, codes, args.start, args.end)
    if args.capital_history:
        result['capital_history'] = refresh_capital_history(args.cache, codes)
    if args.precise_factors:
        result['factors'] = refresh_precise_factors(args.cache, codes,
            progress=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True))
    if args.announcements:
        receipts = sync_szse_announcements(args.cache, codes, args.start, args.end)
        result['announcements'] = [{'captured_at': row['captured_at'], 'rows': len(row['rows']),
                                    'raw_sha256': row['raw_sha256']} for row in receipts]
    if args.capital_statement:
        value = json.loads(args.capital_statement.read_text(encoding='utf-8'))
        if value['code'] not in codes:
            parser.error('capital statement code must be in --codes')
        pdf = args.capital_statement.parent / value['pdf_path']
        result['capital_statement'] = import_capital_statement(args.cache, value['code'], value['statement'], pdf)
    if args.disclosed_capital_reference:
        value = json.loads(args.disclosed_capital_reference.read_text(encoding='utf-8'))
        if value['code'] not in codes:
            parser.error('reviewed reference code must be in --codes')
        result['disclosed_capital_reference'] = import_disclosed_capital_reference(args.cache,
            value['code'], value, document_directory=args.disclosed_capital_reference.parent)
    if args.capital_registration_notice:
        value = json.loads(args.capital_registration_notice.read_text(encoding='utf-8'))
        if value['code'] not in codes:
            parser.error('registration notice code must be in --codes')
        pdf = args.capital_registration_notice.parent / value['pdf_path']
        result['capital_registration_notice'] = import_capital_registration_notice(
            args.cache, value['code'], value['notice'], pdf)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
