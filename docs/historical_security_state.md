# Historical A-share security state (2026-08-31 repair)

Historical backtests must not infer ST from today's `stock_basic.name`, or construct every stock's limit prices from the previous raw close and a guessed 5/10/20% rule.

## Required data

- `stock_st`: full daily ST snapshots, with completion markers in `security_state_coverage`. Per-symbol updates invalidate the day's completeness marker.
- `stk_limit`: full provider-published daily price limits, including `pre_close`. Paginate beyond one request to include funds and all exchanges.
- `namechange`: full effective-name timelines, not just announcements within the backtest window. Per-symbol repairs may be needed: global pagination can lose records at tied date boundaries.
- `bse_mapping`: joins historical ST codes to the retroactively renamed BSE price/name-history codes. Original raw snapshots remain unchanged.
- `stock_basic`: explicitly request all registered fields, including listing/delisting dates and L/D/P listings.

Example, with a dedicated experimental cache:

```sh
python -m jq_tushare_sdk.cli update-data --cache-db /absolute/path/cache.db --start 2024-07-01 --end 2024-12-31 --api stock_basic --api bse_mapping --api namechange --api stock_st --api stk_limit
```

`get_current_data` uses the requested trade day's ST list and limits. Current names never substitute for missing historical names: a code label plus `name_source=security_code` is returned instead. A historical-name/ST discrepancy for an actively quoted security fails closed. An effective ST name is also affirmative evidence for suspended securities omitted from daily snapshots. `get_all_securities(date=...)` filters listing/delisting dates and returns effective historical names.

The cache updater atomically replaces exactly one completed daily snapshot; missing/empty/duplicated pages cannot be marked complete. CLI/web readiness checks validate every trading day's completion marker and record count, include the preceding trade day for pre-open callbacks, and require actual limit rows for positively traded stocks. Missing historical-name labels are reported separately as advisory (the independently complete daily ST snapshot is still mandatory). Overlapping name intervals use the latest effective start; disagreeing names with the same effective start are rejected.

Explicit provider IPO limits such as `99999.99 / 0.0` are preserved. Missing rows are never interpreted as unlimited trading. Funds retain the SDK's legacy limit approximation and are outside this A-share repair. Reports may still use current display-name labels; historical state audits must key on security code and date.

## Verification

Use real cached observations, not synthetic fixtures. On 2026-09-30, the local cache audit covered 15 trade days (2026-08-03 through 2026-08-21) and 83,058 positively traded daily rows, with no ST/name/official-limit errors after interval selection was corrected. Optimized and ordinary current-data lookups agreed on the sampled securities. This is a bounded local verification, not a guarantee that every provider date is complete. Both comparison arms must be rebuilt after this data/SDK change.

The existing full test suite currently reports 287 passed and 16 failures: legacy synthetic backends in the API/data/engine tests do not supply the now-required complete historical snapshots. Those fixtures need migration; do not make production silently fall back to present-day names or guessed limits to bypass these errors. No tests have been skipped to claim a clean full-suite result.

Provider references: [ST daily list](https://tushare.pro/document/2?doc_id=397), [daily price limits](https://tushare.pro/document/2?doc_id=183), [name history](https://tushare.pro/document/2?doc_id=100), [BSE identity mapping](https://tushare.pro/document/2?doc_id=375).
