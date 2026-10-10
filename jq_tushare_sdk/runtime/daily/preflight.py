"""Non-executing strategy checks and read-only daily-cache diagnostics.

These checks describe what can be proved from source and the local cache. They
are neither a Python sandbox nor a guarantee of native JoinQuant equivalence.
The worker remains responsible for validating every actual data request.
"""
from __future__ import annotations

import ast
from datetime import date, datetime, timedelta
from functools import lru_cache
import math
from pathlib import Path
import re
import sqlite3

from jq_tushare_sdk.data.code_map import (
    is_tushare_fund_code, is_tushare_index_code, is_tushare_sw_index_code,
    to_tushare_code,
)


_UNKNOWN = object()
_SECURITY = re.compile(r"\d{6}\.(?:XSHG|XSHE|SH|SZ|BJ|SI|CSI)\Z")
_JQ_MODULES = {"jqdata", "jq_tushare_sdk.api.jqdata"}
_NETWORK_MODULES = {"redis", "requests", "httpx", "aiohttp", "urllib", "http", "socket", "websocket", "websockets", "tushare", "efinance", "akshare", "smtplib", "ftplib"}
_TRADING_MODULES = {"xtquant", "easytrader", "ptrade", "jq_order_wrapper", "ptrade_redis_adapter"}
_KNOWN_LIBRARIES = {"numpy", "pandas", "scipy", "talib", "math", "datetime", "time", "collections", "functools", "itertools", "operator", "statistics", "typing", "enum", "dataclasses", "copy", "decimal", "fractions", "re", "json", "logging", "random", "bisect", "heapq"}
_PRICE_ARGS = {"get_price": ("frequency", 4, "daily"), "history": ("unit", 1, "1d"), "attribute_history": ("unit", 2, "1d"), "get_bars": ("unit", 2, "1d")}
_DYNAMIC_UNIVERSE = {"get_index_stocks", "get_all_securities", "get_fundamentals", "get_fundamentals_continuously"}


@lru_cache(maxsize=1)
def _runtime_exports():
    """Read the checked-in export declaration without executing runtime code."""
    tree = ast.parse((Path(__file__).parents[2] / "api" / "globals.py").read_text(encoding="utf-8"))
    supported, unsupported = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            if any(isinstance(t, ast.Name) and t.id == "exports" for t in node.targets):
                supported.update(k.value for k in node.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str))
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "unsupported" for n in ast.walk(node)):
                unsupported.update(n.value for n in node.iter.elts if isinstance(n, ast.Constant) and isinstance(n.value, str))
    return supported, unsupported


def _key(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _key(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _value(node, values):
    if node is None:
        return _UNKNOWN
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Name, ast.Attribute)):
        return values.get(_key(node), _UNKNOWN)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = [_value(item, values) for item in node.elts]
        return items if all(item is not _UNKNOWN for item in items) else _UNKNOWN
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _value(node.operand, values)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value if isinstance(node.op, ast.UAdd) else -value
    return _UNKNOWN


def _source_facts(tree):
    aliases, dependencies, assignments = {}, set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                dependencies.add(item.name)
                aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            dependencies.add(module or "相对导入")
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = f"{module}.{item.name}"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if _key(target):
                    assignments.setdefault(_key(target), []).append(node.value)
        elif isinstance(node, ast.AugAssign) and _key(node.target):
            assignments.setdefault(_key(node.target), []).append(None)
    # A name assigned different values is intentionally unresolved. This avoids
    # claiming a branch-selected or reassigned schedule is a known close time.
    values = {}
    for _ in range(min(len(assignments) + 1, 20)):
        changed = False
        for name, nodes in assignments.items():
            resolved = [_value(node, values) for node in nodes]
            if resolved and all(item is not _UNKNOWN and item == resolved[0] for item in resolved):
                if name not in values or values[name] != resolved[0]:
                    values[name], changed = resolved[0], True
        if not changed:
            break
    # Also recognize simple function aliases: prices = jq.get_price.
    for _ in range(10):
        changed = False
        for name, nodes in assignments.items():
            if len(nodes) != 1 or not _key(nodes[0]):
                continue
            target = _resolve_name(_key(nodes[0]), aliases)
            if target != name and aliases.get(name) != target:
                aliases[name], changed = target, True
        if not changed:
            break
    return aliases, dependencies, values


def _resolve_name(name, aliases):
    if not name:
        return ""
    for _ in range(10):
        head, _, tail = name.partition(".")
        target = aliases.get(head)
        if not target:
            break
        replacement = target + ("." + tail if tail else "")
        if replacement == name:
            break
        name = replacement
    return name


def _call_name(node, aliases):
    name = _resolve_name(_key(node.func), aliases)
    for prefix in _JQ_MODULES:
        if name.startswith(prefix + "."):
            return name[len(prefix) + 1:], True
    return name, False


def _argument(call, keyword, position, default=_UNKNOWN):
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    if any(item.arg is None for item in call.keywords) or any(isinstance(item, ast.Starred) for item in call.args):
        return None
    if position is not None and len(call.args) > position:
        return call.args[position]
    return ast.Constant(value=default) if default is not _UNKNOWN else None


def _scope_bindings(scope):
    """Names bound in one lexical scope, excluding nested function bodies."""
    names = set()
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = scope.args
        names.update(item.arg for item in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs])
        names.update(item.arg for item in (arguments.vararg, arguments.kwarg) if item is not None)

    class Bindings(ast.NodeVisitor):
        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Store):
                names.add(node.id)

        def visit_FunctionDef(self, node):
            names.add(node.name)

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_Lambda(self, node):
            return

        def visit_Import(self, node):
            names.update(item.asname or item.name.split(".")[0] for item in node.names)

        def visit_ImportFrom(self, node):
            names.update(item.asname or item.name for item in node.names if item.name != "*")

        def visit_ExceptHandler(self, node):
            if node.name:
                names.add(node.name)
            self.generic_visit(node)

    visitor = Bindings()
    body = scope.body if isinstance(scope.body, list) else [scope.body]
    for statement in body:
        visitor.visit(statement)
    return names


def analyze_source(source: str) -> dict:
    """Inspect source with AST only; never import or execute submitted code."""
    result = {"status": "blocked", "can_run": False, "errors": [], "warnings": [], "schedules": [], "dependencies": [], "securities": [], "summary": ""}

    def issue(group, code, message, node=None):
        item = {"code": code, "message": message, "line": getattr(node, "lineno", None)}
        if item not in result[group]:
            result[group].append(item)

    try:
        tree = ast.parse(source)
    except (SyntaxError, TypeError, ValueError) as error:
        result["errors"].append({"code": "syntax_error", "message": f"代码无法解析：{getattr(error, 'msg', str(error))}", "line": getattr(error, "lineno", None)})
        result["summary"] = "请先修正 Python 语法。"
        return result
    aliases, dependencies, values = _source_facts(tree)
    supported, unsupported = _runtime_exports()
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    bindings = {}

    def bound_callable(call):
        if not isinstance(call.func, ast.Name):
            return False
        node = call
        while node is not None:
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if node not in bindings:
                    bindings[node] = _scope_bindings(node)
                if call.func.id in bindings[node]:
                    return True
            node = parents.get(node)
        return False

    top_functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    result["dependencies"] = sorted(dependencies)
    result["securities"] = sorted({node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and _SECURITY.fullmatch(node.value)})
    if "initialize" not in top_functions:
        issue("errors", "missing_initialize", "缺少顶层 initialize(context) 函数。")
    else:
        initialization = top_functions["initialize"]
        positional = len(initialization.args.posonlyargs) + len(initialization.args.args)
        required = positional - len(initialization.args.defaults)
        if required > 1 or (positional == 0 and initialization.args.vararg is None) or any(value is None for value in initialization.args.kw_defaults):
            issue("errors", "initialize_signature", "initialize 必须能以一个 context 位置参数调用。", initialization)
    for name, node in top_functions.items():
        if isinstance(node, ast.AsyncFunctionDef):
            issue("errors", "async_callback", f"首版不支持异步策略函数 {name}。", node)
        if name in {"handle_data", "handle_tick", "before_trading_start", "after_trading_end"}:
            issue("errors", "unsupported_lifecycle", f"日线模块不执行 {name}；请显式改写为 run_daily(..., time='after_close') 并确认交易含义。", node)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for module in modules:
                root = module.split(".")[0]
                if root in _NETWORK_MODULES | _TRADING_MODULES:
                    issue("errors", "external_signal_dependency", f"检测到外部行情/网络/交易依赖 {module}。首版只使用本地缓存与模拟账户，请移除外部信号发送或改写依赖后再运行。", node)
                elif module not in _JQ_MODULES and root not in _KNOWN_LIBRARIES:
                    issue("warnings", "unverified_dependency", f"依赖 {module or '相对模块'} 未验证；导入预检不会执行或检查其内部代码。", node)
            if isinstance(node, ast.ImportFrom) and node.module in _JQ_MODULES:
                for item in node.names:
                    if item.name not in supported | unsupported | {"*", "g", "log"}:
                        issue("errors", "unknown_api", f"本地运行时未导出 {item.name}。", node)
        if not isinstance(node, ast.Call):
            continue
        name, qualified = _call_name(node, aliases)
        if name in unsupported or (qualified and name not in supported and name.split(".")[0] not in {"log", "finance", "g"}):
            issue("errors", "unsupported_api", f"本地运行时尚未实现 {name}，不能直接导入运行。", node)
        elif "." not in name and name not in supported and name not in top_functions and name.startswith(("get_", "order_", "cancel_order")):
            if bound_callable(node):
                issue("warnings", "dynamic_callable", f"{name} 是本地绑定的函数变量；其实际目标需运行时检查，不能仅按名称判断为聚宽接口。", node)
            else:
                issue("errors", "unknown_api", f"本地运行时未导出 {name}；如果它来自自定义模块，请使用明确模块前缀并检查依赖。", node)
        if name in {"send_message", "subscribe", "unsubscribe", "set_subportfolios", "get_current_tick"}:
            issue("errors", "unsupported_api", f"日线首版不支持 {name}。", node)
        if name in {"exec", "eval", "__import__", "importlib.import_module"}:
            issue("errors", "dynamic_code", "动态执行/导入无法静态确认依赖与调度，请改为可检查的明确调用。", node)
        if name in {"run_daily", "run_weekly", "run_monthly"}:
            pos = 1 if name == "run_daily" else 2
            label = _value(_argument(node, "time", pos, "open"), values)
            callback = _key(_argument(node, "func", 0)) or "动态函数"
            callback = _resolve_name(callback, aliases)
            result["schedules"].append({"function": callback, "kind": name[4:], "time": str(label) if label is not _UNKNOWN else "无法静态确定", "line": node.lineno})
            if callback not in top_functions:
                issue("errors", "dynamic_callback", "调度回调必须是当前策略的具名顶层函数，无法确认该回调。", node)
            if label is _UNKNOWN:
                issue("errors", "dynamic_schedule", "无法静态确定调度时间；请显式指定 time='after_close'。", node)
            elif not isinstance(label, str) or label not in {"after_close", "close"}:
                match = re.fullmatch(r"(\d{2}):(\d{2})", label) if isinstance(label, str) else None
                after_close = bool(match and 15 <= int(match[1]) <= 23 and 0 <= int(match[2]) <= 59)
                if not after_close:
                    issue("errors", "intraday_schedule", f"调度时间 {label!r} 不符合日线收盘决策；请确认逻辑后改为 after_close，不能直接把盘中逻辑搬到收盘。", node)
                else:
                    issue("warnings", "close_time_policy", f"{label} 属收盘后调度；本模块统一按日线 15:30 决策，不区分收盘后行情时点。", node)
            elif label == "close":
                issue("warnings", "close_time_policy", "close 在本模块统一按日线 15:30 决策，委托下一交易日开盘模拟成交。", node)
        if name in _PRICE_ARGS:
            keyword, position, default = _PRICE_ARGS[name]
            frequency = _value(_argument(node, keyword, position, default), values)
            # Some native get_price callers put frequency in slot 4 (before
            # count); report a minute unit there too, rather than overlook it.
            legacy = _value(node.args[3], values) if name == "get_price" and len(node.args) > 3 else None
            if isinstance(legacy, str) and re.fullmatch(r"(?:\d+[mst]|minute|tick)", legacy):
                frequency = legacy
            if frequency is _UNKNOWN:
                issue("errors", "dynamic_frequency", f"无法确认 {name} 的数据频率；请明确使用 daily/1d。", node)
            elif frequency not in ("daily", "1d"):
                issue("errors", "intraday_data", f"{name} 请求 {frequency!r} 数据；本版仅支持 daily/1d。", node)
        if name in _DYNAMIC_UNIVERSE:
            issue("warnings", "dynamic_universe", f"{name} 的证券集合或数据范围由运行时决定；缓存预检不能证明该范围完整。", node)
        if name in {"get_fundamentals", "get_fundamentals_continuously", "finance.run_query"}:
            issue("warnings", "financial_visibility", "财务数据还需按运行账户的数据口径检查历史可见性，静态预检不证明 PIT 完整。", node)
    if not result["schedules"]:
        issue("warnings", "no_schedule", "未识别到日线调度；initialize 只在账户首次运行执行，请确认后续交易日的决策入口。")
    result["status"] = "blocked" if result["errors"] else "warning" if result["warnings"] else "compatible"
    result["can_run"] = not result["errors"]
    result["summary"] = (f"发现 {len(result['errors'])} 项阻止运行的问题，请先修改。" if result["errors"] else
                         f"静态检查未发现阻止项，另有 {len(result['warnings'])} 项需留意；实际执行仍会严格校验。" if result["warnings"] else
                         "已识别的接口和调度符合日线口径；此检查不执行代码，也不保证完整兼容或沙箱隔离。")
    return result


def _day(value):
    text = str(value)
    return datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 and text.isdigit() else date.fromisoformat(text[:10])


def _stamp(value):
    return value.strftime("%Y%m%d")


def _price_table(security):
    if is_tushare_sw_index_code(security):
        return "sw_daily"
    if is_tushare_index_code(security):
        return "index_daily"
    return "fund_daily_quote" if is_tushare_fund_code(security) else "daily_quote"


def _required_price_securities(source):
    """Only explicit quote/order arguments establish blocking quote scope.

    In particular, an index-membership argument is not an index-price request.
    Unresolved callback arguments and candidate-pool strings stay unverified.
    """
    try:
        tree = ast.parse(source)
    except (TypeError, SyntaxError, ValueError):
        return set(), []
    aliases, _, values = _source_facts(tree)
    arguments = {
        "get_price": ("security", 0), "attribute_history": ("security", 0),
        "get_bars": ("security", 0), "history": ("security_list", 3),
        "get_current_data": ("securities", 0), "set_benchmark": ("security", 0),
        "order": ("security", 0), "order_value": ("security", 0),
        "order_target": ("security", 0), "order_target_value": ("security", 0),
    }
    required, unresolved = set(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name, _ = _call_name(node, aliases)
        if name not in arguments:
            continue
        keyword, position = arguments[name]
        value = _value(_argument(node, keyword, position), values)
        candidates = value if isinstance(value, list) else [value]
        if candidates and all(isinstance(item, str) and _SECURITY.fullmatch(item) for item in candidates):
            required.update(candidates)
        else:
            unresolved.append(f"{name}（第 {node.lineno} 行）")
    return required, unresolved


def inspect_readiness(cache_db, start_date, benchmark, source, last_date=None, through_date=None) -> dict:
    """Read a consistent SQLite snapshot; no provider calls or strategy loading.

    With no through_date, inspect the next uncommitted exchange session only.
    ``can_run`` means no known local blocker in this inspection, not that every
    dynamic request, account holding, or financial vintage has been verified.
    """
    analysis = analyze_source(source)
    result = {"status": "blocked", "can_run": False, "next_date": None, "target_date": None,
              "latest_daily_date": None, "checks": [], "missing": [], "checked_dates": [],
              "static_securities": analysis["securities"], "required_securities": [],
              "scope_note": "只读检查下一交易日或指定区间、基准和明确价格/下单参数；其他代码字符串只作候选，不据此要求行情。结果不代表全市场或历史窗口完整。动态证券、账户持仓、公司行为和财务历史可见性仍需运行时验证；未在线查询数据源权限。"}

    def check(name, status, detail):
        result["checks"].append({"name": name, "status": status, "detail": detail})
        if status == "blocked":
            result["missing"].append(detail)

    def finish():
        unknown = any(item["status"] == "unknown" for item in result["checks"])
        result["status"] = "blocked" if result["missing"] else "partial" if unknown else "ready"
        result["can_run"] = not result["missing"] and bool(result["checked_dates"])
        return result

    check("数据源权限", "unknown", "本地缓存不能证明 Tushare 当前积分、接口权限和额度；本次未在线请求。")
    check("策略静态检查", "ready" if analysis["can_run"] else "blocked", analysis["summary"])
    if any(item["code"] in {"dynamic_universe", "financial_visibility", "unverified_dependency"} for item in analysis["warnings"]):
        check("动态数据范围", "unknown", "证券筛选、财务查询或外部模块的数据范围不能由当前源码静态确定。")
    try:
        start = _day(start_date)
        cursor = _day(last_date) + timedelta(days=1) if last_date else start
        end = _day(through_date) if through_date else None
        if cursor < start or (end and (end - cursor).days > 3660):
            raise ValueError("日期范围无效或超过单次 10 年检查范围")
    except (TypeError, ValueError) as error:
        check("日期", "blocked", f"检查日期无效：{error}")
        return finish()
    path = Path(cache_db).expanduser().resolve()
    if not path.is_file():
        check("本地缓存", "blocked", "本地缓存文件不存在；请先在数据管理中准备日线数据。")
        return finish()
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def schema(table, columns):
            found = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')} if table in tables else set()
            if not set(columns).issubset(found):
                check(table, "blocked", f"缓存缺少 {table} 表或所需字段 {', '.join(sorted(set(columns) - found))}。")
                return False
            return True

        if schema("daily_quote", ["trade_date", "ts_code", "open", "close", "vol"]):
            latest = connection.execute("SELECT MAX(trade_date) FROM daily_quote").fetchone()[0]
            result["latest_daily_date"] = _day(latest).isoformat() if latest else None
        if not schema("trade_calendar", ["exchange", "cal_date", "is_open"]):
            return finish()
        next_row = connection.execute("SELECT MIN(cal_date) FROM trade_calendar WHERE exchange='SSE' AND is_open=1 AND cal_date>=?", (_stamp(cursor),)).fetchone()[0]
        if not next_row:
            check("交易日历", "blocked", f"官方日历中未找到 {_stamp(cursor)} 起的下一交易日，请补齐日历。")
            return finish()
        next_day = _day(next_row)
        result["next_date"] = next_day.isoformat()
        end = end or next_day
        result["target_date"] = end.isoformat()
        if end < next_day:
            check("待运行交易日", "unknown", "指定区间没有尚未处理的交易日。")
            return finish()
        calendar_start, calendar_end = next_day - timedelta(days=40), end
        kinds = {item["kind"] for item in analysis["schedules"]}
        if "monthly" in kinds:
            calendar_end = date(end.year + (end.month == 12), end.month % 12 + 1, 1) - timedelta(days=1)
        if "weekly" in kinds:
            calendar_end += timedelta(days=6 - calendar_end.weekday())
        flags = {}
        for day_text, flag in connection.execute("SELECT cal_date,is_open FROM trade_calendar WHERE exchange='SSE' AND cal_date BETWEEN ? AND ?", (_stamp(calendar_start), _stamp(calendar_end))):
            if flag not in (0, 1) or day_text in flags:
                check("交易日历", "blocked", f"日历存在无效或重复记录：{day_text}。")
            flags[day_text] = flag
        expected = [_stamp(calendar_start + timedelta(days=n)) for n in range((calendar_end - calendar_start).days + 1)]
        gaps = [day for day in expected if day not in flags]
        if gaps:
            check("交易日历", "blocked", f"官方日历缺 {len(gaps)} 个自然日：{', '.join(gaps[:8])}。")
        else:
            check("交易日历", "ready", f"已核对 {calendar_start} 至 {calendar_end} 的逐日日历。")
        days = sorted(day for day, flag in flags.items() if flag == 1 and _stamp(next_day) <= day <= _stamp(end))
        result["checked_dates"] = [_day(day).isoformat() for day in days]
        previous = sorted(day for day, flag in flags.items() if flag == 1 and day < _stamp(next_day))
        if not previous:
            check("上一交易日", "blocked", "缺少下一运行日之前的官方交易日。")
            return finish()
        required_days = [previous[-1], *days]
        lower, upper = required_days[0], required_days[-1]
        if "daily_quote" in tables:
            available = {row[0] for row in connection.execute("SELECT DISTINCT trade_date FROM daily_quote WHERE trade_date BETWEEN ? AND ?", (lower, upper))}
            missing_days = sorted(set(days) - available)
            check("日线日期", "blocked" if missing_days else "ready", f"缺少日线日期：{', '.join(missing_days[:8])}。" if missing_days else f"{len(days)} 个待运行交易日均有市场日线；这不证明全市场证券齐全。")
        if schema("stock_basic", ["ts_code", "list_status"]):
            statuses = {row[0] for row in connection.execute("SELECT DISTINCT list_status FROM stock_basic")}
            check("证券基础信息", "ready" if {"L", "D"}.issubset(statuses) else "blocked", "已缓存上市与退市证券资料。" if {"L", "D"}.issubset(statuses) else "证券基础信息缺少上市或退市证券列表。")
        for table in ("stock_st", "stk_limit"):
            if not schema(table, ["ts_code", "trade_date"]) or not schema("security_state_coverage", ["api_name", "trade_date", "row_count"]):
                continue
            counts = dict(connection.execute(f'SELECT trade_date,COUNT(*) FROM "{table}" WHERE trade_date BETWEEN ? AND ? GROUP BY trade_date', (lower, upper)))
            markers = dict(connection.execute("SELECT trade_date,row_count FROM security_state_coverage WHERE api_name=? AND trade_date BETWEEN ? AND ?", (table, lower, upper)))
            invalid = [day for day in required_days if counts.get(day, 0) <= 0 or markers.get(day) != counts.get(day)]
            check(table, "blocked" if invalid else "ready", f"{table} 完整快照标记缺失或行数不一致：{', '.join(invalid[:8])}。" if invalid else f"{table} 的 {len(required_days)} 个日期快照行数与完成标记一致。")
        explicit_securities, unresolved = _required_price_securities(source)
        securities = sorted(explicit_securities | ({str(benchmark)} if benchmark else set()))
        result["required_securities"] = securities
        candidate_only = sorted(set(analysis["securities"]) - set(securities))
        if candidate_only:
            check("候选证券字符串", "unknown", f"以下代码未确认为行情/下单参数，未作行情完整性断言：{', '.join(candidate_only[:12])}。指数成分查询仍需运行时检查。")
        if unresolved:
            check("动态行情与下单范围", "unknown", f"无法静态确定证券参数：{', '.join(unresolved[:8])}；该部分数据仍需运行时核对。")
        for security in securities:
            code, table = to_tushare_code(security), _price_table(security)
            if not schema(table, ["ts_code", "trade_date", "open", "close", "vol"]):
                continue
            rows = {row[0]: row[1:] for row in connection.execute(f'SELECT trade_date,open,close,vol FROM "{table}" WHERE ts_code=? AND trade_date BETWEEN ? AND ?', (code, lower, upper))}
            invalid = []
            for day in required_days:
                values = rows.get(day)
                try:
                    opening, close, volume = (float(value) for value in values) if values else (math.nan,) * 3
                    valid = all(math.isfinite(v) for v in (opening, close, volume)) and close > 0 and volume >= 0 and opening >= 0 and (not volume or opening > 0)
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    invalid.append(day)
            check(f"行情 · {security}", "blocked" if invalid else "ready", f"{table} {security} 缺少有效行情：{', '.join(invalid[:8])}。" if invalid else f"{security} 在 {len(required_days)} 个所需日期有有效开收盘价与成交量。")
            if not is_tushare_index_code(code):
                factor_table = "fund_adj_factor" if is_tushare_fund_code(code) else "adj_factor"
                if schema(factor_table, ["ts_code", "trade_date", "adj_factor"]):
                    factors = dict(connection.execute(f'SELECT trade_date,adj_factor FROM "{factor_table}" WHERE ts_code=? AND trade_date BETWEEN ? AND ?', (code, lower, upper)))
                    invalid = [day for day in required_days if not _positive(factors.get(day))]
                    check(f"复权因子 · {security}", "blocked" if invalid else "ready", f"{factor_table} {security} 缺少有效复权因子：{', '.join(invalid[:8])}。" if invalid else f"{security} 所需日期的复权因子齐备；公司行为仍由运行时核对。")
                if not is_tushare_fund_code(code) and schema("stk_limit", ["ts_code", "trade_date", "up_limit", "down_limit"]):
                    limits = {row[0]: row[1:] for row in connection.execute("SELECT trade_date,up_limit,down_limit FROM stk_limit WHERE ts_code=? AND trade_date BETWEEN ? AND ?", (code, lower, upper))}
                    invalid = []
                    for day in required_days:
                        try:
                            high, low = map(float, limits.get(day, (None, None)))
                            valid = math.isfinite(high) and math.isfinite(low) and high > 0 and high >= low >= 0
                        except (TypeError, ValueError):
                            valid = False
                        if not valid:
                            invalid.append(day)
                    check(f"涨跌停 · {security}", "blocked" if invalid else "ready", f"{security} 缺少有效官方涨跌停价：{', '.join(invalid[:8])}。" if invalid else f"{security} 所需日期的官方涨跌停价齐备。")
        try:
            tree = ast.parse(source)
            aliases, _, values = _source_facts(tree)
            history_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _call_name(node, aliases)[0] in _PRICE_ARGS]
            if history_calls:
                check("历史查询窗口", "unknown", "检测到历史价格查询；窗口可能依赖上下文、跳过停牌和动态证券，当前诊断未证明每个历史样本完整，运行时仍会校验。")
        except (TypeError, SyntaxError, ValueError):
            pass
        if not analysis["securities"]:
            check("证券范围", "unknown", "未静态识别到证券代码，仅检查基础日线与所选基准；不代表策略数据齐备。")
    except (sqlite3.Error, OSError, TypeError, ValueError) as error:
        check("缓存读取", "blocked", f"无法完成只读缓存检查：{type(error).__name__}。请检查缓存结构与日期字段。")
    finally:
        if connection is not None:
            connection.close()
    return finish()


def _positive(value):
    try:
        number = float(value)
        return math.isfinite(number) and number > 0
    except (TypeError, ValueError):
        return False
