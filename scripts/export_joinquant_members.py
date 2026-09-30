#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出聚宽研究环境的逐股行业成员，供本地 jq-tushare-sdk 对齐使用。

注意：聚宽研究环境仅支持 ipynb，不支持直接运行 .py 文件。请使用同目录的
``export_joinquant_members.ipynb``（本脚本代码的 notebook 形式），在聚宽
研究环境上传后依次运行即可，输出文件从聚宽「文件」面板下载。

API 可用性取决于账号和环境权限，本脚本探测全局函数与 jqdata 模块属性。
无成员 API 时明确停止，可改用本地 jqdatasdk 导出脚本。

成员导出策略（优先级）：
1. ``get_industry``（全局/jqdata）→ 一次返回 L1/L2/L3，最完整；
2. ``get_industries('sw_l1')`` + ``get_industry_stocks(code, date=as_of)`` →
   L1 成员，支持历史 date；
默认每日快照；较粗的采样无法精确还原快照之间的行业变更。

导入本地：

    python -m jq_tushare_sdk.cli import-jq-industry \\
        --classify path/to/joinquant_industry.txt \\
        --members path/to/joinquant_members.json \\
        --as-of 2026-06-01 \\
        --cache-db data/jq_tushare_cache.db

随后以 joinquant_full 提供者回测：

    JQTS_INDUSTRY_PROVIDER=joinquant_full \\
    python -m jq_tushare_sdk.cli backtest <strategy.py> ... --cache-db data/jq_tushare_cache.db
"""
from __future__ import annotations

import argparse
import datetime
import json


def _resolve_api(name: str):
    """按 全局函数 → 所属模块 的顺序解析 API，找不到返回 None。

    支持 ``jqdata.get_industries``、``jqfactor.get_factors`` 这类带模块名的
    写法；不带模块名时先查全局函数（研究环境注入到全局命名空间），再查
    ``jqdata`` 模块属性。
    """
    if "." in name:
        module, attr = name.split(".", 1)
        try:
            mod = __import__(module, fromlist=[attr])
        except ImportError:
            return None
        return getattr(mod, attr, None)
    if name in globals():
        return globals()[name]
    try:
        import jqdata
    except ImportError:
        return None
    return getattr(jqdata, name, None)


def probe_industry_apis() -> dict[str, bool]:
    return {
        "get_industry": _resolve_api("get_industry") is not None,
        "get_industry_stocks": _resolve_api("get_industry_stocks") is not None,
        "jqdata.get_industries": _resolve_api("jqdata.get_industries") is not None,
        "get_all_securities": _resolve_api("get_all_securities") is not None,
    }


def all_stock_securities(as_of):
    get_all_securities = _resolve_api("get_all_securities")
    if get_all_securities is None:
        raise RuntimeError("研究环境没有 get_all_securities")
    frame = get_all_securities(types=["stock"], date=as_of)
    return sorted(str(code) for code in frame.index.tolist())


def sw_l1_codes(as_of) -> dict[str, str]:
    """返回 {行业代码: 行业名称}。优先 get_industries('sw_l1') 动态获取。"""
    get_industries = _resolve_api("jqdata.get_industries")
    if get_industries is not None:
        try:
            frame = get_industries("sw_l1", date=as_of)
            if frame is not None and not frame.empty and "name" in frame.columns:
                result = {}
                for code, row in frame.iterrows():
                    name = str(row.get("name") or "")
                    code = str(code).strip()
                    if code and name:
                        result[code] = name
                if result:
                    return result
        except Exception:
            pass
    return {}


def industry_snapshot(as_of: str, method: str = "auto", chunk: int = 800):
    """返回 ({security: {sw_l1: {...}, ...}}, 使用的API方法)。"""
    get_industry = _resolve_api("get_industry")
    if method in ("auto", "jqdata") and get_industry is not None:
        codes = all_stock_securities(as_of)
        mapping = {}
        for start in range(0, len(codes), chunk):
            info = get_industry(codes[start : start + chunk], date=as_of) or {}
            for security, levels in info.items():
                payload = {}
                for level in ("sw_l1", "sw_l2", "sw_l3"):
                    value = levels.get(level) if isinstance(levels, dict) else None
                    if isinstance(value, dict):
                        payload[level] = {
                            "industry_code": str(value.get("industry_code") or ""),
                            "industry_name": str(value.get("industry_name") or ""),
                        }
                if payload.get("sw_l1", {}).get("industry_code"):
                    mapping[str(security)] = payload
        if mapping:
            return mapping, "get_industry"

    get_industry_stocks = _resolve_api("get_industry_stocks")
    if method in ("auto", "industry_stocks") and get_industry_stocks is not None:
        mapping = {}
        for code, name in sw_l1_codes(as_of).items():
            stocks = get_industry_stocks(code, date=as_of) or []
            for security in stocks:
                mapping.setdefault(str(security), {})["sw_l1"] = {
                    "industry_code": code,
                    "industry_name": name,
                }
        if mapping:
            return mapping, "get_industries + get_industry_stocks"


    raise RuntimeError("研究环境没有可用的行业 API: " + str(probe_industry_apis()))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export JoinQuant stock-industry members.")
    parser.add_argument("--start", help="first snapshot date, YYYY-MM-DD")
    parser.add_argument("--end", help="last snapshot date, YYYY-MM-DD")
    parser.add_argument("--as-of", help="single snapshot date, YYYY-MM-DD")
    parser.add_argument("--step-days", type=int, default=1)
    parser.add_argument(
        "--method",
        choices=["auto", "jqdata", "industry_stocks"],
        default="auto",
    )
    parser.add_argument("--output", required=True, help="output JSON path")
    args = parser.parse_args(argv)

    if args.as_of and (args.start or args.end):
        parser.error("--as-of cannot be combined with --start/--end")
    if args.step_days < 1:
        parser.error("--step-days must be positive")
    try:
        for value in (args.as_of, args.start, args.end):
            if value:
                datetime.date.fromisoformat(value)
    except ValueError as exc:
        parser.error(str(exc))
    if args.start and args.end and args.start > args.end:
        parser.error("--start must not be after --end")

    if args.as_of:
        dates = [args.as_of]
    elif args.start and args.end:
        dates = []
        cursor = datetime.date.fromisoformat(args.start)
        end_date = datetime.date.fromisoformat(args.end)
        while cursor <= end_date:
            dates.append(cursor.isoformat())
            cursor += datetime.timedelta(days=args.step_days)
        if dates[-1] != args.end:
            dates.append(args.end)
    else:
        parser.error("require either --as-of or both --start and --end")

    print("可用行业 API 探测:")
    for name, ok in probe_industry_apis().items():
        print(f"  {name:<34} {'可用' if ok else '不可用'}")

    if args.as_of:
        payload, method = industry_snapshot(args.as_of, args.method)
        print(f"单日快照 {args.as_of}: {len(payload)} 只股票 (API: {method})")
    else:
        rows = []
        snapshots = []
        for as_of in dates:
            print(f"导出 {as_of} ...", flush=True)
            snapshot, method = industry_snapshot(as_of, args.method)
            snapshots.append((as_of, snapshot))
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
