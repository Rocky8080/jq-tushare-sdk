# SDK Transparent Backtest Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不修改聚宽策略源码且保持订单、成交、持仓信号和收益完全一致的前提下，将基线回测总耗时从 `467.981801` 秒降低到不超过 `328` 秒。

**Architecture:** 在 `DataPortal` 下增加单份规范化行情主缓存，按股票和日期切片，并为每个查询截止日缓存全市场前复权快照；回测引擎只传递策略静态分析得到的日期边界。SQLite 使用复合索引和生命周期受控的只读连接，性能报告分别记录公开 API、主缓存、SQL 和转换耗时，旧查询路径保留为兼容回退。

**Tech Stack:** Python 3.11、Pandas、SQLite、标准库 `collections.OrderedDict`、现有 HTML 报告生成器、真实本地 Tushare SQLite 缓存。

## Global Constraints

- 不修改 `.jqts_web/strategies/20260711-023226_d0fa29e1_small_cap_gem_star_merged_strategy_pro_live.py` 或任何用户策略文件。
- 保持 `get_price`、`history`、`attribute_history` 的返回列、行顺序、日期格式、空值和异常语义。
- 保持开盘回调只能读取前一交易日数据，主缓存不得造成未来数据泄漏。
- `fq="pre"` 必须继续使用查询截止日最新复权因子作为基准。
- 所有公开 API 返回独立 Pandas DataFrame；策略原地修改不得污染缓存。
- 不跳过回调、不改变调仓、持仓修复、风控、下单或估值逻辑。
- 遵循工作区 `AGENTS.md`：不新增模拟数据或模拟测试；复用现有测试，并使用真实缓存脚本验证。
- `optimize_data=False` 必须保留旧路径；新缓存失败时回退，不吞掉原始异常。
- 最终版本为 `0.10.18`，同步 `VERSION`、`jq_tushare_sdk.__version__`、`pyproject.toml`、README 和 CHANGELOG。

---

## File Map

- Create: `jq_tushare_sdk/data/canonical_price_store.py` — 规范化行情主缓存、动态向前扩展、前复权快照 LRU 和缓存统计。
- Create: `scripts/benchmark_price_access.py` — 使用真实 SQLite 缓存比较逐股/批量行情读取并输出机器可读结果。
- Create: `scripts/compare_backtest_runs.py` — 对两个真实回测目录执行订单、成交、信号、收益和摘要一致性比较。
- Modify: `jq_tushare_sdk/data/portal.py` — 将有边界的优化请求路由到主缓存，记录公开 API 和转换耗时，保留旧路径。
- Modify: `jq_tushare_sdk/runtime/engine.py` — 推导行情回看起点、传递缓存边界，并把数据层统计写入 `performance_profile`。
- Modify: `jq_tushare_sdk/adapters/tushare/cache_backend.py` — 增加日期/代码复合索引和持久只读连接。
- Modify: `jq_tushare_sdk/reports/html_report.py` — 展示公开 API、缓存和数据转换统计，并修正 `callbacks` 瓶颈识别。
- Modify: `jq_tushare_sdk/runtime/logger.py` — 在写文件前统一脱敏认证字段和带凭据 URL。
- Modify: `README.md`, `CHANGELOG.md`, `VERSION`, `jq_tushare_sdk/__init__.py`, `pyproject.toml` — 发布说明和版本同步。
- Existing verification: `tests/test_jq_tushare_sdk_data.py`, `tests/test_jq_tushare_sdk_engine.py`, `tests/test_jq_tushare_sdk_output.py` — 不新增模拟用例，只运行现有契约测试。

### Task 1: Add Real-Cache Verification Tools

**Files:**
- Create: `scripts/benchmark_price_access.py`
- Create: `scripts/compare_backtest_runs.py`

**Interfaces:**
- Produces: CLI `python scripts/benchmark_price_access.py --cache-db PATH --end YYYYMMDD --count N --stocks N`，输出 JSON。
- Produces: CLI `python scripts/compare_backtest_runs.py BASELINE CANDIDATE`，一致时退出 0，不一致时打印首个差异并退出 1。

- [ ] **Step 1: Capture the untouched baseline identifiers**

Run:

```bash
jq '{final_value, performance_profile}' backtest_runs/20260711-023240_20260711-023226_d0fa29e1_small_cap_gem_star_merged_strategy_pro_live_20260602_20260702/reports/summary.json
shasum -a 256 .jqts_web/strategies/20260711-023226_d0fa29e1_small_cap_gem_star_merged_strategy_pro_live.py
```

Expected: `total_seconds` is `467.981801`; strategy SHA-256 is `f75c2eb2d9dc4d9a28fb8b3f9032b001e1dbc86bc9db025b2ced01d327cb60ec`.

- [ ] **Step 2: Create the real-cache benchmark script**

Implement the following public flow in `scripts/benchmark_price_access.py`:

```python
def run_mode(cache_db: str, stocks: list[str], end_date: str, count: int, mode: str) -> dict:
    backend = TushareCacheBackend(cache_db, cache_mode="strict_local")
    portal = DataPortal(backend, optimize_data=True)
    started = time.perf_counter()
    if mode == "per_stock":
        rows = sum(
            len(portal.get_price(code, end_date=end_date, count=count, fields=["close"], fq="pre"))
            for code in stocks
        )
    else:
        rows = len(portal.get_price(stocks, end_date=end_date, count=count, fields=["close"], fq="pre"))
    return {
        "mode": mode,
        "stocks": len(stocks),
        "rows": rows,
        "seconds": round(time.perf_counter() - started, 6),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1),
        "portal": portal.performance_snapshot() if hasattr(portal, "performance_snapshot") else {},
    }
```

The script must load listed securities from `stock_basic`, convert with `to_joinquant_code`, run both modes in separate child processes so memory/timing do not share caches, and print one JSON object containing `per_stock` and `batch`.

- [ ] **Step 3: Create the run comparison script**

Implement these normalizers in `scripts/compare_backtest_runs.py`:

```python
DYNAMIC_ORDER_FIELDS = {"entrust_id"}
DYNAMIC_SIGNAL_FIELDS = {"run_id", "run_dir"}

def normalized_csv(path: Path, ignored: set[str] | None = None) -> list[dict]:
    ignored = ignored or set()
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            {key: value for key, value in row.items() if key not in ignored}
            for row in csv.DictReader(handle)
        ]

def normalized_jsonl(path: Path, ignored: set[str]) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(drop_keys(json.loads(line), ignored))
    return rows

def drop_keys(value, ignored: set[str]):
    if isinstance(value, dict):
        return {
            key: drop_keys(item, ignored)
            for key, item in value.items()
            if key not in ignored
        }
    if isinstance(value, list):
        return [drop_keys(item, ignored) for item in value]
    return value

def normalized_summary(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("performance_profile", None)
    return payload
```

Compare `transactions.csv` without `entrust_id`, `orders.csv` without `entrust_id`, signals without run location fields, `performance.csv` exactly, and summary without `performance_profile`. The main function must iterate those five named comparisons, print `{"equivalent": false, "file": ..., "difference": ...}` and return 1 on the first mismatch; otherwise print `{"equivalent": true}` and return 0.

- [ ] **Step 4: Run both tools against the current implementation**

Run:

```bash
.venv/bin/python scripts/benchmark_price_access.py --cache-db data/jq_tushare_cache.db --end 20260605 --count 35 --stocks 1500
.venv/bin/python scripts/compare_backtest_runs.py backtest_runs/20260711-023240_20260711-023226_d0fa29e1_small_cap_gem_star_merged_strategy_pro_live_20260602_20260702 backtest_runs/20260711-023240_20260711-023226_d0fa29e1_small_cap_gem_star_merged_strategy_pro_live_20260602_20260702
```

Expected: benchmark reports both modes; self-comparison exits 0 and prints `equivalent=true`.

- [ ] **Step 5: Commit the verification tools**

```bash
git add scripts/benchmark_price_access.py scripts/compare_backtest_runs.py
git commit -m "chore: add real-cache performance verification tools"
```

### Task 2: Implement the Canonical Price Store

**Files:**
- Create: `jq_tushare_sdk/data/canonical_price_store.py`

**Interfaces:**
- Consumes: `fetch(api_name: str, **params) -> pd.DataFrame` from the backend.
- Produces: `CanonicalPriceStore.select(...) -> pd.DataFrame` containing backend-schema fields.
- Produces: `CanonicalPriceStore.snapshot() -> dict` for performance reporting.

- [ ] **Step 1: Define cache state and statistics**

Create these concrete types:

```python
class CanonicalPriceStoreError(RuntimeError):
    pass


@dataclass
class _ApiFrame:
    frame: pd.DataFrame
    start_date: str
    end_date: str


@dataclass
class CanonicalPriceStats:
    loads: int = 0
    extensions: int = 0
    loaded_rows: int = 0
    load_seconds: float = 0.0
    slice_calls: int = 0
    slice_seconds: float = 0.0
    adjusted_hits: int = 0
    adjusted_misses: int = 0
    adjustment_seconds: float = 0.0
```

`CanonicalPriceStore.__init__` must accept `fetch`, `start_date`, `end_date`, and `adjusted_capacity=2`. Normalize both bounds with `normalize_date`; store raw API frames in `dict[str, _ApiFrame]` and adjusted frames in an `OrderedDict[(api_name, factor_api_name, end_date), pd.DataFrame]`.

- [ ] **Step 2: Implement one-load and dynamic-extension behavior**

Implement `_ensure_raw(api_name, requested_start, requested_end)` with this exact behavior:

```python
load_start = min(self.start_date, normalize_date(requested_start))
load_end = max(self.end_date, normalize_date(requested_end))
current = self._raw.get(api_name)
if current is None:
    frame = self.fetch(api_name, start_date=load_start, end_date=load_end)
    self._raw[api_name] = self._state(frame, load_start, load_end)
elif load_start < current.start_date or load_end > current.end_date:
    frame = self.fetch(api_name, start_date=load_start, end_date=load_end)
    merged = pd.concat([current.frame, frame], ignore_index=True)
    merged = merged.drop_duplicates(["ts_code", "trade_date"], keep="last")
    self._raw[api_name] = self._state(merged, load_start, load_end)
    self._adjusted.clear()
```

`_state` must validate `ts_code` and `trade_date`, coerce both to strings, sort by `trade_date, ts_code` with stable `mergesort`, reset the index, and update load statistics.

Implement `_raw_source(api_name, requested_start, requested_end)` by calling `_ensure_raw`, then returning an internal read-only slice selected with inclusive `trade_date >= requested_start` and `trade_date <= requested_end`. Callers must create a copy before any mutation; only `_adjusted_source` and `select` may consume this internal view.

- [ ] **Step 3: Implement full-market pre-adjustment snapshots**

Implement `_adjusted_source` so that it:

```python
price = self._raw_source(api_name, requested_start, end_date)
factors = self._raw_source(factor_api_name, requested_start, end_date)
factor_frame = factors[["ts_code", "trade_date", "adj_factor"]].copy()
factor_frame["adj_factor"] = pd.to_numeric(factor_frame["adj_factor"], errors="coerce")
latest = (
    factor_frame.dropna(subset=["adj_factor"])
    .sort_values(["ts_code", "trade_date"], kind="mergesort")
    .groupby("ts_code", sort=False)["adj_factor"]
    .tail(1)
)
latest_by_code = factor_frame.loc[latest.index].set_index("ts_code")["adj_factor"]
result = price.merge(factor_frame, on=["ts_code", "trade_date"], how="left", sort=False)
result["_latest_adj_factor"] = result["ts_code"].map(latest_by_code)
scale = result["adj_factor"] / result["_latest_adj_factor"]
scale = scale.where(scale.notna() & (scale > 0), 1.0)
for column in ("open", "high", "low", "close", "pre_close"):
    if column in result.columns:
        result[column] = pd.to_numeric(result[column], errors="coerce") * scale
result = result.drop(columns=["adj_factor", "_latest_adj_factor"], errors="ignore")
```

Cache the result by API/factor/end date, move hits to the end, and evict oldest entries until `len(_adjusted) <= adjusted_capacity`.

- [ ] **Step 4: Implement deterministic selection semantics**

`select` signature:

```python
def select(
    self,
    api_name: str,
    *,
    ts_codes: list[str],
    start_date: str | None,
    end_date: str,
    count: int | None,
    fq: str | None,
    factor_api_name: str | None,
) -> pd.DataFrame:
```

Use adjusted source only when `fq == "pre"` and a factor API is supplied. Apply date range first; for `count`, sort by `ts_code, trade_date` and use `groupby(...).tail(count)`. For 100 or fewer codes, concatenate code groups in requested-code order; for larger sets, use `isin` while preserving source order. Always return a copy and update slice statistics.

- [ ] **Step 5: Run a real-cache store smoke check**

Run a read-only one-off command that creates the store over `20260301..20260702`, requests one code and 1,500 codes for `20260605`, verifies the single-code frame is unaffected after mutating a returned copy, and prints `store.snapshot()`.

Expected: no assertion failure; raw `daily` and `adj_factor` loads are each 1; adjusted cache records a hit on the repeated end date.

- [ ] **Step 6: Commit the store**

```bash
git add jq_tushare_sdk/data/canonical_price_store.py
git commit -m "perf: add canonical price store"
```

### Task 3: Route DataPortal Through the Canonical Store

**Files:**
- Modify: `jq_tushare_sdk/data/portal.py:42-120`
- Modify: `jq_tushare_sdk/data/portal.py:732-899`

**Interfaces:**
- Consumes: `CanonicalPriceStore.select` and `CanonicalPriceStore.snapshot` from Task 2.
- Produces: backward-compatible `DataPortal(..., price_cache_start=None, price_cache_end=None)`.
- Produces: `DataPortal.performance_snapshot() -> dict`.

- [ ] **Step 1: Add optional bounded-cache construction**

Change the constructor to:

```python
def __init__(
    self,
    backend,
    optimize_data: bool = True,
    *,
    price_cache_start: str | None = None,
    price_cache_end: str | None = None,
):
```

Create `CanonicalPriceStore` only when optimization is enabled and both bounds are present. Keep every existing legacy cache for the unbounded and `optimize_data=False` paths. Add counters for API calls, cache hits/misses, and transform seconds.

- [ ] **Step 2: Split get_price into timing wrapper and implementation**

Keep the public signature unchanged. Move its current body to `_get_price_impl`. Extract the current count/range/direct-fetch branch into `_legacy_price_frame(api_name, securities, start_date, end_date, count)`, preserving the existing branch order exactly. Wrap the public method as:

```python
started = time.perf_counter()
try:
    return self._get_price_impl(...)
finally:
    payload = self._portal_calls["get_price"]
    payload["count"] += 1
    payload["seconds"] += time.perf_counter() - started
```

No other public API behavior changes in this step.

- [ ] **Step 3: Add the canonical selection branch**

After normalizing fields, securities and API name, choose the canonical branch only when the store exists and `end_date` is present:

```python
factor_api = "fund_adj" if api_name == "fund_daily" else "adj_factor" if api_name == "daily" else None
df = self._canonical_price_store.select(
    api_name,
    ts_codes=[to_tushare_code(item) for item in securities],
    start_date=normalize_date(start_date) if start_date is not None else None,
    end_date=normalize_date(end_date),
    count=int(count) if count is not None else None,
    fq=fq,
    factor_api_name=factor_api,
)
already_adjusted = fq == "pre" and factor_api is not None
```

Skip `_tail_per_security` and `_apply_price_adjustment` only when the store already performed those operations. Continue through `_validate_price_frame` and `_format_price_frame`. Preserve the existing result-cache key and return-copy behavior.

- [ ] **Step 4: Add safe fallback**

Catch only `MemoryError` and `CanonicalPriceStoreError` raised by the new store. Increment `canonical_fallbacks`, disable the store for the remaining portal lifetime, and rerun the request through `_legacy_price_frame`. The store must wrap missing `ts_code`, `trade_date`, or `adj_factor` schema failures in `CanonicalPriceStoreError`; do not catch unsupported field/frequency/fq errors.

- [ ] **Step 5: Expose layered statistics**

Implement:

```python
def performance_snapshot(self) -> dict:
    return {
        "public_calls": [
            {"api": name, "count": value["count"], "seconds": round(value["seconds"], 6)}
            for name, value in sorted(self._portal_calls.items())
        ],
        "result_cache": {
            "hits": self._result_cache_hits,
            "misses": self._result_cache_misses,
        },
        "canonical_fallbacks": self._canonical_fallbacks,
        "canonical_cache": self._canonical_price_store.snapshot() if self._canonical_price_store else {},
        "format_seconds": round(self._format_seconds, 6),
    }
```

- [ ] **Step 6: Run the existing data contract tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_jq_tushare_sdk_data tests.test_jq_tushare_sdk_api
```

Expected: all existing tests pass; no new mock tests are added.

- [ ] **Step 7: Run the real-cache microbenchmark**

Run Task 1 benchmark again.

Expected: rows match the baseline; per-stock mode is faster; JSON contains canonical load and adjusted-cache statistics.

- [ ] **Step 8: Commit portal integration**

```bash
git add jq_tushare_sdk/data/portal.py
git commit -m "perf: route get_price through canonical cache"
```

### Task 4: Pass Backtest Bounds and Record Portal Metrics

**Files:**
- Modify: `jq_tushare_sdk/runtime/engine.py:35-55`
- Modify: `jq_tushare_sdk/runtime/engine.py:527-570`
- Modify: `jq_tushare_sdk/reports/refresher.py:34-38`

**Interfaces:**
- Consumes: `infer_strategy_price_lookback_start(strategy_path, start_date) -> str`.
- Consumes: `DataPortal.performance_snapshot()` from Task 3.
- Produces: `performance_profile.data_portal` while preserving existing profile fields.

- [ ] **Step 1: Pass bounds from the engine**

Construct the portal with:

```python
price_cache_start = infer_strategy_price_lookback_start(
    self.config.strategy_path,
    self.config.start_date,
)
portal = DataPortal(
    _ProfilingBackend(self.backend, profiler),
    optimize_data=self.config.optimize_data,
    price_cache_start=price_cache_start,
    price_cache_end=self.config.end_date,
)
```

Do not add these derived values to `BacktestConfig`, manifest, or user-facing strategy parameters.

- [ ] **Step 2: Include portal metrics in profiler snapshot**

Change `_PerformanceProfiler.snapshot` to accept `data_portal: dict | None = None` and add:

```python
"data_portal": data_portal or {},
```

Call it with `portal.performance_snapshot()` after all callbacks and output phases have completed.

- [ ] **Step 3: Use the same bounds during report refresh**

Construct the refresher portal with the strategy-derived lookback and `config.end_date`, so refreshed benchmark reads use the same transparent data path without changing report contents.

- [ ] **Step 4: Run engine and output tests**

```bash
.venv/bin/python -m unittest tests.test_jq_tushare_sdk_engine tests.test_jq_tushare_sdk_output
```

Expected: all existing tests pass; generated summaries contain the original fields plus `performance_profile.data_portal`.

- [ ] **Step 5: Commit engine metrics wiring**

```bash
git add jq_tushare_sdk/runtime/engine.py jq_tushare_sdk/reports/refresher.py
git commit -m "perf: profile canonical data access"
```

### Task 5: Optimize SQLite Date-Range Reads

**Files:**
- Modify: `jq_tushare_sdk/adapters/tushare/cache_backend.py:199-238`
- Modify: `jq_tushare_sdk/adapters/tushare/cache_backend.py:365-380`
- Modify: `jq_tushare_sdk/adapters/tushare/cache_backend.py:530-548`

**Interfaces:**
- Produces: `TushareCacheBackend.close() -> None`.
- Keeps: write methods using independent write connections.

- [ ] **Step 1: Add composite indexes**

After per-column indexes, create these exact indexes when the table has `trade_date` and `ts_code`:

```python
conn.execute(
    f"CREATE INDEX IF NOT EXISTS idx_{spec.table}_trade_date_ts_code "
    f"ON {spec.table}(trade_date, ts_code)"
)
```

Apply to all matching API specs, including daily, factors, funds and index daily.

- [ ] **Step 2: Add a locked persistent read connection**

Initialize `_read_conn = None` and `_read_lock = threading.RLock()`. Implement:

```python
def _reader(self):
    with self._read_lock:
        if self._read_conn is None:
            uri = Path(self.cache_db).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-131072")
            conn.execute("PRAGMA mmap_size=268435456")
            self._read_conn = conn
        return self._read_conn

def close(self):
    with self._read_lock:
        if self._read_conn is not None:
            self._read_conn.close()
            self._read_conn = None
```

`_read_sql` must hold `_read_lock` while calling `pd.read_sql_query`. `_connect` remains the short-lived write/schema connection.

- [ ] **Step 3: Close owned backends at CLI/Web boundaries**

Wrap backend ownership in `try/finally` in CLI backtest/check/update/report-refresh flows and `_run_local_backtest`; call `backend.close()` only for backend instances created in that function. Do not make `BacktestEngine` close injected backends.

Use this ownership pattern at each creation site:

```python
backend = TushareCacheBackend(...)
try:
    return operation_using(backend)
finally:
    backend.close()
```

For CLI branches that print before returning, keep printing inside `try` and return after `finally` has closed the reader.

- [ ] **Step 4: Verify the query plan**

Run:

```bash
sqlite3 data/jq_tushare_cache.db "EXPLAIN QUERY PLAN SELECT * FROM daily_quote WHERE trade_date >= '20260401' AND trade_date <= '20260702' ORDER BY trade_date, ts_code;"
```

Expected: query uses `idx_daily_quote_trade_date_ts_code` and does not report `USE TEMP B-TREE`.

- [ ] **Step 5: Run data and Web tests**

```bash
.venv/bin/python -m unittest tests.test_jq_tushare_sdk_data tests.test_jq_tushare_sdk_web tests.test_jq_tushare_sdk_engine
```

Expected: all tests pass and no `ProgrammingError: Cannot operate on a closed database` occurs.

- [ ] **Step 6: Commit SQLite optimization**

```bash
git add jq_tushare_sdk/adapters/tushare/cache_backend.py jq_tushare_sdk/cli.py jq_tushare_sdk/web/app.py
git commit -m "perf: optimize sqlite backtest reads"
```

### Task 6: Expose Layered Metrics and Redact Runtime Secrets

**Files:**
- Modify: `jq_tushare_sdk/reports/html_report.py:342-460`
- Modify: `jq_tushare_sdk/reports/html_report.py:970-999`
- Modify: `jq_tushare_sdk/runtime/logger.py:1-33`

**Interfaces:**
- Consumes: `performance_profile.data_portal` from Task 4.
- Produces: report sections for public API, cache, transformations and backend SQL.
- Produces: `RuntimeLogger` output with sensitive values replaced by `******`.

- [ ] **Step 1: Render layered portal metrics**

Read `portal = profile.get("data_portal") or {}` and add tables for:

```python
public_calls = portal.get("public_calls") or []
cache = portal.get("canonical_cache") or {}
cache_rows = [
    {"metric": "主缓存加载", "value": cache.get("loads", 0)},
    {"metric": "主缓存扩展", "value": cache.get("extensions", 0)},
    {"metric": "加载行数", "value": cache.get("loaded_rows", 0)},
    {"metric": "前复权命中", "value": cache.get("adjusted_hits", 0)},
    {"metric": "前复权未命中", "value": cache.get("adjusted_misses", 0)},
]
```

Keep the existing backend API table. Label it “SQLite 后端读取” so users no longer confuse backend fetch count with strategy-level `get_price` calls.

- [ ] **Step 2: Correct callbacks bottleneck detection**

Change `_performance_bottleneck` from checking `"callback"` to `"callbacks"`. When callbacks dominate, include public `get_price` count and canonical cache hit rate if available.

- [ ] **Step 3: Add centralized log redaction**

In `RuntimeLogger`, compile patterns for dictionary/config assignments and credential-bearing URLs:

```python
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(['\"]?(?:password|passwd|token|secret|api[_-]?key|authorization)['\"]?\s*[:=]\s*)(['\"]?)([^,'\"}\]\s]+)(\2)"
)
_CREDENTIAL_URL = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/\s]+:)([^@/\s]+)(@)")

def _redact_sensitive_text(value) -> str:
    text = str(value)
    text = _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}******{match.group(4)}", text)
    return _CREDENTIAL_URL.sub(r"\1******\3", text)
```

Apply redaction after `%` interpolation and to traceback text before writing. Do not modify the in-memory strategy configuration.

- [ ] **Step 4: Verify redaction with a temporary real logger file**

Run a one-off command that logs a dict containing `password`, `token`, a Redis URL with credentials, and a normal host/port. Read the file and assert none of the supplied secret sentinel strings remain, while host and port remain visible.

Expected: command exits 0 and prints `redaction_ok=true`.

- [ ] **Step 5: Run report and engine tests**

```bash
.venv/bin/python -m unittest tests.test_jq_tushare_sdk_output tests.test_jq_tushare_sdk_engine
```

Expected: all tests pass; report contains “SQLite 后端读取” and the new cache labels.

- [ ] **Step 6: Commit metrics and redaction**

```bash
git add jq_tushare_sdk/reports/html_report.py jq_tushare_sdk/runtime/logger.py
git commit -m "feat: expose layered performance metrics"
```

### Task 7: Run Full Behavioral and Performance Acceptance

**Files:**
- Verify only unless a measured incompatibility requires a focused correction in the files from Tasks 2-6.

**Interfaces:**
- Consumes: Task 1 comparison and benchmark CLIs.
- Produces: one optimized run directory that passes equivalence and performance gates.

- [ ] **Step 1: Confirm the strategy snapshot is unchanged**

```bash
shasum -a 256 .jqts_web/strategies/20260711-023226_d0fa29e1_small_cap_gem_star_merged_strategy_pro_live.py
```

Expected: `f75c2eb2d9dc4d9a28fb8b3f9032b001e1dbc86bc9db025b2ced01d327cb60ec`.

- [ ] **Step 2: Run the complete optimized baseline strategy**

```bash
marker=$(mktemp)
touch "$marker"
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost .venv/bin/python -m jq_tushare_sdk.cli backtest \
  --strategy .jqts_web/strategies/20260711-023226_d0fa29e1_small_cap_gem_star_merged_strategy_pro_live.py \
  --start 2026-06-02 --end 2026-07-02 --cash 1000000 \
  --cache-db data/jq_tushare_cache.db --output-dir backtest_runs --optimize-data
find backtest_runs -mindepth 1 -maxdepth 1 -type d -newer "$marker" -print | sort | tail -1 > /tmp/jqts-optimized-run.txt
rm -f "$marker"
```

Expected: backtest exits 0 and `/tmp/jqts-optimized-run.txt` contains exactly one optimized run directory.

- [ ] **Step 3: Compare behavior against the approved baseline**

```bash
candidate=$(sed -n '1p' /tmp/jqts-optimized-run.txt)
.venv/bin/python scripts/compare_backtest_runs.py \
  backtest_runs/20260711-023240_20260711-023226_d0fa29e1_small_cap_gem_star_merged_strategy_pro_live_20260602_20260702 \
  "$candidate"
```

Expected: `equivalent=true`. If it fails, stop performance work, inspect the first reported difference, and correct only the earliest semantic divergence before rerunning the full comparison.

- [ ] **Step 4: Check performance gates**

```bash
candidate=$(sed -n '1p' /tmp/jqts-optimized-run.txt)
jq '.performance_profile | {total_seconds, data_api_calls, data_portal}' "$candidate/reports/summary.json"
```

Expected: `total_seconds <= 328`; backend counts for `daily` and `adj_factor` are each `<= 5`; `canonical_fallbacks == 0`.

- [ ] **Step 5: Check peak memory and microbenchmark**

Run the Task 1 benchmark and inspect `peak_rss_mb`. Expected: peak RSS does not exceed the original benchmark peak by more than 10%, and per-stock elapsed time improves by at least 50% while row counts remain equal.

- [ ] **Step 6: Run the complete existing test suite**

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost .venv/bin/python -m unittest
git diff --check
```

Expected: all existing tests pass and `git diff --check` prints no errors.

- [ ] **Step 7: Commit only measured corrective tuning, if any**

If Task 7 required code corrections, stage only those files and commit:

```bash
git add jq_tushare_sdk/data/canonical_price_store.py jq_tushare_sdk/data/portal.py jq_tushare_sdk/runtime/engine.py jq_tushare_sdk/adapters/tushare/cache_backend.py jq_tushare_sdk/reports/html_report.py jq_tushare_sdk/runtime/logger.py
git commit -m "perf: meet transparent backtest acceptance gates"
```

If no code changed, skip this commit.

### Task 8: Release Version 0.10.18

**Files:**
- Modify mechanically: `VERSION`, `jq_tushare_sdk/__init__.py`, `pyproject.toml`, `README.md`, `CHANGELOG.md`

**Interfaces:**
- Produces: consistent SDK version `0.10.18` in package metadata, README and reports.

- [ ] **Step 1: Bump the patch version with the repository script**

```bash
.venv/bin/python scripts/bump_version.py patch \
  -m "Add transparent canonical price caching, layered performance metrics, SQLite read optimization, and sensitive log redaction." \
  --date 2026-07-11 -y
```

Expected: script prints `Bumped JQ Tushare SDK to 0.10.18`.

- [ ] **Step 2: Add README performance notes**

Under the local cache/performance section, add this text, replacing the measured percentage and seconds with the Task 7 values only when they are no larger than the verified result:

```markdown
启用 `optimize_data` 时，回测引擎会在推导出的历史回看边界内复用规范化行情主缓存。所有查询仍按当前回调的数据截止日切片，`fq="pre"` 仍使用查询截止日的最新复权因子；缓存初始化失败时自动回退到兼容查询路径。基准策略在交易结果一致的验证下，总耗时由 467.98 秒降低到本次发布实测值。
```

Do not claim a speedup larger than the measured Task 7 result.

- [ ] **Step 3: Verify version consistency**

```bash
rg -n '0\.10\.18' VERSION jq_tushare_sdk/__init__.py pyproject.toml README.md CHANGELOG.md
.venv/bin/python -c 'import jq_tushare_sdk; print(jq_tushare_sdk.__version__)'
```

Expected: all five metadata/documentation files contain `0.10.18`; Python prints `0.10.18`.

- [ ] **Step 4: Run final verification**

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost .venv/bin/python -m unittest
git diff --check
git status --short
```

Expected: all tests pass; diff check is clean; status contains only intended release files before commit.

- [ ] **Step 5: Commit the release metadata**

```bash
git add VERSION jq_tushare_sdk/__init__.py pyproject.toml README.md CHANGELOG.md
git commit -m "v0.10.18 optimize transparent backtest data access"
```

- [ ] **Step 6: Inspect final history without pushing**

```bash
git log --oneline --decorate -10
git status --short
```

Expected: implementation commits and `v0.10.18` are present; working tree is clean. Pushing remains a separate user-authorized action.
