# FavT5 v3 — frozen BTC rule (shadow only)

Status 2026-11-12: **implemented, shadow only.** Reference archive `FavT5_v3_Stress_Test.zip`
(sha256 `39e6d27c5398dac7e576c6006c543809cbadbc1dfb3eb6e7100e6bed8eb00853`, all 80 internal checksums verified), unpacked
untracked at `data/favt5-v3/reference/`.

- Scorer: `src/predhft/favt5_v3_index.py` is the archive's `deep/favt5_v3_index.py` byte for byte
  (sha256 `c2db8eb2...0fad0`, pinned by a test). It is never edited; the shadow only feeds it and records its answer.
- Reproduction (tests/test_favt5_v3.py): the example snapshot returns the archived intent exactly; scoring all 5,315
  archived T-480 rows returns exactly the 77 archived orders (side, size, limit, cost).
- Shadow runner: `src/predhft/favt5_v3_shadow.py`, task `PredHFT-FavT5-v3-Shadow`, ledger
  `data/favt5-v3/shadow/fills.jsonl`. Arms: `btc_v3` (the rule), `btc_fav_t5` (same-budget favourite control),
  `eth_challenge` (same scorer on the ETH contract's own index, tracked separately).
- Offline comparisons: `research/favt5_v3/offline.py` -> `reports/favt5_v3/offline_comparisons.json`.

How the live inputs map onto the scorer's snapshot (choices made here, not in the archive):
`quoted_strike` = the exchange's `floor_strike` for the same ticker, fetched before the decision (model input);
`official_strike` = the strike rebuilt from our own received index (mean of the 60 seconds before the open), so a strike
we cannot confirm is rejected; quote event = exchange time of the last sequence-valid book frame for that ticker.

Branch `favt5-v3`, worktree `D:/prediction-hft-v3`, forked from `main` at `8fb8a1e` (FavT5 v1 strategy files last
changed in `9d3103c`). No live order route exists in this branch and none may be added without the owner's separate
authorization. Promotion is never automatic.

## Parameters as ordered by the owner (do not retune; do not narrow the band using the published holdout)

| Item | Value |
|---|---|
| Series | `KXBTC15M` (ETH tracked separately as a native-index challenge) |
| Decision | once per expiry, exactly 480 s before close; a boundary missed through downtime is skipped, never made up |
| Binding | quote, strike and close time must belong to the same exchange ticker |
| Strike | official window strike available by decision time; must match the captured strike within $0.02; unpublished = reject |
| Index | BRTI one-second observations; last index observation retained at least 3 s old; separate one-second availability check |
| Quote | receive-time-valid Kalshi YES bid and ask, aged at most 3 s; spread above $0.06 = reject |
| Rejects | stale input, missing volatility history, wide spread, unpublished strike, strike difference over $0.02 |
| Sides | YES and NO scored symmetrically |
| Entry band | buy an offer priced $0.15–$0.60 |
| Edge | modeled net edge at full limit size ≥ $0.05 per contract after a $0.01 adverse tick, quadratic taker fee and conservative whole-cent cash rounding |
| Ranking | qualifying sides ranked by expected net dollars |
| Size | one hypothetical $10 all-in cap per expiry (comparable paper measurement; shared with ETH in any joint simulation) |
| Exit | hold to official settlement |
| Intent key | deterministic, one per series and expiry, so reconnects and restarts cannot duplicate an order |
| Fees / grid | read exchange fee and price-grid metadata at startup; flag replay divergences before comparing returns; keep the frozen replay fee assumption in the research ledger |
| Shadow fills | measured arrival latency, sequence-validated displayed depth, price/size limits, partial fills, realistic fee rounding, no assumed fill across book gaps |
| Replays | independent 1, 2, 5, 15 and 30 second execution replays of identical intents |
| Logging | every decision and skip; predicted side probability, offer, fee, modeled dollars, available size, expected vs realized net, side, entry band, vol regime, official outcome; diagnostics: signed trade flow, BTC/ETH lead-lag, top-book imbalance |

## Offline comparisons owed
1. Same-budget BTC T-5 favourite at $0.965–$0.990.
2. Attribution of the 15–under-30¢ subset versus 30–60¢ on the same v3 intents (development +$138.07, confirmation +$98.15 for the cheap subset, per the owner).
3. Separate ETH native-index (ETHUSD_RTI) challenge.

## Review criteria (precommitted by the owner; evidence only, never an automatic promotion)
At least 100 genuinely forward BTC fills and 60 fully observed UTC days, whichever takes longer. The strategy stays in
research if realized after-fee P&L or Sharpe fails to beat the same-budget favourite, if a three-day block 95% lower
bound for paired net dollars per day fails to clear zero, or if the five-second stress is negative.

## Exchange metadata read 2026-11-12
`KXBTC15M` / `KXETH15M`: `fee_type = quadratic`, `fee_multiplier = 1`, `price_level_structure = tapered_deci_cent`,
`strike_type = greater_or_equal`, settlement = simple average of the sixty one-second index values in `[close-60, close)`
(verified against 552 official outcomes, max difference $0.005 = rounding).
