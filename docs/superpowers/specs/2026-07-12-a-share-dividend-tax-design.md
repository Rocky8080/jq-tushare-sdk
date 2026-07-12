# A-Share Dividend Tax Design

## Goal

Make A-share cash-dividend tax follow the holding-period policy: 20 percent
within one calendar month (inclusive), 10 percent through one calendar year
(inclusive), and zero after one calendar year.

## Design

Cash dividends remain prepaid at 20 percent on the payment date. Each FIFO
purchase tax lot records its acquisition time. When a lot is sold, the broker
compares its acquisition date with the sale date, computes its final rate, and
returns the over-withheld portion through the sale cash settlement. A negative
`dividend_tax` on the sale trade represents that refund.

The change is deliberately limited to settled dividends for shares still held
at payment time, which is the existing tax-lot model's supported lifecycle.

## Tests

Use real broker orders to assert the final cash result and sale-trade tax
adjustment for the 20 percent, 10 percent, and zero percent bands.
