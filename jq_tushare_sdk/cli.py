import argparse
import os
import sys

from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.data.readiness import DataReadinessCheck
from jq_tushare_sdk.data.readiness import update_missing_data
from jq_tushare_sdk.reports.refresher import refresh_backtest_report
from jq_tushare_sdk.runtime.engine import BacktestEngine
from jq_tushare_sdk.web.app import serve_web_console

_REQUIRED_LOCAL_APIS = [
    "trade_cal",
    "daily",
    "daily_basic",
    "adj_factor",
    "index_weight",
    "stock_basic",
    "index_daily",
    "income",
]


def build_parser():
    parser = argparse.ArgumentParser(prog="jqts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_data = subparsers.add_parser("check-data")
    check_data.add_argument("strategy", nargs="?")
    check_data.add_argument("--strategy", dest="strategy_option")
    _add_shared_args(check_data)

    update_data = subparsers.add_parser("update-data")
    update_data.add_argument("--api", action="append", dest="apis")
    update_data.add_argument("--ts-code", "--ts_code", dest="ts_code")
    _add_cache_args(update_data)

    refresh_report = subparsers.add_parser("refresh-report")
    refresh_report.add_argument("run_dir")
    refresh_report.add_argument("--cache-db", required=True)

    web = subparsers.add_parser("web")
    web.add_argument("--project-root", default=".")
    web.add_argument("--cache-db", default="data/jq_tushare_cache.db")
    web.add_argument("--output-dir", default="backtest_runs")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8790)

    backtest = subparsers.add_parser("backtest")
    backtest.add_argument("strategy", nargs="?")
    backtest.add_argument("--strategy", dest="strategy_option")
    backtest.add_argument("--deterministic", dest="deterministic", action="store_true", default=True)
    backtest.add_argument("--no-deterministic", dest="deterministic", action="store_false")
    backtest.add_argument("--hash-seed", default="0")
    backtest.add_argument("--optimize-data", dest="optimize_data", action="store_true", default=True)
    backtest.add_argument("--no-optimize-data", dest="optimize_data", action="store_false")
    _add_shared_args(backtest)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "web":
        serve_web_console(
            project_root=args.project_root,
            cache_db=args.cache_db,
            output_dir=args.output_dir,
            host=args.host,
            port=args.port,
        )
        return 0

    if args.command == "update-data":
        backend = TushareCacheBackend(
            args.cache_db,
            token=os.environ.get("TUSHARE_TOKEN"),
            cache_mode="api_first",
        )
        try:
            if args.ts_code:
                apis = args.apis or ["index_daily"]
                counts = {
                    api_name: backend.update_data(
                        api_name,
                        start_date=args.start,
                        end_date=args.end,
                        ts_code=args.ts_code,
                    )
                    for api_name in apis
                }
            else:
                counts = backend.update_range(args.start, args.end, apis=args.apis)
            for api_name, count in counts.items():
                print(f"{api_name}: {count} rows")
            return 0
        finally:
            _close_backend(backend)

    if args.command == "refresh-report":
        backend = TushareCacheBackend(
            args.cache_db,
            token=os.environ.get("TUSHARE_TOKEN"),
            cache_mode="strict_local",
        )
        try:
            result = refresh_backtest_report(args.run_dir, backend=backend)
            if result.get("updated"):
                print(f"Report refreshed: {result.get('report_path')}")
            else:
                print(f"Report still has unsupported metrics: {result.get('reason')}")
            return 0 if result.get("updated") else 1
        finally:
            _close_backend(backend)

    strategy_path = args.strategy or args.strategy_option
    if not strategy_path:
        raise SystemExit("strategy path is required")
    _maybe_reexec_for_deterministic_backtest(args, argv)

    config = BacktestConfig(
        strategy_path=strategy_path,
        start_date=args.start,
        end_date=args.end,
        initial_cash=args.cash,
        cache_db=args.cache_db,
        output_dir=args.output_dir,
        optimize_data=getattr(args, "optimize_data", True),
    )
    backend = TushareCacheBackend(
        args.cache_db,
        token=os.environ.get("TUSHARE_TOKEN"),
        cache_mode="strict_local",
    )

    try:
        if args.command == "check-data":
            issues = _check_local_readiness(config, backend)
            if issues:
                return 1
            print("Data readiness check passed")
            return 0

        issues = _ensure_local_readiness(config, backend)
        if issues:
            return 1
        manifest = BacktestEngine(config, backend=backend).run()
        print(f"Backtest complete: {manifest.run_dir}")
        return 0
    finally:
        _close_backend(backend)


def _add_shared_args(parser):
    _add_cache_args(parser)
    parser.add_argument("--cash", type=float, default=1000000.0)
    parser.add_argument("--output-dir", default="backtest_runs")


def _add_cache_args(parser):
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--cache-db", required=True)


def _check_local_readiness(config, backend):
    issues = _local_readiness_issues(config, backend)
    if not issues:
        return []
    _print_readiness_issues(config, issues)
    return issues


def _ensure_local_readiness(config, backend):
    issues = _local_readiness_issues(config, backend)
    if not issues:
        return []

    print("Local cache is incomplete; attempting automatic Tushare data update...")
    try:
        counts = update_missing_data(backend, issues)
    except Exception as exc:
        print(f"Automatic data update failed: {exc}")
        _print_readiness_issues(config, issues)
        return issues

    if counts:
        print("Automatic data update completed:")
        for label, count in counts.items():
            print(f"  {label}: {count} rows")
    else:
        print("No automatic data update request could be inferred from readiness issues.")

    issues = _local_readiness_issues(config, backend)
    if issues:
        _print_readiness_issues(config, issues)
    return issues


def _local_readiness_issues(config, backend):
    return DataReadinessCheck(backend).check_required(config, _REQUIRED_LOCAL_APIS)


def _print_readiness_issues(config, issues):
    for issue in issues:
        print(f"{issue.api_name}: {issue.message}")
        print(f"  {issue.suggestion}")
    print(f"  Local cache DB: {config.cache_db}")
    print("  Populate the missing local cache data before rerunning.")


def _close_backend(backend) -> None:
    close = getattr(backend, "close", None)
    if close is not None:
        close()


def _maybe_reexec_for_deterministic_backtest(args, argv=None) -> None:
    if args.command != "backtest" or not getattr(args, "deterministic", False):
        return
    hash_seed = str(getattr(args, "hash_seed", "0"))
    if os.environ.get("PYTHONHASHSEED") == hash_seed:
        return
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = hash_seed
    env["JQ_TUSHARE_SDK_DETERMINISTIC_REEXECED"] = "1"
    if argv is None:
        command = [sys.executable, "-m", "jq_tushare_sdk.cli", *sys.argv[1:]]
    else:
        command = [sys.executable, "-m", "jq_tushare_sdk.cli", *list(argv)]
    os.execvpe(sys.executable, command, env)


if __name__ == "__main__":
    raise SystemExit(main())
