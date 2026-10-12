## [Unreleased] - 2026-10-12

- 2026-10-12 数据排查：复权凭证按请求范围选择一份完整记录，后续较窄补数不遮住旧完整记录；有正式停牌证据的补齐行情保留当日真实复权因子，原价模式明确为 1。
- 股本历史核验区分旧制度的非流通股与现代限售股；逐行隔离未知/不一致分类，保留完整原始响应。重复采集相同内容不再误报公告导入冲突，股数绑定实际响应页，读取凭证核对内容 Hash。
- 股本公告证据增加预登记、最终登记、预期上市的独立字段和可见时间门禁，不将预登记或月末上界用作最终中登登记日期，也不据此自动覆盖估值。
- 原生业绩预告就绪诊断区分来源、窗口、证券范围和采集/导入时间缺口；当前归档检查独立于历史策略运行，仍禁止将后来获取的数据用于更早决策。
- 对账示例 v0.4.0 增加明确的价格回放配置和原生预告行身份诊断；新增扩样及预告归档诊断工具，财报继续保留原公告口径。扩大本地验证不构成聚宽原生、全市场或历史 PIT 一致认证。

- 按根因归并对账差异，保留每个独立字段及全部日期/阶段位置，并核算是否完整覆盖原差异分组。新增逐股精度的东方财富股本历史归档；月末日期不冒充逐笔登记日期。
- 新增显式 `disclosed_reference` 研究口径：独立流通 A 股记录与发行人期末总股本交叉验证，绑定原始公告 PDF/Hash 和已核对的累计利润、普通股权益；按公告可见时间及来源范围使用，重算市值和 PE/PB，保留原表。今日补采不取得历史 PIT 认证。
- 新增可选 `daily_ex_reference` 复权规则，用已观察到的除权昨收参考价和前一日原价计算比例并归档原始输入、完整小数、规则及捕获时间。报价本身仍有舍入精度限制；不宣称恢复精确聚宽因子。补数完成后才准备该来源，普通/优化路径均限制到请求股票。
- 对账示例 v0.3.3 保留完整股本/估值计算凭证；与 log 9 比较仍固定 v0.3.0 源码。财报收入保持原公告口径。
- 新增独立高精度复权来源 `sina_hfq`：归档原始后/前复权响应、十进制字符串与捕获时间，交叉核验端点，统一覆盖普通、优化及停牌种子路径；显式选择来源，保留 Tushare 原表，缺覆盖阻止运行，不能将本次捕获伪装为历史 PIT。
- 新增深交所公告原始发布时间归档和股本公告证据导入；将月末生效上界、逐笔生效日、交易所发布时间与实际捕获时间分开，按查询可见时点展示。月报缺失精确登记日/流通股本时不自动改写估值。财报保持原始公告口径。
- 对账示例 v0.3.2 单独输出完整因子回执和股本公告来源摘要，避免全市场状态字典导致来源属性截断；保留 v0.3.1 的合法空字段与截断 Hash 修复。同区间 log 9 比较继续使用冻结 v0.3.0。
- 依据真实聚宽 v0.3.0 日志复跑 2026-09-01 至 09-08；验证 `STK_FIN_FORCAST.code/pub_date` 对账投影；完整原生表只读取经核验的聚宽导出归档，缺窗口、范围或决策前观察记录时阻塞，历史 vintage 仍未认证。价格与股本溯源明确区分源因子精度、日线快照日和公司行动生效日。
- 对账工具新增股本日期匹配、因子跳变隐含现金及原生累计收入转单季诊断；示例 v0.3.1 保留合法可空字段与原值、修正诊断表的 partial 误报，并显式记录属性截断和返回表结构。相同窗口双端对比仍使用冻结的 v0.3.0，不混配版本。
- 依据真实聚宽 v0.2.0 新日志继续诊断；补齐 `valuation.capitalization/circulating_cap` 和 `income.operating_revenue/net_profit` 源字段映射，并对陈旧股本置为不可用。没有用参考值覆盖 Tushare 源值。
- 数据对账示例更新至 v0.3.0，补充股本、年度/季度收入明细和实际预告样本；新增保留双端完整解码、重复观测位置、复权传播和样本决策影响的诊断工具。历史预告版本及完整原生执行等价性仍未认证。
- 日线成交量统一为股/份、成交额统一为元；前复权价格按证券精度输出，成交量按反向因子调整，并支持 `factor` 与显式 `fq=None`。普通与优化数据路径使用相同口径，缺失因子明确报错。
- 行情缺口只有取得实际停牌记录才允许填充；指数缺失线明确阻塞，避免把陈旧价格伪装成连续行情。日线准备自动补齐停复牌快照。
- 利润表按季度完整分页下载，权限不足时使用逐证券接口；自动补齐策略财报依赖。修复 date 查询的 statDate nan 异常，增加发布日/报告期、流通市值和股票标准基础字段。新抓取修订不取得历史 PIT 认证。
- 默认行业口径为聚宽分类名称 + Tushare 历史成员（`joinquant_taxonomy`），执行与溯源共用同一选择；需已有聚宽分类导入，仍可显式选择 `tushare` / `joinquant_full`。
- examples 中的数据对账策略更新为 v0.2.0，固定 Python 判断函数，记录复权因子、类型、来源、耗时与异常；新增 `--prepare` 自动补数和 `--protocol` 预期快照检查。仍保持零订单、零通知。

- 聚宽原生预告/估值导入工具校验完整分页、空终页、原始字段、内容 Hash 和真实观察/导入时间；支持只核验不写入，导入保持追加与幂等，不解锁更早历史决策。

## [0.11.0] - 2026-10-11

- 提供 `jq_tushare_sdk.daily` 公开连续日线接口与运行契约版本，保留收盘决策、次日开盘模拟成交逻辑。
- 日线运行、就绪检查、数据补齐和缓存锁归属 SDK，内部实现不依赖策略平台。
- 策略账户、Web 页面、邮件、持久化和进程调度迁移到同级独立项目 jq-strategy-platform，SDK 不再包含其静态资源和服务。
- 旧 `simulation-web` 命令提供迁移提示；平台安装与数据迁移见平台 README。
- 公开日志脱敏接口，统一包声明版本；检查点保留既有对象路径并记录后续实际 SDK 版本。
- 包含共享缓存跨进程写入锁和策略进程只读保护，数据补齐与账户执行共用同一锁协议。

迁移不重算既有策略历史；依赖本次平台拆分的应用需采用 SDK 0.11.0。验证记录见同级平台 RELEASE.md。

## [0.10.35] - 2026-10-10

- Add explicit research data contracts, signed-PE/PB missing-value provenance, and announcement cutoffs for research quarterly queries.
- Certify complete forecast publication windows, including verified empty days, and reject missing data before loading declared strategies.
- Keep native forecast schema and strict historical-vintage limitations explicit; research operation does not imply JoinQuant reproduction.
- Export the finance API to the actual strategy namespace and expose financial-mode selection in Web settings.
- Allow large scalar stock pools during runtime inspection while preserving Redis isolation and bounded container/reference/depth checks.
- Read A-share corporate actions from unified caches and yearly shards, deduplicate events, use gross cash dividends and total stock-distribution ratios, and preserve record-date entitlements and FIFO tax cohorts. Persist an auditable corporate-action ledger. Held deferred, fractional, or uncertified taxable-bonus events fail explicitly; the cash-dividend tax model remains a disclosed research assumption.

## [0.10.34] — fundamentals and execution consistency

- Separate fundamentals request date, report period and replay visibility; reject future EOD reads and date+statDate ambiguity.
- Intersect required tables, propagate empty table schemas, distinguish annual cumulative and single-quarter income.
- Map PE to TTM and preserve missing values; add explicit financial modes, append-only revision storage, signed TTM diagnostics and native forecast capability reporting.
- Recognize 302 stocks with exchange/lifecycle checks; keep stale valuation identity and report unavailable values.
- Apply JoinQuant engine slippage, STAR quantity increments and dated stock T+1 settlement; return native dates and schedule against the complete exchange calendar.
- Historical cache migration cannot recover lost report revisions. Native STK_FIN_FORCAST remains unsupported until its schema is verified.

# Changelog

All notable changes to JQ Tushare SDK are documented in this file.

This project follows Semantic Versioning.

## [0.10.33] - 2026-09-30

### Added

- Every backtest run now records an `industry_provenance` block in `manifest.json`: the effective `JQTS_INDUSTRY_PROVIDER`, the `JQTS_INDUSTRY_COMPAT` diagnostic flag, SW membership table row counts, and the imported JoinQuant classification/member source SHA-256. `scripts/compare_backtest_runs.py` refuses to compare runs whose industry semantic differs (or is recorded on only one side), so Eastmoney-`stock_basic.industry` runs (pre-0.10.29 semantics) can never be mistaken for JoinQuant/platform-consistent results.
- Readiness now blocks a backtest that calls `get_industry` when the configured provider's tables are missing, instead of silently falling back to Eastmoney `stock_basic.industry` (the deliberate `JQTS_INDUSTRY_COMPAT=stock_basic_as_sw_l1` diagnostic is exempt). `joinquant_taxonomy` / `joinquant_full` providers require their imported classification / member tables.
- `scripts/export_joinquant_members.ipynb`（以及同代码的 `export_joinquant_members.py` 参考版）导出聚宽研究环境的逐股行业成员，供 `import-jq-industry --members` 导入，实现 `joinquant_full` 下本地回测与聚宽平台的逐股对齐。
- `is_tushare_a_share_code` classifies Shanghai/Shenzhen A-share symbols.
- Historical stock state uses full daily `stock_st` snapshots, official `stk_limit` prices, effective `namechange` timelines and BSE code aliases. Completion markers prevent partial symbol downloads from being treated as a complete market snapshot.
- CLI and web readiness check historical snapshots, including the preceding trade day, and request automatic backfill. Provider permissions remain required; missing name labels are advisory and use code labels, never current names.

### Fixed

- The `stock_basic` readiness missing-symbol check now ignores B-shares (`200xxx.SZ`, `900xxx.SH`) that trade in `daily` but are not part of the `stock_basic` A-share universe; previously these false positives blocked every backtest.
- Industry readiness checks all tables required by taxonomy/full providers, including module-qualified calls. Comparison distinguishes missing SW membership from populated caches and records all imported source hashes deterministically.
- Exporters use as-of stock universes, validate date ranges and positive sampling intervals, default to daily snapshots, and close the last membership interval at the requested window boundary. Failed API calls do not silently save partial exports; local login hides password input.
- Historical name lookups select the latest effective interval when older intervals overlap, while rejecting conflicting names with the same effective start.

### Changed

- Documented that pre-0.10.29 local backtests used Eastmoney `stock_basic.industry` semantics and are not comparable with JoinQuant-platform results; 0.10.29+ defaults to real SW2021 membership.
- Dated security lists filter listing/delisting dates and security info returns the recorded delisting date. Historical backtest results may change after ST/limit corrections; rerun both comparison arms with the same SDK and frozen cache.

## [0.10.32] - 2026-08-06

### Added

- `import-jq-industry` imports JoinQuant's dated SW L1/L2/L3 classification table into SQLite with a source SHA-256 audit trail.
- Optional member imports accept JoinQuant JSON, JSONL, CSV, or TSV exports and populate a separate dated stock-membership table.
- `JQTS_INDUSTRY_PROVIDER=joinquant_taxonomy` keeps Tushare membership but emits JoinQuant's official dated industry names; `joinquant_full` requires and uses imported JoinQuant stock membership.
- JoinQuant-compatible `indicator` fundamentals expose disclosed revenue/profit YoY and sequential growth derived from the local income cache.

### Changed

- JoinQuant `851251` and Tushare `851251.SI` industry codes are normalized to one key. Industry names are no longer treated as cross-provider identifiers.
- Corrected the documentation: JoinQuant's industry table contains historical classifications and the post-2021 hierarchy, rather than only SW2014.
- Snapshot member imports now require a dated `--as-of` boundary, and `joinquant_full` never falls back to `stock_basic.industry` when imported L1 data is incomplete.

### Validation

- Imported 700 real JoinQuant classification rows (31 active L1, 134 L2, 346 L3) from the supplied official table.
- On the v6.5.8 2026-06-01 through 2026-07-31 L2 backtest, `joinquant_taxonomy` produced byte-identical performance, orders, and transactions to the Tushare-name baseline, confirming that taxonomy-name alignment alone does not change portfolio grouping or restore the old `stock_basic.industry` return.

## [0.10.31] - 2026-08-06

### Changed

- `get_industry` can now return date-sensitive `sw_l2` and `sw_l3` entries from a new lossless `index_member_all` cache while preserving the existing L1-only fallback for older databases.
- `update-data --api index_member_all` caches the complete Tushare SW2021 hierarchy instead of discarding L2/L3 codes and names.
- `JQTS_INDUSTRY_COMPAT=stock_basic_as_sw_l1` is available only for reproducing pre-0.10.29 backtests; the flag deliberately restores the old, non-JoinQuant industry semantics and must not be used for platform-parity validation.
- Web backtests now run in isolated worker processes instead of the long-lived HTTP server process. On macOS, one-shot non-keepalive `Interactive` LaunchAgents plus `USER_INITIATED` thread/process activity declarations avoid background CPU demotion; each agent is unloaded after completion so all Pandas/cache memory is released.
- Web workers use the CLI's deterministic `PYTHONHASHSEED=0`, preserving repeatable stock/order iteration across process launches.
- Final `get_price` DataFrame results now use a row-budgeted LRU cache (500,000 rows by default, configurable with `JQTS_PRICE_RESULT_CACHE_ROWS`) and expose entries, rows, peak rows, and evictions in performance metrics.
- Web progress and cancellation are relayed across an atomic JSON worker protocol; cancellation first requests graceful run-directory cleanup and force-stops the isolated process only after a timeout.

### Performance

- The same v6.5.8 strategy, database, and 2026-06-01 through 2026-07-31 parameters completed in 173.58 seconds through the isolated worker versus 1711.86 seconds in the prior long-lived Web process (9.86x faster). Final equity (1,177,961.4816), 427 transactions, 453 orders, and the daily equity curve were unchanged.

## [0.10.30] - 2026-08-06

### Changed

- Canonical price slices now use per-security date arrays and binary search instead of rebuilding an LRU view for every distinct dynamic stock group.
- Pre-adjustment factor arrays are cached per security and refreshed only when that security's raw coverage changes, eliminating repeated full-group factor scans.
- Canonical SQLite reads project only required columns, skip redundant SQL sorting, and default to a configurable 512 MB page cache plus 2 GB mmap (`JQTS_SQLITE_CACHE_MB` / `JQTS_SQLITE_MMAP_MB`).

### Performance

- A strict same-strategy/same-database A/B run for 2026-06-01 through 2026-07-31 fell from 380.0 seconds to 175.6 seconds (2.16x faster). Factor rows scanned fell from 58.4 million to 1.13 million, while orders, transactions, daily performance, final equity, and normalized target-position signals remained identical.

## [0.10.29] - 2026-08-05

### Added

- Readiness check now warns (advisory, non-blocking) when a backtest starts right after a holiday-sized closure: strategies using natural-day short factor windows (e.g. `end_date - 5 days`) will see those factors degrade for the first one or two sessions.
- Backtest summary and HTML report now surface "deferred rebalance" events: scheduled rebalance callbacks whose weekly/monthly rebalance was skipped by the strategy (e.g. open-gap protection / quality gate), which otherwise silently makes local fills differ from the platform by one trading day.
- `get_industry` now resolves real SW (申万) L1 industries instead of Eastmoney `stock_basic.industry` values masquerading as `sw_l1`. New `index_member` / `index_classify` cache tables (SW2021) are backfilled by `update-data`; membership rows are indexed once per portal instance and resolved on demand, falling back to `stock_basic.industry` for stocks outside SW coverage.
- Diagnostic environment switches can opt unspecified `get_price` calls into `fq="pre"` (`JQTS_DEFAULT_FQ=pre`) or disable partial current-day bars (`JQTS_DISABLE_PARTIAL_BAR=1`) without editing strategy source.

### Fixed

- `check-data`, the backtest preflight, and the Web console no longer block or trigger data updates for advisory readiness findings, and serialize the advisory flag for the Web frontend.
- Income readiness now rejects suspiciously sparse quarters using symbol-count, adjacent-quarter, and listed-company coverage thresholds instead of treating any non-empty response as complete.
- Reporting-deadline boundaries advance on the following calendar day so intraday backtests do not assume end-of-deadline-day filings are already available.
- Deferred-rebalance diagnostics now capture the actual callback and simulated trade date while the callback runs, avoiding duplicate callback attribution and expensive log/date rescans.
- The deferred-rebalance report tab is hidden when no corresponding section exists.
- SW member updates now call Tushare's documented `index_member_all` endpoint and map its `l1_code` / `ts_code` fields into the local compatibility schema; historical lookups choose the membership active on the requested date.

## [0.10.28] - 2026-08-04

### Fixed

- Validate every consecutive income quarter required by the full backtest window instead of accepting any single recent quarter.
- Generate exact automatic refill requests for missing income periods, including comparison quarters needed by growth calculations.
- Make range-based income updates cover the full backtest window and required comparison periods, using reporting deadlines to avoid requiring quarters that were not yet due.

## [0.10.27] - 2026-08-02

### Changed

- Compress repeated adjustment factors into per-security change nodes while retaining exact-date availability and historical end-date anchoring.
- Use vectorized integer-key factor lookup to avoid rebuilding pandas multi-indexes in the pre-adjustment hot path.
- Show cumulative factor rows scanned, generated change nodes, and node ratio in performance reports.
- Ignore generated backtest output directories with custom `backtest_runs*` suffixes.

### Fixed

- Skip requested securities that have no cached price rows instead of failing a mixed-security `count` query.
- Accept both compact and hyphenated dates in vectorized JoinQuant price output formatting.

## [0.10.26] - 2026-07-27

### Added

- Run the optional JoinQuant `after_code_changed(context)` and `process_initialize(context)` lifecycle hooks after `initialize(context)` in local backtests.
- Infer strategy benchmarks declared in `process_initialize` as well as `initialize`.

## [0.10.25] - 2026-07-26

### Added

- Show strategy version and annualized Sharpe ratio in backtest history, including safe fallbacks for older runs.

### Fixed

- Treat a priced `MarketOrderStyle` as a market-order protection boundary instead of using the protection price as the fill price.
- Reject market orders whose simulated execution price breaches the supplied protection boundary.

## [0.10.24] - 2026-07-22

### Changed

- Load `get_price(..., count=N)` data from a bounded historical window instead of scanning the full configured backtest history.
- Prefetch at most 45 calendar days of price and adjustment-factor data to reduce repeated SQLite extensions during sequential backtest days without exposing future rows to strategies.
- Validate and automatically refill `adj_factor` together with daily prices for the strategy's inferred historical lookback window.
- Process A-share cash dividends, FIFO holding-period dividend tax adjustments, and realized profit and loss in local backtests.
- Export `dividend_tax` and `realized_pnl` with transaction records and apply stock/ETF tick precision to market-order slippage prices.

### Fixed

- Prevent data readiness checks from accepting a cache whose visible backtest range is complete but whose price or adjustment-factor lookback is too short.

## [0.10.23] - 2026-07-22

### Fixed

- Align `get_current_data()` with JoinQuant's current-unit semantics at 09:30: expose the current trading day's known opening price instead of the previous trading day's open.
- Use the opening price as `last_price` at the opening snapshot and calculate daily price limits from the previous close.
- Keep historical, fundamentals, and pre-open data boundaries unchanged to avoid future-data leakage.

## [0.10.22] - 2026-07-16

### Changed

- Add an in-page searchable strategy library so embedded browsers can select project strategies without a native file dialog.
- Keep native file import as a secondary action and add drag-and-drop fallback.
- Route JoinQuant-style `801xxx.XSHG` industry indexes to Tushare `sw_daily` data and include the Shenwan level-one index set in automatic cache updates.
- Map the `000985.XSHG` benchmark to Tushare's `000985.CSI` code and make readiness checks generate API-specific refill requests for index gaps.

## [0.10.21] - 2026-07-11

### Changed

- Add live backtest progress, elapsed timing, and cooperative cancellation to the Web console.

## [0.10.20] - 2026-07-11

### Changed

- Align 09:30 partial daily bars and weekly trading-day scheduling with JoinQuant.
- Fill paused price rows without leaking future daily data.
- Keep explicit income quarters exact while preserving latest-visible defaults.

## [0.10.19] - 2026-07-11

### Changed

- Infer, validate, and backfill strategy index price dependencies, including Shanghai Composite routing.

## [0.10.18] - 2026-07-11

### Changed

- Add transparent canonical price caching, layered performance metrics, SQLite read optimization, and sensitive log redaction.

## [0.10.17] - 2026-07-07

### Changed

- Ignore local experiment strategies and document private strategy handling.

## [0.10.16] - 2026-07-07

### Changed

- Add interactive hover tooltips for report charts.

## [0.10.15] - 2026-07-07

### Changed

- Add Sharpe ratio and capital turnover metrics to risk report.

## [0.10.14] - 2026-07-06

### Changed

- Optimize repeated get_price reads with date-range and result caches

## [0.10.13] - 2026-07-05

### Changed

- Run initial weekly callbacks when the first backtest day is after the scheduled weekday
- Match JoinQuant fixed-spread slippage by applying half the spread per trade side
- Align `order_target_value` sizing and cash checks with JoinQuant's pre-slippage price basis

## [0.10.12] - 2026-07-05

### Changed

- Show failed backtest errors in the web console

## [0.10.11] - 2026-07-05

### Changed

- Embed dual moving-average backtest screenshot in docs

## [0.10.10] - 2026-07-05

### Changed

- Remove factor alignment example template

## [0.10.9] - 2026-07-05

### Changed

- Fix attribute_history data readiness and holiday weekly scheduling

## [0.10.8] - 2026-07-05

### Changed

- Prefer project strategy files when stale uploaded snapshots are submitted.

## [0.10.7] - 2026-07-05

### Changed

- Show strategy source metadata and warn before running stale uploaded snapshots.

## [0.10.6] - 2026-07-05

### Changed

- Expand the dual moving-average baseline to weekly multi-ETF rotation.
- Use zero sell tax in the ETF baseline cost model.

## [0.10.5] - 2026-07-05

### Added

- Add local dual moving-average momentum baseline strategy.

### Changed

- Avoid repeated sub-lot rebalancing in the dual moving-average baseline while the signal is unchanged.

## [0.10.4] - 2026-07-05

### Added

- Add JoinQuant factor alignment template for data comparisons.

### Changed

- Support ETF adjustment factors for local fund price adjustments.

## [0.10.3] - 2026-07-04

### Changed

- Accept zero `close_today_commission`, support `FixedSlippage`, and expose `get_security_info`.
- Backfill historical `stock_basic` list statuses so delisted securities keep metadata in historical backtests.

## [0.10.2] - 2026-07-04

### Changed

- Look back for index weight snapshots before backtest starts.

## [0.10.1] - 2026-07-04

### Changed

- Use announcement-date-safe income fallback for fundamentals.

## [0.10.0] - 2026-07-04

### Changed

- Automatically backfill missing local cache data before backtests.

## [0.9.0] - 2026-07-04

### Changed

- Add fund_daily cache support for ETF price reads and common strategy compatibility aliases.

## [0.8.2] - 2026-07-04

### Changed

- Widen Web console date inputs to avoid clipped date text.

## [0.8.1] - 2026-07-03

### Changed

- Keep completed Web console jobs out of the main report workspace.

## [0.8.0] - 2026-07-03

### Changed

- Fix backtest readiness checks and automate Web data-check/report-refresh flow.

## [0.7.3] - 2026-07-03

### Changed

- Move unit tests into a dedicated tests package.

## [0.7.2] - 2026-07-03

### Changed

- Refresh SDK version labels in historical HTML reports.

## [0.7.1] - 2026-07-03

### Changed

- Clear stale unsupported benchmark reasons when refreshing reports.

## [0.7.0] - 2026-07-03

### Changed

- Add historical report refresh for benchmark-dependent metrics.

## [0.6.9] - 2026-07-03

### Changed

- Explain missing benchmark data in readiness checks and reports.

## [0.6.8] - 2026-07-03

### Changed

- Skip SDK target-capture closures during runtime safety scans.

## [0.6.7] - 2026-07-03

### Changed

- Mark incomplete Web console runs without generated reports.

## [0.6.6] - 2026-07-03

### Changed

- Default Web console dates to the previous cached trading day and a one-month window.

## [0.6.5] - 2026-07-03

### Changed

- Allow standard integer initial cash values such as 1000000 in the Web console.

## [0.6.4] - 2026-07-03

### Changed

- Remove the repeated conclusion section from generated HTML reports.

## [0.6.3] - 2026-07-03

### Changed

- Avoid reloading the selected Web console report during background polling.

## [0.6.2] - 2026-07-03

### Changed

- Compact Web console run controls and move advanced paths into settings.

## [0.6.1] - 2026-07-03

### Changed

- Refine Web console layout with top-run controls, file picker upload, and separate history view.

## [0.6.0] - 2026-07-03

### Added

- Add local Web console for launching backtests and browsing run reports.

## [0.5.1] - 2026-07-03

### Changed

- Add transparent data-layer optimization for batched current data snapshots.
- Add `--no-optimize-data` for performance comparisons and compatibility diagnostics.

## [0.5.0] - 2026-07-03

### Changed

- Add deterministic backtest re-exec and per-run data fetch caching for repeated local queries.

## [0.4.2] - 2026-07-03

### Changed

- Show the running SDK version in generated HTML reports.

## [0.4.1] - 2026-07-03

### Changed

- Clamp open-time data APIs to the previous trading day to avoid look-ahead bias.

## [0.4.0] - 2026-07-03

### Changed

- Redesign the HTML report into a task-focused analysis workspace.

## [0.3.0] - 2026-07-03

### Changed

- Add local backtest performance profiling and bottleneck guidance to HTML reports.

## [0.2.1] - 2026-07-03

### Changed

- Polish local HTML report navigation and remove unsupported JoinQuant-only controls.

## [0.2.0] - 2026-07-03

### Changed

- Add JoinQuant-style benchmark, excess return, alpha, and beta metrics.

## [0.1.0] - 2026-07-03

### Added

- Initial local JoinQuant-compatible daily backtest runtime.
- Tushare-backed SQLite cache for market data, fundamentals, index data, and trading calendars.
- Local data readiness checks before running a backtest.
- Order, trade, position, fee, daily performance, and log outputs.
- Per-run output directories for reproducible report artifacts.
- HTML backtest report with overview, transaction details, daily holdings, and logs.
- Public README with installation, cache preparation, backtest, report, and security guidance.
