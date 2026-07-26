# Changelog

All notable changes to JQ Tushare SDK are documented in this file.

This project follows Semantic Versioning.

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
