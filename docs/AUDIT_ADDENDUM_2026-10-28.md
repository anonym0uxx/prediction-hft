# Audit addendum — 2026-10-28 (independent auditor; corrects and extends `docs/AUDIT_2026-10-28.md`, which is left unchanged)

All times Pacific (PT). All data **PAPER**: simulated fills, no exchange account, no order/fill IDs; the Kalshi key only reads the cfbenchmarks websocket. Code HEAD `a95281c`, nothing committed. Numbers reproduce with `uv run python scripts/addendum_2026_10_28.py` (→ `data/p22/addendum_2026-10-28.json`) and `uv run python scripts/build_ledger.py` (→ `data/ledger/`). The audited cohort is **fills with t ≤ 08:05 PT 10-28 (97 settled)**, the same snapshot the audit used; live totals at writing (09:25 PT) are given where they differ.

## 1. Suspension verification

| item | finding |
|---|---|
| Runners switched | Scheduled tasks `PredHFT-S7-P13-BTC15M`, `-ETH15M`, `-SOL15M` (roots `data/s7p13-KX{BTC,ETH,SOL}15M`). Task command lines now `… --thr-mode dyn --proj-gate --scored-only --ts-levers --bet-gate --root data/s7p13-KX*15M`; status Running; Last Run 10/28 09:05:51. |
| Effective timestamp | runner.log start lines `2026-10-28T16:05:51Z` (BTC, ETH) / `16:05:52Z` (SOL) = **09:05:51–52 PT**, each reading `scored_only=True … proj_gate=True ts_levers=True bet_gate=True`. |
| Code version | git HEAD `a95281c` (committed 08:27 PT; the restart loaded it). |
| Execution mode after switch | 0 `PAPER BUY` / `STALE` / `SKIP` / `GATE REJECT` lines after 16:05:51Z in all three logs; 0 trades with t ≥ switch; `open_positions` = [] in all three (the audit's "0 open before the switch" confirmed from trades.jsonl status). Recording continues: 28/26/30 final-minute snapshots per asset for the 09:15 close with reject taxonomy populated (`ts_arm_window` 18, `no_offer_pinned` 8, …); TS arm selection written at 09:13:12 PT; cfb raw capture `data/cfb/raw/*.20261028.raw.jsonl` growing (9,621 lines). |
| Code path | `s7_live.run` line 378: `if scored_only or have_hour or tk in state.open_positions or not (t_close <= mins_left <= t_open): continue` — one gate ahead of candidate construction, re-check (line ~411-422), STALE (420), bet-gate (445-447) and `PAPER BUY`/`record_open` (462-463). Snapshot + taxonomy are written at line 359 (before the gate); `settle()` runs at line 258 (before the gate); TS replay is fed from `ts_snaps` collected at line 358. **Retries:** there is no order queue or persistent order state in paper; a STALE just logs and the next poll re-decides from scratch — that re-decision is also blocked by line 378. Hence no outstanding entry orders can exist for the suspended strategies. |
| Unrelated runners | P12 tasks `PredHFT-S7-15M-{BTC,ETH,SOL}` untouched: last start lines `2026-10-27T23:05:22-23Z` = **16:05:22 PT 10-27** (code at that time = commit `a395cf8`, 16:05:16 PT), no restart since, `scored_only=False proj_gate=False`; still filling (ETH 08:59, ETH+SOL 09:14 PT). P20 not touched. **Note for the owner:** `PredHFT-S7-P13-BTCD` (hourly KXBTCD, `--proj-gate`, no `--scored-only`) was restarted 08:27:36 PT for the module reload and is still a live paper taker with the P13 final-minute rule (0 fills ever in that root; last NOFILL 09:00 PT). It is outside the three named tasks; decide whether the suspension should cover it. |
| P12 freeze boundary | **2026-10-27 16:05 PT** (official feed live, s7p15 restart 16:05:22 PT). Every ledger row carries `era_pre_post_freeze`; every fill in §2 is tagged by it. |

## 2. Reporting units reconciled

**95 vs 97.** The compact table row "P12 15M · replica, pre-rebase … 23 buy … 23 fills" was produced by `p12_counts()` over a window starting **23:00 PT 10-26**, while the P&L/fee/hit columns of the same row come from `version_of()` (all 25 pre-rebase fills). Re-running the funnel from 23:00 PT gives exactly the published 167 markets / 6,469 checks / 305 candidates / 23 buys / 18 stale / 79 skips; from 22:44 PT it gives 171 / 6,543 / 311 / **25** / 18 / 80. The two records the window drops:

| ticker | t | side | qty @ price | fee | pnl |
|---|---|---|---|---|---|
| KXETH15M-26OCT270145-45 | 2026-10-26 22:44:02 PT | NO | 5 @ 0.057 | $0.02 | −$0.31 |
| KXSOL15M-26OCT270145-45 | 2026-10-26 22:44:04 PT | NO | 5 @ 0.50 | $0.09 | −$2.59 |

Same two records explain **63 vs 65**: P12 rows in the compact table 23+5+35 = 63; P12 fills by version tag 25+5+35 = **65** (used by the longshot test). The −$7.76, $1.91 fees, 60 % hit (15/25) in that row already include both records; only the count column was wrong.

**Current P12 is not the aggregate.** Cohort table (audited 97; settled-only; quoted cost = booked cost − 1-tick penalty; max exposure = peak open cost within the configuration with positions released at close+120 s; drawdown = worst peak-to-trough of cumulative realised P&L ordered by settlement — **no marking exists in paper, so both are settled-only**):

| configuration | asset | fills (W) | contracts | quoted cost | fees | penalty | proceeds | net | max exposure | drawdown |
|---|---|---|---|---|---|---|---|---|---|---|
| **P12 official feed (FROZEN, post-freeze)** | ALL | 35 (11) | 304 | $87.83 | $3.15 | $3.04 | $98.00 | **+$3.98** | $11.16 | −$10.77 |
| | BTC | 15 (4) | 130 | 34.98 | 1.21 | 1.30 | 36.00 | −1.49 | 8.50 | −9.25 |
| | ETH | 8 (2) | 68 | 21.67 | 0.76 | 0.68 | 18.00 | −5.11 | 8.64 | −5.44 |
| | SOL | 12 (5) | 106 | 31.18 | 1.18 | 1.06 | 44.00 | +10.58 | 7.92 | −6.21 |
| P12 replica, post-rebase (pre-freeze) | ALL | 5 (1) | 39 | 6.00 | 0.29 | 0.39 | 7.00 | **+$0.32** | 4.13 | −2.43 |
| P12 replica, pre-rebase | ALL | 25 (15) | 170 | 102.15 | 1.91 | 1.70 | 98.00 | −7.76 | 25.76 | −19.14 |
| | BTC / ETH / SOL | 8/8/9 | 55/60/55 | 38.11/32.13/31.90 | .57/.79/.55 | .55/.60/.55 | 40/25/33 | +0.77 / −8.53 / −0.01 | 9.64/9.10/8.50 | −3.87/−15.87/−3.14 |
| P13 projection replica, pre-rebase (floor .97) | ALL | 7 (5) | 62 | 46.85 | 0.73 | 0.62 | 42.00 | −6.20 | 18.01 | −13.71 |
| | BTC / ETH / SOL | 3/2/2 | 26/20/16 | 19.13/16.50/11.22 | .32/.20/.21 | .26/.20/.16 | 16/10/16 | −3.71 / −6.90 / +4.41 | 9.11/8.90/5.80 | −5.78/−7.93/0 |
| P10 hourly redesign | ALL | 12 (2) | 89 | 29.65 | 0.92 | 0.89 | 12.00 | −19.46 | 8.50 | −20.24 |
| | BTC / ETH / SOL | 7/3/2 | 52/22/15 | 24.14/4.31/1.20 | .58/.25/.09 | .52/.22/.15 | 12/0/0 | −13.24 / −4.78 / −1.44 | 8.50/3.30/0.70 | −14.02/−4.78/−1.44 |
| P1/P2 hourly model-rule (stopped) | ALL | 13 (5) | 119 | 23.86 | 1.12 | 1.19 | 44.00 | +17.83 | 7.73 | −5.40 |
| | BTC / ETH / SOL | 10/1/2 | 92/9/18 | 16.72/3.96/3.18 | .77/.16/.19 | .92/.09/.18 | 27/9/8 | +8.59 / +4.79 / +4.45 | 7.20/4.05/2.40 | −3.41/0/−2.53 |
| **all 97** | | 97 (39) | 783 | 296.34 | 8.12 | 7.83 | 301.00 | −11.29 | 43.77 (portfolio-wide) | −40.64 (portfolio-wide) |

- Current policy = **P12-official: 35 fills, +$3.98** (0.86 c/contract, cluster-SE by close 7.0 c). Post-rebase replica: 5 fills, +$0.32. Their sum +$4.30 is descriptive arithmetic across two feed variants, not a pooled expectancy (different spot source, different noise term). The four other configurations sum to **−$15.59** (P12 pre-rebase −7.76, P13 pre-rebase −6.20, P10 −19.46, P1/P2 +17.83; the three retired losers alone −$33.42) — research cost of retired/pre-rebase code. The aggregate **−$11.29 does not describe current P12 policy.**
- Live at 09:25 PT: 100 settled, 806 contracts, net **−$7.35**; P12-official 38 fills, +$7.91 (ETH 09:14 and SOL 09:14 both won), still 0.0 ± 7 c/contract in SE terms.
- P12 per-asset dispersion (BTC −1.49 / ETH −5.11 / SOL +10.58 on 35 fills) is itself a warning that the ALL figure is one draw.

## 3. What "97/97 reconciled" validates

It is **settlement-label reconciliation of simulated fills**: for each of 97 trade rows, `result` equals the exchange's settled result, `pnl == qty·1{win} − cost − fee` to the cent, `fee == kalshi_taker_fee(qty, price)`, `cost == qty·price`. No exchange order, fill, or position ID exists to reconcile against. Fill assumptions as booked (all 97 rows carry `slippage = 0.01`):

1. price = our-side touch on a **second** orderbook fetch (re-check) **+ 0.01**, capped at 0.99 (`s7_live.py:422`); quoted touch may sit on the 0.001 grid (18/97 fills have sub-penny quoted asks, e.g. 0.047, 0.914).
2. qty = integer min(Kelly size, lot cap × equity scale, displayed size at the touch), **halved if displayed < 50** (26/97 fills), never above displayed (0 violations).
3. fee = ceil-to-cent per order of 0.07·q·p·(1−p).
4. **Latency guard 1.5 s applies only to `proj_gate` (P13) rows**; P12 (model rule) has none (`latency_guard_s = None` in its rows). 25/97 fills were booked after > 1.5 s re-check latency: all 13 P1/P2, 9/12 P10, 3 P12 (max 7.8 s; P12-official max 2.86 s, 07:59 PT).
5. One pass per poll; no resting orders, no partials beyond the halving rule, no queue.

**Decomposition (audited 97):** proceeds $301.00 − quoted cost $296.34 = **+$4.66**; − fees $8.12 − penalty $7.83 = **−$11.29** (residual 0.00). By configuration (gross vs quoted → net): P12-official +10.17 → +3.98; P12 post-rebase +1.00 → +0.32; P12 pre-rebase −4.15 → −7.76; P13 pre-rebase −4.85 → −6.20; P10 −17.65 → −19.46; P1/P2 +20.14 → +17.83. Execution costs (fees + penalty) are **2.04 c/contract**, i.e. larger than the whole P12-official net (0.86 c/contract).

**"Ten ticks is conservative" — qualified.** The +0.01 is a flat price haircut. It can *overstate* price cost (a marketable order at a 0.047 touch pays 0.047, not 0.057) while *understating*: (a) partial/missed fills — the post-fill check (`still_fillable`) exists only since 10-27 22:55 and is log-only; (b) adverse selection — the P12 touch at T-55 s is typically a stale resting order that is pulled when the feed moves; (c) depth exhaustion — qty ≤ displayed is tested against *one* snapshot; (d) **two paper runners consuming the same displayed liquidity**: 10 tickers carry fills from two families; 6 are 15M same-side pairs (s7p13 + s7p15), all pre-rebase: BTC 1315 (Δt 7.5 s), BTC 1430 (4.4 s), ETH 1315 (0.9 s), ETH 1430 (3.6 s), SOL 1030 (2.9 s), **SOL 1415 (1.1 s; both NO @ 0.93 against the same displayed 13.89 contracts: 5 + 6 = 11 of 13.89)**. Combined qty never exceeds the smaller displayed size, but the two books were independently debited. Since the rebase `risk.size` blocks same-ticker holds symmetrically; on the post-rebase tape there were 2 coincident candidate snapshots (BTC 1, SOL 1, within 5 s, same side) and 0 double fills. The 4 hourly pairs are mixed-side (s7live/s7p10/s7final).
Also found: the 0.99 cap makes the paper price **lower** than the executable offer whenever the offer > 0.98 (min(0.999+0.01, 0.99) = 0.99). No fill is affected (max fill price 0.97), but 161/269 "offered" P22 records are — see §4.

**Fee inputs validated.** `GET /series/KX{BTC,ETH,SOL}15M` and `KXBTCD` (10-28): `fee_type = quadratic`, `fee_multiplier = 1` → 0.07·C·P·(1−P); `/series/fee_changes` empty. Rounding per docs.kalshi.com/getting_started/fee_rounding: trade fee ceil $0.000001 per fill, then balance aligned to $0.01 (non-direct member) via a rounding fee + accumulator; our ceil-to-cent per order is an upper bound: 97 fills booked $8.12 vs exact $7.62 (**+$0.50 overstatement**, the gap a direct member or the accumulator would recover). Quantities integer (0 fractional booked; markets allow `count_fp`); price grid 0.001 handled (fees recomputed from the sub-penny price, 0 mismatches). P12 execution assumptions are **not** changed; any variant above is sensitivity only.

## 4. P22 calculation audited (`scripts/p22_paired.py`)

| element | definition in code |
|---|---|
| forecast probability | `p_cal(d_bp, σ₁, r, noise)`: official = cfb ticks received ≤ t_h (`Feed.upto`), replica = replica rows + basis correction, 0.6 bp noise; `proj = (1−r)·partial + r·spot`, r = (60−n)/60 floored at 0.02; σ₁ = last 1-min sigma ≤ t_h (fallback 5 bp). 400/960 `p_off` values are exactly 0 or 1 (Φ saturation at |z| ≳ 8) — clipping, applies to both forecasts equally. |
| purchased-side rule | `side = "yes" if d_bp > 0 else "no"` (forecast sign), **not** argmax over a threshold. |
| bid/ask normalisation | YES ask = `asks` levels or `yes_ask`; NO ask = 1 − YES bid levels; absent side = `None` (`side_present`). `mid` = (yes_bid+yes_ask)/2 only when two-sided (one-sided "mid" stored but excluded from Brier-mid). |
| quantity / price / fee | lot 10 walked through top-3 depth (None if depth < 10); price = min(offer + 0.01, 0.99); fee = `kalshi_taker_fee(10, price)/10` once. |
| latency | survival = next snapshot in (t_h+1 s, t_h+5 s] still offers ≤ price. **Snapshot cadence (~4 s poll) ≠ quote lifetime**; "gone by next poll" says nothing about when it left. |
| averaging populations | Brier off/rep over all 192 markets × horizon; Brier mid and "Brier off on that subset" over the two-sided subset (111/84/57/37/14); mean edge over records with an intended-side offer of depth ≥ 10 (96/74/53/34/12); realised net over edge ≥ 5 c candidates (1/1/0/0/2); SE clustered by close and by hour block. |

**"Mean executable edge" is not the mechanical −(a−b)/2.** The side is the forecast's, so p_side is mostly ≥ 0.8 (histogram of p_side on offered records: 1.0: 105, 0.9: 63, 0.8: 36, 0.7: 27, 0.6: 29, 0.5: 9). Replacing p_side with the market's own side-mid reproduces the mechanical value (−0.48 c = half-spread + tick + fee); the actual mean is **−7.96 c** — a real statement that the forecast, on the records where its side is purchasable, prices that side below the ask.

**Pre-defined rules** (declared before this run: R1 side = argmax p_side with p_side ≥ 0.6 and intended-side depth ≥ 10; R2 same with p_side ≥ 0.9; R3 = R1 ∧ offer ≤ 0.95, i.e. inside the P13 cap). Model-implied edge (c/contract) vs realised forward net (c/contract, SE by close):

| T-h | with book | two-sided | intended side offered ≥10 | no offer on intended side | ALL offered: mean edge / real | R1 | R2 | R3 |
|---|---|---|---|---|---|---|---|---|
| 60 | 192 | 111 | 96 | 96 | −9.3 / +1.1±2.3 (n 96) | −8.9 / +0.9±1.7 (80) | −2.8 / +1.1±0.1 (42)† | −16.7 / +0.4±7.4 (19) |
| 45 | 192 | 84 | 74 | 118 | −8.7 / −1.6±2.6 (74) | −8.6 / −3.2±2.7 (68) | −2.6 / +1.2±0.2 (36)† | −14.6 / −10.2±8.7 (17) |
| 30 | 192 | 57 | 53 | 139 | −7.5 / −6.7±3.0 (53) | −6.5 / −5.4±3.2 (49) | −1.3 / +0.9±0.0 (33)† | −19.1 / −51.6±19.1 (6) |
| 20 | 192 | 37 | 34 | 158 | −5.4 / −6.4±4.3 (34) | −4.9 / −7.3±4.2 (33) | −0.4 / +0.9±0.0 (25)† | −15.0 / −67.5±19.6 (4) |
| 10 | 192 | 14 | 12 | 180 | −1.8 / −8.9±7.1 (12) | −1.8 / −8.9±7.1 (12) | −0.7 / +0.9±0.0 (8)† | +4.6 / −38.4±37.7 (3) |

† **R2 is a measurement artefact**: these are 0.99–0.999 favourites where the cap books the paper price at 0.99 *below* the 0.999 offer; every one "wins" +0.9 c = 100·(1 − 0.99 − fee). Excluding offers > 0.98 leaves n = 4/3/0/0/0 (edge −5.9/−1.7 c). R3 at T-10 (+4.6 c model edge, n = 3, realised −38 c) is the only favourable model-edge cell — **exploratory, n = 3, and its realised return is negative**. Share of offered records with edge > 0: 15 % → 67 % from T-60 → T-10, but ≥ 5 c in 1/1/0/0/2 records. Model-implied edge and realised net move in opposite directions at T-30/20/10 — the forecast's confidence grows into markets that are already right.

**Brier "equality".** No exact equality exists: 0/303 two-sided records have `p_off == mid`; no fallback or reuse of the market probability enters `p_off`. Paired Brier(off) − Brier(mid) on the two-sided subset: T-60 +0.0085 (SE close 0.0064), T-45 +0.018 (0.0089), T-30 −0.0007 (0.0085), T-20 −0.0064 (0.011), T-10 +0.0105 (0.022). Correct statement: **no statistically resolved Brier improvement over the market in this sample** (and at T-45 the official forecast is ~2 SE *worse*). The audit's "equals" was loose language, not an identity.

**Coverage.** Intended-side liquidity ≠ two-sided book: with YES bid 0.999 and no NO bid the complementary representation gives NO ask 0.001 and *no YES ask*; the forecast sided with the pinned favourite in every such case, so "no offer on the intended side" is the correct label for 96/118/139/158/180 of 192 markets (T-60 → T-10). The 14/192 two-sided at T-10 is a 7 % subset of the P16 arm-window population; **it is not P12's population** (P12 trades at T-60…−6 s on markets with mid in [0.03, 0.97] and spread ≤ 6 c) and nothing here generalises to P12.

## 5. Tightened conclusions

- **Timestamps that exist**: cfb `value_ts_ms` (CF publish) → `received_at_ms` (Kalshi) p50 52 ms, p90 75 ms; → local `t` p50 18 ms. Book snapshot: only the poll's loop-start `t` (the orderbook REST response is not timestamped by the exchange and the fetch time is not recorded separately); decision `t_dec` → re-check latency p50 0.45 s. **No aligned exchange-side book timestamp exists**, so "the opportunity disappears before receipt" is not established. Replace with: **no executable advantage at observed decision times.** 0.2 s feed freshness is feed age, not end-to-end latency (add book fetch, decision, order transit, matching — all unmeasured in paper).
- **Settlement reconstruction vs probability model.** `avg ≥ K` reconstruction is right in 198/198 (incl. the ETH tie, KXETH15M-26OCT271945-45, booked YES). That validates the window/tie/rounding logic, **not** the model: P12 NO 9 @ 0.24 on that market was placed with p_model(YES) = 0.628, i.e. p_side(NO) = 0.372 against a 0.24 ask, while spot was +0.55 bp *above* K with 3 of 60 samples in — a 13 c model edge on a 37 % proposition that lost on the tie. Across the 36 P12-official fills: mean p_side 0.458 vs hit rate 0.306 (Brier of p_side 0.182 vs Brier of the paid price 0.136) — the market price forecast P12's own outcomes better than its model did. Near-strike inputs at decision: 26/36 fills within ±1 bp of K; 8/36 taken at partial_n = 3 (first eligible poll, T-57 s) where remaining-path sd dominates; ETH tie mass (P(|avg−K| < 0.005) ≈ 1–2 %) is not in p_cal. These are observations for the frozen evaluation, not changes.
- **"−0.17 → −0.37 bp" is a MAE difference, not signed bias.** It is mean|err|(official) − mean|err|(replica): T-60 1.629 − 1.796 = −0.167; T-10 0.105 − 0.478 = −0.373 — the same quantities as "0.10 vs 0.48 bp" at T-10. Signed bias separately: official −0.186 / −0.071 / −0.049 / −0.044 / −0.008 bp (T-60…10), replica −0.122 / −0.050 / +0.005 / −0.056 / −0.009 bp; official RMSE 2.50 → 0.16 bp, replica 2.60 → 0.61 bp. The official feed is more *precise*; neither is detectably *biased* at T-10.

## 6. Forward evaluation plan (replaces the audit's restart package)

Not restart criteria: Brier gain > 0.01, 50 % 1-s survival, or any single favourable cell. **Primary endpoint (P12 forward confirmation):** mean net $ per contract on **new post-freeze P12 fills** (t > 09:25 PT 10-28), at booked price/qty/fees (assumptions §3 unchanged), outcome = shared exchange settlement; SE clustered by 15-min close (pools the three assets' same-close fills) and by hour block, report the larger. **Minimum economically worthwhile edge: +2.0 c/contract net** (≈ the 2.04 c/contract execution cost, so a one-tick error in the fill assumption cannot flip the sign). Observed to date: 36 fills, −0.04 c/contract, sd 37 c, SE 6.9 c.

Review rule (one-sided α = 0.025 spent O'Brien-Fleming-style; **review milestones, not readiness proofs**):
- **100 new fills** (information ≈ ⅓): efficacy only if z ≥ 2.80 for H₁: μ > 2 c; non-binding futility if the point estimate < 0 or the upper 95 % bound < 2 c → stop the cohort.
- **300 new fills**: efficacy if z ≥ 1.98. Otherwise the cohort is closed as "not confirmed".
- **Power statement — independent data are *not* sufficient at 300**: with sd 37 c, 80 % power for a 2 c edge at z 2.80 needs ≈ 4,500 fills (≈ 2,000 for 3 c); 300 fills resolve only |μ| ≳ 6 c. State this when reporting; do not present 300 as adequate for a 2 c claim.
- P22 confirmation cohort: **500 new markets per asset**, endpoint = realised net c/contract of **R1 at T-30** (declared here; all cells above are discovery), same clustering; Brier vs mid reported as secondary. Discovery (this doc, any new cell) stays labelled exploratory; nothing graduates without the pre-declared endpoint.
- Regimes prospective, never pooled silently: time-of-day blocks (00–06, 06–09:30, 09:30–16, 16–24 PT), σ₁ tercile at decision, scheduled events — **today 10-28 FOMC: 11:00 PT statement, 11:30 PT press conference; window tagged 10:45–12:30 PT** (`regime_fomc_window`). P12-official fills so far: 0 in US-cash hours, 0 in the FOMC window.

## 7. Portfolio risk and productivity

**$25 global daily halt replay** (`data/risk/events.jsonl`; day boundary = 00:00 UTC = **17:00 PT**, `risk.Ledger._day0`): UTC day 10-27 cumulative realised crossed −$25 at **11:32:04 PT** on the two 14:30Z settlements (BTC −3.87, ETH −6.86 → −$30.11); 0 positions open at the crossing. The ledger also contains **two settle events absent from every trades.jsonl**: `s1-maker KXLOWTDEN-26OCT26-T40 −$6.48` and `-B40.5 −$6.80` (06:05 PT), which count toward the halt. In paper the halt is shadow-logged (21 `halt_shadow` rows, 14:43–16:59 PT; written only when a candidate called `size()`). **13 fills were taken while shadow-halted** (15:14–16:59 PT, incl. the P13 BTC 1815 and twelve P12): net **−$0.74** (4 wins incl. SOL 1945 +7.75, 9 losses) — blocking them would have avoided $0.74, not the −$30 day; blocked fills are not avoided losses. Reset: at 17:00 PT the counter restarted at 0 (day total −$30.13), so the halt lasted 5.5 h of a 24 h day. Weekly −$50 never reached (ledger total −$26.78 incl. s1-maker). Drawdown figures everywhere are **settled-only**; portfolio-wide peak open cost $43.77, worst settled drawdown −$40.64 (all 97). **Current P12 alone:** peak exposure $11.16, drawdown −$10.77, 35 fills.

**Productivity, official era (16:05 PT 10-27 → 09:25 PT 10-28, 69 fifteen-minute closes per asset):**

| group | asset | windows | closes w/ candidate | opportunity rate | fills | fill rate | net $/window | capital occupancy (time-avg open cost / $250) |
|---|---|---|---|---|---|---|---|---|
| P13/P16 recorders | BTC/ETH/SOL | 69 each | 1/1/1 | 1.4 % | 0 | 0 | 0 | 0 |
| P12 | BTC | 69 | 17 | 24.6 % | 15 | 21.7 % | −0.022 | 0.04 % |
| P12 | ETH | 69 | 11 | 15.9 % | 10 | 14.5 % | −0.009 | 0.03 % |
| P12 | SOL | 69 | 14 | 20.3 % | 13 | 18.8 % | +0.145 | 0.04 % |

Since the gate restart (21:51 PT) the recorders show 0/1/1 (BTC/ETH/SOL) candidate closes in 46 windows each, 0 fills. Capital is idle > 99.9 % of the time; P12's edge, if any, has to be judged per contract, not on capital.

## 8. Ledger

`uv run python scripts/build_ledger.py` → **`data/ledger/decisions_fills.jsonl`** (+ `.csv`, `SCHEMA.json`): **356 rows** at 09:25 PT (100 fills, 189 skips, 67 stales; 13 families), **59 fields**, one row per decision (fill, or attempted submission that did not fill). Logic in `src/predhft/ledger_join.py`; tests `tests/test_ledger_join.py` (5). Schema: `row_id, row_kind, family, version_tag, config_flags, config_cmd, series, asset, ticker, close_ts, close_pt, decision_ts, decision_pt, decision_latency_s, intended_side, p_model, p_cal, p_side, p_market_mid, fav_at_decision, d_bp, partial_n, spot_src, cfb_age_s, sigma1, quoted_touch, booked_price, avail_at_touch, snap_t, snap_age_s, snap_yes_bid, snap_yes_ask, snap_bid_size, snap_ask_size, snap_bids_top3, snap_asks_top3, qty, cost, fee, execution_penalty, exec_assumptions, status, result, expiration_value, floor_strike, proceeds, net_pnl, portfolio_open_cost, portfolio_open_cost_s7, portfolio_equity, portfolio_realized_utc_day, halt_state, regime_tod_block, regime_vol_tercile, regime_fomc_window, era_pre_post_freeze, era_pre_post_suspension, reject_reason, limitation`. Portfolio state is replayed from the risk ledger at each decision (open cost releases unsettled `open` events at close+120 s; `_s7` excludes s1-maker posts). Decomposition over the ledger reproduces §3 (residual 0). Known nulls with `limitation`: `snap_bids_top3/asks_top3` null on all 356 (no decision yet joined to a post-`a95281c` depth snapshot — P12 runs pre-depth code); `p_cal` null on 255 (model-rule rows); `result` null on 116 unfilled decisions whose market is not in the cached fetch; `decision_ts` is loop-start t (no exchange timestamp) on every row.

## 9. Code and tests

New (uncommitted): `src/predhft/ledger_join.py`, `scripts/build_ledger.py`, `scripts/addendum_2026_10_28.py`, `tests/test_ledger_join.py`. No runner, risk, sizing, threshold or P12 code touched. Full suite: **111 passed** (106 + 5).
