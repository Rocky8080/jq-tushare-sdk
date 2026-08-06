from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path


CLASSIFY_TABLE = "jq_industry_classify"
MEMBER_TABLE = "jq_industry_member"


def normalize_industry_code(value) -> str:
    text = str(value or "").strip().upper()
    return text[:-3] if text.endswith(".SI") else text


def normalize_security_code(value) -> str:
    text = str(value or "").strip().upper()
    if text.endswith(".SH"):
        return text[:-3] + ".XSHG"
    if text.endswith(".SZ"):
        return text[:-3] + ".XSHE"
    return text


def normalize_membership_date(value) -> str:
    text = str(value or "").strip().replace("-", "")
    return "" if text.lower() in {"", "none", "nan"} else text[:8]


def parse_classification_text(path: str | Path) -> tuple[list[dict[str, str]], str]:
    source = Path(path)
    raw = source.read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    level = None
    rows: list[dict[str, str]] = []
    headings = {
        "申万一级行业": "L1",
        "申万二级行业": "L2",
        "申万三级行业": "L3",
    }
    for raw_line in raw.decode("utf-8-sig").splitlines():
        line = raw_line.strip()
        if line in headings:
            level = headings[line]
            continue
        if level is None or not re.match(r"^\d{6}\t", raw_line):
            continue
        fields = raw_line.split("\t")
        fields.extend([""] * (5 - len(fields)))
        industry_code, industry_name, start_date, end_date, parent_code = fields[:5]
        rows.append(
            {
                "industry_code": normalize_industry_code(industry_code),
                "industry_name": industry_name.strip(),
                "level": level,
                "start_date": normalize_membership_date(start_date),
                "end_date": normalize_membership_date(end_date),
                "parent_code": normalize_industry_code(parent_code),
                "source_sha256": source_sha256,
            }
        )
    if not rows:
        raise ValueError(f"no JoinQuant industry classification rows found in {source}")
    return rows, source_sha256


def _industry_info_to_member(security, info, as_of: str) -> dict[str, str]:
    result = {
        "security": normalize_security_code(security),
        "sw_l1_code": "",
        "sw_l2_code": "",
        "sw_l3_code": "",
        "in_date": normalize_membership_date(as_of),
        "out_date": "",
    }
    for level in ("sw_l1", "sw_l2", "sw_l3"):
        value = info.get(level, {}) if isinstance(info, dict) else {}
        if isinstance(value, dict):
            result[f"{level}_code"] = normalize_industry_code(value.get("industry_code"))
    return result


def parse_members(path: str | Path, *, as_of: str | None = None) -> tuple[list[dict[str, str]], str]:
    source = Path(path)
    raw = source.read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig")
    rows: list[dict[str, str]] = []

    if source.suffix.lower() in {".json", ".jsonl"}:
        if source.suffix.lower() == ".jsonl":
            payload = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            payload = json.loads(text)
        if isinstance(payload, dict):
            if not normalize_membership_date(as_of):
                raise ValueError("snapshot member mappings require --as-of YYYY-MM-DD")
            for security, info in payload.items():
                rows.append(_industry_info_to_member(security, info, as_of or ""))
        elif isinstance(payload, list):
            rows.extend(_normalize_member_row(row, as_of=as_of) for row in payload)
        else:
            raise ValueError("JoinQuant member JSON must be a security mapping or a row list")
    else:
        sample = text[:4096]
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        reader = csv.DictReader(text.splitlines(), dialect=dialect)
        rows.extend(_normalize_member_row(row, as_of=as_of) for row in reader)

    rows = [row for row in rows if row["security"] and row["sw_l1_code"]]
    if not rows:
        raise ValueError(f"no JoinQuant stock-industry member rows found in {source}")
    if any(not row["in_date"] for row in rows):
        raise ValueError("JoinQuant member rows require in_date/date or --as-of")
    return rows, source_sha256


def _normalize_member_row(row: dict, *, as_of: str | None) -> dict[str, str]:
    security = row.get("security") or row.get("code") or row.get("stock") or row.get("ts_code")
    return {
        "security": normalize_security_code(security),
        "sw_l1_code": normalize_industry_code(row.get("sw_l1_code") or row.get("l1_code")),
        "sw_l2_code": normalize_industry_code(row.get("sw_l2_code") or row.get("l2_code")),
        "sw_l3_code": normalize_industry_code(row.get("sw_l3_code") or row.get("l3_code")),
        "in_date": normalize_membership_date(row.get("in_date") or row.get("date") or as_of),
        "out_date": normalize_membership_date(row.get("out_date")),
    }


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CLASSIFY_TABLE} (
            industry_code TEXT,
            industry_name TEXT,
            level TEXT,
            start_date TEXT,
            end_date TEXT,
            parent_code TEXT,
            source_sha256 TEXT,
            update_time TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (industry_code, start_date)
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MEMBER_TABLE} (
            security TEXT,
            sw_l1_code TEXT,
            sw_l2_code TEXT,
            sw_l3_code TEXT,
            in_date TEXT,
            out_date TEXT,
            source_sha256 TEXT,
            update_time TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (security, in_date, out_date)
        )
        """
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{CLASSIFY_TABLE}_level_code "
        f"ON {CLASSIFY_TABLE}(level, industry_code)"
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{MEMBER_TABLE}_security "
        f"ON {MEMBER_TABLE}(security)"
    )


def import_joinquant_industry(
    cache_db: str | Path,
    *,
    classify_path: str | Path,
    members_path: str | Path | None = None,
    as_of: str | None = None,
) -> dict:
    classify_rows, classify_sha256 = parse_classification_text(classify_path)
    member_rows = []
    member_sha256 = None
    if members_path is not None:
        member_rows, member_sha256 = parse_members(members_path, as_of=as_of)

    cache_path = Path(cache_db)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cache_path) as connection:
        ensure_tables(connection)
        connection.execute(f"DELETE FROM {CLASSIFY_TABLE}")
        connection.executemany(
            f"""
            INSERT INTO {CLASSIFY_TABLE} (
                industry_code, industry_name, level, start_date, end_date,
                parent_code, source_sha256
            ) VALUES (
                :industry_code, :industry_name, :level, :start_date, :end_date,
                :parent_code, :source_sha256
            )
            """,
            classify_rows,
        )
        if members_path is not None:
            connection.execute(f"DELETE FROM {MEMBER_TABLE}")
            connection.executemany(
                f"""
                INSERT INTO {MEMBER_TABLE} (
                    security, sw_l1_code, sw_l2_code, sw_l3_code,
                    in_date, out_date, source_sha256
                ) VALUES (
                    :security, :sw_l1_code, :sw_l2_code, :sw_l3_code,
                    :in_date, :out_date, :source_sha256
                )
                """,
                [{**row, "source_sha256": member_sha256} for row in member_rows],
            )
        connection.commit()

    active_counts = {
        level: sum(1 for row in classify_rows if row["level"] == level and not row["end_date"])
        for level in ("L1", "L2", "L3")
    }
    return {
        "classification_rows": len(classify_rows),
        "active_counts": active_counts,
        "classification_sha256": classify_sha256,
        "member_rows": len(member_rows),
        "member_sha256": member_sha256,
        "membership_mode": "joinquant_full" if member_rows else "joinquant_taxonomy",
    }
