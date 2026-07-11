# Strategy Index Readiness Design

## Problem

The backtest readiness check currently validates only the configured benchmark's
`index_daily` history. Strategies may use additional indexes through
`get_price()`, including calls inside loops over module-level index lists. A
backtest can therefore pass readiness while some market-regime indexes have too
little history.

The 5.8.1 alignment run demonstrated the failure: the benchmark and two indexes
had 70 observations, while `000688.XSHG`, `000852.XSHG`, and `000001.XSHG` had
only 24 and were silently excluded by the strategy. The resulting trend score
was 70.0 locally instead of 45.8 on JoinQuant, changing the regime from RiskOff
to Rotation before stock selection began.

## Goals

- Infer every statically discoverable index used for price history without
  requiring strategy source changes.
- Infer the maximum requested `count` for each index, including loop variables
  bound to module-level index lists.
- Validate and automatically backfill each required index before a backtest.
- Keep benchmark validation and existing strategy compatibility intact.
- Report the exact incomplete index and required date range when readiness still
  fails after an update.

## Non-Goals

- Dynamic index symbols constructed at runtime are not inferred.
- This change does not alter `get_index_stocks()` point-in-time behavior.
- This change does not modify financial-statement fallback or strategy logs.
- This change does not attempt to force local market-regime values to a preset
  JoinQuant result; it only guarantees the required source history is present.

## Design

### Static requirement inference

Add an inference function in `jq_tushare_sdk.data.readiness` that returns an
ordered mapping from JoinQuant index symbol to maximum positive `count`.

The analyzer will reuse the existing AST static-expression evaluator and module
environment. It will resolve:

- direct calls such as `get_price("000300.XSHG", count=70)`;
- module constants such as `GEM_INDEX`;
- loop variables whose iterable is a statically resolvable module list, such as
  `for idx in REGIME_INDEXES: get_price(idx, count=70)`.

Only codes recognized as indexes by the SDK's existing code conventions are
included: Shenzhen `399xxx` codes and the supported Shanghai index code set.
Unresolved symbols are ignored rather than causing readiness to fail.

### Required history range

For each inferred index, convert its maximum `count` to the same conservative
calendar lookback already used by price readiness:

`max(count * 3 + 7, 14)` calendar days before the backtest start.

Calls with an explicit static `start_date` may be supported later; this change
is scoped to count-based index history, which caused the confirmed failure.
The configured benchmark remains required across the existing benchmark range.
If a symbol is both benchmark and strategy index, the earlier required start
date wins.

### Readiness and automatic update

Replace benchmark-only index validation with strategy-index validation that:

1. Builds the combined benchmark and inferred-index requirements.
2. Fetches each symbol independently from the local `index_daily` cache.
3. Compares its actual minimum and maximum dates with its required range.
4. Produces one readiness issue containing an update request per incomplete
   symbol.
5. Uses the existing automatic update and recheck flow unchanged.

The issue message will list each incomplete symbol and its missing boundary so
the Web UI can show an actionable cause when an API update cannot fill it.

### Failure behavior

- Missing or syntactically invalid strategy files preserve current graceful
  behavior and validate only the benchmark.
- A dynamic or unresolved index expression does not block the backtest.
- If local data remains shorter than the inferred requirement after update, the
  backtest stops before strategy execution instead of silently dropping indexes
  from market-regime calculations.

## Verification

Follow a red-green workflow with existing real-code test infrastructure:

1. Add a readiness regression where the benchmark has enough history but three
   loop-referenced indexes begin too late. Confirm the test fails because the
   current checker reports no issue.
2. Add inference coverage for direct constants and loop-bound index lists with
   different counts.
3. Implement the minimal analyzer and readiness integration, then make the new
   tests pass.
4. Run the existing data, engine, CLI, and Web suites.
5. Run readiness against the uploaded 5.8.1 strategy and verify that all six
   `REGIME_INDEXES` receive update requests when incomplete.
6. After data is available, rerun 2026-07-07 through 2026-07-10 and verify all
   six indexes participate in the trend calculation before comparing downstream
   trades.

## Acceptance Criteria

- The 5.8.1 strategy yields six inferred index-price requirements, with a
  70-observation requirement for every `REGIME_INDEXES` member.
- Readiness fails when any required index lacks the inferred lookback even when
  the benchmark is complete.
- Automatic update requests include the correct `ts_code`, start date, and end
  date for every incomplete index.
- Existing benchmark-only strategies behave as before.
- Existing tests and the new regression tests pass.
