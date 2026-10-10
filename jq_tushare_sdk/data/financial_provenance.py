"""Financial revisions and conservative point-in-time arithmetic.

Announcement dates in a latest-only cache do not establish a historical vintage.
A newly observed revision becomes usable no earlier than its capture time unless
its source provides a separately verified version availability timestamp.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


class FinancialVintageUnavailable(RuntimeError):
    def __init__(self, message):
        super().__init__(message)
        self.reason = str(message).split(':', 1)[0]
        self.details = {'message': str(message)}


def capture_timestamp() -> str:
    """Canonical Shanghai clock independent of the computer's local timezone."""
    return datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None).isoformat(timespec='seconds')


def asof_timestamp(value) -> str:
    timestamp = pd.Timestamp(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        timestamp = timestamp.normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    elif isinstance(value, str) and (len(value.replace('-', '')) == 8):
        timestamp = timestamp.normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert('Asia/Shanghai').tz_localize(None)
    return timestamp.isoformat(timespec='seconds')


def normalized_payload(row: dict) -> tuple[str, str]:
    payload = {str(key): (None if pd.isna(value) else value.item() if isinstance(value, np.generic) else value)
               for key, value in row.items()}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str, allow_nan=False)
    return text, hashlib.sha256(text.encode('utf-8')).hexdigest()


def select_visible_reports(frame: pd.DataFrame, as_of) -> pd.DataFrame:
    """Select latest known consolidated cumulative version per security/report."""
    if frame.empty:
        return frame.copy()
    result = frame.copy()
    if as_of is not None:
        cutoff = asof_timestamp(as_of)
        if '_available_at' not in result:
            raise FinancialVintageUnavailable('financial_vintage_missing: revision availability absent')
        result = result[result['_available_at'].notna() & (result['_available_at'].astype(str) <= cutoff)]
    if 'report_type' in result:
        # Tushare report_type 1: consolidated cumulative income statement.
        result = result[result['report_type'].astype(str).isin(['1', '1.0'])]
    keys = [key for key in ('ts_code', 'end_date', 'report_type') if key in result]
    order = [key for key in ('ts_code', 'end_date', '_available_at', '_observed_at', '_payload_sha256') if key in result]
    if order:
        result = result.sort_values(order)
    if keys:
        result = result.drop_duplicates(keys, keep='last')
    result.attrs.update(frame.attrs)
    return result.reset_index(drop=True)


def signed_ttm_by_security(reports: pd.DataFrame, as_of) -> dict[str, tuple[float, str]]:
    """Return signed TTM profit only when all cumulative inputs are visible vintages."""
    if reports.attrs.get('provenance_state') not in {'revisions_verified', 'research_reconstructed'}:
        return {}
    if reports.attrs.get('provenance_state') == 'revisions_verified':
        reports = select_visible_reports(reports, as_of)
    else:
        reports = reports.sort_values([column for column in ('ts_code', 'end_date', 'f_ann_date', 'ann_date') if column in reports]).drop_duplicates(['ts_code', 'end_date'], keep='last')
    if reports.empty or not {'ts_code', 'end_date', 'n_income_attr_p'}.issubset(reports):
        return {}
    result = {}
    for code, group in reports.groupby('ts_code', sort=False):
        values = {str(row.end_date).replace('-', '')[:8]: pd.to_numeric(row.n_income_attr_p, errors='coerce')
                  for row in group.itertuples()}
        latest = max(values)
        year, month_day = int(latest[:4]), latest[4:]
        if month_day == '1231':
            profit = values[latest]
        else:
            annual = values.get(f'{year - 1}1231', float('nan'))
            prior = values.get(f'{year - 1}{month_day}', float('nan'))
            profit = values[latest] + annual - prior
        status = 'zero_ttm_profit' if np.isfinite(profit) and profit == 0 else 'revision_ttm' if np.isfinite(profit) else 'ttm_inputs_missing'
        result[str(code)] = (float(profit), status)
    return result
