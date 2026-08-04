import ast
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from jq_tushare_sdk.data.code_map import is_tushare_fund_code
from jq_tushare_sdk.data.code_map import is_tushare_index_code
from jq_tushare_sdk.data.code_map import is_tushare_sw_index_code
from jq_tushare_sdk.data.code_map import normalize_date
from jq_tushare_sdk.data.code_map import to_tushare_code
from jq_tushare_sdk.data.income_periods import required_income_periods


@dataclass(frozen=True)
class DataUpdateRequest:
    api_name: str
    start_date: str | None = None
    end_date: str | None = None
    params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ReadinessIssue:
    api_name: str
    message: str
    suggestion: str
    update_requests: tuple[DataUpdateRequest, ...] = ()


_UPDATE_PRIORITY = {
    "stock_basic": 0,
    "trade_cal": 1,
    "daily": 2,
    "daily_basic": 3,
    "adj_factor": 4,
    "fund_adj": 5,
    "fund_daily": 6,
    "index_daily": 7,
    "sw_daily": 8,
    "index_weight": 9,
    "income": 10,
}
_MARKET_DAILY_APIS = {"daily", "daily_basic", "adj_factor", "fund_adj", "index_daily"}
_PRICE_LOOKBACK_APIS = {"daily", "adj_factor", "fund_adj"}
_REQUIRED_STOCK_BASIC_LIST_STATUSES = {"L", "D"}


def update_missing_data(backend, issues: list[ReadinessIssue]) -> dict[str, int]:
    requests = []
    for issue in issues:
        requests.extend(issue.update_requests)
    requests.sort(key=_update_request_sort_key)

    counts: dict[str, int] = {}
    seen = set()
    for request in requests:
        params = dict(request.params)
        key = (
            request.api_name,
            request.start_date,
            request.end_date,
            tuple(sorted(params.items())),
        )
        if key in seen:
            continue
        seen.add(key)
        counts[_format_update_request(request)] = backend.update_data(
            request.api_name,
            start_date=request.start_date,
            end_date=request.end_date,
            **params,
        )
    return counts


class DataReadinessCheck:
    def __init__(self, backend):
        self.backend = backend

    def check_required(self, config, apis: list[str]) -> list[ReadinessIssue]:
        start = normalize_date(config.start_date)
        end = normalize_date(config.end_date)
        price_start = infer_strategy_price_lookback_start(getattr(config, "strategy_path", None), start)
        calendar_start = min(start, price_start)
        market_start, market_end = self._market_date_bounds(start, end)
        price_market_start, price_market_end = self._market_date_bounds(price_start, end)
        issues = []
        fund_issue = self._check_strategy_funds(config, price_market_start, price_market_end)
        if fund_issue is not None:
            issues.append(fund_issue)
        for api_name in apis:
            if api_name == "index_daily":
                issue = self._check_required_indexes(config, price_market_start, price_market_end)
                if issue is not None:
                    issues.append(issue)
                continue
            if api_name == "index_weight":
                issue = self._check_index_weight(config, start, end)
                if issue is not None:
                    issues.append(issue)
                continue
            if api_name == "income":
                issue = self._check_income(config, start, end)
                if issue is not None:
                    issues.append(issue)
                continue
            if api_name == "trade_cal":
                check_start, check_end = calendar_start, end
            elif api_name in _PRICE_LOOKBACK_APIS:
                check_start, check_end = price_market_start, price_market_end
            elif api_name in _MARKET_DAILY_APIS:
                check_start, check_end = market_start, market_end
            else:
                check_start, check_end = start, end
            status = self.backend.status(api_name)
            if not status.get("exists") or int(status.get("record_count", 0) or 0) == 0:
                issues.append(
                    ReadinessIssue(
                        api_name=api_name,
                        message=f"Local cache has no data for {api_name}.",
                        suggestion=f"python update_data.py --api {api_name} --start-date {check_start} --end-date {check_end}",
                        update_requests=(_update_request_for_api(api_name, check_start, check_end),),
                    )
                )
                continue
            if api_name == "stock_basic":
                issue = self._check_stock_basic_statuses()
                if issue is not None:
                    issues.append(issue)
                continue
            min_date = status.get("min_date")
            max_date = status.get("max_date")
            if min_date and check_start < str(min_date):
                issues.append(
                    ReadinessIssue(
                        api_name=api_name,
                        message=(
                            f"{api_name} starts at {min_date}, "
                            f"missing required start {_display_date(check_start)}."
                        ),
                        suggestion=f"python update_data.py --api {api_name} --start-date {check_start} --end-date {min_date}",
                        update_requests=(_update_request(api_name, check_start, str(min_date)),),
                    )
                )
            elif max_date and check_end > str(max_date):
                issues.append(
                    ReadinessIssue(
                        api_name=api_name,
                        message=f"{api_name} ends at {max_date}, missing requested end {config.end_date}.",
                        suggestion=f"python update_data.py --api {api_name} --start-date {max_date} --end-date {check_end}",
                        update_requests=(_update_request(api_name, str(max_date), check_end),),
                    )
                )
        return issues

    def _check_stock_basic_statuses(self) -> ReadinessIssue | None:
        try:
            frame = self.backend.fetch("stock_basic")
        except Exception:
            return None
        if frame is None or frame.empty or "list_status" not in frame.columns:
            missing = sorted(_REQUIRED_STOCK_BASIC_LIST_STATUSES)
        else:
            present = {
                str(value).strip()
                for value in frame["list_status"].tolist()
                if str(value).strip()
            }
            missing = sorted(_REQUIRED_STOCK_BASIC_LIST_STATUSES - present)
        if not missing:
            return None
        names = ", ".join(missing)
        return ReadinessIssue(
            api_name="stock_basic",
            message=f"stock_basic is missing list_status {names} for historical backtests.",
            suggestion="python update_data.py --api stock_basic",
            update_requests=(_update_request("stock_basic"),),
        )

    def _check_required_indexes(self, config, start: str, end: str) -> ReadinessIssue | None:
        benchmark = infer_strategy_benchmark(getattr(config, "strategy_path", None)) or getattr(config, "benchmark", None)
        requirements: dict[str, tuple[str, str]] = {}

        def add_requirement(symbol, required_start: str) -> None:
            ts_code = to_tushare_code(str(symbol))
            current = requirements.get(ts_code)
            if current is None or required_start < current[1]:
                requirements[ts_code] = (str(symbol), required_start)

        if benchmark:
            add_requirement(benchmark, start)
        for symbol, count in infer_strategy_index_price_requirements(getattr(config, "strategy_path", None)).items():
            lookback_days = max(int(count) * 3 + 7, 14)
            calendar_required_start = (
                datetime.strptime(normalize_date(config.start_date), "%Y%m%d")
                - timedelta(days=lookback_days)
            ).strftime("%Y%m%d")
            required_start, _ = self._market_date_bounds(calendar_required_start, end)
            add_requirement(symbol, required_start)

        incomplete = []
        update_requests = []
        missing_api_names = set()
        for ts_code, (symbol, required_start) in requirements.items():
            api_name = "sw_daily" if is_tushare_sw_index_code(ts_code) else "index_daily"
            try:
                frame = self.backend.fetch(
                    api_name,
                    ts_code=ts_code,
                    start_date=required_start,
                    end_date=end,
                )
            except Exception:
                frame = None
            min_date, max_date = _frame_date_bounds(frame, "trade_date")
            if min_date is not None and max_date is not None and required_start >= min_date and end <= max_date:
                continue

            if min_date is None or max_date is None:
                missing_start, missing_end = required_start, end
                detail = "has no local data"
            else:
                missing = []
                if required_start < min_date:
                    missing.append(f"starts at {min_date}, missing requested start {required_start}")
                    missing_start, missing_end = required_start, min_date
                else:
                    missing_start, missing_end = max_date, end
                if end > max_date:
                    missing.append(f"ends at {max_date}, missing requested end {end}")
                    missing_start, missing_end = missing_start, end
                detail = ", ".join(missing)
            incomplete.append(f"{symbol}({ts_code}) {detail}")
            missing_api_names.add(api_name)
            update_requests.append(
                _update_request(api_name, missing_start, missing_end, ts_code=ts_code)
            )

        if not incomplete:
            return None
        suggestion = " && ".join(
            f"python -m jq_tushare_sdk.cli update-data --api {request.api_name} "
            f"--start {request.start_date} --end {request.end_date} --cache-db <cache_db> "
            f"--ts-code {dict(request.params)['ts_code']}"
            for request in update_requests
        )
        issue_api_name = next(iter(missing_api_names)) if len(missing_api_names) == 1 else "index_daily"
        return ReadinessIssue(
            api_name=issue_api_name,
            message=f"{issue_api_name} is incomplete for required indexes: {', '.join(incomplete)}.",
            suggestion=suggestion,
            update_requests=tuple(update_requests),
        )

    def _check_income(self, config, start: str, end: str) -> ReadinessIssue | None:
        required_periods = required_income_periods(start, end)
        status = self.backend.status("income")
        if not status.get("exists") or int(status.get("record_count", 0) or 0) == 0:
            return ReadinessIssue(
                api_name="income",
                message="Local cache has no data for income.",
                suggestion=(
                    "python -m jq_tushare_sdk.cli update-data --api income "
                    f"--start {start} --end {end} --cache-db <cache_db>"
                ),
                update_requests=tuple(
                    _update_request("income", period=period)
                    for period in required_periods
                ),
            )

        missing_periods = []
        for period in required_periods:
            try:
                frame = self.backend.fetch("income", period=period)
            except Exception:
                frame = None
            if frame is None or frame.empty:
                missing_periods.append(period)

        if not missing_periods:
            return None

        return ReadinessIssue(
            api_name="income",
            message=(
                "income is missing required consecutive quarters for backtest "
                f"{config.start_date} to {config.end_date}: {', '.join(missing_periods)}."
            ),
            suggestion=(
                "python -m jq_tushare_sdk.cli update-data --api income "
                f"--start {start} --end {end} --cache-db <cache_db>"
            ),
            update_requests=tuple(
                _update_request("income", period=period)
                for period in missing_periods
            ),
        )

    def _check_index_weight(self, config, start: str, end: str) -> ReadinessIssue | None:
        symbols = infer_strategy_index_symbols(getattr(config, "strategy_path", None))
        lookback_start = _index_weight_lookback_start(start)
        if symbols:
            missing = []
            update_requests = []
            for symbol in symbols:
                ts_code = to_tushare_code(symbol)
                try:
                    frame = self.backend.fetch("index_weight", index_code=ts_code, end_date=start)
                except Exception:
                    frame = None
                if frame is None or frame.empty:
                    missing.append(f"{symbol}({ts_code})")
                    update_requests.append(_update_request("index_weight", lookback_start, end, index_code=ts_code))
            if not missing:
                return None
            return ReadinessIssue(
                api_name="index_weight",
                message=(
                    "Local cache has no index_weight data on or before requested start "
                    f"{config.start_date} for index {', '.join(missing)}."
                ),
                suggestion=(
                    "python -m jq_tushare_sdk.cli update-data --api index_weight "
                    f"--start {lookback_start} --end {end} --cache-db <cache_db>"
                ),
                update_requests=tuple(update_requests),
            )

        status = self.backend.status("index_weight")
        if not status.get("exists") or int(status.get("record_count", 0) or 0) == 0:
            return ReadinessIssue(
                api_name="index_weight",
                message="Local cache has no data for index_weight.",
                suggestion=f"python update_data.py --api index_weight --start-date {lookback_start} --end-date {end}",
                update_requests=(_update_request("index_weight", lookback_start, end),),
            )
        min_date = status.get("min_date")
        if min_date and start < str(min_date):
            return ReadinessIssue(
                api_name="index_weight",
                message=f"index_weight starts at {min_date}, missing requested start {config.start_date}.",
                suggestion=f"python update_data.py --api index_weight --start-date {lookback_start} --end-date {min_date}",
                update_requests=(_update_request("index_weight", lookback_start, str(min_date)),),
            )
        return None

    def _check_strategy_funds(self, config, start: str, end: str) -> ReadinessIssue | None:
        symbols = infer_strategy_fund_symbols(getattr(config, "strategy_path", None))
        if not symbols:
            return None
        missing = []
        update_requests = []
        for symbol in symbols:
            ts_code = to_tushare_code(symbol)
            for api_name in ("fund_daily", "fund_adj"):
                api_missing, api_requests = self._check_symbol_daily_api(api_name, symbol, ts_code, start, end)
                missing.extend(api_missing)
                update_requests.extend(api_requests)
        if not missing:
            return None
        commands = [
            f"python -m jq_tushare_sdk.cli update-data --api {request.api_name} "
            f"--start {request.start_date} --end {request.end_date} --cache-db <cache_db> "
            f"--ts-code {dict(request.params).get('ts_code')}"
            for request in update_requests
        ]
        return ReadinessIssue(
            api_name="fund_daily",
            message=(
                "Local cache has incomplete fund_daily/fund_adj data between "
                f"{config.start_date} and {config.end_date} for ETF/fund {', '.join(missing)}."
            ),
            suggestion=" && ".join(commands),
            update_requests=tuple(update_requests),
        )

    def _check_symbol_daily_api(self, api_name: str, symbol: str, ts_code: str, start: str, end: str):
        try:
            frame = self.backend.fetch(
                api_name,
                ts_code=ts_code,
                start_date=start,
                end_date=end,
            )
        except Exception:
            frame = None
        min_date, max_date = _frame_date_bounds(frame, "trade_date")
        if min_date is None or max_date is None:
            return [f"{symbol}({ts_code}) {api_name}"], [_update_request(api_name, start, end, ts_code=ts_code)]
        missing = []
        update_requests = []
        if start < min_date:
            missing.append(f"{symbol}({ts_code}) {api_name} starts at {min_date}")
            update_requests.append(_update_request(api_name, start, min_date, ts_code=ts_code))
        if end > max_date:
            missing.append(f"{symbol}({ts_code}) {api_name} ends at {max_date}")
            update_requests.append(_update_request(api_name, max_date, end, ts_code=ts_code))
        return missing, update_requests

    def _market_date_bounds(self, start: str, end: str) -> tuple[str, str]:
        try:
            frame = self.backend.fetch("trade_cal", exchange="SSE", start_date=start, end_date=end)
        except Exception:
            return start, end
        if frame is None or frame.empty or "cal_date" not in frame.columns:
            return start, end
        calendar_values = [
            value
            for value in (normalize_date(item) for item in frame["cal_date"].dropna().tolist())
            if start <= value <= end
        ]
        if not calendar_values or min(calendar_values) > start or max(calendar_values) < end:
            return start, end
        open_frame = frame
        if "is_open" in frame.columns:
            try:
                open_frame = frame[frame["is_open"].astype(int) == 1]
            except Exception:
                open_frame = frame
        values = [
            value
            for value in (normalize_date(item) for item in open_frame["cal_date"].dropna().tolist())
            if start <= value <= end
        ]
        if not values:
            return start, end
        return min(values), max(values)


def infer_strategy_benchmark(strategy_path) -> str | None:
    if not strategy_path:
        return None
    path = Path(strategy_path)
    if not path.is_file():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return None

    module_env: dict[str, object] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            value = _evaluate_static_expression(statement.value, module_env)
            for target in statement.targets:
                if isinstance(target, ast.Name) and value is not _UNRESOLVED:
                    module_env[target.id] = value

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    for lifecycle_name in ("initialize", "process_initialize"):
        lifecycle = functions.get(lifecycle_name)
        if lifecycle is None:
            continue
        benchmark = _benchmark_from_lifecycle(
            lifecycle,
            module_env,
            functions,
            visited=set(),
        )
        if benchmark:
            return benchmark
    return None


def infer_strategy_index_symbols(strategy_path) -> list[str]:
    if not strategy_path:
        return []
    path = Path(strategy_path)
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return []

    module_env: dict[str, object] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            value = _evaluate_static_expression(statement.value, module_env)
            for target in statement.targets:
                if isinstance(target, ast.Name) and value is not _UNRESOLVED:
                    module_env[target.id] = value

    symbols = []
    seen = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_name(node.func, "get_index_stocks") and node.args):
            continue
        value = _evaluate_static_expression(node.args[0], module_env)
        if isinstance(value, str) and value not in seen:
            symbols.append(value)
            seen.add(value)
    return symbols


def infer_strategy_fund_symbols(strategy_path) -> list[str]:
    if not strategy_path:
        return []
    path = Path(strategy_path)
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return []

    symbols = []
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if is_tushare_fund_code(value) and value not in seen:
                symbols.append(value)
                seen.add(value)
    return symbols


def infer_strategy_price_lookback_start(strategy_path, start_date: str) -> str:
    max_count = infer_strategy_max_price_count(strategy_path)
    if max_count <= 0:
        return normalize_date(start_date)
    lookback_days = max(max_count * 3 + 7, 14)
    start_dt = datetime.strptime(normalize_date(start_date), "%Y%m%d")
    return (start_dt - timedelta(days=lookback_days)).strftime("%Y%m%d")


def _display_date(value: str) -> str:
    normalized = normalize_date(value)
    return datetime.strptime(normalized, "%Y%m%d").strftime("%Y-%m-%d")


def infer_strategy_max_price_count(strategy_path) -> int:
    if not strategy_path:
        return 0
    path = Path(strategy_path)
    if not path.is_file():
        return 0
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return 0

    module_env = _module_static_env(tree)
    context_env = _context_numeric_env(tree, module_env)
    max_count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        count_node = _price_count_argument_node(node)
        if count_node is None:
            continue
        value = _evaluate_numeric_expression(count_node, module_env, context_env)
        if isinstance(value, (int, float)) and value > max_count:
            max_count = int(value)
    return max_count


def infer_strategy_index_price_requirements(strategy_path) -> dict[str, int]:
    if not strategy_path:
        return {}
    path = Path(strategy_path)
    if not path.is_file():
        return {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return {}

    module_env = _module_static_env(tree)
    context_env = _context_numeric_env(tree, module_env)
    visitor = _IndexPriceCallVisitor(module_env, context_env)
    visitor.visit(tree)
    requirements: dict[str, int] = {}
    calls = sorted(visitor.calls, key=lambda item: item[:2])
    for _, _, symbols, count in calls:
        for symbol in symbols:
            if is_tushare_index_code(symbol):
                requirements[symbol] = max(requirements.get(symbol, 0), count)
    return requirements


def _price_count_argument_node(node: ast.Call):
    for keyword in node.keywords:
        if keyword.arg == "count":
            return keyword.value

    if _is_name(node.func, "attribute_history") and len(node.args) >= 2:
        return node.args[1]
    if _is_name(node.func, "history") and node.args:
        return node.args[0]
    if _is_name(node.func, "get_price") and len(node.args) >= 4:
        return node.args[3]
    return None


def _benchmark_from_initialize(function: ast.FunctionDef, module_env: dict[str, object]) -> str | None:
    env = dict(module_env)
    for statement in function.body:
        if isinstance(statement, ast.Assign):
            value = _evaluate_static_expression(statement.value, env)
            for target in statement.targets:
                if isinstance(target, ast.Name) and value is not _UNRESOLVED:
                    env[target.id] = value
            continue
        call = statement.value if isinstance(statement, ast.Expr) else None
        if isinstance(call, ast.Call) and _is_name(call.func, "set_benchmark") and call.args:
            value = _evaluate_static_expression(call.args[0], env)
            return value if isinstance(value, str) else None
    return None


def _benchmark_from_lifecycle(
    function: ast.FunctionDef,
    module_env: dict[str, object],
    functions: dict[str, ast.FunctionDef],
    visited: set[str],
) -> str | None:
    if function.name in visited:
        return None
    visited.add(function.name)

    benchmark = _benchmark_from_initialize(function, module_env)
    if benchmark:
        return benchmark

    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        helper = functions.get(node.func.id)
        if helper is None:
            continue
        benchmark = _benchmark_from_lifecycle(
            helper,
            module_env,
            functions,
            visited,
        )
        if benchmark:
            return benchmark
    return None


_UNRESOLVED = object()


def _module_static_env(tree: ast.AST) -> dict[str, object]:
    module_env: dict[str, object] = {}
    for statement in getattr(tree, "body", []):
        if isinstance(statement, ast.Assign):
            value = _evaluate_static_expression(statement.value, module_env)
            for target in statement.targets:
                if isinstance(target, ast.Name) and value is not _UNRESOLVED:
                    module_env[target.id] = value
    return module_env


class _IndexPriceCallVisitor(ast.NodeVisitor):
    def __init__(self, module_env: dict[str, object], context_env: dict[str, float]):
        self.module_env = module_env
        self.context_env = context_env
        self.loop_bindings: list[dict[str, tuple[str, ...] | None]] = []
        self.calls: list[tuple[int, int, tuple[str, ...], int]] = []

    def visit_For(self, node: ast.For):
        self.visit(node.iter)
        bindings = {}
        if isinstance(node.target, ast.Name):
            bindings[node.target.id] = _static_loop_string_values(node.iter, self.module_env)
        self.loop_bindings.append(bindings)
        for statement in node.body:
            self.visit(statement)
        self.loop_bindings.pop()
        for statement in node.orelse:
            self.visit(statement)

    def visit_Call(self, node: ast.Call):
        if _is_name(node.func, "get_price") and node.args:
            count_node = _price_count_argument_node(node)
            if count_node is not None:
                count = _evaluate_numeric_expression(count_node, self.module_env, self.context_env)
                if isinstance(count, (int, float)) and count > 0:
                    symbols = self._resolve_symbols(node.args[0])
                    if symbols:
                        self.calls.append(
                            (
                                getattr(node, "lineno", -1),
                                getattr(node, "col_offset", -1),
                                symbols,
                                int(count),
                            )
                        )
        self.generic_visit(node)

    def _resolve_symbols(self, node: ast.AST) -> tuple[str, ...]:
        if isinstance(node, ast.Name):
            for bindings in reversed(self.loop_bindings):
                if node.id in bindings:
                    return bindings[node.id] or ()
        value = _evaluate_static_expression(node, self.module_env)
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)):
            return tuple(item for item in value if isinstance(item, str))
        if isinstance(value, set):
            return tuple(sorted(item for item in value if isinstance(item, str)))
        return ()


def _static_loop_string_values(node: ast.AST, module_env: dict[str, object]) -> tuple[str, ...] | None:
    values = _evaluate_static_expression(node, module_env)
    if not isinstance(values, (list, tuple, set)):
        return None
    strings = [value for value in values if isinstance(value, str)]
    return tuple(dict.fromkeys(strings))


def _context_numeric_env(tree: ast.AST, module_env: dict[str, object]) -> dict[str, float]:
    context_env: dict[str, float] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = _evaluate_numeric_expression(node.value, module_env, context_env)
        if not isinstance(value, (int, float)):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "context"
            ):
                context_env[target.attr] = float(value)
    return context_env


def _evaluate_numeric_expression(node: ast.AST, module_env: dict[str, object], context_env: dict[str, float]):
    value = _evaluate_static_expression(node, module_env)
    if isinstance(value, (int, float)):
        return value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "context":
        return context_env.get(node.attr, _UNRESOLVED)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        operand = _evaluate_numeric_expression(node.operand, module_env, context_env)
        return -operand if isinstance(operand, (int, float)) else _UNRESOLVED
    if isinstance(node, ast.BinOp):
        left = _evaluate_numeric_expression(node.left, module_env, context_env)
        right = _evaluate_numeric_expression(node.right, module_env, context_env)
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return _UNRESOLVED
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div) and right:
            return left / right
        if isinstance(node.op, ast.FloorDiv) and right:
            return left // right
    return _UNRESOLVED


def _evaluate_static_expression(node: ast.AST, env: dict[str, object]):
    try:
        return ast.literal_eval(node)
    except Exception:
        pass
    if isinstance(node, ast.List):
        values = [_evaluate_static_expression(element, env) for element in node.elts]
        return values if all(value is not _UNRESOLVED for value in values) else _UNRESOLVED
    if isinstance(node, ast.Tuple):
        values = tuple(_evaluate_static_expression(element, env) for element in node.elts)
        return values if all(value is not _UNRESOLVED for value in values) else _UNRESOLVED
    if isinstance(node, ast.Set):
        values = {_evaluate_static_expression(element, env) for element in node.elts}
        return values if _UNRESOLVED not in values else _UNRESOLVED
    if isinstance(node, ast.Name):
        return env.get(node.id, _UNRESOLVED)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        mapping = _evaluate_static_expression(node.func.value, env)
        key = _evaluate_static_expression(node.args[0], env) if node.args else _UNRESOLVED
        default = _evaluate_static_expression(node.args[1], env) if len(node.args) > 1 else None
        if isinstance(mapping, dict) and key is not _UNRESOLVED:
            return mapping.get(key, default)
    return _UNRESOLVED


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _index_weight_lookback_start(start_date: str) -> str:
    start_dt = datetime.strptime(normalize_date(start_date), "%Y%m%d")
    return (start_dt - timedelta(days=370)).strftime("%Y%m%d")


def _update_request(api_name: str, start_date: str | None = None, end_date: str | None = None, **params) -> DataUpdateRequest:
    return DataUpdateRequest(
        api_name=api_name,
        start_date=normalize_date(start_date) if start_date else None,
        end_date=normalize_date(end_date) if end_date else None,
        params=tuple(sorted((key, str(value)) for key, value in params.items())),
    )


def _update_request_for_api(api_name: str, start_date: str, end_date: str) -> DataUpdateRequest:
    if api_name == "stock_basic":
        return _update_request(api_name)
    return _update_request(api_name, start_date, end_date)


def _update_request_sort_key(request: DataUpdateRequest):
    return (
        _UPDATE_PRIORITY.get(request.api_name, 99),
        request.api_name,
        request.start_date or "",
        request.end_date or "",
        request.params,
    )


def _format_update_request(request: DataUpdateRequest) -> str:
    date_part = ""
    if request.start_date or request.end_date:
        date_part = f" {request.start_date or ''}-{request.end_date or ''}"
    params = dict(request.params)
    param_part = ""
    if params:
        param_part = " " + " ".join(f"{key}={value}" for key, value in sorted(params.items()))
    return f"{request.api_name}{date_part}{param_part}".strip()


def _frame_date_bounds(frame, column: str) -> tuple[str | None, str | None]:
    if frame is None or frame.empty or column not in frame.columns:
        return None, None
    values = [normalize_date(value) for value in frame[column].dropna().tolist()]
    if not values:
        return None, None
    return min(values), max(values)
