#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地导出聚宽逐股行业成员（推荐路径）。

研究环境的 API 可用性取决于账号权限；本脚本使用聚宽官方 jqdatasdk，
按快照日期获取股票池及 L1/L2/L3 成员。每日快照限定导出窗口，不向未来延伸。

用法：
    pip install jqdatasdk
    python scripts/export_joinquant_members_local.py \\
        --username <聚宽账号> \\
        --as-of 2026-06-01 --output jq_members.json
    # 或区间模式（含历史成员变更）
    python scripts/export_joinquant_members_local.py \\
        --username <聚宽账号> \\
        --start 2026-04-01 --end 2026-07-31 --step-days 1 \\
        --output jq_members.json

导入本地 jq-tushare-sdk：
    python -m jq_tushare_sdk.cli import-jq-industry \\
        --classify path/to/joinquant_industry.txt \\
        --members jq_members.json --as-of 2026-06-01 \\
        --cache-db data/jq_tushare_cache.db
随后 JQTS_INDUSTRY_PROVIDER=joinquant_full 回测。
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import json


def industry_snapshot(jq, as_of: str) -> dict:
    """返回快照日期股票池的官方逐股行业映射。"""
    codes = sorted(str(code) for code in jq.get_all_securities(types=["stock"], date=as_of).index)
    mapping = {}
    for start in range(0, len(codes), 800):
        mapping.update(jq.get_industry(codes[start:start + 800], date=as_of) or {})
    if not mapping:
        raise RuntimeError(f"No industry members returned for {as_of}; export aborted.")
    return mapping


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export JoinQuant members via jqdatasdk.")
    parser.add_argument("--username", help="JoinQuant account (phone/email)")
    parser.add_argument("--password", help="JoinQuant password")
    parser.add_argument("--start", help="first snapshot date, YYYY-MM-DD")
    parser.add_argument("--end", help="last snapshot date, YYYY-MM-DD")
    parser.add_argument("--as-of", help="single snapshot date, YYYY-MM-DD")
    parser.add_argument("--step-days", type=int, default=1)
    parser.add_argument("--output", required=True, help="output JSON path")
    args = parser.parse_args(argv)

    if args.as_of and (args.start or args.end):
        parser.error("--as-of cannot be combined with --start/--end")
    if not args.as_of and not (args.start and args.end):
        parser.error("require either --as-of or both --start and --end")
    if args.step_days < 1:
        parser.error("--step-days must be positive")
    try:
        if args.as_of:
            datetime.date.fromisoformat(args.as_of)
        elif datetime.date.fromisoformat(args.start) > datetime.date.fromisoformat(args.end):
            parser.error("--start must not be after --end")
    except ValueError as exc:
        parser.error(str(exc))

    try:
        import jqdatasdk as jq
    except ImportError:
        print("jqdatasdk 未安装，请先执行: pip install jqdatasdk")
        return 2

    username = args.username or input("聚宽账号: ").strip()
    password = args.password or getpass.getpass("聚宽密码: ")
    try:
        jq.auth(username, password)
        print(f"jqdatasdk 登录成功，剩余配额: {jq.get_query_count()}")
    except Exception as exc:
        print(f"jqdatasdk 登录失败: {exc}")
        return 1

    if args.as_of:
        payload = industry_snapshot(jq, args.as_of)
        print(f"单日快照 {args.as_of}: {len(payload)} 只股票")
    else:
        dates = []
        cursor = datetime.date.fromisoformat(args.start)
        end_date = datetime.date.fromisoformat(args.end)
        while cursor <= end_date:
            dates.append(cursor.isoformat())
            cursor += datetime.timedelta(days=args.step_days)
        if dates[-1] != args.end:
            dates.append(args.end)
        snapshots = []
        for as_of in dates:
            print(f"导出 {as_of} ...", flush=True)
            snapshots.append((as_of, industry_snapshot(jq, as_of)))
        rows = []
        for index, (as_of, snapshot) in enumerate(snapshots):
            out_date = (snapshots[index + 1][0] if index + 1 < len(snapshots)
                        else (end_date + datetime.timedelta(days=1)).isoformat())
            for security, levels in snapshot.items():
                rows.append(
                    {
                        "security": security,
                        "sw_l1_code": levels.get("sw_l1", {}).get("industry_code", ""),
                        "sw_l2_code": levels.get("sw_l2", {}).get("industry_code", ""),
                        "sw_l3_code": levels.get("sw_l3", {}).get("industry_code", ""),
                        "in_date": as_of.replace("-", ""),
                        "out_date": out_date.replace("-", "") if out_date else "",
                    }
                )
        payload = rows
        print(f"区间导出: {len(rows)} 条成员记录")

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    print(f"已写入: {args.output}")
    print(
        "\n下一步本地导入:\n"
        "  python -m jq_tushare_sdk.cli import-jq-industry \\\n"
        f"    --classify path/to/joinquant_industry.txt --members {args.output} \\\n"
        f"    {'--as-of ' + args.as_of + ' ' if args.as_of else ''}"
        "--cache-db data/jq_tushare_cache.db\n"
        "然后 JQTS_INDUSTRY_PROVIDER=joinquant_full 回测。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
