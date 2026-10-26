# S7 lag study — KXETHD vs Coinbase spot
hours=82 (2026-10-23T10:00:00Z → 2026-10-26T20:00:00Z), decision rows=4694

## Calibration (Brier): model vs market mid
| horizon | n | model | market mid |
|---|---|---|---|
| T-10m | 882 | 0.0335 | 0.0368 |
| T-5m | 928 | 0.0231 | 0.0245 |
| T-3m | 942 | 0.0169 | 0.0188 |
| T-2m | 978 | 0.0120 | 0.0145 |
| T-1m | 964 | 0.0086 | 0.0124 |

## Taker rule at the touch, hold to expiry, 10-lot fee (net cents/contract)
| horizon | thr | n | avg | 1se | hit | hours>0/hours |
|---|---|---|---|---|---|---|
| T-10m | 0.03 | 162 | +6.1c | 2.8c | 32% | 34/73 |
| T-10m | 0.05 | 126 | +7.6c | 3.4c | 32% | 27/64 |
| T-10m | 0.08 | 94 | +7.5c | 4.2c | 32% | 22/55 |
| T-10m | 0.12 | 60 | +13.4c | 5.6c | 37% | 15/41 |
| T-5m | 0.03 | 135 | +5.5c | 3.1c | 30% | 32/72 |
| T-5m | 0.05 | 115 | +8.2c | 3.4c | 31% | 30/66 |
| T-5m | 0.08 | 86 | +10.9c | 4.3c | 36% | 27/58 |
| T-5m | 0.12 | 63 | +15.5c | 5.5c | 41% | 24/49 |
| T-3m | 0.03 | 91 | +8.7c | 3.9c | 33% | 25/65 |
| T-3m | 0.05 | 78 | +10.7c | 4.4c | 33% | 23/61 |
| T-3m | 0.08 | 69 | +13.0c | 4.9c | 35% | 22/57 |
| T-3m | 0.12 | 52 | +15.8c | 5.8c | 38% | 19/46 |
| T-2m | 0.03 | 77 | +13.6c | 4.3c | 39% | 25/67 |
| T-2m | 0.05 | 67 | +14.4c | 4.6c | 39% | 23/61 |
| T-2m | 0.08 | 59 | +17.0c | 5.0c | 37% | 20/54 |
| T-2m | 0.12 | 45 | +18.6c | 5.9c | 40% | 17/43 |
| T-1m | 0.03 | 48 | +16.9c | 6.3c | 46% | 22/48 |
| T-1m | 0.05 | 45 | +19.2c | 6.4c | 47% | 21/45 |
| T-1m | 0.08 | 41 | +19.2c | 6.9c | 44% | 18/41 |
| T-1m | 0.12 | 34 | +19.1c | 8.0c | 38% | 13/34 |

Caveats: taker fills assumed at the 1-min candle close touch; settlement index is BRTI 60-s average (Coinbase used as proxy); sigma trailing 120 min, no lookahead.