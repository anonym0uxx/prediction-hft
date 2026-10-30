# Trade forensics — KXSOL15M paper tape, 2026-10-29

Generated 2026-10-29 19:57 PT from `scripts/forensics_2026_10_29.py` (35 s, CPU) and `scripts/conditional_ev_2026_10_29.py` (3 s).
Machine outputs: `reports/forensics/{trades,attribution,conditions_search,conditions_brief,hold_vs_exit,opus,tails}.json`, console in `reports/forensics/run.log` and `conditional_ev_run.log`; secondary 1-contract map in `docs/CONDITIONAL_EV_2026-10-29.md` + `data/sol-v1/conditional_ev_2026-10-29.json`.
Nothing here was committed, no live component was touched, all data reads were read-only. Times in prose are Pacific.

Market facts used: KXSOL15M settles YES iff the 60-s average official SOLUSD_RTI before close >= the 60-s average before open; Kalshi taker fee `0.07*C*P*(1-P)` ceil'd to the cent per order (`sol_v1.fee_pc`). Settlement for the counterfactual universe is the exchange `settle` record where logged, else `sol_v1.settle_from_feed` on `data/cfb/SOL.jsonl`; exchange-vs-feed disagreements: **0**.

## 0. Headline

1. **The tape is flat to slightly negative and nothing in it is a trading rule.** SOL-v1 logged 71 filled positions (597 contracts) for **-$10.79 = -1.81 c/contract ± 2.98 (close-clustered SE)**. The whole counterfactual universe (buy either side at the depth-weighted executable price + 1 tick, hold to settle, 63,312 snapshot-rows over 112 closes) averages **-2.3 c/contract** = the spread + fee, with every side-symmetric slice within ±1 SE of that.
2. **"Exits win, settles lose" is selection, not skill.** The exit rule is a take-profit: positions that reach it are winners-in-progress by construction (35 exits, +$64.51, 63% hit), the ones that don't are the losers (36 settles, **0/36 won**, -$75.30). Holding the 35 exited positions to settlement would have made +$84.41, i.e. exiting cost **-$19.90 (-6.77 c/ct ± 6.10, t = -1.1)**. "Always hold" turns the book from -$10.79 to **+$9.11**; the difference is not significant.
3. **Optimal-conditions search: 357 conditions examined (490 grid cells, 357 with data), zero Bonferroni survivors on the positive side, zero SPA-significant winners.** The only Holm survivor among IS-screened winners is `medge5-15c & NO` (OOS +20.1 c/ct ± 6.8, 39 closes) — it is side-literal in a sample where SOL fell 102.2 → 95.0 and YES settled only 38 % of closes, so it is the drift, not a condition. Side-symmetric SPA p = 0.24 (studentised) / 0.59 (non-studentised): consistent with no winner at all.
4. **The do-not-trade list is real and survives correction**: `px .25-.50 & spread 1-3 c` is Bonferroni-significant OOS over all 357 conditions (t = -3.67; -13.8 c/ct); the whole `px .25-.50` family (-10 to -13 c/ct in **both** halves, t ≤ -2.5 each) and `px .10-.25 & spread 1-3 c` (OOS hit 9.4 % vs 16-18 % implied) are SPA-significant losers (p = 0.0045 all conditions, 0.0015 side-symmetric). Buying any side at 25-50 c has been a -11 c/ct proposition. The taken trades agree: 23 fills at 10-25 c hit 4.3 % (-6.6 c/ct ± 4.5).
5. **Opus / 4-5 c tail bets are not supported.** SOL 1-min candles (54,894 bars, 38 days) have fatter-than-Gaussian 5-min tails (|z| > 2.58: 3.6 % vs 1 %; |z| > 3: 2.2 % vs 0.27 %) and the realised-vol sigma is ~17 % too small (rms z = 1.175), but the **market already prices this**: at executable longshot prices 2-5 c the realised hit is 2.2 % vs 3.2 % market-implied vs 4.5 % model; 5-10 c: 7.1 % vs 7.3 % vs 9.6 %; 10-20 c: 8.8 % vs 14.9 % vs 17.8 % (t = -2.5). Every longshot bucket is net negative. Opus's own counterfactual longshots (41 fills ≤ 10 c) hit 2.4 % for -1.9 c/ct; its two live 4-5 c fills both lost.

## 1. Trade-universe reconstruction

| Source | What | Count |
|---|---|---|
| `data/sol-v1/snapshots.jsonl` | evaluation snapshots (~2 s cadence) | 45,612 over 113 closes (112 with a result) |
| `data/sol-v1/fills.jsonl` + `decisions.jsonl` | SOL-v1 intents / filled positions / exits / settles | 84 intents → **71 filled** (35 exits, 36 settles), 13 unfilled |
| `data/llm-direct/fills.jsonl` | Opus direct intents | 7 → **3 settled fills** (50 ct), 3 unfilled, 1 open at 19:50 PT |
| `data/s7p15-KX{BTC,ETH,SOL}15M/` | P12 15M history | 73 fills (578 ct) |
| `data/s7p13-KX{BTC,ETH,SOL}15M/` | P13 15M history | 7 fills (62 ct) |
| **`trades.json`** | every logged intent with state features at decision time + realised outcome | **171 rows, 154 filled** |
| counterfactual universe | every SOL-v1 entry snapshot (no position held, `entry_min..max_secs_left`) × side with a displayed book, executable px in [0.02, 0.98] | **63,312 rows, 112 closes** |
| `data/candles/SOL_1m.parquet` | tail study | 54,894 bars, 38.1 days |

Execution model everywhere: `sol_v1.side_levels`/`walk` depth-weighted for lot 10 (partial allowed, never beyond displayed depth) + 1 tick, fee ceil-to-cent per order, **absent side ⇒ no fill** (never a 1.00 fallback). Exit values from the logged `hold_exit` polls (`exit_proceeds`, `exit_qty`) = `sol_v1.exit_value`.

Coverage caveat: entry snapshots are only logged while no position is held, so the universe inside the last 5 minutes is conditioned on the policy *not* holding (60 of 112 closes had a position). Closes covered per time bucket: T-10-14m 111, T-5-10m 112, T-2-5m 77, T-60-120s 34, T-10-60s 17.

Walk-forward split: first 56 closes (2026-10-28 14:00 → 10-29 05:45 PT) = in-sample / screening; last 56 closes (06:00 → 19:45 PT) = out-of-sample. YES settled 41.1 % IS, 35.7 % OOS, 38.4 % overall.

## 2. Attribution of realised P&L

### 2.1 All logged paper families (net c/contract, SE clustered by close)

| family | fills | contracts | P&L $ | net c/ct | SE | hit | mean px |
|---|---|---|---|---|---|---|---|
| sol-v1 | 71 | 597 | -10.79 | -1.81 | 2.98 | 0.31 | 0.28 |
| opus-direct | 3 | 50 | +6.29 | +12.58 | 17.83 | 0.33 | 0.18 |
| s7p15-KXSOL15M | 25 | 188 | +11.74 | +6.24 | 8.12 | 0.44 | 0.39 |
| s7p15-KXBTC15M | 24 | 194 | -1.28 | -0.66 | 6.38 | 0.42 | 0.42 |
| s7p15-KXETH15M | 24 | 196 | -14.30 | -7.30 | 7.98 | 0.33 | 0.36 |
| s7p13-KXSOL15M | 2 | 16 | +4.41 | +27.56 | 15.8 | 1.0 | 0.77 |
| s7p13-KXBTC15M | 3 | 26 | -3.71 | -14.27 | 25.1 | 0.67 | 0.76 |
| s7p13-KXETH15M | 2 | 20 | -6.90 | -34.5 | 44.8 | 0.5 | 0.85 |

No family is more than ~1 SE from zero. Total across all families: -$14.54 on 1,287 contracts.

### 2.2 SOL-v1 fills by state at decision time (71 positions, realised incl. exits)

| cut | bucket | fills | ct | P&L $ | net c/ct | SE | hit |
|---|---|---|---|---|---|---|---|
| time-left | T-5-10m | 41 | 341 | -6.35 | -1.86 | 3.63 | 0.39 |
| | T-2-5m | 27 | 231 | -8.35 | -3.61 | 5.48 | 0.22 |
| | T-60-120s | 3 | 25 | +3.91 | +15.6 | 7.7 | 0.0 (all exits) |
| price | px < .10 | 4 | 33 | -2.63 | -7.96 | 1.41 | 0.0 |
| | px .10-.25 | 23 | 195 | -12.92 | **-6.63** | 4.53 | **0.043** |
| | px .25-.50 | 43 | 361 | +3.92 | +1.09 | 4.53 | 0.47 |
| | px .50-.75 | 1 | 8 | +0.84 | — | — | 1.0 |
| side | yes | 34 | 288 | -3.05 | -1.06 | 4.37 | 0.24 |
| | no | 37 | 309 | -7.74 | -2.50 | 4.51 | 0.38 |
| \|z\| | < .5 | 53 | 446 | +7.87 | +1.76 | 3.81 | 0.40 |
| | .5-1 | 16 | 134 | -17.69 | **-13.2** | 2.93 | **0.06** |
| | 1-2 | 2 | 17 | -0.97 | -5.7 | 0.9 | 0.0 |
| end | exit | 35 | 294 | +64.51 | +21.94 | 2.40 | 0.63 |
| | settle | 36 | 303 | -75.30 | -24.85 | 1.69 | **0.00** |

"hit" for exits = exit proceeds > basis. The |z| .5-1 and px .10-.25 cells are the same trades viewed twice: the policy buying the 10-25 c side when the index is half a sigma away, which won 1 of 23.

### 2.3 Exits win / settles lose — verdict: **selection**

* 35 exits made +$64.51. Had they been held to settlement they would have made **+$84.41** (22 of the 35 would have settled as winners). Exit minus hold = **-$19.90 = -6.77 c/ct ± 6.10 (n = 35, t = -1.1)**. Only 13 of 35 exits beat holding.
* 36 settles lost **36 of 36** (mean entry 23.5 c). All 36 had at least one poll on their path where the displayed bid would have returned more than settlement (perfect-foresight "rescue" value $96.26) — but a path-max is not a policy; see §5.
* Mechanism: the exit rule fires when the position is in profit (mean p_side at exit = 0.52 vs 0.28 at entry). Conditioning on having been exited *is* conditioning on having moved into the money; conditioning on having settled *is* conditioning on never having done so. The split therefore carries no information about whether exiting adds value; the only valid comparison is exit vs hold on the *same* positions, and that is negative.

## 3. Conditional expectancy maps (counterfactual universe, buy & hold to settle)

Unit: net c/contract at the executable price + tick, fee included, averaged over every snapshot-row in the state; SE clustered by close. "full" = all 112 closes (**in-sample** for anything that was used to pick a condition), "OOS" = last 56 closes. "hit" vs "implied" = realised win rate vs mean executable price. Rows marked ⚑ are side-literal and reflect the downtrend in SOL over the sample, not a condition.

### 3.1 Marginals

| state | n rows | closes | net c (full) | SE | hit / implied | net c (OOS) | SE | closes OOS |
|---|---|---|---|---|---|---|---|---|
| side = no ⚑ | 31,504 | 112 | +7.53 | 3.58 | .653 / .566 | +8.43 | 4.99 | 56 |
| side = yes ⚑ | 31,808 | 112 | -11.35 | 3.53 | .334 / .436 | -12.13 | 4.91 | 56 |
| with_model (p_side > mid) | 31,813 | 112 | -2.17 | 1.82 | .334 / .343 | -1.14 | 2.75 | 56 |
| against_model | 31,499 | 112 | -1.74 | 1.85 | .654 / .659 | -2.68 | 2.77 | 56 |
| favourite (px ≥ .5) | 32,064 | 112 | -0.08 | 2.24 | .751 / .739 | +0.12 | 3.14 | 56 |
| longshot (px < .5) | 31,248 | 112 | -3.88 | 2.28 | .228 / .255 | -3.98 | 3.19 | 56 |
| px < .10 | 6,631 | 88 | -1.04 | 2.48 | .047 / .054 | -1.04 | 3.24 | 48 |
| px .10-.25 | 8,223 | 105 | **-6.70** | 2.92 | **.119 / .176** | -6.31 | 3.74 | 53 |
| px .25-.50 | 16,394 | 111 | -3.62 | 2.88 | .357 / .376 | -4.11 | 4.24 | 55 |
| px .50-.75 | 16,699 | 109 | -1.58 | 2.63 | .618 / .617 | -0.92 | 3.86 | 55 |
| px .75-.90 | 8,740 | 107 | +3.02 | 3.00 | .861 / .820 | +2.69 | 3.93 | 54 |
| px ≥ .90 | 6,625 | 88 | -0.36 | 2.79 | .942 / .942 | -0.53 | 3.65 | 48 |
| model edge ≤ 0 | 35,792 | 112 | -1.80 | 1.57 | .632 / .638 | -2.52 | 2.32 | 56 |
| model edge 0-5 c | 22,687 | 112 | -2.14 | 2.05 | .336 / .345 | -0.51 | 3.25 | 56 |
| model edge 5-15 c | 4,794 | 102 | -2.29 | 3.84 | .202 / .214 | -5.12 | 4.36 | 49 |
| model edge > 15 c | 39 | 18 | +3.7 | 10.8 | .31 / .26 | +5.9 | 18.0 | 9 |
| \|z\| ≥ 2 & with_model | 314 | 38 | +2.37 | 1.04 | .713 / .687 | +3.01 | 1.51 | 21 |
| \|z\| ≥ 2 & against_model | 499 | 38 | -2.79 | 0.26 | .008 / .034 | -2.62 | 0.34 | 23 |
| mid .10-.30 & with_model | 7,151 | 81 | -6.37 | 3.12 | .274 / .326 | -6.65 | 3.79 | 42 |
| mid < .10 & with_model | 4,421 | 61 | -2.65 | 0.74 | .249 / .271 | -2.85 | 0.77 | 34 |
| comove_disagree & with_model | 3,729 | 81 | -6.31 | 3.02 | .361 / .409 | +0.99 | 4.90 | 42 |

Key readings: the model's "edge" has no monotone relationship with realised net (edge 5-15 c is *worse* than edge ≤ 0). Model probability is systematically above both market and realised on the longshot side (model 0.205 vs implied 0.176 vs realised 0.119 at 10-25 c). `|z| ≥ 2 & with_model` (+2.4 c, t ≈ 2.3 both halves) is the one side-symmetric positive cell; 38 closes, 314 rows, mean price 0.69 — i.e. buying the 70 c favourite when the index is already 2σ through the strike with minutes left; it survives neither Bonferroni nor SPA and is not proposed.

### 3.2 Time-left × price, both sides, OOS (last 56 closes) — net c/ct (closes)

| T-left \ px | < .10 | .10-.25 | .25-.50 | .50-.75 | .75-.90 | ≥ .90 |
|---|---|---|---|---|---|---|
| **YES** 10-14m | -8.2 (10) | -13.8 (25) | -15.4 (46) | -10.5 (43) | +6.2 (16) | +5.8 (4) |
| YES 5-10m | -6.1 (19) | -10.3 (27) | -10.4 (30) | -16.2 (34) | +1.4 (20) | -24.7 (8) |
| YES 2-5m | -4.4 (22) | -13.4 (15) | -25.3 (15) | -21.2 (15) | -29.2 (14) | -7.5 (11) |
| YES 60-120s | -5.5 (10) | -2.6 (9) | -28.0 (8) | -54.7 (6) | -82 (2) | -91 (1) |
| **NO** 10-14m | -6.4 (4) | -13.1 (14) | +6.3 (42) | +9.7 (46) | +9.6 (25) | +7.3 (12) |
| NO 5-10m | +19.6 (9) | -1.5 (19) | +9.4 (34) | +6.6 (30) | +6.1 (27) | +5.8 (19) |
| NO 2-5m | +4.1 (12) | +21.4 (14) | +19.1 (15) | +22.0 (15) | +12.6 (16) | +3.7 (22) |
| NO 60-120s | +14.5 (2) | +84 (2) | +56.8 (5) | +24.0 (8) | +1.4 (9) | +3.4 (12) |

The YES/NO mirror is the drift (SOL fell through most of the OOS window). The side-symmetric statement is the one that holds: the **.10-.25 column is negative for both sides in every time bucket with ≥ 10 closes** (YES -13.8/-10.3/-13.4, NO -13.1/-1.5; the NO 2-5m +21 is 14 closes, SE 16). Full-sample versions are in `attribution.json → maps_2d.full_tb_x_pxb_{yes,no}`; time × model-edge maps in `maps_2d.{full,oos}_tb_x_edgeb` (edge 5-15 c is negative in every time bucket OOS: -12.2, -6.4, -6.8 c at 10-14m / 5-10m / 2-5m).

### 3.3 Secondary 1-contract map (`conditional_ev_2026_10_29.py`)

Independent construction: one contract per close × side × 30-s bucket (5,037 rows, 112 closes) via `sol_v1.paper_fill`. All rows: **-2.30 c ± 0.06 (t = -37)**. 42 counterfactual conditions + 10 taken-trade cuts screened IS; **0 screened, 0 survivors, 0 Bonferroni** (z = 3.04). Taken-trade cuts reproduce §2.2 (exit +22.1 c ± 2.4, settle -24.8 c ± 1.7, px < .10 -8.1 c, side/time cuts all |t| < 0.6).

## 4. "Optimal conditions" search with correction

* Features (14): time bucket, price bucket, side, |z|, model edge, spread, depth, realised vol, partial-window count, hour PT, BTC/ETH co-move, mid bucket, with/against model, favourite/longshot. Conditions = singles + 19 feature pairs + 3 triples = **490 grid cells, 357 with any data = raw count examined**. A condition is a *policy* (at most one side per snapshot; snapshots where both sides qualify are dropped so the mechanical -2 c of buying both sides is never counted).
* Per-close matrix (112 × 357) of mean net c/ct; IS screen = first 56 closes (mean > 0, t ≥ 1, ≥ 10 closes → 31 positive candidates; mirrored → 99 negative); OOS test on last 56 with (a) Holm over the screened set, (b) Bonferroni over **all 357** (one-sided z = 3.633), (c) Hansen SPA (`model_zoo.spa_test`, 2,000 stationary-bootstrap draws, mean block 4 closes) over every condition with ≥ 30 OOS closes.
* Degenerate all-win cells (`T-10-14m & px ≥ .90` 16 closes, `T-5-10m & px ≥ .90 & NO` 19, `px ≥ .90 & vol < .7 bp` 6) are excluded from survivors: a 100 % hit rate's SE is zero by construction; rule-of-three lower bound on hit is 0.81/0.84/0.50 — buying 93 c favourites for +7 c gross is the base rate of favourites, not an edge.

### 4.1 Corrected result (positive side)

| test | result |
|---|---|
| Bonferroni over 357, OOS, positive | **none** |
| Holm over 31 IS-screened, OOS | `medge5-15c & NO` only (p = 0.0016 vs 0.0018 threshold; OOS +20.1 c ± 6.8, 39 closes; IS +10.7 ± 7.0, t = 1.5) — **side-literal ⇒ drift** |
| SPA, all 166 conditions ≥ 30 OOS closes, vs no-trade | best `medge5-15c & NO`, t = 2.85; **p = 0.105** (studentised), 0.054 (non-stud.) |
| SPA, 126 side-symmetric conditions | best `px .50-.75`, t = 2.38; **p = 0.24** / 0.59 |
| t ≥ 2 in both halves, ≥ 15 closes each, non-degenerate | `medge0-5c & NO` only (side-literal) |
| Full-sample SPA (in-sample, for reference) | best `T-10-14m & px ≥ .90` t = 28.9 — degenerate all-win cell; non-studentised p = 0.17 |

**Verdict: no favourable condition survives correction. Nothing was fitted on < 30 closes that is called anything but "insufficient".**

### 4.2 Do-not-trade list (negative side; survives correction)

| condition | IS net c (closes, t) | OOS net c (closes, t) | OOS hit / implied | test passed |
|---|---|---|---|---|
| `px .25-.50 & spread 1-3 c` | -11.1 (55, -3.1) | **-13.8 (54, -3.67)** | .244 / ~.38 | **Bonferroni over all 357**; Holm |
| `px .25-.50 & depth ≥ 200` | -9.7 (55, -2.6) | -12.9 (55, -3.6) | .251 | Holm |
| `px .25-.50` (any side, any time) | -10.0 (56, -2.8) | -12.0 (55, -3.4) | .259 / .376 | Holm; both-halves |
| `px .25-.50 & \|z\| < .5` | -9.7 (53, -2.6) | -12.6 (54, -3.4) | .272 | Holm; both-halves |
| `px .25-.50 & vol .7-1.2 bp` | -7.7 (33, -1.3) | -15.3 (24, -3.2) | .219 | Holm (marginal, p = threshold; only 24 OOS closes) |
| `px .10-.25 & spread 1-3 c` | -9.3 (45, -2.4) | -9.9 (50, -3.0) | **.094 / .17** | **SPA best loser, p = 0.0045 (all) / 0.0015 (side-sym.)**; both-halves |
| `px .25-.50 & {spread ≤ 1 c, depth < 50, depth 50-200, medge 0-5 c, medge ≤ 0}` | -9 to -12 | -10 to -13 (t -2.6 to -3.1) | | both-halves t ≤ -2 |
| `medge ≤ 0 & YES`, `T-{10-14m,5-10m,2-5m} & medge ≤ 0 & YES` ⚑ | -14 to -17 | -13 to -15 | | both-halves (side-literal; drift) |

Interpretation: the 25-50 c region is where both sides are coin-flip-priced and the book is thin enough that the executable price + tick + fee costs ~3 c against a realised edge of zero; the 10-25 c region realises roughly **half** its implied probability (9-12 % vs 17 %). These are the states where SOL-v1 placed 66 of its 71 trades.

## 5. Hold vs exit (71 SOL-v1 positions, in-sample rule comparison, nothing proposed)

Rules are evaluated on the logged `hold_exit` poll path (executable exit proceeds at displayed bids, full-lot liquidity only), else settlement.

| rule | P&L $ | net c/ct | SE |
|---|---|---|---|
| policy as logged | -10.79 | -1.81 | 2.98 |
| **always hold to settlement** | **+9.11** | +1.53 | 4.04 |
| exit if proceeds ≥ cost | -6.60 | -1.10 | 1.94 |
| exit if proceeds ≥ 1.25 × cost | +10.92 | +1.83 | 2.98 |
| exit if proceeds ≥ 1.5 × cost | +14.25 | +2.39 | 3.51 |
| exit if proceeds ≥ 2 × cost | +4.75 | +0.80 | 3.70 |
| exit at first poll (round trip the spread) | -24.74 | -4.14 | 0.31 |
| stop: exit if p_side < .25 | +6.53 | +1.09 | 3.39 |
| perfect-foresight path max (NOT a policy) | +162.07 | | |

Findings: (i) every rule is within 1 SE of every other except "exit at first poll", which is the deterministic spread + fee loss; (ii) the logged exit rule is the second-worst of the eight; (iii) the ranking of the take-profit multipliers (1.5 > 1.25 > 2 > 1) is not monotone, i.e. noise; (iv) the 36 settled losers all passed through a poll with a positive bid, but a rule that would have captured that ("stop at p_side < .25") is +$6.53, not +$96 — the rescue value is hindsight. **No hold/exit policy change is supported by 71 positions; the direction of evidence is that the current take-profit exit subtracts ~7 c/ct (± 6).**

## 6. Opus counterfactuals

Opus forecasts scored: 180 (own probability `p_own` where logged, else reconciled `p_llm`); counterfactual = buy the side `p_own` favours vs mid, lot 10, at the executable price + tick from the nearest SOL-v1 snapshot (≤ 8 s).

| scenario | n | closes | hit | mean exec px | mean p_own(side) | cf net c/ct | SE |
|---|---|---|---|---|---|---|---|
| all 180 disagreements, buy own side | 180 | 90 | .306 | .306 | .324 | -1.2 | 2.8 |
| top-30 |disagreement| NO_TRADEs | 30 | 27 | .200 | .251 | .296 | -6.3 | 7.5 |
| own-edge > 0 only | 110 | 76 | .191 | .224 | .252 | -4.3 | 3.3 |
| longshots, exec px ≤ .10 | 41 | 35 | **.024** | .041 | .059 | -1.9 | 2.6 |

Live fills: 10-28 17:10 PT YES @ 46.1 c × 15 → won (+$7.82); 10-29 14:25 PT NO @ 3.8 c × 20 → lost (-$0.82); 10-29 16:55 PT YES @ 4.4 c × 15 → lost (-$0.71). Net +$6.29 on 50 contracts is one 46 c coin-flip that landed. Three further intents never filled (absent-side book; correctly not booked). One YES @ 15.1 c × 10 opened 19:50 PT and is unsettled at generation.

Had Opus acted on every disagreement it would have done what the universe does (-1 to -2 c/ct); acting only where it believed it had an edge would have been *worse* (-4.3 c/ct), and its top-30 convictions hit 20 % at a 25 c average price. Its 4-6 % tail bets realise 2.4 %.

## 7. Tail-frequency verdict (are 4-5 c longshots supported?)

`data/candles/SOL_1m.parquet`, 54,894 one-minute bars (38.1 days). z = h-minute log return / (trailing 15-min realised 1-min sigma × √h), the same estimator family the policy uses.

| 5-min horizon, two-sided P(\|z\| > q) | q = 1.645 | 1.96 | 2.326 | 2.576 | 3.0 |
|---|---|---|---|---|---|
| Gaussian | .100 | .050 | .020 | .010 | .0027 |
| empirical | .124 | .080 | .049 | .036 | .022 |
| ratio | 1.24 | 1.61 | 2.44 | 3.6 | 8.1 |
| empirical after κ = 1.15 vol inflation | .089 | .054 | .032 | .023 | .014 |
| ratio (κ) | 0.89 | 1.08 | 1.6 | 2.3 | 5.0 |

rms z = 1.175 at 5 min (1.146 at 1 min, 1.17-1.18 at 10-14 min): the realised-vol sigma under-measures by ~17 %, and beyond that the tails are genuinely fat. Price equivalents (one-sided): what a Gaussian model prices at **5 c** is empirically worth **6.1-6.3 c** (4.3-4.6 c after κ); Gaussian 2.5 c → 3.9-4.2 c; Gaussian 1 c → 2.4-2.6 c.

But the relevant comparison is against the **market**, not the Gaussian, and the market is already there:

| executable longshot px | rows | closes | realised hit | market-implied | model p | net c/ct | SE |
|---|---|---|---|---|---|---|---|
| 2-5 c | 3,174 | 78 | **.022** | .032 | .045 | -1.31 | 1.25 |
| 5-10 c | 3,457 | 87 | .071 | .073 | .096 | -0.80 | 3.66 |
| 10-20 c | 5,186 | 96 | **.088** | .149 | .178 | **-7.02** | 2.76 |
| 20-35 c | 9,048 | 111 | .259 | .271 | .301 | -2.64 | 3.50 |
| 35-50 c | 10,383 | 100 | .387 | .422 | .433 | -5.21 | 2.87 |

Model-side calibration: when the model says 10-20 % the side wins 7.4 %; 20-35 % → 21 %; 0-5 % → 3.1 % (fine). **Verdict: fat tails are real relative to a Gaussian with the policy's sigma, but they are priced in — market-implied ≥ realised in every longshot bucket, and the model is the most over-confident of the three. Opus's 4-5 c bets are supported neither by the candle-implied fair value (≈ market) nor by realised outcomes (2.2-2.4 % hit at 3-4 c cost + fee).** The fix the data points at is the model's sigma (κ ≈ 1.15 and a tail that is not Gaussian beyond 2σ), not a longshot programme.

## 8. `conditions_brief.json` — schema and intended use

```json
{
  "meta": { "generated_pt", "yes_settlement_rate": {all, first_half, second_half, n_closes}, "sol_index_first_last",
            "split": {in_sample_closes, out_of_sample_closes, n_is, n_oos}, "execution_model", "coverage_caveat",
            "how_to_read", "drift_warning", ... },
  "n_conditions_examined": 357,
  "corrected_result": { "holm_pos": [...], "holm_neg": [...], "spa_oos_p_consistent_nonstud", "spa_oos_losers_p_consistent_nonstud" },
  "states": [ {
      "scope":   "full_sample_IN_SAMPLE" | "second_half_OOS",
      "state":   { "<feature>": "<bucket>", ["pick": "with_model"|"against_model"] }      // 1 feature + pick, or tb×pxb×side, or tb×edgeb×side
      "n": rows, "n_closes": closes, "hit": realised, "mkt_implied": mean exec px, "model_p": mean model p_side,
      "net_c": c/contract buy-and-hold, "se_c": close-clustered SE,
      "verdict": "favourable" | "unfavourable" | "indeterminate" | "insufficient",   // needs >= 20 closes, non-degenerate hit, |t| >= 2
      "side_literal": bool, "verdict_is_uncorrected": true, ["caveat": "...drift..."]
  } ]   // 360 states: 11 favourable, 34 unfavourable, 174 indeterminate, 141 insufficient
}
```

Feature keys: `tb` (T-10-14m, T-5-10m, T-2-5m, T-60-120s, T-10-60s), `pxb` (px<.10 … px>=.90), `side`, `zb`, `edgeb` (medge<=0/0-5c/5-15c/>15c), `sprb`, `depb`, `volb`, `partb`, `hourb`, `comove`, `midb`, `with_model`, `fav`.

How a prompt builder should use it:
1. Look up the current snapshot's buckets (`b_time(secs_left)`, `b_px(executable px)`, `b_edge(p_side - px)`, etc. from `forensics_2026_10_29.py`) and fetch the matching `second_half_OOS` state first, `full_sample_IN_SAMPLE` second; always carry `n_closes` and `se_c` into the prompt, never `net_c` alone.
2. **Drop any state with `side_literal: true` from "edge" claims** (quote its `caveat` instead); only side-symmetric states describe conditions rather than the week's drift.
3. Treat `unfavourable` as actionable (the do-not-trade list in §4.2 is the corrected version; the brief's verdicts are per-cell and *uncorrected*, `verdict_is_uncorrected: true`). Treat `favourable` as "not a loss leader" at best — `corrected_result` is the authority and currently says no favourable condition survives.
4. `insufficient` (< 20 closes, or hit ∈ {0, 1}) must be rendered as "no data", not as a neutral prior.
5. The realised-vs-implied pair (`hit` vs `mkt_implied`) is the calibration fact to show the model; `model_p` next to it shows how far the Gaussian model is from both.

## 9. Guards, unverified items, and what another cut would lose

* Every number above is from the 19:57 PT run; the tape is live-appending (113 closes at run time, one Opus position open), so counts drift by a few per hour. Re-running both scripts regenerates everything in ~40 s.
* In-sample vs out-of-sample is labelled on every table; the hold/exit comparison (§5) and the taken-trade attribution (§2) are in-sample by nature (n = 71) and are described, not proposed.
* Nothing fitted on < 30 closes is called optimal; the only cells with |t| ≥ 2 and ≥ 30 closes on both sides are the negative ones in §4.2.
* The counterfactual universe inside the last 5 minutes is conditioned on SOL-v1 not holding (coverage caveat §1); the T-60-120s / T-10-60s rows should be read as 17-34 closes, not 112.
* `p_own` for Opus is only available for the two logged cohorts; otherwise the reconciled `p_llm` is used (`opus.json → note`).
* The tail study uses 1-min candles with a 15-obs sigma window as a proxy for the policy's 1-s / 900-s estimator; the ~17 % rms-z excess is robust across 1-14 min horizons but the exact κ is estimator-dependent.
* Not verified: the `data/candles/SOL_1m.parquet` source feed vs the RTI index (the candles are exchange closes, the market settles on the RTI 60-s average; the tail ratios are about the SOL process, not about the settlement rule).
