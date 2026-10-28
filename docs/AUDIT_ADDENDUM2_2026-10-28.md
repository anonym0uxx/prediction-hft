# AUDIT ADDENDUM 2 — 2026-10-28 (P22 v2 repair, P12 restart pre-check, calibration diagnostic, statistical spec, authoritative report)

Written 2026-10-28 10:35 PT. Paper only; 0 orders, 0 capital. Numbers come from `data/p22/addendum2_2026-10-28.json` (`scripts/addendum2_2026_10_28.py`, as-of 09:05:51 PT for the fixed cohort, 10:23:24 PT for "current") and `data/p22/p22_v2_cells_2026-10-28.json` (`scripts/p22_v2_recompute.py`). Nothing committed; no runner, risk, sizing or threshold code changed.

## 0. Boundaries (Pacific Time)

| boundary | timestamp | meaning |
|---|---|---|
| P12 freeze | **2026-10-27 16:05:22 PT**, code `a395cf8` | start of the P12 forward-confirmation cohort (scope (a)); families `s7p15-KX{BTC,ETH,SOL}15M`, tasks `PredHFT-S7-15M-{BTC,ETH,SOL}` |
| P13/P16 15M scored-only | 2026-10-28 09:05:51 PT | `s7p13-KX*15M` runners record, never order |
| P13-BTCD scored-only | 2026-10-28 09:55:23 PT | `PredHFT-S7-P13-BTCD`; 0 fills ever (the hourly −$13.24 belongs to `s7p10-KXBTCD`/P10) |
| **P22-R1 eligibility + fee freeze** | **2026-10-28 10:28:00 PT** (`docs/PREREG.md` §P22-R1) | P22 confirmation cohort = markets with close ≥ this instant; everything before is discovery |
| Risk-ledger day | 00:00 UTC = 17:00 PT | $25 daily halt counter |

## 1. P22 v2 repair (`src/predhft/p22_exec.py`, measurement version `p22-v2`)

**Defect (v1).** Paper price = `min(raw_offer + 0.01, 0.99)`. For raw offers in (0.98, 0.99] the one-tick haircut was truncated; for raw offers > 0.99 the paper price was *below* the displayed offer (0.995 → 0.99). 161 of 269 "offered" records were affected and every R2 "+0.9 c win" was this artefact (addendum §4 †).

**Fix (v2).** `exec_price = raw_offer + 0.01`, never clipped; `exec_status ∈ {ok (raw ≤ 0.98), outside_modeled_range (raw > 0.98), no_offer, unavailable}`. `outside_modeled_range` records are **excluded from numerator and denominator** of every edge / realised-net cell and reported as their own count. Fee unchanged (Kalshi taker 0.07·q·P·(1−P), ceil-to-cent per lot-10 order, ÷ 10). 960 records recomputed → `data/p22/p22_paired_2026-10-28.v2.jsonl`; the v1 file is untouched. Status counts (official forecast): ok 109, outside_modeled_range 160, no_offer 691; 0 unrecoverable.

**Which cells changed.** Denominators: ALL_offered and R1 lose exactly the outside-range records (49/39/37/26/9 at T-60/45/30/20/10); R2 collapses to 4/3/0/0/0 (it was almost entirely 0.99+ favourites); **R3 is unchanged at every horizon** (its `offer ≤ 0.95` filter already excluded the affected records). Realised net moves against the forecast everywhere the cell survives: R1 at T-30 goes from −5.4 ± 3.2 c (n 49) to **−24.9 ± 10.9 c (n 12)**.

| T-h s | rule | v1 n | v1 edge c | v1 realised c ± SE | v2 n | v2 edge c | v2 realised c ± SE | excluded (raw > 0.98) | Δn |
|---|---|---|---|---|---|---|---|---|---|
| 60 | ALL_offered **(changed)** | 96 | -9.3 | +1.1 ± 2.3 | 47 | -13.8 | +1.2 ± 4.8 | 49 | -49 |
| 60 | R1 **(changed)** | 80 | -8.9 | +0.9 ± 1.7 | 31 | -15.1 | +0.9 ± 4.5 | 49 | -49 |
| 60 | R2 **(changed)** | 42 | -2.8 | +1.1 ± 0.1 | 4 | -5.9 | +2.9 ± 0.6 | 38 | -38 |
| 60 | R3 | 19 | -16.7 | +0.4 ± 7.4 | 19 | -16.7 | +0.4 ± 7.4 | 0 | +0 |
| 45 | ALL_offered **(changed)** | 74 | -8.7 | -1.6 ± 2.5 | 35 | -13.7 | -4.4 ± 5.5 | 39 | -39 |
| 45 | R1 **(changed)** | 68 | -8.6 | -3.2 ± 2.7 | 29 | -14.5 | -8.6 ± 6.3 | 39 | -39 |
| 45 | R2 **(changed)** | 36 | -2.6 | +1.2 ± 0.2 | 3 | -1.7 | +4.9 ± 1.9 | 33 | -33 |
| 45 | R3 | 17 | -14.6 | -10.2 ± 8.7 | 17 | -14.6 | -10.2 ± 8.7 | 0 | +0 |
| 30 | ALL_offered **(changed)** | 53 | -7.5 | -6.7 ± 3.0 | 16 | -18.5 | -24.4 ± 8.6 | 37 | -37 |
| 30 | R1 **(changed)** | 49 | -6.5 | -5.4 ± 3.2 | 12 | -18.1 | -24.9 ± 10.9 | 37 | -37 |
| 30 | R2 **(changed)** | 33 | -1.3 | +0.9 ± 0.0 | 0 | — | insufficient data (n=0) | 33 | -33 |
| 30 | R3 | 6 | -19.1 | -51.6 ± 19.1 | 6 | -19.1 | -51.6 ± 19.1 | 0 | +0 |
| 20 | ALL_offered **(changed)** | 34 | -5.4 | -6.4 ± 4.3 | 8 | -19.0 | -30.0 ± 16.3 | 26 | -26 |
| 20 | R1 **(changed)** | 33 | -4.9 | -7.3 ± 4.2 | 7 | -19.0 | -37.7 ± 16.5 | 26 | -26 |
| 20 | R2 **(changed)** | 25 | -0.4 | +0.9 ± 0.0 | 0 | — | insufficient data (n=0) | 25 | -25 |
| 20 | R3 | 4 | -15.0 | -67.5 ± 19.6 | 4 | -15.0 | -67.5 ± 19.6 | 0 | +0 |
| 10 | ALL_offered **(changed)** | 12 | -1.8 | -8.9 ± 7.1 | 3 | +4.6 | -38.4 ± 37.7 | 9 | -9 |
| 10 | R1 **(changed)** | 12 | -1.8 | -8.9 ± 7.1 | 3 | +4.6 | -38.4 ± 37.7 | 9 | -9 |
| 10 | R2 **(changed)** | 8 | -0.7 | +0.9 ± 0.0 | 0 | — | insufficient data (n=0) | 8 | -8 |
| 10 | R3 | 3 | +4.6 | -38.4 ± 37.7 | 3 | +4.6 | -38.4 ± 37.7 | 0 | +0 |


Units: edge = mean model-implied edge, c/contract = 100·(p_side − price − fee); realised = 100·(win − price − fee); SE clustered by 15-min close; "insufficient data (n=0)" replaces NaN; SE suppressed when the cell has < 2 closes. **R1 at T-30 remains the pre-declared confirmation endpoint** (addendum §6); all 960 records above are discovery and are never pooled with the confirmation cohort, which starts at the R1 freeze (10:28:00 PT) and currently has n = 0.

## 2. P12 restart pre-check

- **Decision neutrality proven:** `tests/test_p12_decision_neutrality.py` — **7 passed** — replays all **29,844 recorded P12 snapshots** (`data/s7p15-*/books.jsonl`) through the `a395cf8` (legacy) and HEAD book representations: identical touch on the whole price grid, exact snapshot reconstruction (0 failures), identical replayed decision on every snapshot, injecting/deleting every new field leaves every decision unchanged, and a static check that no decision function references a new field (new fields appear only in logging statements of `s7_live.py`).
- **New logging fields** (`src/predhft/p12_replay.py`): snapshot — `yes_bid_present, yes_ask_present, bids, asks, cfb_age_s, book_recv_t, book_fetch_s`; trade — `book_recv_t, recheck_recv_t, recheck_fetch_s, quote_age_s, intended_lot, avail_qty`. None enters price, fee, sizing or threshold.
- **Sensitivity 1 — decision latency > 1.5 s (NOT applied; P12 has no latency guard):** fixed cohort n = 2 (`KXSOL15M-26OCT281100-00` 2.45 s −$1.34; `KXBTC15M-26OCT281100-00` 2.86 s −$0.92): cohort net $1.76 → $4.02 without them. Current cohort n = 3 (adds `KXETH15M-26OCT281215-15` 2.05 s **+$6.72**): net $10.43 → $5.97 without them. The sign of the effect flipped with one fill — a guard cannot be justified from this.
- **Sensitivity 2 — fee rounding (NOT applied; ceil-to-cent per order stays):** fixed cohort fees booked $3.26 vs exact $3.05 (+$0.21 overstatement; net $1.76 → $1.97); current $3.60 vs $3.36 (+$0.24; $10.43 → $10.67).
- **Restart commands for the parent (PowerShell, one asset at a time; run only once, for instrumentation):**

```powershell
# verified 10:36 PT: each asset currently shows 2 python.exe with the same command line (venv launcher + interpreter) under a uv.exe parent
foreach ($a in 'BTC','ETH','SOL') {
  Get-CimInstance Win32_Process -Filter "Name like 'python%' or Name = 'uv.exe'" |
    Where-Object { $_.CommandLine -match "s7p15-KX$($a)15M" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 3
  Start-ScheduledTask -TaskName "PredHFT-S7-15M-$a"
  Start-Sleep -Seconds 5
}
Get-ScheduledTask -TaskName 'PredHFT-S7-15M-*' | Select-Object TaskName, State
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 's7p15' } | Select-Object ProcessId, CommandLine
```
Task action (unchanged): `uv.exe run python -m predhft.s7_live --series KX{BTC,ETH,SOL}15M --product {BTC,ETH,SOL}-USD --period 900 --t-open 1.0 --t-close 0.1 --lot 10 --sigma ewma94 --dist t --nu 8 --scale 0.8 --thr-mode dyn --root data/s7p15-KX*15M`. Afterwards confirm `data/s7p15-KX*15M/runner.log` shows one new start line per asset with the same flags as the previous start, and that the first new `books.jsonl` row carries `book_recv_t`. The cohort boundary stays 16:05:22 PT 10-27 because the decision path is unchanged.

## 3. P12 calibration diagnostic (observational; no tuning)

**Cohort.** Settled P12 fills with fill time in [freeze 2026-10-27 16:05:00 PT, as-of 2026-10-28 09:05:51 PT]: **n = 36** (fixed cohort, used for every statistic below). "Current" (as-of 10:23:24 PT) adds rows 37*–40*: n = 40.

**Headline (fixed cohort).** mean p_side(model) 0.458 vs hit rate 0.306 (gap +0.153, cluster-bootstrap 95 % CI by close [+0.008, +0.300], by hour block [+0.028, +0.277], B = 4000); mean p_side(market mid) 0.301. Paired on identical records: Brier model 0.182 vs market 0.137 (diff **+0.045**, CI by close [−0.015, +0.118]); log score 0.586 vs 0.435 (diff +0.151, CI [−0.049, +0.456]); realised $ under the model's decision (= the trades) **+$1.76** (CI [−39.25, +44.17]) vs under the market's implied decision (buy only when side-mid > booked price + fee) +$0.47 on 12 trades; model-implied $ of the same trades +$48.19 vs market-implied −$1.55. Net per contract +0.57 c, SE 6.8 c (close) / 5.7 c (hour). Current cohort: gap +0.127 (CI by close [−0.005, +0.262]), Brier diff +0.037, realised +$10.43, +3.02 c/contract, SE 6.5 c.
Reading: the market mid is the better forecaster of P12's own outcomes on both proper scores (point estimates), but neither difference is resolved at 95 % with 23 close-clusters; the realised $ CI is ±$40.

**Per-record table** (rows 37*–40* are post-as-of additions, not in the fixed-cohort statistics):

| # | ticker | decision (PT) | T-left s | side | p_side model | p_side mkt mid | price | qty | fee $ | outcome | realised $ | latency s | partial_n |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | KXSOL15M-26OCT271930-30 | 10-27 16:29:04 PT | 55.9 | yes | 0.669 | 0.425 | 0.490 | 8 | 0.14 | loss | -4.06 | 0.45 | 5 |
| 2 | KXETH15M-26OCT271930-30 | 10-27 16:29:22 PT | 37.8 | no | 0.250 | 0.087 | 0.090 | 9 | 0.06 | loss | -0.87 | 0.65 | 23 |
| 3 | KXBTC15M-26OCT271930-30 | 10-27 16:29:24 PT | 35.4 | no | 0.522 | 0.445 | 0.340 | 9 | 0.15 | loss | -3.21 | 0.45 | 25 |
| 4 | KXSOL15M-26OCT271945-45 | 10-27 16:44:02 PT | 57.6 | no | 0.302 | 0.155 | 0.130 | 9 | 0.08 | win | +7.75 | 0.45 | 3 |
| 5 | KXETH15M-26OCT271945-45 | 10-27 16:44:02 PT | 57.3 | no | 0.372 | 0.215 | 0.240 | 9 | 0.12 | loss | -2.28 | 0.57 | 3 |
| 6 | KXBTC15M-26OCT271945-45 | 10-27 16:44:13 PT | 46.0 | yes | 0.996 | 0.855 | 0.870 | 9 | 0.08 | win | +1.09 | 0.34 | 14 |
| 7 | KXSOL15M-26OCT272000-00 | 10-27 16:59:02 PT | 57.2 | no | 0.129 | 0.067 | 0.076 | 9 | 0.05 | loss | -0.73 | 0.8 | 3 |
| 8 | KXBTC15M-26OCT272030-30 | 10-27 17:29:05 PT | 54.6 | no | 0.982 | 0.933 | 0.944 | 9 | 0.04 | win | +0.46 | 0.45 | 6 |
| 9 | KXSOL15M-26OCT272030-30 | 10-27 17:29:09 PT | 50.7 | no | 0.282 | 0.170 | 0.092 | 9 | 0.06 | loss | -0.89 | 0.45 | 10 |
| 10 | KXBTC15M-26OCT272045-45 | 10-27 17:44:02 PT | 57.6 | yes | 0.161 | 0.070 | 0.090 | 9 | 0.06 | loss | -0.87 | 0.46 | 3 |
| 11 | KXBTC15M-26OCT272100-00 | 10-27 17:59:02 PT | 57.5 | yes | 0.226 | 0.125 | 0.105 | 6 | 0.04 | loss | -0.67 | 1.05 | 3 |
| 12 | KXBTC15M-26OCT272200-00 | 10-27 18:59:07 PT | 52.6 | no | 0.152 | 0.079 | 0.065 | 9 | 0.04 | loss | -0.63 | 0.45 | 8 |
| 13 | KXETH15M-26OCT272245-45 | 10-27 19:44:12 PT | 47.5 | no | 0.305 | 0.190 | 0.200 | 9 | 0.11 | loss | -1.91 | 1.1 | 13 |
| 14 | KXSOL15M-26OCT272315-15 | 10-27 20:14:06 PT | 53.7 | yes | 0.348 | 0.245 | 0.250 | 8 | 0.11 | win | +5.89 | 0.45 | 7 |
| 15 | KXETH15M-26OCT272315-15 | 10-27 20:14:33 PT | 26.3 | no | 0.998 | 0.943 | 0.960 | 9 | 0.03 | win | +0.33 | 0.45 | 34 |
| 16 | KXBTC15M-26OCT280030-30 | 10-27 21:29:02 PT | 57.3 | no | 0.781 | 0.535 | 0.430 | 9 | 0.16 | win | +4.97 | 0.45 | 3 |
| 17 | KXETH15M-26OCT280030-30 | 10-27 21:29:04 PT | 55.3 | no | 0.586 | 0.395 | 0.420 | 9 | 0.16 | win | +5.06 | 0.41 | 5 |
| 18 | KXBTC15M-26OCT280145-45 | 10-27 22:44:10 PT | 49.2 | no | 0.516 | 0.445 | 0.430 | 7 | 0.13 | loss | -3.14 | 0.3 | 11 |
| 19 | KXSOL15M-26OCT280230-30 | 10-27 23:29:06 PT | 53.5 | no | 0.803 | 0.620 | 0.650 | 9 | 0.15 | win | +3.00 | 0.3 | 7 |
| 20 | KXBTC15M-26OCT280230-30 | 10-27 23:29:13 PT | 46.3 | yes | 0.235 | 0.105 | 0.140 | 9 | 0.08 | loss | -1.34 | 0.3 | 14 |
| 21 | KXBTC15M-26OCT280245-45 | 10-27 23:44:02 PT | 57.7 | yes | 0.453 | 0.295 | 0.300 | 9 | 0.14 | loss | -2.84 | 1.4 | 3 |
| 22 | KXSOL15M-26OCT280245-45 | 10-27 23:44:12 PT | 47.5 | no | 0.340 | 0.225 | 0.220 | 9 | 0.11 | loss | -2.09 | 0.49 | 13 |
| 23 | KXSOL15M-26OCT280345-45 | 10-28 00:44:19 PT | 40.8 | no | 0.988 | 0.305 | 0.440 | 9 | 0.16 | loss | -4.12 | 0.3 | 20 |
| 24 | KXSOL15M-26OCT280415-15 | 10-28 01:14:06 PT | 53.0 | no | 0.955 | 0.875 | 0.880 | 9 | 0.07 | win | +1.01 | 0.32 | 7 |
| 25 | KXBTC15M-26OCT280545-45 | 10-28 02:44:05 PT | 54.9 | no | 0.304 | 0.145 | 0.130 | 9 | 0.08 | loss | -1.25 | 0.65 | 6 |
| 26 | KXSOL15M-26OCT280615-15 | 10-28 03:14:18 PT | 42.0 | yes | 0.318 | 0.175 | 0.200 | 9 | 0.11 | win | +7.09 | 0.6 | 18 |
| 27 | KXBTC15M-26OCT280630-30 | 10-28 03:29:06 PT | 53.5 | no | 0.254 | 0.076 | 0.070 | 9 | 0.05 | loss | -0.68 | 0.45 | 7 |
| 28 | KXETH15M-26OCT280630-30 | 10-28 03:29:10 PT | 49.0 | no | 0.542 | 0.230 | 0.260 | 9 | 0.13 | loss | -2.47 | 0.3 | 11 |
| 29 | KXETH15M-26OCT280645-45 | 10-28 03:44:03 PT | 56.1 | yes | 0.138 | 0.049 | 0.059 | 5 | 0.02 | loss | -0.32 | 0.45 | 4 |
| 30 | KXBTC15M-26OCT280745-45 | 10-28 04:44:05 PT | 54.1 | no | 0.236 | 0.099 | 0.109 | 9 | 0.07 | win | +7.95 | 0.45 | 7 |
| 31 | KXSOL15M-26OCT280745-45 | 10-28 04:44:20 PT | 39.2 | no | 0.303 | 0.125 | 0.097 | 9 | 0.06 | loss | -0.93 | 0.31 | 21 |
| 32 | KXBTC15M-26OCT281000-00 | 10-28 06:59:09 PT | 50.1 | yes | 0.217 | 0.067 | 0.043 | 9 | 0.03 | loss | -0.42 | 0.97 | 10 |
| 33 | KXETH15M-26OCT281100-00 | 10-28 07:59:06 PT | 53.3 | no | 0.408 | 0.345 | 0.280 | 9 | 0.13 | loss | -2.65 | 0.75 | 7 |
| 34 | KXSOL15M-26OCT281100-00 | 10-28 07:59:14 PT | 45.9 | no | 0.615 | 0.130 | 0.140 | 9 | 0.08 | loss | -1.34 | 2.45 | 15 |
| 35 | KXBTC15M-26OCT281100-00 | 10-28 07:59:17 PT | 42.5 | no | 0.403 | 0.305 | 0.096 | 9 | 0.06 | loss | -0.92 | 2.86 | 18 |
| 36 | KXETH15M-26OCT281200-00 | 10-28 08:59:03 PT | 57.0 | yes | 0.414 | 0.270 | 0.300 | 7 | 0.11 | loss | -2.21 | 0.45 | 3 |
| 37* | KXSOL15M-26OCT281215-15 | 10-28 09:14:02 PT | 57.5 | no | 0.238 | 0.055 | 0.076 | 7 | 0.04 | loss | -0.57 | 1.1 | 3 |
| 38* | KXETH15M-26OCT281215-15 | 10-28 09:14:05 PT | 54.5 | no | 0.378 | 0.280 | 0.240 | 9 | 0.12 | win | +6.72 | 2.05 | 6 |
| 39* | KXETH15M-26OCT281230-30 | 10-28 09:29:07 PT | 52.4 | yes | 0.108 | 0.031 | 0.040 | 9 | 0.03 | loss | -0.39 | 0.3 | 8 |
| 40* | KXSOL15M-26OCT281230-30 | 10-28 09:29:11 PT | 48.4 | yes | 0.839 | 0.695 | 0.660 | 9 | 0.15 | win | +2.91 | 0.53 | 12 |


**Conditional calibration (fixed cohort; n and closes given; nothing with n < 5 is interpretable):**

| entry price | n | closes | mean p_side model | mean p_side mkt | hit rate | net $ |
|---|---|---|---|---|---|---|
| <0.20 | 16 | 14 | 0.263 | 0.116 | 0.125 | +3.84 |
| 0.20-0.50 | 15 | 12 | 0.504 | 0.316 | 0.267 | -7.97 |
| 0.50-0.80 | 1 | 1 | 0.803 | 0.620 | 1.000 | +3.00 |
| >=0.80 | 4 | 4 | 0.983 | 0.902 | 1.000 | +2.89 |

| time left | n | closes | mean p_side model | mean p_side mkt | hit rate | net $ |
|---|---|---|---|---|---|---|
| T-60..T-55 s (first eligible polls) | 11 | 9 | 0.385 | 0.236 | 0.273 | +3.80 |
| T-55..T-45 s | 18 | 14 | 0.472 | 0.324 | 0.333 | +0.59 |
| T-45..T-30 s | 6 | 5 | 0.464 | 0.240 | 0.167 | -2.96 |
| T-30..T-6 s | 1 | 1 | 0.998 | 0.943 | 1.000 | +0.33 |

| asset | n | closes | mean p_side model | mean p_side mkt | hit rate | net $ |
|---|---|---|---|---|---|---|
| BTC | 15 | 15 | 0.429 | 0.305 | 0.267 | -1.50 |
| ETH | 9 | 9 | 0.446 | 0.303 | 0.222 | -7.32 |
| SOL | 12 | 12 | 0.504 | 0.293 | 0.417 | +10.58 |

| side | n | closes | mean p_side model | mean p_side mkt | hit rate | net $ |
|---|---|---|---|---|---|---|
| yes | 11 | 11 | 0.380 | 0.244 | 0.273 | +1.34 |
| no | 25 | 17 | 0.493 | 0.326 | 0.320 | +0.42 |


**ETH tie probability inside the forecast.** Settlement books `avg ≥ K` (2 dp for ETH) with a tie counting YES (verified on `KXETH15M-26OCT271945-45`). `p_model`/`p_cal` are continuous (normal / Student-t) probabilities of `avg > K`: the tie mass is not represented, so p_side(YES) is understated and p_side(NO) overstated by P(|avg − K| < 0.005). Over the 11 ETH fills the mean tie mass is **0.0052** (range 0.0019–0.0106); 1 of 75 cached ETH markets settled on a tie. Effect per fill below; it is a diagnostic, not a change:

| ticker | decision PT | side | K | proj at decision | remaining sd $ | tie mass | p_side model | effect on p_side of tie=YES | outcome | realised $ | settled on tie |
|---|---|---|---|---|---|---|---|---|---|---|---|
| KXETH15M-26OCT271930-30 | 10-27 16:29:22 PT | no | 2347.92 | 2348.052 | 0.351 | 0.0106 | 0.2502 | -0.0106 | loss | -0.87 | False |
| KXETH15M-26OCT271945-45 | 10-27 16:44:02 PT | no | 2348.19 | 2348.321 | 0.72 | 0.0055 | 0.3722 | -0.0055 | loss | -2.28 | True |
| KXETH15M-26OCT272245-45 | 10-27 19:44:12 PT | no | 2353.52 | 2353.707 | 0.661 | 0.0058 | 0.3053 | -0.0058 | loss | -1.91 | False |
| KXETH15M-26OCT272315-15 | 10-27 20:14:33 PT | no | 2355.02 | 2354.546 | 0.223 | 0.0019 | 0.9981 | -0.0019 | win | +0.33 | False |
| KXETH15M-26OCT280030-30 | 10-27 21:29:04 PT | no | 2352.05 | 2351.985 | 0.537 | 0.0074 | 0.5858 | -0.0074 | win | +5.06 | False |
| KXETH15M-26OCT280630-30 | 10-28 03:29:10 PT | no | 2366.58 | 2366.543 | 0.641 | 0.0062 | 0.5420 | -0.0062 | loss | -2.47 | False |
| KXETH15M-26OCT280645-45 | 10-28 03:44:03 PT | yes | 2366.86 | 2366.416 | 0.719 | 0.0046 | 0.1383 | +0.0046 | loss | -0.32 | False |
| KXETH15M-26OCT281100-00 | 10-28 07:59:06 PT | no | 2328.27 | 2328.405 | 1.054 | 0.0038 | 0.4084 | -0.0038 | loss | -2.65 | False |
| KXETH15M-26OCT281200-00 | 10-28 08:59:03 PT | yes | 2331.56 | 2331.421 | 1.166 | 0.0034 | 0.4144 | +0.0034 | loss | -2.21 | None |
| KXETH15M-26OCT281215-15 | 10-28 09:14:05 PT | no | 2331.46 | 2331.602 | 0.828 | 0.0047 | 0.3775 | -0.0047 | win | +6.72 | None |
| KXETH15M-26OCT281230-30 | 10-28 09:29:07 PT | yes | 2331.33 | 2330.743 | 0.816 | 0.0038 | 0.1080 | +0.0038 | loss | -0.39 | None |

## 4. Statistical specification (`src/predhft/stats_spec.py`, `tests/test_stats_spec.py`)

- **Estimand** θ = Σ net P&L / Σ filled contracts over post-freeze P12 fills ($/contract; ratio estimator, linearised cluster variance). **H0: θ ≤ 0**, one-sided α = 0.025. Rejection establishes θ > 0 only — never θ ≥ 2 c. **2 c is the power-planning alternative and nothing else.**
- **Measured (current cohort, n 40 fills / 345 contracts):** θ = 3.02 c; SE iid-fills 6.05 c, cluster-by-close 6.53 c (G 25), cluster-by-hour 5.31 c (G 16); **DEFF(close) = 1.164** (ICC 0.034), DEFF(hour) 0.77; effective fills 34.4; z = 0.46; sd per-contract P&L 37.8 c.
- **Power at δ = 2 c, sd 37.8 c, 80 %:** 2,800 independent fills; ×1.003 for a 2-look OBF design → 2,810 effective; **÷ DEFF → 3,271 fills ≈ 2,045 closes**. The addendum's ~4,500 = ((2.80 + 0.84)·37/2)² applied the *interim* z 2.80 as if final and treated fills as independent — a one-look precision target, not the power of the sequential design.
- **Lan-DeMets O'Brien-Fleming boundaries from effective information:** against I_max = information for 80 % power at 2 c, 100 and 300 fills are information fractions **0.031 and 0.092**; boundaries z = **8.0 and 7.31** (α spent ≈ 0 and 1.3e-13), final z = 1.96. If instead 100/300 are *declared* the only looks (fractions 1/3, 1): z = 3.71 and 1.96 (the addendum's 2.80/1.98 were the equal-spaced constants for fractions 1/2, 1); power at 2 c if 300 is final = **13.4 %**. 300 fills give SE ≈ 2.4 c: they resolve |θ| ≳ 5 c, not 2 c.
- **Milestone status:** 100 / 300 fills are **descriptive review milestones with no automated pass** (owner directive 10-28) until this spec is validated. Current: 40/100, information fraction 1.2 % of the 3,271 design.

## 5. Authoritative scoped report (`C:/Users/Alon/AppData/Local/hermes/scripts/predhft_daily.py`, cron 09:05 PT, stdlib only)

Rewritten from scratch (legacy kept as `predhft_daily_legacy_2026-10-28.py.bak`). One as-of instant; four scopes never pooled: **(a)** frozen P12 cohort (θ, cluster SEs, DEFF, per asset, milestones as descriptive, both sensitivities labelled, occupancy time-average vs peak, opportunity/fill rates with denominators), **(b)** scored-only research (fills since suspension must be 0; snapshot counts; P21 hits; P22 v2 table with full column names/units, "insufficient data (n=k)", SE suppressed for one-close cells), **(c)** historical closed books per family with PREREG id and status, **(d)** global paper accounting from `data/risk/events.jsonl` with the two S1 ledger-only settlements counted once and a residual check.

**Reconciliations built in.** Legacy "n=20 / last 24h: none" = settled rows in the five hard-coded hourly roots (never read `s7p15-*`); authoritative: 25 hourly fills (−$1.63), 102 trades.jsonl fills (−$4.83) + S1 −$13.28 = −$18.11 over 104 settlements, residual $0.00. Legacy "S1 open/resting=128" = 48 resting + 80 filled simulated **orders** (not contracts, not $) on markets that have all closed (last close 10-28 01:00 PT; runner stopped 10-27 02:11 PT) = stale state, 0 exposure; global open counts status=open trades ∪ unreleased risk-ledger opens by ticker (1 now). Heartbeat alerts only for the 13 expected-active processes (P12 ×3, P13 recorders ×4, CFB, replica, P21, weather, gas, sports) with per-process freshness limits and task state; retired tasks (S1, S7-Final, S7-Paper ×3, S7-Index ×2, Rain-J1) are listed, never alerted.

**Denominators.** window = 15-min close with ≥ 1 book snapshot at T ≤ 60 s; opportunity rate = closes with ≥ 1 ledger decision row (fill/skip/stale) / windows; fill rate = fills / windows; occupancy = time-average open cost (fill → close + 120 s) / $250, peak = max simultaneous open cost. P12 since freeze: 74 windows per asset, opportunity 23.0/14.9/18.9 %, fill rate 20.3/14.9/18.9 % (BTC/ETH/SOL); time-average open cost $0.26 (0.10 % of bankroll), peak $11.16 (4.46 %).

**Run output (`python predhft_daily.py`, 2 s):**

```
PredHFT paper report — as of 2026-10-28 10:32:07 PT (every number below uses this instant; paper only, 0 orders, 0 capital)
Boundaries: P12 freeze 2026-10-27 16:05:22 PT (a395cf8) | P13/P16 15M scored-only 2026-10-28 09:05:51 PT | P13-BTCD scored-only 2026-10-28 09:55:23 PT | P22-R1 freeze 2026-10-28 10:28:00 PT

(a) FROZEN P12 COHORT — s7p15-KX{BTC,ETH,SOL}15M, settled fills with fill time >= P12 freeze. Estimand theta = total net $ / total contracts (c/contract), H0: theta <= 0.
  fills n=41 | contracts=354 | net +6.49 $ | theta=+1.8 c/contract | SE(cluster by 15-min close, G=26)=6.5 c | SE(cluster by hour block, G=17)=5.3 c | DEFF(close)=1.16
  z vs H0 (close clusters)=+0.28; hit rate=0.317; mean p_side(model)=0.454 vs mean paid price=0.294
    BTC: n=15 contracts=130 net -1.49 $ theta=-1.1 c ± 8.9 (G=15)
    ETH: n=12 contracts=102 net -4.93 $ theta=-4.8 c ± 10.9 (G=12)
    SOL: n=14 contracts=122 net +12.91 $ theta=+10.6 c ± 11.8 (G=14)
  last 24 h: fills n=41 net +6.49 $ | open paper positions now (status=open in s7p15 trades.jsonl)=0
  milestones (DESCRIPTIVE review points, no automated pass): 41/100 and 41/300 fills; information fraction vs 80 %-power design at 2 c (3,271 fills at DEFF 1.16): 1.3%
  SENSITIVITY (not applied, P12 frozen): fills with decision latency > 1.5 s n=3 net +4.46 $ -> cohort net without them +2.04 $; fee rounding: booked 3.76 $ vs exact 3.51 $ (ceil-to-cent stays)
  capital occupancy: time-average open cost 0.269 $ = 0.108% of 250 $ bankroll; PEAK simultaneous open cost 11.16 $ = 4.46%
    BTC: windows (closes with >=1 snapshot at T<=60 s)=74 | candidate closes (>=1 ledger decision row: fill/skip/stale)=17 -> opportunity rate=23.0% | fills=15 -> fill rate=20.3%
    ETH: windows (closes with >=1 snapshot at T<=60 s)=74 | candidate closes (>=1 ledger decision row: fill/skip/stale)=11 -> opportunity rate=14.9% | fills=12 -> fill rate=16.2%
    SOL: windows (closes with >=1 snapshot at T<=60 s)=74 | candidate closes (>=1 ledger decision row: fill/skip/stale)=14 -> opportunity rate=18.9% | fills=14 -> fill rate=18.9%
    (ledger decisions_fills.jsonl: 356 rows, built 2026-10-28 09:18:06 PT; candidate counts are stale after that time)

(b) SCORED-ONLY RESEARCH — no orders expected; a fill here is a defect.
  P13/P16 15M s7p13-KXBTC15M: fills since suspension=0 | snapshots last 24 h=4484 | last snapshot 2026-10-28 10:29:50 PT
  P13/P16 15M s7p13-KXETH15M: fills since suspension=0 | snapshots last 24 h=4433 | last snapshot 2026-10-28 10:29:50 PT
  P13/P16 15M s7p13-KXSOL15M: fills since suspension=0 | snapshots last 24 h=4462 | last snapshot 2026-10-28 10:29:48 PT
  P13-BTCD hourly s7p13-KXBTCD: fills since suspension=0 | snapshots last 24 h=1193 | last snapshot 2026-10-28 09:59:47 PT
  P21 struct scanner: hits total=6, last 24 h=6 (H1 needs >= 1 executable hit/day, median net >= 0.50 $, persisting >= 60 s)
  P22 v2 (measurement p22-v2, file p22_v2_cells_2026-10-28.json, 960 market x horizon records, DISCOVERY sample: all closes < R1 freeze; confirmation cohort n=0 so far)
  Rules: ALL_offered = every record whose intended side is offered with depth >= lot (status ok); R1 = p_side >= 0.6 and intended-side depth >= lot; R2 = p_side >= 0.9 and intended-side depth >= lot; R3 = R1 and raw offer <= 0.95 (inside the P13 fav cap)
  Columns: horizon T-h (s) | markets with book | two-sided books | intended side offered at any price | raw offer > 0.98 = outside modeled range (excluded from numerator AND denominator) | per rule: n in cell / mean model-implied edge (c/contract) / realised net (c/contract) ± SE clustered by close (suppressed when < 2 closes)
    T-60: book=192 two-sided=111 offered=96 outside-range=49 | ALL_offered: n=47 edge -13.8 / real +1.2 ± 4.8 | R1: n=31 edge -15.1 / real +0.9 ± 4.5 | R2: n=4 edge -5.9 / real +2.9 ± 0.6 | R3: n=19 edge -16.7 / real +0.4 ± 7.4
    T-45: book=192 two-sided=84 offered=74 outside-range=39 | ALL_offered: n=35 edge -13.7 / real -4.4 ± 5.5 | R1: n=29 edge -14.5 / real -8.6 ± 6.3 | R2: n=3 edge -1.7 / real +4.9 ± 1.9 | R3: n=17 edge -14.6 / real -10.2 ± 8.7
    T-30: book=192 two-sided=57 offered=53 outside-range=37 | ALL_offered: n=16 edge -18.5 / real -24.4 ± 8.6 | R1: n=12 edge -18.1 / real -24.9 ± 10.9 | R2: insufficient data (n=0; 33 excluded outside range) | R3: n=6 edge -19.1 / real -51.6 ± 19.1 <- PRIMARY ENDPOINT CELL (R1, T-30)
    T-20: book=192 two-sided=37 offered=34 outside-range=26 | ALL_offered: n=8 edge -19.0 / real -30.0 ± 16.3 | R1: n=7 edge -19.0 / real -37.7 ± 16.5 | R2: insufficient data (n=0; 25 excluded outside range) | R3: n=4 edge -15.0 / real -67.5 ± 19.6
    T-10: book=192 two-sided=14 offered=12 outside-range=9 | ALL_offered: n=3 edge +4.6 / real -38.4 ± 37.7 | R1: n=3 edge +4.6 / real -38.4 ± 37.7 | R2: insufficient data (n=0; 8 excluded outside range) | R3: n=3 edge +4.6 / real -38.4 ± 37.7

(c) HISTORICAL (closed books; never pooled with (a)) — family: prereg | rule | status | fills / contracts / net $
  s7live: P1/P6 | BTC hourly model-rule taker thr .05, windows T-8..T-2 (varied) | last start 10-26 19:42 PT, no fills after 10-26 20:56 PT | 3 / 23 / +4.49 $
  s7live-KXETHD: P1 | ETH hourly model-rule | last start 10-26 19:42 PT, no fills after 10-26 20:56 PT | 1 / 9 / +4.79 $
  s7live-KXSOLD: P1 | SOL hourly model-rule | last start 10-26 19:42 PT, no fills after 10-26 20:56 PT | 2 / 18 / +4.45 $
  s7final-KXBTCD: P2 | BTC final-minute T-1.0..T-0.15 thr .05 partial-TWAP | last start 10-27 08:05 PT, no fills after 10-27 06:59 PT | 7 / 69 / +4.10 $
  s7p10-KXBTCD: P10/P11 | BTC T-3.5..T-2.5 Student-t dyn-thr EWMA sigma | scored-only since 10-27 16:05 PT | 7 / 52 / -13.24 $
  s7p10-KXETHD: P10 | ETH same (lot 10 amendment) | scored-only since 10-27 16:05 PT | 3 / 22 / -4.78 $
  s7p10-KXSOLD: P10 | SOL same | scored-only since 10-27 16:05 PT | 2 / 15 / -1.44 $
  s7p13-KXBTCD: P13/P16 | BTC hourly final-minute projection-side T-0.92..T-0.15 proj_gate cap .95 | scored-only since 10-28 09:55:23 PT (0 fills ever) | 0 / 0 / +0.00 $
  s7p13-KX*15M (P13/P16/P18/P20 15M taker, pre-suspension): 7 / 62 / -6.20 $
  s7p15-KX*15M pre-freeze P12 (development era, excluded from (a)): 30 / 209 / -7.44 $
  all hourly families: 25 fills net -1.63 $; the legacy cron's 'n=20 / last 24h: none' summed only s7live, s7live-KXETHD, s7live-KXSOLD, s7final-KXBTCD, s7p10-KXBTCD = 20 fills and never read s7p15-*

(d) GLOBAL PAPER ACCOUNTING — source data/risk/events.jsonl (settle events; UTC day boundary = 17:00 PT)
  realised all-time -22.05 $ over 105 settlements = trades.jsonl settled -8.77 $ (103 fills, 13 families) + s1-maker ledger-only -13.28 $ (2 settlements, counted ONCE here, in no strategy scope) | residual +0.00 $
  today (UTC day since 2026-10-27 17:00:00 PT) +8.07 $ vs halt -25 $ | last 24 h -21.55 $ over 54 settlements (54 trades.jsonl fills, 41 of them P12) | weekly halt -50 $
  halt_shadow events: all-time 21, last 24 h 21 (paper: logged, not blocking) | Kalshi 429s last 24 h 0
  open exposure now: trades.jsonl status=open 0 | risk-ledger open events not yet released (close+120 s in future, excl. s1-maker) 0 -> global open positions (union by ticker) = 0, open cost 0.00 $
  S1 maker orders.jsonl: {'cancelled': 3, 'filled': 80, 'resting': 48, 'settled': 2} -> legacy 'open/resting=128' = resting+filled simulated ORDERS (not contracts, not $) on markets all closed (last close 2026-10-28 01:00:00 PT; runner stopped 10-27 02:11 PT) = STALE STATE, 0 exposure; only the 2 'settled' rows were ever booked (-13.28 $ above)

HEARTBEAT (expected-active only): all OK
  not monitored (retired/stopped families; task state shown for information only): PredHFT-S1-Maker=Disabled, PredHFT-S7-Final-BTC=Disabled, PredHFT-S7-Paper=Running, PredHFT-S7-Paper-ETH=Running, PredHFT-S7-Paper-SOL=Running, PredHFT-S7-Index-INX=Running, PredHFT-S7-Index-NDX=Running, PredHFT-Rain-J1=Running

DEFINITIONS: fill = trades.jsonl row (paper execution at re-checked touch + 0.01, qty = min(Kelly, lot cap, displayed), Kalshi taker fee ceil-to-cent per order). settled = fill whose market settled. theta = sum(pnl)/sum(qty), SE from cluster-linearised ratio estimator; DEFF = cluster variance / iid-fill variance. window = 15-min close with >= 1 book snapshot at T <= 60 s. opportunity rate = candidate closes / windows; fill rate = fills / windows. occupancy = time-average open cost (fill -> close + 120 s) / 250 $; peak = max simultaneous open cost. 'insufficient data (n=k)' replaces NaN; no SE for one-cluster samples. Scopes (a)-(d) are never summed except in (d).
```

## 6. Hourly rule mapping (from runner.log start lines + trades.jsonl)

| family dir | PREREG id | rule | runner flags seen (runner.log) | starts | first start (UTC) | last start (UTC) | last start scored-only | fills | wins | contracts | net $ | first fill (PT) | last fill (PT) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| data/s7live | P1/P6 | P1 (PREREG) S7 crypto taker, model rule thr .05, windows T-8/T-5/T-3.5..T-2 (changed across restarts); P6 sigma gate later | thr=0.05 window=T-3.5..T-2.5min lot=10; thr=0.05 window=T-5..T-2min lot=20; thr=0.05 window=T-5.0..T-2.0min lot=20; thr=0.05 window=T-8..T-2min lot=20 | 8 | 2026-10-26T19:42:22 | 2026-10-27T02:42:25 | False | 3 | 1 | 23 | +4.49 | 2026-10-26 17:55:06 PT | 2026-10-26 20:56:37 PT |
| data/s7live-KXETHD | P1 | P1 ETH | thr=0.05 window=T-3.5..T-2.5min lot=10; thr=0.05 window=T-5..T-2min lot=10; thr=0.05 window=T-5.0..T-2.0min lot=10 | 6 | 2026-10-26T20:48:59 | 2026-10-27T02:42:25 | False | 1 | 1 | 9 | +4.79 | 2026-10-26 18:55:39 PT | 2026-10-26 18:55:39 PT |
| data/s7live-KXSOLD | P1 | P1 SOL | thr=0.05 window=T-3.5..T-2.5min lot=10; thr=0.05 window=T-5..T-2min lot=10; thr=0.05 window=T-5.0..T-2.0min lot=10 | 6 | 2026-10-26T20:48:59 | 2026-10-27T02:42:25 | False | 2 | 1 | 18 | +4.45 | 2026-10-26 17:56:20 PT | 2026-10-26 18:57:04 PT |
| data/s7final-KXBTCD | P2 | P2 final-minute T-1.0..T-0.15, thr .05, partial-TWAP | thr=0.05 window=T-1.0..T-0.15min lot=10; thr=0.05 window=T-1.0..T-0.15min lot=10 sigma=trail120 dist=normal thr_mode=fixed scored_only=False; thr=0.05 window=T-1.0..T-0.15min lot=10 sigma=trail120 dist=normal thr_mode=fixed scored_only=False proj_gate=False | 6 | 2026-10-27T00:25:46 | 2026-10-27T15:05:28 | False | 7 | 2 | 69 | +4.10 | 2026-10-26 17:59:02 PT | 2026-10-27 06:59:03 PT |
| data/s7p10-KXBTCD | P10/P11 | P10 redesign T-3.5..T-2.5, Student-t nu 8 scale .8, dyn thr, EWMA sigma; P11 hour-of-day tilt registered on this tape | thr=0.05 window=T-3.5..T-2.5min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=False; thr=0.05 window=T-3.5..T-2.5min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=False proj_gate=False; thr=0.05 window=T-3.5..T-2.5min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=True proj_gate=False | 16 | 2026-10-27T04:10:48 | 2026-10-27T23:05:23 | True | 7 | 2 | 52 | -13.24 | 2026-10-26 21:56:31 PT | 2026-10-27 07:57:05 PT |
| data/s7p10-KXETHD | P10 | P10 ETH (amendment 22:20 PT 10-26: paper lot 10) | thr=0.05 window=T-3.5..T-2.5min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=False; thr=0.05 window=T-3.5..T-2.5min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=True; thr=0.05 window=T-3.5..T-2.5min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=True proj_gate=False | 17 | 2026-10-27T04:10:49 | 2026-10-27T23:05:24 | True | 3 | 0 | 22 | -4.78 | 2026-10-26 23:56:32 PT | 2026-10-27 03:56:33 PT |
| data/s7p10-KXSOLD | P10 | P10 SOL (same amendment) | thr=0.05 window=T-3.5..T-2.5min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=False; thr=0.05 window=T-3.5..T-2.5min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=True; thr=0.05 window=T-3.5..T-2.5min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=True proj_gate=False | 17 | 2026-10-27T04:10:49 | 2026-10-27T23:05:24 | True | 2 | 0 | 15 | -1.44 | 2026-10-26 22:56:40 PT | 2026-10-26 23:56:31 PT |
| data/s7p13-KXBTCD | P13/P16 | P13 hourly final-minute projection-side taker T-0.92..T-0.15, proj_gate, cap .95, lot 10; P16 edge >= 5 c after REBASE | thr=0.05 window=T-0.92..T-0.15min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=False proj_gate=True; thr=0.05 window=T-0.92..T-0.15min lot=10 sigma=ewma94 dist=t thr_mode=dyn scored_only=True proj_gate=True | 17 | 2026-10-27T13:30:09 | 2026-10-28T16:55:23 | True | 0 | 0 | 0 | +0.00 | — | — |


## 7. Files and tests (all uncommitted)

New: `src/predhft/p22_exec.py`, `src/predhft/p12_replay.py`, `src/predhft/stats_spec.py`, `scripts/p22_v2_recompute.py`, `scripts/addendum2_2026_10_28.py`, `tests/test_p22_exec.py`, `tests/test_p12_decision_neutrality.py`, `tests/test_stats_spec.py`, `data/p22/p22_paired_2026-10-28.v2.jsonl`, `data/p22/p22_v2_cells_2026-10-28.json`, `data/p22/addendum2_2026-10-28.json`, this document. Modified: `scripts/p22_paired.py` (emits v2, writes `p22_paired_v2_<date>.jsonl`, never rewrites v1 files), `tests/test_audit_2026_10_28.py`, `docs/PREREG.md` (§P22-R1 freeze), `C:/Users/Alon/AppData/Local/hermes/scripts/predhft_daily.py`. Full suite: **132 passed** (`uv run --with pytest python -m pytest -q tests`).
