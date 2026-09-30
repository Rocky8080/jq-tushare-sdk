"""Industry data provenance for local backtests.

``get_industry`` resolves SW industries from different sources depending on
``JQTS_INDUSTRY_PROVIDER`` / ``JQTS_INDUSTRY_COMPAT`` and on which SW cache
tables happen to be present.  Different runs can therefore classify the same
stocks differently (Eastmoney ``stock_basic.industry`` vs. real SW2021 vs.
imported JoinQuant membership), which changes portfolio grouping and, for
industry-diversified strategies, returns.

This module snapshots the effective industry configuration plus the relevant
cache-table fingerprints so every backtest run records which industry semantic
it used.  The snapshot is written into ``manifest.json`` by
:class:`jq_tushare_sdk.reports.output_manager.OutputManager` and is read by
``scripts/compare_backtest_runs.py`` to refuse cross-semantic comparisons.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_INDUSTRY_PROVIDERS = ("tushare", "joinquant_taxonomy", "joinquant_full")
_SW_MEMBER_ALL_TABLE = "sw_industry_member_all"
_SW_MEMBER_TABLE = "sw_industry_member"
_JQ_CLASSIFY_TABLE = "jq_industry_classify"
_JQ_MEMBER_TABLE = "jq_industry_member"


def effective_industry_provider() -> str:
    """Return the industry provider that will actually be used."""
    provider = os.environ.get("JQTS_INDUSTRY_PROVIDER", "tushare").strip()
    if provider not in _INDUSTRY_PROVIDERS:
        raise ValueError("JQTS_INDUSTRY_PROVIDER must be tushare, joinquant_taxonomy, or joinquant_full")
    return provider


def effective_industry_compat() -> str:
    """Return the diagnostic compat flag, or ``""`` when not set."""
    return os.environ.get("JQTS_INDUSTRY_COMPAT", "")


def industry_provenance(cache_db: str | Path) -> dict:
    """Snapshot the effective industry semantic for one cache database.

    Tolerates missing databases, tables and columns: every field falls back to
    ``None`` (``provider``/``compat`` always resolve from the environment).
    """
    payload: dict = {
        "provider": effective_industry_provider(),
        "compat": effective_industry_compat(),
        "sw_member_all_rows": None,
        "sw_member_rows": None,
        "jq_classification_sha256": None,
        "jq_member_sha256": None,
    }
    path = Path(cache_db)
    if not path.is_file():
        return payload
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    except (sqlite3.Error, OSError):
        return payload
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for key, table, column in (
            ("sw_member_all_rows", _SW_MEMBER_ALL_TABLE, None),
            ("sw_member_rows", _SW_MEMBER_TABLE, None),
            ("jq_classification_sha256", _JQ_CLASSIFY_TABLE, "source_sha256"),
            ("jq_member_sha256", _JQ_MEMBER_TABLE, "source_sha256"),
        ):
            if table not in tables:
                continue
            try:
                if column is None:
                    count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    payload[key] = int(count)
                else:
                    rows = connection.execute(
                        f"SELECT DISTINCT {column} FROM {table} "
                        f"WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column}"
                    ).fetchall()
                    payload[key] = ",".join(str(row[0]) for row in rows) if rows else None
            except sqlite3.Error:
                continue
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    return payload
