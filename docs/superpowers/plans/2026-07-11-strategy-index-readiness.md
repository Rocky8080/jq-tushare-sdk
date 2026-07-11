# Strategy Index Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically infer, validate, and backfill every count-based index price dependency used by a strategy, including loop-bound index lists and `000001.XSHG`.

**Architecture:** Add one shared index-code classifier in `code_map.py`, use it for DataPortal price routing, and add a call-aware AST analyzer in `readiness.py`. The readiness checker will merge benchmark and inferred strategy requirements, then produce symbol-specific `index_daily` update requests through the existing automatic update flow.

**Tech Stack:** Python 3.11, `ast`, pandas, SQLite-backed `TushareCacheBackend`, `unittest` existing suite.

## Global Constraints

- Do not modify strategy source files.
- Do not use future rows when satisfying a count-based history request.
- Keep unresolved dynamic symbols non-fatal.
- Preserve benchmark-only strategy behavior.
- Use real local cache probes rather than adding mock-data tests, per project rules.
- Do not expose credentials from uploaded strategy or log files.

---

### Task 1: Share Index Recognition and Route the Shanghai Composite Correctly

**Files:**
- Modify: `jq_tushare_sdk/data/code_map.py`
- Modify: `jq_tushare_sdk/data/portal.py:12-20,47-50,522-538`

**Interfaces:**
- Produces: `is_tushare_index_code(code: str) -> bool`.
- Consumes: `to_tushare_code(code: str) -> str`.

- [ ] **Step 1: Run a real-cache failing route probe**

Run:

```bash
.venv/bin/python - <<'PY'
from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.data.portal import DataPortal

backend = TushareCacheBackend("data/jq_tushare_cache.db", cache_mode="strict_local")
try:
    portal = DataPortal(backend, optimize_data=False)
    frame = portal.get_price(
        "000001.XSHG",
        end_date="2026-07-06",
        count=5,
        fields=["close"],
        panel=False,
    )
    assert len(frame) == 5, f"expected 5 Shanghai Composite rows, got {len(frame)}"
finally:
    backend.close()
PY
```

Expected: FAIL because `000001.XSHG` is routed to stock `daily` instead of `index_daily`.

- [ ] **Step 2: Add the shared classifier**

In `code_map.py`, add:

```python
_SH_INDEX_CODES = {
    "000001", "000016", "000300", "000688", "000852",
    "000905", "000906", "000985",
}


def is_tushare_index_code(code: str) -> bool:
    raw = to_tushare_code(code)
    if "." not in raw:
        return False
    symbol, exchange = raw.split(".", 1)
    if exchange == "SZ":
        return symbol.startswith("399")
    return exchange == "SH" and symbol in _SH_INDEX_CODES
```

- [ ] **Step 3: Route DataPortal through the shared classifier**

Import `is_tushare_index_code` in `portal.py`, remove the duplicated class constants and `_is_index_code`, and change `_single_price_api_name` to:

```python
def _single_price_api_name(self, security) -> str:
    code = to_tushare_code(security)
    if is_tushare_index_code(code):
        return "index_daily"
    if is_tushare_fund_code(code):
        return "fund_daily"
    return "daily"
```

- [ ] **Step 4: Verify the real route and existing data suite**

Rerun the Step 1 probe. Expected: PASS with five rows.

Run:

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  .venv/bin/python -m unittest tests.test_jq_tushare_sdk_data
```

Expected: existing data tests pass.

- [ ] **Step 5: Commit**

```bash
git add jq_tushare_sdk/data/code_map.py jq_tushare_sdk/data/portal.py
git commit -m "fix: route all supported indexes to index daily"
```

---

### Task 2: Infer Count-Based Index Price Requirements

**Files:**
- Modify: `jq_tushare_sdk/data/readiness.py:405-490`

**Interfaces:**
- Consumes: `is_tushare_index_code(code: str) -> bool` from Task 1.
- Produces: `infer_strategy_index_price_requirements(strategy_path) -> dict[str, int]`.

- [ ] **Step 1: Run the failing inference probe against the uploaded 5.8.1 strategy**

Run:

```bash
.venv/bin/python - <<'PY'
from jq_tushare_sdk.data.readiness import infer_strategy_index_price_requirements

path = ".jqts_web/strategies/20260711-103058_7c2b13a2_small_cap_gem_star_merged_strategy_pro_live_581.py"
requirements = infer_strategy_index_price_requirements(path)
expected = {
    "399006.XSHE", "000688.XSHG", "000300.XSHG",
    "000905.XSHG", "000852.XSHG", "000001.XSHG",
}
assert expected <= set(requirements), requirements
assert all(requirements[code] >= 70 for code in expected), requirements
PY
```

Expected: FAIL because the function does not exist.

- [ ] **Step 2: Resolve loop bindings within lexical scope**

Use an AST visitor with a loop-binding stack. When entering a `for` body,
evaluate its iterable with the existing module environment and push bindings
for a simple name target. Pop that binding after visiting the body so a
same-named variable outside the loop cannot inherit index values. Nested loops
use the innermost active binding.

```python
class _IndexPriceCallVisitor(ast.NodeVisitor):
    def __init__(self, module_env):
        self.module_env = module_env
        self.loop_bindings = []
        self.calls = []

    def visit_For(self, node):
        values = _evaluate_static_expression(node.iter, self.module_env)
        binding = _loop_binding(node.target, values)
        self.loop_bindings.append(binding)
        for statement in node.body:
            self.visit(statement)
        self.loop_bindings.pop()
        for statement in node.orelse:
            self.visit(statement)
```

- [ ] **Step 3: Implement call-aware requirement inference**

For every `get_price` call, resolve its first positional argument from either
the module environment or the currently active lexical loop bindings. Evaluate
its `count` with `_price_count_argument_node` and
`_evaluate_numeric_expression`. Sort discovered calls by `lineno` and
`col_offset` before building the result. Keep only positive counts and symbols
accepted by `is_tushare_index_code`; retain the maximum count per symbol in
first-seen source order.

The public function must return `{}` for a missing file or syntax error and must not raise for unresolved calls.

- [ ] **Step 4: Verify inference and existing readiness helpers**

Rerun the Step 1 probe. Expected: PASS with all six market-regime indexes at count 70 or greater.

Run:

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  .venv/bin/python -m unittest tests.test_jq_tushare_sdk_data
```

Expected: existing data tests pass.

- [ ] **Step 5: Commit**

```bash
git add jq_tushare_sdk/data/readiness.py
git commit -m "feat: infer strategy index price requirements"
```

---

### Task 3: Validate and Backfill Every Required Index

**Files:**
- Modify: `jq_tushare_sdk/data/readiness.py:71-144,170-208`

**Interfaces:**
- Consumes: `infer_strategy_index_price_requirements(strategy_path) -> dict[str, int]`.
- Produces: one `ReadinessIssue(api_name="index_daily")` with symbol-specific `DataUpdateRequest` entries.

- [ ] **Step 1: Run a failing real-cache readiness probe**

Run:

```bash
.venv/bin/python - <<'PY'
from jq_tushare_sdk.adapters.tushare.cache_backend import TushareCacheBackend
from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.data.readiness import DataReadinessCheck

path = ".jqts_web/strategies/20260711-103058_7c2b13a2_small_cap_gem_star_merged_strategy_pro_live_581.py"
config = BacktestConfig(
    strategy_path=path,
    start_date="2026-07-07",
    end_date="2026-07-10",
    initial_cash=1_000_000,
    cache_db="data/jq_tushare_cache.db",
)
backend = TushareCacheBackend(config.cache_db, cache_mode="strict_local")
try:
    issues = DataReadinessCheck(backend).check_required(config, ["index_daily"])
finally:
    backend.close()
requests = [dict(request.params).get("ts_code") for issue in issues for request in issue.update_requests]
assert {"000688.SH", "000852.SH", "000001.SH"} <= set(requests), requests
PY
```

Expected: FAIL because current readiness checks only the benchmark.

- [ ] **Step 2: Build combined benchmark and strategy requirements**

Replace `_check_benchmark_index` with `_check_required_indexes`. Start with the inferred benchmark using the existing benchmark range, then merge inferred count requirements. Convert each count to:

```python
lookback_days = max(int(count) * 3 + 7, 14)
required_start = (
    datetime.strptime(normalize_date(config.start_date), "%Y%m%d")
    - timedelta(days=lookback_days)
).strftime("%Y%m%d")
```

Use the earlier start when a symbol has multiple requirements. All symbols require data through the backtest end.

- [ ] **Step 3: Produce symbol-specific missing-range requests**

Fetch `index_daily` independently for each Tushare code and compare actual bounds. For each incomplete symbol, append `_update_request("index_daily", missing_start, missing_end, ts_code=ts_code)`. Return a single issue whose message and suggestion list the incomplete symbols and whose update requests preserve requirement order.

The `check_required` branch for `index_daily` must always `continue` after this specialized validation so the generic table-wide status check cannot hide per-symbol gaps.

- [ ] **Step 4: Verify readiness behavior**

Rerun the Step 1 probe. Expected: PASS and requests include the three confirmed short-history indexes.

Also run a benchmark-only strategy probe using the existing `tests.test_jq_tushare_sdk_data.DataReadinessCheckTests.test_readiness_reports_missing_strategy_benchmark_index_daily` test through the full data suite.

Run:

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  .venv/bin/python -m unittest tests.test_jq_tushare_sdk_data tests.test_jq_tushare_sdk_engine tests.test_jq_tushare_sdk_web
```

Expected: all selected suites pass.

- [ ] **Step 5: Commit**

```bash
git add jq_tushare_sdk/data/readiness.py
git commit -m "fix: require complete strategy index history"
```

---

### Task 4: Real Acceptance and Release 0.10.19

**Files:**
- Modify mechanically: `VERSION`, `jq_tushare_sdk/__init__.py`, `pyproject.toml`, `README.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: complete strategy index readiness from Tasks 1-3.
- Produces: version `0.10.19` and documented automatic index dependency checks.

- [ ] **Step 1: Run full verification**

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  .venv/bin/python -m unittest
git diff --check
```

Expected: all existing tests pass and diff check is clean.

- [ ] **Step 2: Verify the 5.8.1 requirements against real cache**

Run the Task 2 and Task 3 probes together. Expected: all six regime indexes are inferred; the three short-history indexes are reported with symbol-specific update requests; `000001.XSHG` reads from `index_daily`.

If `TUSHARE_TOKEN` is available in the service environment, run the existing automatic update flow and recheck. Do not print the token. If it is unavailable, report the exact generated update requests without claiming data was downloaded.

- [ ] **Step 3: Bump the patch version**

```bash
.venv/bin/python scripts/bump_version.py patch \
  -m "Infer, validate, and backfill strategy index price dependencies, including Shanghai Composite routing." \
  --date 2026-07-11 -y
```

Expected: `Bumped JQ Tushare SDK to 0.10.19`.

- [ ] **Step 4: Document index dependency readiness**

Add a README note near automatic data readiness explaining that direct and loop-bound index `get_price(count=...)` dependencies are inferred and checked individually before execution.

- [ ] **Step 5: Verify release metadata and commit**

```bash
rg -n '0\.10\.19' VERSION jq_tushare_sdk/__init__.py pyproject.toml README.md CHANGELOG.md
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost .venv/bin/python -m unittest
git diff --check
git add VERSION jq_tushare_sdk/__init__.py pyproject.toml README.md CHANGELOG.md
git commit -m "v0.10.19 complete strategy index readiness"
```

Expected: version is consistent, all tests pass, and the release commit contains only intended metadata and documentation files.
