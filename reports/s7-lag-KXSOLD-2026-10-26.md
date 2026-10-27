# S7 lag study — KXSOLD vs Coinbase spot
hours=40 (2026-10-25T08:00:00Z → 2026-10-27T00:00:00Z), decision rows=1862

## Calibration (Brier): model vs market mid
| horizon | n | model | market mid |
|---|---|---|---|
| T-10m | 367 | 0.0512 | 0.0605 |
| T-5m | 346 | 0.0323 | 0.0391 |
| T-3m | 349 | 0.0213 | 0.0273 |
| T-2m | 372 | 0.0124 | 0.0191 |
| T-1m | 428 | 0.0036 | 0.0088 |

## Taker rule at the touch, hold to expiry, 10-lot fee (net cents/contract)
| horizon | thr | n | avg | 1se (hour-clustered) | hit | hours>0/hours |
|---|---|---|---|---|---|---|
| T-10m | 0.03 | 53 | +2.3c | 4.5c | 21% | 11/29 |
| T-10m | 0.05 | 33 | +3.7c | 7.2c | 21% | 7/25 |
| T-10m | 0.08 | 14 | +5.3c | 10.4c | 21% | 3/12 |
| T-10m | 0.12 | 3 | +9.1c | 33.3c | 33% | 1/3 |
| T-5m | 0.03 | 29 | +8.7c | 6.4c | 28% | 8/23 |
| T-5m | 0.05 | 23 | +9.5c | 7.7c | 26% | 6/20 |
| T-5m | 0.08 | 10 | +13.9c | 14.7c | 30% | 3/10 |
| T-5m | 0.12 | 7 | +13.7c | 18.7c | 29% | 2/7 |
| T-3m | 0.03 | 18 | +6.7c | 8.7c | 22% | 4/17 |
| T-3m | 0.05 | 14 | +10.3c | 11.1c | 29% | 4/14 |
| T-3m | 0.08 | 12 | +7.4c | 11.8c | 25% | 3/12 |
| T-3m | 0.12 | 8 | +10.6c | 17.6c | 25% | 2/8 |
| T-2m | 0.03 | 13 | -1.8c | 7.9c | 15% | 2/13 |
| T-2m | 0.05 | 11 | -1.9c | 9.2c | 9% | 1/11 |
| T-2m | 0.08 | 8 | +2.9c | 12.3c | 12% | 1/8 |
| T-2m | 0.12 | 4 | +13.6c | 25.0c | 25% | 1/4 |
| T-1m | 0.03 | 6 | -9.5c | 7.4c | 17% | 1/6 |
| T-1m | 0.05 | 6 | -9.5c | 7.4c | 17% | 1/6 |
| T-1m | 0.08 | 5 | -8.7c | 9.0c | 20% | 1/5 |
| T-1m | 0.12 | 4 | -17.2c | 3.7c | 0% | 0/4 |

Caveats: taker fills assumed at the 1-min candle close touch; settlement index is BRTI 60-s average (Coinbase used as proxy); sigma trailing 120 min; Coinbase candles keyed by bucket END (lookahead fix 2026-10-26); se clustered by settlement hour.