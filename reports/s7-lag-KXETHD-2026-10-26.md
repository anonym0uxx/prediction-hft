# S7 lag study — KXETHD vs Coinbase spot
hours=83 (2026-10-23T13:00:00Z → 2026-10-27T00:00:00Z), decision rows=4757

## Calibration (Brier): model vs market mid
| horizon | n | model | market mid |
|---|---|---|---|
| T-10m | 896 | 0.0374 | 0.0365 |
| T-5m | 942 | 0.0243 | 0.0241 |
| T-3m | 952 | 0.0186 | 0.0177 |
| T-2m | 988 | 0.0154 | 0.0141 |
| T-1m | 979 | 0.0110 | 0.0119 |

## Taker rule at the touch, hold to expiry, 10-lot fee (net cents/contract)
| horizon | thr | n | avg | 1se (hour-clustered) | hit | hours>0/hours |
|---|---|---|---|---|---|---|
| T-10m | 0.03 | 143 | -2.0c | 2.7c | 18% | 22/68 |
| T-10m | 0.05 | 102 | +1.1c | 3.4c | 22% | 20/56 |
| T-10m | 0.08 | 56 | +0.5c | 4.1c | 20% | 10/35 |
| T-10m | 0.12 | 22 | +1.6c | 7.3c | 18% | 4/15 |
| T-5m | 0.03 | 108 | +1.0c | 3.1c | 22% | 20/65 |
| T-5m | 0.05 | 83 | +1.1c | 3.7c | 23% | 17/57 |
| T-5m | 0.08 | 57 | +1.8c | 4.5c | 21% | 11/43 |
| T-5m | 0.12 | 26 | +10.0c | 7.6c | 27% | 7/23 |
| T-3m | 0.03 | 74 | -1.7c | 5.1c | 24% | 15/58 |
| T-3m | 0.05 | 54 | -2.2c | 6.2c | 19% | 10/46 |
| T-3m | 0.08 | 36 | +1.9c | 7.2c | 19% | 7/31 |
| T-3m | 0.12 | 23 | -4.9c | 8.0c | 13% | 3/21 |
| T-2m | 0.03 | 61 | -6.5c | 4.8c | 18% | 11/56 |
| T-2m | 0.05 | 46 | -3.2c | 5.6c | 20% | 9/42 |
| T-2m | 0.08 | 32 | -6.0c | 6.9c | 19% | 6/30 |
| T-2m | 0.12 | 19 | +2.1c | 9.4c | 26% | 5/18 |
| T-1m | 0.03 | 29 | +8.9c | 9.5c | 41% | 12/29 |
| T-1m | 0.05 | 26 | +15.8c | 9.4c | 46% | 12/26 |
| T-1m | 0.08 | 19 | +14.2c | 10.5c | 42% | 8/19 |
| T-1m | 0.12 | 15 | +9.3c | 11.4c | 40% | 6/15 |

Caveats: taker fills assumed at the 1-min candle close touch; settlement index is BRTI 60-s average (Coinbase used as proxy); sigma trailing 120 min; Coinbase candles keyed by bucket END (lookahead fix 2026-10-26); se clustered by settlement hour.