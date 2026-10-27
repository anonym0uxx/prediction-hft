# SHARPE PLAN — 2026-10-27 (data cut ~16:50Z / 09:50 PT; 0.71 d since the 00:10Z reset)

Scope: every running family; realized stats, portfolio Sharpe/Kelly, per-family verdicts, new-signal tests (our books/replica/tape + OKX perp, Deribit DVOL, Kalshi tape), ranked build list, stop list. No code changed. All numbers computed by `reports/sharpe/*.py` (outputs `reports/sharpe/*.json`); Kalshi results for 319 tickers fetched once and cached (`reports/sharpe/results.json`). Clustering = `close_ts` unless stated. "c/ct" = net cents per contract after fees.

Honesty note up front: 43 settled trades in 0.71 days is not a Sharpe measurement, it is a sign check. Every per-family Sharpe below has a se larger than itself. The only quantities measured with real precision in this tape are mechanical ones (projection-sign accuracy, basis, lead-lag), and those are where the plan concentrates.

---

## 1. Realized per-family statistics (settled paper trades since reset)

| family | n | hit | mean c/ct | sd c/ct | se_cl | $ P&L | avg cost $/trade | trades/day | SR/trade | SR/day (proj.) | max DD $ | EV per $ at risk |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| s7p10-KXBTCD (P10 hourly) | 7 | 0.29 | −22.4 | 45.9 | 18.7 | −13.24 | 3.52 | 14.2 | −0.49 | −1.84 | −14.02 | −0.54 |
| s7p10-KXETHD (now scored) | 3 | 0.00 | −19.0 | 11.1 | 7.8 | −4.78 | 1.51 | — | −1.72 | — | −4.78 | −1.06 |
| s7p10-KXSOLD (now scored) | 2 | 0.00 | −10.7 | 3.1 | 3.1 | −1.44 | 0.68 | — | — | — | −1.44 | −1.07 |
| s7p15-KXBTC15M (P12) | 5 (+1 open) | 0.80 | +13.1 | 18.3 | 9.2 | +3.28 | 3.28 | 14.9 | +0.72 | +2.77 | −0.90 | +0.20 |
| s7p15-KXETH15M (P12) | 5 | 0.60 | +21.4 | 43.3 | 21.7 | +3.37 | 2.25 | 10.9 | +0.49 | +1.63 | −3.97 | +0.30 |
| s7p15-KXSOL15M (P12) | 7 | 0.43 | −1.4 | 33.9 | 13.8 | −0.55 | 2.86 | 15.2 | −0.04 | −0.16 | −3.14 | −0.03 |
| P12 pooled | 17 | 0.59 | +9.6 | 34 | ~8 | +6.11 | 2.81 | 24.0 | +0.18 ($) | +0.84 | −3.97 | +0.13 |
| s7p13-KXSOL15M (P13) | 1 | 1.00 | +40.2 | — | — | +4.02 | 5.80 | (ran 1.7 h) | — | — | 0 | +0.69 |
| s7final-KXBTCD (P2, old rule) | 7 | 0.29 | +5.7 | 27.1 | 11.1 | +4.10 | 2.18 | 10.6 | +0.21 | +0.68 | −3.09 | +0.27 |
| s7live ×3 (P1, stopped) | 6 | 0.50 | +32 | ~48 | — | +13.73 | 1.5 | — | — | — | −2.5 | +1.5 |
| s1-maker (P3, stopped) | 2 settled | — | — | — | — | −13.28 | — | — | — | — | — | — |
| ALL settled | 43 | 0.49 | — | sd $2.95/trade | se_cl $0.48 | +8.50 | 2.54 | 60.6 | +0.07 ($) | +0.49 | — | +0.08 |

SR/day (proj.) = SR/trade × √(trades/day), i.e. what the daily Sharpe would be if today's per-trade distribution repeated independently; the clustered version (same `close_ts` pooled) differs by <10% because almost no two trades share a close. None of these is distinguishable from zero: the pooled 43-trade mean is $0.20 ± $0.48 (clustered).

Weather/rain J1, sports S6, J3/J4, index, AAA: 0 paper trades since reset (J1 trades only `locked_*` states; none had a gap). Scored rows: weather_j1 6,493 resolved rows / 48 clusters: Brier model .0127 vs mid .0078 (BSS −0.64; the non-locked probabilistic rows are the damage: .079 vs .048 on 1,035 rows); locked rows 5,458 / 146 tickers: **0 false locks**. sports_s6 1,620 rows / 6 clusters: BSS vs mid −0.27 ± (se 234e-4 on a 434e-4 gap). J4 1 resolved row, J3 0. Index runners: `runner.log` has only start lines (3 restarts), 0 rows.

---

## 2. Portfolio: current mix and Kelly

### 2.1 Current mix (P10-BTC + P12×3 + P13), 0.71 d
n=25, Σ −$3.11, mean −$0.12/trade, sd $2.77, se_cl $0.53, trades/day 35 → projected daily SR −0.28 (± ~1). The positive total of +$8.50 comes from families that are stopped (P1 +13.73) or superseded (P2 +4.10) plus P12 +6.11.

### 2.2 Correlation structure (measured, replica 1 Hz since reset)
| pair | ρ 1-min returns (n) | ρ 15-min returns (n) |
|---|---|---|
| BTC–ETH | 0.883 (470) | 0.936 (27) |
| BTC–SOL | 0.813 (445) | 0.856 (24) |
| ETH–SOL | 0.859 (456) | 0.906 (20) |

Consequence: BTC/ETH/SOL trades on the same `close_ts` are one bet (effective n ≈ 1.2 of 3). P12 and P13 on the same 15M market (today 14:30Z: P12 SOL +1.41 and P13 SOL +4.02 on KXSOL15M-1030, P12 ETH −3.97 on the same close) are the same bet — perfectly correlated when on the same side. The risk ledger's `max_cost_per_settlement_ts_group` = $30 (3 × lot 10) treats them as independent; they are not.

### 2.3 Shrunk edges and Kelly fractions (EDGE_PLAN C1: τ = 3c prior; k = 0.25·min(1, e*/(2·se*)); trade only if e* − 1.96·se* > 0)
| family | ē c/ct | se_cl | e* | se* | e* − 1.96 se* | k | allocation |
|---|---|---|---|---|---|---|---|
| P10 BTC hourly | −22.4 | 18.7 | −0.6 | 2.96 | <0 | 0 | **scored-only** (and pause rule below) |
| P12 pooled 15M | +9.6 | ~8 | +1.2 | 2.8 | <0 | 0 | scored-only by C1; keep paper only to reach n |
| P13 (books-synthetic, §4.1: +9.9 ± 3.6, 13 mkts) | +9.9 | 3.6 | +4.1 | 2.3 | −0.4 | 0.11 (½ of quarter-Kelly, 1–2σ) | **only family with a non-zero k**; lot 10 paper stays |
| P2 s7final | +5.7 | 11.1 | +0.4 | 2.9 | <0 | 0 | stop (superseded by P13-BTCD) |

Kelly-optimal weights with the measured correlations (f = Σ⁻¹μ on per-$ returns): the only positive-mean, measurable-variance family after shrinkage is P13, so the "portfolio" optimum is P13 alone at k ≈ 0.11 of bankroll per independent `close_ts`, with the three assets on one `close_ts` sharing a single $-budget (ρ ≈ 0.9 ⇒ divide by ~2.7, not 1). Everything else has μ* ≤ 0 → weight 0 (scored-only). Per-$ return stats for the record: P12 mean +0.084/$, sd 1.33 (independent-Kelly f = μ/σ² = 0.05); P10 −0.50/$, sd 0.94; P2 −0.46/$, sd 1.05; P1-old +1.89/$, sd 3.49 (6 trades, longshots).

---

## 3. Per-family verdicts: what supports continuing, the binding constraint on EV, the single highest-EV non-refit change

| family | support for continuing | binding constraint on EV (not hit rate) | highest-EV non-refit change |
|---|---|---|---|
| **P10 hourly T-3.5..2.5 (BTC)** | None in the tape: 2/7, −22c ± 19; hindcast prior +5.8c at T-1 was for a different window; model-vs-mid Brier not yet measurable (12 hours). Pause rule is "CI upper < 0 at n ≥ 60"; upper CI now = +14c, so it continues formally. | **The market is right at T-3 when it disagrees with us**: our 7 trades were taken at mid 0.10–0.50 against a book with 1k–1.7k resting (`avail_at_touch` 1003, 1693), i.e. we are the taker against informed MMs who also see Coinbase. Book-imbalance at T-3 has zero predictive residual (§4.3: slope −0.004 ± 0.079, 139 rows / 12 hours). The constraint is information, not threshold. | Convert to **scored-only** now (keeps the P10/P11 Brier test running at zero P&L cost; 7 trade-hours/day of −3c expected is pure noise). Not a refit: it changes stake, not rule. Re-enable only if books-Brier < mid at 95% on ≥100 hours (its own P10 ETH/SOL rule applied to BTC). |
| **P12 15M final-minute favourite (3 assets)** | +6.11 on 17 (+9.6c ± 8); BTC 4/5, ETH 3/5, SOL 3/7. The information content is real but comes from the same partial-TWAP projection as P13 (§4.1); P12 just applies a model threshold and takes the ask on both favourites and dogs (avg px .37–.66, i.e. it buys *dogs* half the time: 8 of 17 at ≤ .45). | **It trades the contested cases where the projection is uncertain**: all 7 SOL trades were at |d| < 2 bp (SOL basis sd 0.59 bp) → 3/7. P12's P&L is P13's edge diluted by dog trades. Not fixable by threshold without refitting. | Keep running unchanged for its own n (P12 says 300/asset) but **cap it at scored-only on markets where P13 has already taken a position on the same `close_ts`** (same bet twice counted as two). Zero-refit: a ledger check, not a model change. |
| **P13 final-minute projection taker** | Strongest mechanical evidence in the repo: sign(proj−K) at |d| ≥ 1 bp = **99.5% (190 snaps / 9 mkts), 100% (214/15), 100% (2,627/38)**; contested subset (fav ≤ .95) 98.2–100% (57+15+16 snaps). Books-synthetic P13 (first qualifying snap per market): **13 opportunities / 105 markets, 12/13, +9.9c ± 3.6, avg px .824**. One live trade +4.02. | **Opportunity supply**: only 12.4% of 15M markets ever show fav ≤ .95 with |d| ≥ 1 bp inside T-55..T-10 (US hours 12–16Z: 0–15% of snaps; 05–11Z: 6–29%). Of the 13, **6 were at px .92–.96 and the sizing floor refuses them**: `p_side = max(p, .95)` vs `c = .96 + fee` → "no edge after shrink" (16:44Z BTC log) — the registered rule (ask ≤ .95) is unexecutable at ask .94–.95. Plus 1 STALE at latency 1.25 s (BTC 1245, +7.6c missed). | **Make the registered rule executable** (item 1 in §5): belief floor .97 (measured ≥ .995), latency guard 1.5 s with the price re-check as the real protection. Not a refit: the side rule, D_MIN, window, lot are unchanged; the sizing floor was an inconsistency with the rule as written. Extending the window earlier is **not** a fix: spot-only sign at T-2..T-1 is 55–87% at |d| < 8 bp and the synthetic P&L there is −5 to −25c (§4.1b); the market prices the remaining variance correctly. |
| **Index runners (KXINXU/KXNASDAQ100U)** | Nothing: 0 rows in 3 sessions (restarts at 23:37Z, 00:28Z, 14:43Z; window T-8..T-2 before 20:00Z). | Observability: no rows means no Brier, no decision. P5 cannot start. | Confirm the runner writes scored rows today 19:52–19:58Z (one session); if still 0 rows after today, stop the process (API load, no information). |
| **Weather / rain J1** | Locked states: 0 false locks on 5,458 resolved locked rows / 146 tickers → the verifier's lock logic is sound. But **gap ≥ 5c on a locked state with size ≥ 20: 1 row of 4,038** (KXLOWTAUS-27-T56 at local hour 0, ask .01 → a day-rollover artefact to audit, not an edge). Morning-low hours 10–13 local now observed (294 locked rows): gaps 0. Non-locked probabilities are worse than mid (Brier .079 vs .048; e.g. KXLOWTBOS-27-T44 p_model .99 vs market .02 with METAR min 47 at 13:00 local — `p_lower=0.05` is far too low for a late-October evening). Rain: `rain-j1` writes no scoreboard rows (still). | **Supply of mispriced locked states ≈ 0** at the hours we observe. The capacity question (P4/P7) is answered negatively so far for 10–13 and 17–23 local; 14–16 local still unobserved (0 rows). | Keep the recorder (cheap), stop expecting P&L; fix `rain_verify` to write rows (P8 is unmeasurable); audit the hour-0 "lock" (date boundary) before Nov 1 DST. |
| **Sports S6 / J3 / J4** | S6 BSS vs mid −0.27 on 6 games; J3 0 resolved; J4 1 row. | Nothing to evaluate; months to resolution for J4 season markets. | Keep S6 as observer at the current poll; no build time. |

---

## 4. New signals: tests, effect sizes, causal-timing safety

All tests use data available at decision time (replica rows timestamped at write; OKX candles are exchange-timestamped and *older* than the replica row they are paired with; Kalshi tape timestamps are exchange-side). "n" is rows, clusters in parentheses.

### 4.1 Projection sign (what P13 already uses) — measured properly, 15M, T-55..T-10 s
| \|d\| bp | sign acc (se) | n (mkts) | contested (fav ≤ .95) acc | n | market-disagrees → proj right |
|---|---|---|---|---|---|
| 0–0.5 | .794 (.123) | 107 (8) | .758 | 91 | **1/14** |
| 0.5–1 | .863 (.140) | 80 (9) | .831 | 65 | **1/11** |
| 1–2 | .995 (.006) | 190 (9) | .982 | 57 | 0/1 |
| 2–4 | 1.000 | 214 (15) | 1.000 | 15 | — |
| ≥4 | 1.000 | 2,627 (38) | 1.000 | 16 | — |

Reading: below 1 bp the *market* beats the projection (when they disagree the market is right 23/25) — D_MIN = 1 bp is exactly where the basis noise (official/replica TWAP basis sd 0.51/0.43/0.59 bp for BTC/ETH/SOL 15M, means +0.29/+0.62/+1.23 bp, n 29/29/30; hourly +0.45/+0.65/+1.18, n 51/48/38) stops mattering. BASIS_BP .44/.7/1.2 is confirmed, not refit.

**4.1b Earlier entry (spot-only sign, T-2..T-1, s7p15 books):** |d| < 2 bp: 55–60% acc, synthetic −8 to −15 c/ct (n 48–163); 2–4 bp: 66–75%, −9 to −25c; 4–8 bp: 87–98% but contested only 19–61% and −5 to −11c; ≥ 8 bp: 100% but 1–4% contested (+5 to +8c, n 15 mkts 8). **Verdict: no window extension**; the market prices 60–90 s of variance correctly.

### 4.2 OKX perp (BTC-USDT-SWAP, 1-s candles, 59,400 s) → our replica / Coinbase mid — **real, large, causal-safe**
| k | corr(perp past k s, replica next k s) | reverse | contemp. | β | n | se | sign agree when \|perp move\| > 2 bp |
|---|---|---|---|---|---|---|---|
| 1 s | .353 | .046 | .099 | .33 | 41,407 | .005 | .761 (142) |
| 2 s | **.677** | .053 | .245 | .56 | 41,961 | .007 | **.955 (507)** |
| 5 s | .438 | .070 | .652 | .38 | 42,131 | .011 | .801 (2,351) |
| 10 s | .276 | .051 | .827 | .25 | 42,199 | .015 | .692 (6,023) |
| 30 s | .112 | −.010 | .937 | .11 | 42,136 | .027 | .565 |
| 60 s | .057 | .002 | .966 | .06 | 42,039 | .038 | .540 |
| 5 s, final 60 s only | .432 | | | | 2,981 | .041 | .845 (110) |

Asymmetry (lead .68 vs reverse .05) is unambiguous. Part of it is our replica's own REST latency (1–2.5 s joins), but that is precisely the latency our projection suffers, so the perp is a free 2–5 s look-ahead on the `r·spot` term of proj. Perp–replica basis: mean +13.55 bp, sd 1.2 bp (n 49,833); 1-min basis change → next-1-min spot: corr .138, β .51 bp/bp, n 35,759, se .041 (minor, 2% R²; funding is 8-hourly and irrelevant at 15 min).

**Perp-adjusted projection on BTC 15M snaps (1,109 snaps / 36 mkts)**: spot replaced by `okx[t] / median(okx/replica, prior 300 s) × (1+BASIS)`; plain vs adjusted sign accuracy — |d| 0–0.5: .857 → **1.000** (28 snaps / 2 mkts); contested fav ≤ .95: .927 (se .061) → **1.000** (55 / 6); flips (adjusted sign ≠ plain): 4 snaps / **1 market**, perp right 4/4. Effect direction is right; **n_markets = 1 for the decisive cases** → pre-register, do not claim. Spec in §5 item 2.

### 4.3 Book-depth asymmetry (imb = (bid_size−ask_size)/(sum)) → residual (y − mid) — **no signal**
| cell | tercile means (lo/mid/hi) | slope β (se) | n (clusters) |
|---|---|---|---|
| 15M T-90..T-10 s, contested | −.024 / −.093 / −.084 | −.041 (.029) | 783 (20) |
| 15M T-45..T-10 s | −.014 / +.028 / +.004 | −.005 (.029) | 165 (10) |
| hourly T-3.5..T-2.5 | +.055 / +.008 / +.062 | −.004 (.079) | 139 (12) |
| hourly final minute | +.36 / +.02 / +.21 | −.066 (.050) | 46 (5) |
Confirms EDGE_PLAN B3 (7 h) with 12–20 hours. Drop.

### 4.4 Kalshi tape taker-flow in the final 90 s (33 contested 15M markets, median 35–39k contracts per window) — **anti-informative**
| window | β(y − last px on flow) (se) | flow sign agrees w/ outcome | last price side agrees | flow ≠ price side: flow right |
|---|---|---|---|---|
| T-90..T-45 | .23 (.15) | .848 | .970 | **0/4** |
| T-60..T-30 | .05 (.05) | .606 | 1.000 | **0/13** |
| T-45..T-15 | .005 (.016) | .364 | 1.000 | **0/21** |
Late takers buy the losing side (dog-buyers). The tape's last price at T-30 was right 33/33; P13's edge is price (buying .82–.95 for a ≥ .995 event), never direction. A "fade the late taker flow" rule is the same bet as P13 with worse timing — refused as a refit.

### 4.5 Cross-asset lead-lag BTC → ETH/SOL (replica 1 Hz) — **not exploitable**
k=1 s: BTC→ETH .232 but ETH→BTC .348 (n 29.7k); k=2 s .171 vs .270; k=5 s .077 vs .210; k=30 s −.015. Symmetric-or-reversed ⇒ it is replica polling autocorrelation, not price discovery. Final-60-s sign agreement when |BTC move| > 1 bp: 57–65% (n 58–518). The OKX perp (4.2) dominates it for all three assets (OKX has ETH/SOL swaps; untested here, spec in §5).

### 4.6 First-minutes drift after the opening average is fixed (replica, 55 windows/asset) — **random walk**
β(final d on first-minute d1): BTC 1.52 ± .62, ETH 1.13 ± .66, SOL 1.01 ± .55 (RW predicts 1.0); sign agreement 54–60% with sd(d1)/sd(dy) = .21 predicting ~.57. Nothing.

### 4.7 Sigma: EWMA(.94) vs realized, DVOL — **EWMA is fine; DVOL untestable at this n**
Hours where books logged σ: realized/EWMA ratio BTC .95 ± .07 (4 h), ETH 1.09 ± .09 (6), SOL 1.09 ± .10 (4). Deribit DVOL 37.3–38.3 ⇒ 5.1–5.3 bp/min implied vs 2.2–4.2 realized (2 hours only; the public endpoint returned 2 hourly points). Implied/realized ≈ 0.4–0.8 is the normal variance premium; it would matter only for P10-type pricing, which is scored-only. Spec: log `dvol_bp_min` hourly; test corr with next-hour realized at n ≥ 100 h before any use.

### 4.8 Time-of-day / US open (15M, share of T-50 s snaps with fav ≤ .95)
05Z .26 (19) · 06Z .14 (35) · 07Z .09 (45) · 08Z .08 (51) · 09Z .06 (64) · 10Z .25 (12) · 11Z .29 (73) · 12Z .00 (37) · 13Z .00 (36) · 14Z .14 (92) · 15Z .15 (164) · 16Z .05 (114). Opportunity supply is not obviously higher in US hours; too few hours to rank. Log and wait (P11-style, no sizing change).

### 4.9 Polymarket 15m/5m vs Kalshi 15M — **different settlement variable; only a direction cross-check**
Polymarket BTC up/down 5m/15m now settle on the Chainlink **full-window TWAP vs the open print** (`btc-updown-15m-<ts>`, resolution `btc-usd-twap-60s-streams`), not final-60-s vs prior-60-s. Mid-window divergence is structural, not mispricing. Not tested (no payoff mapping); recorder keeps the daily ladder only. Drop as a P13 input.

### 4.10 Not tested, noted
- **Kalshi websocket `cfbenchmarks_value` (index value + running `avg_60s_data`) is the settlement feed, free with an API key.** It removes the replica basis (sd 0.5 bp) entirely → D_MIN could go to ~0.3 bp, roughly tripling P13 supply (|d| 0.3–1 bp snaps are 187 vs 404 at ≥1 bp). Requires an account; parked.
- Strike-ladder consistency: 0 violations / 11,815 pairs already (EDGE_PLAN B4). Not repeated.
- Macro prints / weekend regimes: 0.71 days of data; untestable.

---

## 5. Ranked implementation list (≤ 6; ordered by expected Sharpe gain ÷ refit risk; implement verbatim)

| # | change | file / function | exact spec | logging fields | PREREG pass / pause |
|---|---|---|---|---|---|
| **1** | **P13 executable as registered**: belief floor and latency guard | `src/predhft/s7_live.py` `run()` lines ~286 and ~295 | (a) `latency <= 1.5` (was 1.0) in the `proj_gate` `ok` test; the re-check `ya2 <= 0.95 and yas2 >= lot` stays and is the real guard. (b) `p_side = max(p_side, 0.97)` (was .95) when `proj_gate` — measured sign accuracy ≥ .995 at \|d\| ≥ 1 bp; .97 still yields `kelly_contracts(.97, .96+.0027)` = 12 ≥ min_lot 5 at ask .95. No change to side rule, D_MIN, window, lot, ask ≤ .95. | add to the trade dict and NOFILL line: `floor_used`, `latency_guard_s`, `fav_at_decision`; add NOFILL reason key `kelly_zero` (count of SKIPs by `risk_size`) | Register as **P13 amendment 2026-10-27 (executability)**, same pass rule: n ≥ 60 pooled BTC+ETH, mean net > 0 at 95% market-clustered, hit ≥ 85%; pause if hit < 70% at n ≥ 20. Separately report the px .94–.96 sub-cell (expect +4..+7c, hit ≥ .98); if its hit < .95 at n ≥ 30 revert the floor to .95. |
| **2** | **Perp-adjusted spot for the projection (scored first, 1 week)** | new `src/predhft/perp_feed.py` (OKX `/api/v5/market/candles?instId={BTC,ETH,SOL}-USDT-SWAP&bar=1s&limit=5` or `/market/history-trades`, 1 Hz, writes `data/perp/<P>.latest.json {t, px, basis_med_bp}`; basis = median(perp/replica) over prior 300 s matched seconds); `s7_live.run()` after `spot` is set: `spot_perp = px / (1 + basis_med_bp·1e-4) · (1 + BASIS_BP)`, `proj_perp = (1−r)·partial + r·spot_perp` | **Scored-only**: compute both `d_bp` and `d_perp_bp`; trading side still from `d_bp`. Skip if perp row age > 2 s. | books snap: `spot_perp`, `d_perp_bp`, `perp_age_s`, `perp_basis_bp`; trade dict: same. | **P14 (register before enabling)**: on T-55..T-10 snaps where sign(d) ≠ sign(d_perp) OR \|d\| < 1 ≤ \|d_perp\|, count markets and outcomes. Pass to trade on `d_perp` = perp-side right in ≥ 95% on ≥ 40 such *markets* (cluster = market) AND overall sign accuracy at \|d_perp\| ≥ 1 bp ≥ .99 on ≥ 100 markets. Then D_MIN on `d_perp` stays 1 bp (no lowering without its own cell). |
| **3** | **One bet per `close_ts` across P12/P13 and across assets** | `src/predhft/risk.py` `CFG` + `Ledger.cost_settlement()` | add `CFG["max_cost_per_settlement_ts_crypto"] = 15.0` applied when `series` ∈ {KXBTC15M, KXETH15M, KXSOL15M, KXBTCD, KXETHD, KXSOLD} with `group=None` (all families) — ρ ≈ .9 ⇒ three lot-10 trades at the same close are one $30 bet; $15 keeps the per-close risk at the single-market cap. Also: P12 (`s7p15-*`) `risk_size` returns 0 with reason `"p13_holds_close_ts"` if any `s7p13-*` open event exists within 60 s of `close_ts` (P12 keeps scoring every snap). | SKIP reason strings as above; daily summary: `crypto_close_ts_exposure_max` | Not a model change; no PREREG entry needed beyond a note under P12/P13 ("evaluation unchanged; P12 trades only closes P13 did not take"). |
| **4** | **P10 BTC → scored-only; stop P2 s7final** | process args: `s7p10-KXBTCD` add `--scored-only`; kill `s7final-KXBTCD` | P10 keeps writing `sigma-lo/hi` and books rows (P10/P11 Brier tests continue at zero P&L). `s7p13-KXBTCD` already covers the hourly final minute. | — | PREREG append: "P10 BTC paper trading paused 2026-10-27 at n=7 (−22c ± 19), evaluation continues as scored; P2 superseded by P13-KXBTCD." |
| **5** | **Supply logging for P13 (so the binding constraint is measured, not guessed)** | `s7_live.run()` NOFILL block | per market write one row to `data/<fam>/supply.jsonl`: `{close_ts, min_fav, t_min_fav, d_at_min_fav, first_t_d_ge_1bp, fav_at_first_d_ge_1bp, pinned_at_T55, utc_hour}` | as listed | Not a rule; feeds the P11-style hour-of-day table. Decision rule pre-registered: no hour-of-day sizing until an hour bucket has ≥ 40 opportunities. |
| **6** | **Replica cadence**: shave the 1–2.5 s join latency the perp test exposed | `src/predhft/index_replica.py` `sample()` / `loop()` | thread join timeout 2.5 → 1.2 s (venue slower than 1.2 s is dropped from that second's median; `rep` needs ≥ 2 venues as now); record `n_venues` and `max_age_s` per row | row fields `n_venues`, `sample_ms` | None (infrastructure). Verify basis sd does not rise (BTC15M .51 bp baseline) over 24 h. |

Refused (refits of the same tape): any threshold/σ/ν change for P10 or P12; a "fade late taker flow" rule (4.4); extending the P13 window earlier (4.1b); hour-of-day sizing now (4.8); book-imbalance gates (4.3); cross-asset lead (4.5); first-minute momentum (4.6); Polymarket inputs (4.9).

---

## 6. STOP (noise and API load)

| stop | why | API savings |
|---|---|---|
| `s7final-KXBTCD` (P2 fixed-thr final minute) | superseded by `s7p13-KXBTCD` on the same strikes/minute; two runners polling the same 3 books in the final 90 s | ~3 req/s in the final minute of every hour |
| `s7p10-KXBTCD` paper trading → scored-only (not the process) | −22c ± 19 on 7; market right when disagreeing; EV from stake = 0 | none (scoring continues) |
| `s7index-*` if still 0 rows after today's 19:52–19:58Z session | 3 sessions, 0 rows | 2 processes |
| `s1-maker` stays stopped; `s7live*` stay stopped | −13.28 / superseded | — |
| `recorder/polymarket.jsonl` ladder | not a P13 input (different settlement variable); daily ladder unused | 1 req / 30 s |
| P12 polling cadence outside T-1.5 | P12 books rows at T-2..T-1.5 (545 rows) are the "earlier entry" test and it is negative (4.1b); keep snaps only inside T-1.5 | ~30% of P12 calls |
| `weather_j1` non-locked probabilistic rows (keep locked rows + first_lock) | Brier .079 vs mid .048 → they only pollute the family score; P4/P7 need locked rows only | none (same calls) |

Keep: replica (the whole edge), P13 ×4, P12 ×3 (for n), weather/rain recorders (fix rain rows), sports observer.

---

### Appendix — reproduction
`uv run python reports/sharpe/fam_stats.py` (§1), `fetch_results.py` (results cache), `signals_books.py` (§4.1/4.3/4.8), `signals_replica.py` (§4.5/4.6/4.7 basis), `signals_okx.py` (§4.2, DVOL), `signals_tape.py` (§4.4, 4.2 perp-proj). Data files: `reports/sharpe/{fam_stats,all_trades,results,signals_books,signals_replica,signals_okx_dvol,signals_tape_perp,okx_1s,tape}.json`.
