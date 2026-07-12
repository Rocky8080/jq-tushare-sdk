# A-Share Dividend Tax Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the A-share holding-period dividend tax policy in broker cash settlement.

**Architecture:** Store the acquisition date with each FIFO A-share tax lot. Keep payment-date withholding at 20 percent, then calculate a negative sale-side `dividend_tax` for the refundable difference using calendar-month and calendar-year boundaries.

**Tech Stack:** Python 3, standard library `datetime` and `calendar`, unittest.

## Global Constraints

- Preserve existing uncommitted company-action changes.
- Use calendar anniversary boundaries: exactly one month remains in the 20 percent band; exactly one year remains in the 10 percent band.
- Avoid changes outside dividend-tax settlement and its regression tests.

---

### Task 1: Holding-period Regression Coverage

**Files:**
- Modify: `tests/test_jq_tushare_sdk_broker.py`

**Interfaces:**
- Consumes: `Broker.order`, `Broker.capture_dividend_lots`, and `Broker.attach_dividend_tax`.
- Produces: A failing regression test for 20 percent, 10 percent, and zero final rates.

- [ ] **Step 1: Write the failing test**

```python
def test_sell_refunds_prepaid_dividend_tax_by_holding_period(self):
    # Buy, prepay a 20% tax on a 0.50 dividend, then sell at each boundary.
    # Assert adjustments of 0.0, -5.0, and -10.0 for 20%, 10%, and 0% final tax.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_jq_tushare_sdk_broker.TestBroker.test_sell_refunds_prepaid_dividend_tax_by_holding_period`

Expected: FAIL because the current sale path always returns `0.0` dividend tax.

### Task 2: Calendar-Aware Tax-Lot Settlement

**Files:**
- Modify: `jq_tushare_sdk/broker/broker.py`
- Test: `tests/test_jq_tushare_sdk_broker.py`

**Interfaces:**
- Consumes: `context.current_dt` at buy and sell time; a lot's `pending_tax` from payment-date withholding.
- Produces: `_consume_dividend_tax_lots(security, amount)` returns a zero or negative cash adjustment and records it on `Order.dividend_tax` and `Trade.dividend_tax`.

- [ ] **Step 1: Add acquisition time to `_DividendTaxLot`**

```python
@dataclass
class _DividendTaxLot:
    lot_id: int
    remaining_amount: float
    acquired_at: datetime | None = None
    pending_tax: float = 0.0
```

- [ ] **Step 2: Compute the final rate for each consumed FIFO lot**

```python
if sold_on <= add_calendar_months(acquired_on, 1):
    rate = 0.20
elif sold_on <= add_calendar_months(acquired_on, 12):
    rate = 0.10
else:
    rate = 0.0
refund = withheld_for_consumed_shares * (1.0 - rate / 0.20)
```

- [ ] **Step 3: Return the refund as a negative transaction cost**

```python
return -refund
```

- [ ] **Step 4: Run focused regression tests**

Run: `uv run python -m unittest tests.test_jq_tushare_sdk_broker`

Expected: PASS.

### Task 3: Final Verification

**Files:**
- Modify: `jq_tushare_sdk/broker/broker.py`
- Modify: `tests/test_jq_tushare_sdk_broker.py`

- [ ] **Step 1: Run project data and broker suite**

Run: `uv run python -m unittest tests.test_jq_tushare_sdk_data tests.test_jq_tushare_sdk_broker`

Expected: PASS.

- [ ] **Step 2: Check the scoped diff**

Run: `git diff --check -- jq_tushare_sdk/broker/broker.py tests/test_jq_tushare_sdk_broker.py`

Expected: exit code 0.
