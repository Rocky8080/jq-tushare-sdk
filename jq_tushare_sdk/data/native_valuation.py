"""Immutable native valuation receipts for current use, without historical claims."""
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

from .cache_lock import cache_write_lock
from .code_map import normalize_date, to_tushare_code


SCHEMA = "jq-native-valuation-snapshot/1"
CHINA = ZoneInfo("Asia/Shanghai")


def _stamp(value):
    at = pd.Timestamp(value)
    return at.tz_localize(CHINA) if at.tzinfo is None else at.tz_convert(CHINA)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def validate_native_valuation_snapshot(document):
    """Validate complete native content without touching a database or filesystem."""
    manifest, rows = document["manifest"], document["rows"]
    if (manifest.get("schema") != SCHEMA or manifest.get("provider") != "joinquant"
            or manifest.get("table") != "valuation" or manifest.get("scope") != {"kind": "all"}
            or manifest.get("complete") is not True or manifest.get("terminal_empty_page") is not True
            or manifest.get("pagination") != "code_gt_ascending"):
        raise ValueError("native valuation document does not establish complete native scope")
    day = normalize_date(manifest["query_date"])
    datetime.strptime(day, "%Y%m%d")
    for value in (manifest["observed_at"], manifest["query_started_at"]):
        if pd.Timestamp(value).tzinfo is None:
            raise ValueError("native source observation requires an explicit timezone")
    observed, started = _stamp(manifest["observed_at"]), _stamp(manifest["query_started_at"])
    if day > observed.strftime("%Y%m%d"):
        raise ValueError("native valuation query date is later than observation")
    columns = manifest.get("columns", [])
    numeric_fields = {"pe_ratio", "pb_ratio", "market_cap", "circulating_market_cap", "circulating_cap"}
    if (not isinstance(columns, list) or len(set(columns)) != len(columns)
            or not {"code", *numeric_fields}.issubset(columns)
            or set(manifest.get("dtypes", {})) != set(columns)
            or any(set(row) != set(columns) for row in rows)):
        raise ValueError("native valuation declared schema and rows do not agree")
    now = pd.Timestamp.now(tz=CHINA)
    if observed > now + pd.Timedelta(seconds=1) or observed < started:
        raise ValueError("native valuation observation timestamp is invalid")
    if len(rows) != manifest.get("row_count") or not rows or _hash(rows) != manifest.get("rows_sha256"):
        raise ValueError("native valuation row count or content hash mismatch")
    codes = [row.get("code") for row in rows]
    if any(not isinstance(code, str) for code in codes) or codes != sorted(set(codes)):
        raise ValueError("native valuation codes are not unique and ordered")
    for row in rows:
        to_tushare_code(row["code"])
        for field in numeric_fields:
            if field not in row or (row[field] is not None and
                    (isinstance(row[field], bool) or not isinstance(row[field], (int, float)) or not math.isfinite(row[field]))):
                raise ValueError("native valuation required fields are missing or invalid")
    offset, cursor = 0, ""
    pages = manifest.get("pages", [])
    if not pages or pages[-1].get("row_count") != 0:
        raise ValueError("native valuation requires a terminal empty page")
    for number, page in enumerate(pages):
        count = page.get("row_count")
        if not isinstance(count, int) or count < 0 or page.get("after_code") != cursor:
            raise ValueError("native valuation pagination identity mismatch")
        batch = rows[offset:offset + count]
        if len(batch) != count or _hash(batch) != page.get("rows_sha256"):
            raise ValueError("native valuation page content hash mismatch")
        if pd.Timestamp(page["observed_at"]).tzinfo is None:
            raise ValueError("native page observation requires an explicit timezone")
        when = _stamp(page["observed_at"])
        if not started <= when <= observed:
            raise ValueError("native valuation page capture time is outside query window")
        if not count and number != len(pages) - 1:
            raise ValueError("native valuation has data after an empty terminal page")
        if batch:
            cursor = batch[-1]["code"]
        offset += count
    if offset != len(rows):
        raise ValueError("native valuation pagination does not cover all rows")
    return {"schema": SCHEMA, "data_date": day, "row_count": len(rows),
            "document_sha256": _hash(document), "observed_at": observed.isoformat()}


def import_native_valuation(cache_db, document, *, source_reference):
    """Validate a complete real JoinQuant exporter document and append it intact."""
    if os.environ.get("JQTS_SIMULATION_READ_ONLY") == "1":
        raise PermissionError("native valuation import is disabled in read-only simulation")
    if not source_reference:
        raise ValueError("native valuation requires its export source reference")
    verified = validate_native_valuation_snapshot(document)
    day, observed = verified["data_date"], _stamp(verified["observed_at"])
    rows = document["rows"]
    text, digest = _json(document), verified["document_sha256"]
    imported = datetime.now(CHINA).isoformat(timespec="microseconds")
    with cache_write_lock(cache_db), sqlite3.connect(cache_db) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS native_valuation_observations (
            document_sha256 TEXT PRIMARY KEY, data_date TEXT NOT NULL,
            observed_at TEXT NOT NULL, imported_at TEXT NOT NULL,
            source_reference TEXT NOT NULL, document_json TEXT NOT NULL)""")
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS native_valuation_observations_no_{operation.lower()}
                BEFORE {operation} ON native_valuation_observations BEGIN
                SELECT RAISE(ABORT,'native valuation observations are immutable'); END""")
        conn.execute("INSERT OR IGNORE INTO native_valuation_observations VALUES (?,?,?,?,?,?)",
                     (digest, day, observed.isoformat(), imported, str(source_reference), text))
    return {"schema": SCHEMA, "data_date": day, "row_count": len(rows),
            "document_sha256": digest, "observed_at": observed.isoformat(), "imported_at": imported}


def read_native_valuation(cache_db, data_date, knowledge_asof):
    """Only expose a native receipt already observed and imported at live cutoff."""
    day, known = normalize_date(data_date), _stamp(knowledge_asof)
    if known > pd.Timestamp.now(tz=CHINA) + pd.Timedelta(seconds=1) or known.strftime("%Y%m%d") < day:
        raise ValueError("native valuation live knowledge timestamp is invalid")
    path = Path(cache_db).expanduser().resolve()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_valuation_observations'").fetchone():
            return None
        rows = conn.execute("SELECT * FROM native_valuation_observations WHERE data_date=? ORDER BY observed_at DESC, imported_at DESC", (day,)).fetchall()
        item = next((row for row in rows if max(_stamp(row["observed_at"]), _stamp(row["imported_at"])) <= known), None)
        if item is None:
            return None
        if hashlib.sha256(item["document_json"].encode()).hexdigest() != item["document_sha256"]:
            raise ValueError("native valuation stored content hash mismatch")
        doc = json.loads(item["document_json"])
    return {"schema": SCHEMA, "data_date": day, "document_sha256": item["document_sha256"],
            "observed_at": item["observed_at"], "imported_at": item["imported_at"],
            "source_reference": item["source_reference"], "historical_vintage_verified": False,
            "rows": {to_tushare_code(row["code"]): row for row in doc["rows"]}}
