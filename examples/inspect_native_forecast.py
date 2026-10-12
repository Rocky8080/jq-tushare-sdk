"""Explain native forecast readiness without weakening historical execution.

--inspect-current-archive exports a current observed archive for analysis only.
It never promotes those rows to an earlier historical decision.
"""
import argparse
import json
from pathlib import Path
import sys

SDK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK))
from jq_tushare_sdk.data.native_forecast import native_forecast_readiness, inspect_native_forecast_archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--start', required=True, help='publication window start')
    parser.add_argument('--end', required=True, help='publication window end')
    parser.add_argument('--codes', help='explicit comma-separated scope; omit for the entire market')
    parser.add_argument('--decision-at', required=True, help='actual decision time with +08:00 or UTC timezone')
    parser.add_argument('--strict-pit', action='store_true')
    parser.add_argument('--inspect-current-archive', action='store_true')
    parser.add_argument('--out', type=Path, required=True, help='new directory under SDK artifacts')
    args = parser.parse_args()
    codes = [c.strip() for c in args.codes.split(',') if c.strip()] if args.codes else None
    out = args.out.resolve()
    if not out.is_relative_to((SDK / 'artifacts').resolve()):
        parser.error('--out must be under this SDK artifacts directory')
    result = native_forecast_readiness(args.cache.resolve(strict=True), args.start, args.end,
        codes=codes, decision_at=args.decision_at, strict_pit=args.strict_pit)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'readiness.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.inspect_current_archive:
        frame = inspect_native_forecast_archive(args.cache, args.start, args.end, codes=codes)
        frame.to_json(out / 'observed-archive.json', orient='records', force_ascii=False, date_format='iso')
        (out / 'archive-provenance.json').write_text(json.dumps(frame.attrs, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ready_for_requested_decision':result['ready'], 'reason':result['reason'],
        'historical_vintage_verified':False, 'out':str(out)}, ensure_ascii=False))
    return 0 if result['ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
