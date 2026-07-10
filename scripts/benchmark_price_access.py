import argparse
import json
import multiprocessing
import os
import resource
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.data.code_map import to_joinquant_code
from jq_tushare_sdk.data.portal import DataPortal


def run_mode(cache_db: str, stocks: list[str], end_date: str, count: int, mode: str) -> dict:
    backend = TushareCacheBackend(cache_db, cache_mode="strict_local")
    portal = DataPortal(backend, optimize_data=True)
    started = time.perf_counter()
    if mode == "per_stock":
        rows = sum(
            len(portal.get_price(code, end_date=end_date, count=count, fields=["close"], fq="pre"))
            for code in stocks
        )
    elif mode == "batch":
        rows = len(portal.get_price(stocks, end_date=end_date, count=count, fields=["close"], fq="pre"))
    else:
        raise ValueError(f"unsupported benchmark mode: {mode}")
    return {
        "mode": mode,
        "stocks": len(stocks),
        "rows": rows,
        "seconds": round(time.perf_counter() - started, 6),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1),
        "portal": portal.performance_snapshot() if hasattr(portal, "performance_snapshot") else {},
    }


def _run_child(queue, cache_db: str, stocks: list[str], end_date: str, count: int, mode: str) -> None:
    try:
        queue.put((True, run_mode(cache_db, stocks, end_date, count, mode)))
    except Exception as exc:
        queue.put((False, f"{type(exc).__name__}: {exc}"))


def _run_isolated(cache_db: str, stocks: list[str], end_date: str, count: int, mode: str) -> dict:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_run_child, args=(queue, cache_db, stocks, end_date, count, mode))
    process.start()
    process.join()
    if process.exitcode != 0:
        raise RuntimeError(f"benchmark child for {mode} exited with status {process.exitcode}")
    succeeded, result = queue.get()
    if not succeeded:
        raise RuntimeError(f"benchmark child for {mode} failed: {result}")
    return result


def _listed_stocks(cache_db: str, limit: int) -> list[str]:
    backend = TushareCacheBackend(cache_db, cache_mode="strict_local")
    frame = backend.fetch("stock_basic")
    listed = frame[frame["list_status"].astype(str) == "L"].sort_values("ts_code")
    stocks = [to_joinquant_code(code) for code in listed["ts_code"].tolist()]
    if len(stocks) < limit:
        raise ValueError(f"stock_basic contains only {len(stocks)} listed securities, need {limit}")
    return stocks[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark real-cache price access modes.")
    parser.add_argument("--cache-db", required=True)
    parser.add_argument("--end", dest="end_date", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--stocks", type=int, required=True)
    args = parser.parse_args()
    if args.count <= 0 or args.stocks <= 0:
        parser.error("--count and --stocks must be positive")
    if not os.path.isfile(args.cache_db):
        parser.error(f"cache database does not exist: {args.cache_db}")

    stocks = _listed_stocks(args.cache_db, args.stocks)
    result = {
        "per_stock": _run_isolated(args.cache_db, stocks, args.end_date, args.count, "per_stock"),
        "batch": _run_isolated(args.cache_db, stocks, args.end_date, args.count, "batch"),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
