import ast
from dataclasses import dataclass
from pathlib import Path

from jq_tushare_sdk.data.code_map import normalize_date
from jq_tushare_sdk.data.code_map import to_tushare_code


@dataclass(frozen=True)
class ReadinessIssue:
    api_name: str
    message: str
    suggestion: str


class DataReadinessCheck:
    def __init__(self, backend):
        self.backend = backend

    def check_required(self, config, apis: list[str]) -> list[ReadinessIssue]:
        start = normalize_date(config.start_date)
        end = normalize_date(config.end_date)
        issues = []
        for api_name in apis:
            if api_name == "index_daily":
                issue = self._check_benchmark_index(config, start, end)
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
            status = self.backend.status(api_name)
            if not status.get("exists") or int(status.get("record_count", 0) or 0) == 0:
                issues.append(
                    ReadinessIssue(
                        api_name=api_name,
                        message=f"Local cache has no data for {api_name}.",
                        suggestion=f"python update_data.py --api {api_name} --start-date {start} --end-date {end}",
                    )
                )
                continue
            min_date = status.get("min_date")
            max_date = status.get("max_date")
            if min_date and start < str(min_date):
                issues.append(
                    ReadinessIssue(
                        api_name=api_name,
                        message=f"{api_name} starts at {min_date}, missing requested start {config.start_date}.",
                        suggestion=f"python update_data.py --api {api_name} --start-date {start} --end-date {min_date}",
                    )
                )
            elif max_date and end > str(max_date):
                issues.append(
                    ReadinessIssue(
                        api_name=api_name,
                        message=f"{api_name} ends at {max_date}, missing requested end {config.end_date}.",
                        suggestion=f"python update_data.py --api {api_name} --start-date {max_date} --end-date {end}",
                    )
                )
        return issues

    def _check_benchmark_index(self, config, start: str, end: str) -> ReadinessIssue | None:
        benchmark = infer_strategy_benchmark(getattr(config, "strategy_path", None)) or getattr(config, "benchmark", None)
        if not benchmark:
            return None
        ts_code = to_tushare_code(str(benchmark))
        try:
            frame = self.backend.fetch(
                "index_daily",
                ts_code=ts_code,
                start_date=start,
                end_date=end,
            )
        except Exception:
            frame = None
        if frame is not None and not frame.empty:
            return None
        return ReadinessIssue(
            api_name="index_daily",
            message=f"Local cache has no index_daily data for benchmark {benchmark} between {config.start_date} and {config.end_date}.",
            suggestion=f"python update_data.py --api index_daily --start-date {start} --end-date {end} --ts_code {ts_code}",
        )

    def _check_income(self, config, start: str, end: str) -> ReadinessIssue | None:
        status = self.backend.status("income")
        if not status.get("exists") or int(status.get("record_count", 0) or 0) == 0:
            return ReadinessIssue(
                api_name="income",
                message="Local cache has no data for income.",
                suggestion=f"python update_data.py --api income --start-date {start} --end-date {end}",
            )

        for period in self._income_periods_for_range(end):
            frame = self.backend.fetch("income", period=period)
            if frame is not None and not frame.empty:
                return None

        return ReadinessIssue(
            api_name="income",
            message=f"income has no recent quarterly data for requested end {config.end_date}.",
            suggestion=f"python update_data.py --api income --start-date {start} --end-date {end}",
        )

    def _check_index_weight(self, config, start: str, end: str) -> ReadinessIssue | None:
        symbols = infer_strategy_index_symbols(getattr(config, "strategy_path", None))
        if symbols:
            missing = []
            for symbol in symbols:
                ts_code = to_tushare_code(symbol)
                try:
                    frame = self.backend.fetch("index_weight", index_code=ts_code, end_date=start)
                except Exception:
                    frame = None
                if frame is None or frame.empty:
                    missing.append(f"{symbol}({ts_code})")
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
                    f"--start 20250101 --end {end} --cache-db <cache_db>"
                ),
            )

        status = self.backend.status("index_weight")
        if not status.get("exists") or int(status.get("record_count", 0) or 0) == 0:
            return ReadinessIssue(
                api_name="index_weight",
                message="Local cache has no data for index_weight.",
                suggestion=f"python update_data.py --api index_weight --start-date {start} --end-date {end}",
            )
        min_date = status.get("min_date")
        if min_date and start < str(min_date):
            return ReadinessIssue(
                api_name="index_weight",
                message=f"index_weight starts at {min_date}, missing requested start {config.start_date}.",
                suggestion=f"python update_data.py --api index_weight --start-date {start} --end-date {min_date}",
            )
        return None

    def _income_periods_for_range(self, end_date: str) -> list[str]:
        end = normalize_date(end_date)
        year = int(end[:4])
        month = int(end[4:6])
        current_quarter = ((month - 1) // 3) + 1
        periods = []
        for offset in range(0, 6):
            quarter_index = current_quarter - offset
            period_year = year
            while quarter_index <= 0:
                quarter_index += 4
                period_year -= 1
            periods.append(f"{period_year}q{quarter_index}")
        return periods


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

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "initialize":
            benchmark = _benchmark_from_initialize(node, module_env)
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


_UNRESOLVED = object()


def _evaluate_static_expression(node: ast.AST, env: dict[str, object]):
    try:
        return ast.literal_eval(node)
    except Exception:
        pass
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
