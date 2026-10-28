# TOURNAMENT — algorithm tournament for KX{BTC,ETH,SOL}15M outcome prediction (2026-10-28)

Status: **offline research + scored-only forward hook. Paper only. Nothing committed. No runner, risk, sizing or threshold code
changed; `src/predhft/sol_v1.py` untouched.** Code: `src/predhft/model_zoo.py`, `scripts/tournament.py`, `tests/test_model_zoo.py`.
Artefacts: `reports/tournament/` (`results_latest.json`, per-point predictions parquet, SPA bootstrap draws).

Owner question (Alon): "we may be using the wrong algorithms — test/simulate different algorithms on our historical data to see
which would have done best, then forward test that" (Markov chains, more complex predictive algorithms). This document is the
tournament that answers it **without** the usual way of fooling ourselves: one outcome definition, strict walk-forward, every
configuration counted, a multiple-comparison correction over the whole set, and economics only through the live policy's own
execution code. §0–§4 were written and frozen **before** any model was run (pre-declaration); §5 onward is filled in from
`reports/tournament/results_latest.json` afterwards and nothing in §0–§4 was edited after seeing results.

Pre-declaration written: **2026-10-28 14:35 PT**. Data end: 2026-10-28 21:28Z (14:28 PT).

---

## 0. The target variable (identical for every model)

Market: Kalshi KX{BTC,ETH,SOL}15M. Outcome YES iff the 60-s average of the official CF Benchmarks index (BRTI / ETHUSD_RTI /
SOLUSD_RTI) over the minute before close ≥ the 60-s average over the minute before open (strike `K`), tie = YES, strike
precision 2 dp (BTC/ETH) / 4 dp (SOL).

Outcome source, in priority order, recorded per window as `outcome_src`:
1. `kalshi`: exchange result (data/ts/results.json, only SOL closes already cached — no new fetch for the tournament);
2. `official`: both 60-tick averages reconstructable from `data/cfb/<A>.jsonl` (official 1 Hz feed, since 2026-10-27 22:33Z);
3. `proxy`: OHLC4 of the Coinbase 1-min candle spanning each minute (`candles.window_outcomes`), measured proxy error vs the
   official average: sd 0.8–1.0 bp, ≈5 % of labels flip (P17 §1); the error is symmetric and independent of the features.

Where the outcome is `proxy`, the strike used in every feature is the same proxy (`avg_open` OHLC4), so features and label are
mutually consistent. No model ever sees anything timestamped at or after its decision time (1-min candles: only candles with
open ts ≤ t−60, i.e. closed; official feed: only ticks with `value_ts ≤ t` **and** receipt time ≤ t; books: receipt ≤ t).

## 1. Two tapes

| tape | span | assets | decision points | market mid | comparator |
|---|---|---|---|---|---|
| **A — candles** | 2026-09-22 → 2026-10-28 21:28Z (36.9 days, ~3,540 windows/asset) | BTC, ETH, SOL | T−14, −10, −5, −2, −1 min (candle-quantised; T−30 s is not representable with 1-min candles) | none (no historical Kalshi quotes) | SOL-v1 candle analogue (`solv1_candle`, §2 B1) |
| **B — quotes** | official-feed period 2026-10-27 22:48Z → 2026-10-28 21:28Z; Kalshi quote path (`data/open_quotes/quotes.jsonl`, 30-s cadence open→T−60 s, since 10-28 04:30Z, 68 closes/asset) + final-two-minute 1 Hz books (`data/s7p13-KX*15M`, `data/s7p15-KX*15M`) | BTC, ETH, SOL (SOL primary; BTC/ETH reported separately and pooled) | the snapshot nearest (≤ 10 s) each of T−14, −10, −5, −2, −1 min, T−30 s; **plus** every snapshot for the economic replay | yes — two-sided books with spread ≤ 10 c (policy's own rule) | market mid (primary), SOL-v1 exact model (`solv1_exact`, B2) |

Tape A is where fitted models get their parameters (returns history) and where candle-only skill is measured; tape B is where
the only question that matters for money — *does any model know something the market mid does not* — is asked.

## 2. Model universe (every configuration listed here is run and counted; none is dropped afterwards)

Every parametric model maps into **the same settlement-average formula as the live policy** (`sol_v1.p_settle_ge`: observed
partial average `P` on `n` ticks, `m` remaining samples, exact discrete lead factor `V`, `p = Φ((nP + m(x_now + drift) − 60K)/(m·σ_1s·x·√V))`)
by supplying its own *(drift, remaining variance)*. Variance models supply σ; drift models supply the expected index move over
the horizon (σ from the plain realised-vol baseline); ML models output `p` directly. Per-minute variance forecasts are
converted to the per-second σ² used by the formula as the horizon average `(Σ_k σ²_k)/(60·h)` (declared approximation: the lead
structure weights early minutes slightly more).

**κ (variance inflation).** Baselines keep the policy's κ (1.0 inside T−2 min, 1.15 before). Every other variance model is run
in two declared variants: `k1` (κ = 1, the model's variance taken at face value) and `kf` (κ = rms standardised settlement
residual on the training fold, by horizon group final/mid/early — walk-forward, never from the test block).

| id | family | configurations (grid is the whole grid; nothing else was tried) | n cfg |
|---|---|---|---|
| **B0** `mid` | baseline | market mid where a two-sided book exists (tape B only) | 1 |
| **B1** `solv1_candle` | baseline | policy formula, σ_1s = realised 1-min vol over the last 15 closed minutes / √60, policy κ, no drift | 1 |
| **B2** `solv1_exact` | baseline | the live policy's `p_model` exactly (`realized_sigma_1s` over 900 s of the official feed, policy κ) — tape B only | 1 |
| **B3** `partial_logit` | baseline | P17 BASE: logistic on [partial_move_bp, partial_z, mins_left] fitted walk-forward | 1 |
| **V1** `rv_{15,60}` | variance | constant realised vol over the last 15 / 60 closed minutes × {k1, kf} | 4 |
| **V2** `ewma_{0.94,0.97}` | variance | RiskMetrics EWMA of squared 1-min returns, λ ∈ {0.94, 0.97} × {k1, kf} | 4 |
| **V3** `har` | variance | HAR-RV: log-linear regression of forward realised variance over the remaining horizon on log RV(15), RV(60), RV(360) minutes, one regression per horizon bucket, fitted walk-forward × {k1, kf} | 2 |
| **V4** `garch11`, `egarch11` | variance / sequence | Gaussian GARCH(1,1) and EGARCH(1,1) on 1-min log returns, QML fitted walk-forward on all prior returns (own MLE), filtered causally through the test block, multi-step variance forecast summed over the horizon × {k1, kf} | 4 |
| **V5** `mc_vol_{1,2,3}` | Markov | discrete Markov chain on the volatility regime (terciles of RV(5) with thresholds from the training fold), order 1–3, transition matrix from the training fold; expected variance over the horizon = Σ_k Σ_s P(s at k) · E[r² | s] × {k1, kf} | 6 |
| **V6** `hmm_{2,3}` | Markov (hidden) | Gaussian HMM on 1-min returns (hmmlearn, 2 / 3 states, fitted walk-forward), causal forward filter for the state posterior at t, regime-conditional **drift and variance** propagated with the transition matrix over the horizon × {k1, kf} | 4 |
| **V7** `bocpd` | change-point | Bayesian online change-point detection (Adams–MacKay, constant hazard 1/120 min, Normal–inverse-gamma on returns), predictive variance = run-length-posterior mixture × {k1, kf} | 2 |
| **V8** `cusum` | change-point gate | CUSUM on squared standardised returns (k = 0.5, h = 5 in σ² units); if an alarm fired in the last 10 min use RV(5) else RV(60) × {k1, kf} | 2 |
| **D1** `mc_sign_{1,2,3}` | Markov | Markov chain on the sign of the 1-min return (order 1–3, training-fold transitions and conditional |r| means), drift = chained expected return over the horizon; σ = rv_15 | 3 |
| **D2** `ar_{1,3,5}` | sequence | AR(p) on 1-min log returns (OLS, walk-forward), multi-step cumulative drift; σ = rv_15 | 3 |
| **D3** `kalman_ll_{0.1,1.0}`, `kalman_lt` | sequence | local-level Kalman filter on log index with signal/noise q ∈ {0.1, 1.0} (filtered level replaces x_now), and a local-linear-trend filter (q = 0.1, filtered slope = drift); σ = rv_15 | 3 |
| **M1** `logit_{C0.1,C1}` | ML | L2 logistic (median-impute, standardise) on the compact feature set F (below) | 2 |
| **M2** `hgb_{d2,d3}` | ML | HistGradientBoosting, depth 2 / 3, 150 trees, lr 0.05, min leaf 50, on F | 2 |
| **M3** `rf` | ML | RandomForest 300 trees, min leaf 50, max features √, on F | 1 |
| **E1** `ens_avg` | ensemble | equal-weight average of logits of {solv1_candle, garch11_k1, hmm_2_k1, hgb_d3} | 1 |
| **E2** `ens_stack` | ensemble | logistic stacker on the same four logits, trained only on **previous folds' out-of-sample** predictions (time-ordered) | 1 |
| | | **total model configurations** | **48** (+ `mid`) |

Compact feature set **F** (all from data available at t): `z_base` (B1's standardised distance to the strike), `partial_move_bp`,
`secs_left`, realised vol over 1 / 5 / 15 closed minutes (bp), `rv_ratio` (rv5/rv60), BTC and ETH 1- and 5-min returns and
BTC partial move (cross-asset co-move; for BTC itself ETH/SOL are the references), `hour_utc` as sin/cos. **Excluded, declared:**
order-flow imbalance (`data/context/*`, 1 day of history — cannot be trained walk-forward), book imbalance / mid as ML inputs
(tape B has ~90 closes per asset — too few to train; a model that *blends* the mid is a separate question, answered by the
policy's blend weights in SOL_V1 §4), perp lead (rejected in SOL_V1 §3 at R² 0.003).

Configurations are counted per *model* (48); the multiple-comparison universe in §4 additionally multiplies by the four
time zones (pooled, early, mid, final), i.e. **192 strategies** enter the SPA test, so that "the model that wins in the one zone
where it wins" is penalised for the zone search too.

## 3. Walk-forward protocol

* **Tape A folds.** UTC days 1–10 (09-22 → 10-01) are training-only. Test folds = consecutive **2-day blocks** from 10-02 to
  10-28 (14 folds, the last one partial). Before each fold every model is refit on *all* returns / windows strictly before the
  fold's first second (expanding window). Filters (GARCH, EWMA, HMM forward, BOCPD, CUSUM) run causally through the test block
  with the fold's parameters. The stacker is trained on the OOS predictions of earlier folds only, so its first scored fold is
  fold 3.
* **Tape B folds.** The official-feed closes (10-27 22:48Z → 10-28 21:15Z, ~90 per asset) are split into **10 consecutive
  blocks of closes** (same boundaries for the three assets). Parametric models are refit on candle returns strictly before the
  block's first close; ML/stacker use tape-A points strictly before that instant. Nothing in a block is used for that block.
* Reported splits: tape A OOS (all 14 folds pooled; per-asset and pooled), tape B (all 10 blocks; per asset and pooled).
  There is no "in-sample" column anywhere in the results: every number is out-of-fold.

## 4. Scoring (pre-declared)

**Proper scores.** Brier and log loss (p clipped to [1e-4, 1−1e-4]), per point. **Calibration slope** = slope of logit(y) on
logit(p) by logistic regression (1 = calibrated, < 1 over-confident). **Zones**: final ≤ 120 s, mid 120–600 s, early > 600 s
(the policy's own split), and pooled.

**Paired comparison.** On tape B the primary quantity is `Δ_k = Brier(mid) − Brier(model k)` averaged within close (one number
per close per model so that the 1-Hz final-minute rows cannot out-vote the 30-s rows) and then across closes; SE clustered by
**close** and by **hour block** (both reported; the larger is the one used in words). On tape A the same against
`solv1_candle`. Positive Δ = model better.

**Multiple comparison (the guard).** Hansen's **SPA** test and White's **Reality Check** over the full universe of §2 (48 models
× 4 zones = 192 strategies, loss = per-close Brier differential vs the benchmark), **stationary bootstrap over closes**
(Politis–Romano, mean block length 4 closes = 1 h, **B = 4000**, seed 20261028), test statistic `max_k √n·Δ̄_k / ω̂_k`.
Reported: the best strategy, its raw one-sided p, the RC p, the SPA consistent p (Hansen's recentring with the
√(2 log log n) threshold), and the lower/upper SPA p-values. A model "survives" only if **SPA p ≤ 0.05 on tape B vs the mid**.
Secondary SPA: tape A vs `solv1_candle` (is anything better than the exact-formula candle baseline at all?).

**Economics (descriptive, not a selection criterion).** SOL tape B only. Each model's `p` is injected in place of `p_model` in
a replay that calls the policy's own pure functions (`p_used_from`, `entry_ev`, `paper_fill`, `hold_or_exit`, `kelly_size_fn`,
same `CFG`, same gates and fee function; execution terms z/sd for the adverse haircut from the policy's own decomposition so
that *only the probability differs*). Two variants: policy blend as deployed (`blend`), and model alone (`raw`, w = 1). Outputs:
trades, contracts, net $, c/contract with close-clustered SE, hit, max drawdown, Sharpe per 15-min interval incl. idle. Economic
numbers are reported for every model but **cannot promote a model**: with ~90 closes they have SEs of ±10 c/ct.

**Promotion rule (pre-declared).** A model is promoted to *forward candidate* only if on tape B it has SPA p ≤ 0.05 vs the mid
**and** its paired Δ in the zone where it wins is ≥ 2 cluster-SE (both clusterings) **and** its tape-A skill vs `solv1_candle`
is positive (sanity: it is not fitting quote-period noise). A forward candidate's probabilities are logged scored-only next to
SOL-v1's on every live SOL evaluation (§7). It replaces `p_model` in the policy only after ≥ 100 forward closes where its forward
paired Brier vs the mid in the T−2..T−10 zone is better by ≥ 1 cluster-SE **and** better than SOL-v1's own forward paired Brier
— same rule as SOL_V1 §10. If no model survives §4, the hook still logs the top-3 by tape-B pooled Δ (scored-only, no trading
change) so that the negative can be re-tested on forward data without touching the policy.

---

## 5. Results (run 2026-10-28 15:08–15:21 PT; nothing in §1–§4 was edited afterwards — deviations are declared in §5.0)

Artifacts: `reports/tournament/results_full.json` (everything, incl. per-strategy SPA t-stats and the economics for all 47
models), `predictions_full.parquet` (every out-of-fold probability, 38 715 tape-A points + 33 773 tape-B snapshots),
`tables_full.md` (full ranked tables T1–T5, all 47 configurations; regenerate with `scripts/tournament_tables.py`),
`summary_full.md`, `tapes.pkl` / `preds_full.npz` (stage checkpoints), `forward_models.pkl` (the hook's fitted models),
`full_run.log`. Runtime: 7.2 min for predictions + economics on CPU, 1 min for the SPA stage.

### 5.0 Declared deviations from §1–§4 (found while making the protocol run; all in the conservative direction)

1. **Configuration count is 47, not 48.** The §2 table's per-row counts sum to 48 only because `mid` (B0) was counted; the
   zoo has **47 model configurations + the market mid**. The SPA universe is therefore 47 × 4 zones = **188 strategies**
   (184 when the benchmark is `solv1_exact`, 180 on tape A where `solv1_exact`/`ens_stack`-early have no data). `n_configurations`
   in `results_full.json` is the machine count. Nothing was added or removed from the grid after the first run.
2. **Training labels must be *known* before a fold starts.** §3 said "refit on all windows strictly before the fold's first
   second"; the implementation requires the training window's **close** ≤ fold start (not merely its decision time), otherwise
   up to 14 labels per asset would carry price information from inside the test block's first windows. Same rule for the
   stacker's history. (Test `test_fit_context_training_returns_end_before_fold_start`.)
3. **SPA studentisation.** On null data (K = 40 iid Gaussian strategies, n = 120 closes, block 4) the §4 statistic
   `max_k √n·Δ̄_k/ω̂_k` rejected at the 5 % level **14–17 % of the time**: with ~90 closes the block-bootstrap ω̂_k is noisy and
   the max over K picks the under-estimated ones. White's original **non-studentised** statistic `max_k √n·Δ̄_k` (same
   stationary bootstrap, same SPA-consistent recentring) has size 2–3 % on iid null and 8 % under AR(1) ρ = 0.3. **Both are
   reported; the non-studentised SPA-c p decides.** (Tests `test_spa_null_data_gives_large_p_and_rejects_rarely`,
   `test_spa_detects_a_real_edge`.) A positive by the studentised test alone would not have been accepted; as it happens the
   decision is the same under both.
4. **Forward hook content.** §4 said "top-3 by tape-B pooled Δ" if nothing survives. The hook logs those three **plus the
   tape-A champion** (the one comparison where anything survived the correction), four scored-only models in total.
   Output path is `data/tournament/forward.jsonl` (not `data/sol-v1/`), so nothing is written into the live policy's directory.
5. **Decision-time subset on tape B.** The T−30 s snapshot exists only for the final-minute book days (BTC/ETH 32 closes; SOL 0
   with a two-sided ≤ 10 c book — the SOL book is one-sided in the last 30 s almost always), so the SOL "final" zone is
   T−2 min and T−1 min.

### 5.1 Headline

* **Against the market mid (the question that matters) nothing wins.** SOL, 88 closes with a two-sided book, 345 paired
  decision snapshots, 188 strategies: best studentised strategy `ens_stack|mid-zone` Δ = +0.0037 Brier (t = 1.55, naive
  one-sided p = 0.065); best by mean `bocpd_k1|mid-zone` +0.0059. **SPA-c p = 0.66 (studentised), 0.81 (non-studentised);
  RC p = 0.71 / 0.95.** Pooled three assets (92 closes): best `bocpd_kf|mid` +0.0029, t = 1.30, **SPA-c p = 0.69 / 0.85**.
  No zone survives on its own either (best zone p = 0.33, SOL mid-zone). The mid is a strong forecaster here: the live
  policy `solv1_exact` itself is Δ = +0.0015 (t = 0.5) vs the mid on SOL and −0.0015 (t = −1.0) pooled.
* **Against the live policy's own probability (`solv1_exact`) nothing survives either.** Best `garch11_kf|early` Δ = +0.0037,
  t = 2.65, naive p = 0.006 — the kind of number that would look like a finding without the correction — **SPA-c p = 0.15
  (studentised), 0.56 (non-studentised)**; 184 strategies, 92 closes, 67 of them with a positive mean.
* **On candles alone (tape A, 36 days, 2 581 closes/asset, 14 walk-forward folds) the fitted-κ variance models do beat the
  exact-formula candle baseline, and it survives the correction**: pooled best `egarch11_kf` Δ = +0.0009 Brier (t = 6.0 by close,
  6.3 by hour), SOL-only best `ens_stack|mid` +0.0022 (t = 4.3); **SPA-c p = 0.000 studentised, 0.011 / 0.008
  non-studentised** (RC non-studentised p = 0.51 — the RC is dominated by the high-variance Kalman strategies' bootstrap
  noise; SPA-c removes them). The effect is stable across halves (dev1 folds 1–7: +0.0010 t 5.1; dev2 folds 8–14: +0.0007 t 3.6)
  and across assets (BTC/ETH/SOL all positive). **It is tiny** (0.7 % of the Brier) and it is a *calibration* effect: the
  candle baseline's slope is 1.17 (over-confident at the T−2..T−10 horizons) and every `_kf` model's is 1.04 — the fitted κ
  per zone is doing the work, not the GARCH/EGARCH/HAR dynamics (rv_15_kf, ewma_kf, garch_kf, egarch_kf are within 0.0001 of
  each other). On tape B the same models are +0.001–0.003 vs the mid with t ≈ 1 — the candle-only edge over the candle baseline
  does not translate into an edge over the market.
* **Markov chains, HMM regimes, change-point gates, drift models, ML: nothing.** Sign-Markov and AR drift: Δ ≈ +0.0001 (t ≈ 1)
  on tape A, indistinguishable from the baseline. Markov-vol chains (`mc_vol_*`): −0.0006 (t = −2.5), *worse* than a 15-min
  realised vol. CUSUM gate: −0.0001. HMM: −0.0004 (`hmm_2_kf`); on tape B +0.002 (t 0.7) vs mid. BOCPD: +0.0005 on A (t 3.6, the
  `_kf` version), top of tape B by mean (+0.0033, t 1.2) — the best candidate, still nowhere near surviving. Kalman local-level
  on the log index is **badly** negative (−0.010 to −0.054 Brier): replacing the last price by a smoothed level in a
  settlement-average market throws away the most informative number. ML on compact features (logit/HGB/RF): +0.0000 to +0.0004,
  t ≤ 2; RF is exactly the baseline. `partial_logit` (P17 BASE) is −0.0006 vs the candle baseline, −0.0033 (t −1.9) pooled vs
  the mid. The stacker is the best SOL candle model (+0.0022 in its zone) because it averages the four members' calibration.
* **Economics (descriptive only).** SOL tape B, 92 closes, the policy's own execution path with each probability injected,
  blend as deployed: `solv1_exact` 25 trades, +0.3 ± 5.5 c/ct; best-of-47 `ewma_0.97_kf` 17 trades, +18.9 ± 7.8 c/ct, which is
  what the maximum of 47 noisy numbers looks like and is **not** evidence; the four hook models are +3 to +8 c/ct ± 7–9 (15–23
  trades). Raw (no blend) everything is within ±3 c/ct of zero with SE 2.6–3.1. No economic number here is distinguishable from
  zero, as pre-declared.

**Decision: no model is promoted. The SOL-v1 probability model stays as it is.** Four models are logged scored-only (§7).

### 5.2 Ranked table — tape B (official feed + books), SOL, paired vs the market mid; top-10 + baselines

Paired Δ = mean per-close [Brier(mid) − Brier(model)] on snapshots with a two-sided ≤ 10 c book (positive = better than the
market); t_c / t_h = Δ / SE clustered by close / by UTC hour. "SPA t" = studentised t in the SPA matrix (pooled-zone column).
Unpaired Brier / log-loss / slope are over all 478 SOL decision snapshots (incl. one-sided-book ones), so they are not
comparable with the mid's row (345 snapshots) — only the Δ columns are like-for-like. All 48 rows: `reports/tournament/tables_full.md` T2.

| # | model | family | SOL Brier | log-loss | cal. slope | SOL Δ vs mid | t_c / t_h | Δ final / mid / early (t_c) | pooled 3-asset Δ vs mid (t_c / t_h) | SOL Δ vs `solv1_exact` (t_c) | SPA t SOL / pooled |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | bocpd_kf | change-point | 0.1083 | 0.3191 | 0.98 | +0.0033 | 1.2 / 1.4 | +0.0020 (0.6) / +0.0055 (1.4) / −0.0006 (−0.2) | +0.0007 (0.5 / 0.4) | +0.0007 (0.7) | 1.37 / 0.48 |
| 2 | bocpd_k1 | change-point | 0.1081 | 0.3199 | 1.13 | +0.0028 | 0.9 / 1.1 | +0.0016 (0.4) / +0.0059 (1.2) / −0.0015 (−0.4) | −0.0006 (−0.4 / −0.3) | +0.0008 (1.1) | 1.01 / −0.37 |
| 3 | garch11_kf | variance | 0.1088 | 0.3202 | 0.97 | +0.0025 | 1.0 / 1.3 | +0.0019 (0.6) / +0.0046 (1.4) / −0.0013 (−0.4) | +0.0006 (0.4 / 0.4) | +0.0002 (0.2) | 1.27 / 0.44 |
| 4 | hmm_2_kf | HMM | 0.1086 | 0.3222 | 1.01 | +0.0021 | 0.7 / 0.9 | +0.0032 (0.7) / +0.0015 (0.4) / +0.0007 (0.2) | +0.0005 (0.3 / 0.4) | +0.0002 (0.2) | 0.88 / 0.36 |
| 5 | garch11_k1 | variance | 0.1086 | 0.3211 | 1.15 | +0.0019 | 0.6 / 0.8 | +0.0017 (0.4) / +0.0049 (1.0) / −0.0027 (−0.8) | −0.0009 (−0.5 / −0.5) | +0.0002 (0.5) | 0.76 / −0.58 |
| 6 | egarch11_kf | variance | 0.1095 | 0.3229 | 0.92 | +0.0019 | 0.8 / 1.0 | +0.0011 (0.4) / +0.0035 (1.3) / −0.0006 (−0.2) | +0.0004 (0.3 / 0.3) | −0.0005 (−0.4) | 1.04 / 0.35 |
| 7 | hmm_3_kf | HMM | 0.1087 | 0.3227 | 1.01 | +0.0019 | 0.6 / 0.9 | +0.0030 (0.7) / +0.0014 (0.4) / +0.0005 (0.1) | +0.0004 (0.3 / 0.3) | +0.0001 (0.1) | 0.81 / 0.29 |
| 8 | ens_stack | ensemble | 0.1096 | 0.3232 | 0.93 | +0.0019 | 0.8 / 1.2 | +0.0003 (0.1) / +0.0037 (1.4) / −0.0001 (−0.0) | +0.0003 (0.2 / 0.2) | −0.0007 (−0.4) | 1.08 / 0.19 |
| 9 | rv_15_k1 | variance | 0.1097 | 0.3236 | 0.96 | +0.0017 | 0.6 / 0.8 | −0.0001 (−0.0) / +0.0048 (1.1) / −0.0036 (−0.9) | −0.0003 (−0.2 / −0.2) | −0.0007 (−0.6) | 0.77 / −0.20 |
| 10 | ens_avg | ensemble | 0.1092 | 0.3223 | 1.06 | +0.0016 | 0.6 / 0.8 | +0.0009 (0.3) / +0.0046 (1.1) / −0.0026 (−0.8) | −0.0005 (−0.3 / −0.3) | −0.0003 (−0.4) | 0.73 / −0.34 |
| 12 | **solv1_exact** (B2, live policy) | baseline | 0.1090 | 0.3208 | 1.08 | +0.0015 | 0.5 / 0.6 | +0.0031 (0.9) / +0.0028 (0.6) / −0.0050 (−1.5) | −0.0015 (−1.0 / −1.0) | 0 | 0.61 / −1.07 |
| 18 | har_kf (tape-A champion, hook) | variance | 0.1100 | 0.3240 | 0.91 | +0.0013 | 0.5 / 0.6 | +0.0006 (0.2) / +0.0036 (1.2) / −0.0026 (−0.7) | −0.0003 (−0.2 / −0.2) | −0.0010 (−0.7) | 0.68 / −0.24 |
| 21 | **partial_logit** (B3) | baseline | 0.1113 | 0.3355 | 1.07 | +0.0011 | 0.3 / 0.3 | −0.0032 (−0.6) / +0.0036 (1.1) / −0.0007 (−0.2) | −0.0033 (−1.9 / −1.5) | −0.0019 (−0.8) | 0.37 / −1.63 |
| 22 | **solv1_candle** (B1) | baseline | 0.1099 | 0.3239 | 1.03 | +0.0010 | 0.3 / 0.4 | −0.0001 (−0.0) / +0.0052 (0.9) / −0.0054 (−1.3) | −0.0010 (−0.5 / −0.5) | −0.0009 (−0.8) | 0.38 / −0.56 |
| 35 | hgb_d3 (best ML) | ML | 0.1110 | 0.3293 | 0.93 | +0.0004 | 0.1 / 0.2 | −0.0019 (−0.6) / +0.0033 (0.8) / −0.0028 (−0.6) | −0.0010 (−0.7 / −0.5) | −0.0021 (−1.0) | 0.19 / −0.64 |
| 39 | **mid** (B0) | baseline | 0.1541* | 0.4529* | 0.85 | 0 | — | — | — | −0.0015 (−0.5) | — |
| 48 | kalman_ll_0.1 (worst) | drift | 0.1567 | 0.5276 | 0.45 | −0.0540 | −3.6 / −3.9 | −0.0947 (−3.7) / −0.0116 (−0.9) / −0.0310 (−2.0) | −0.0468 (−6.2 / −5.6) | −0.0445 (−3.1) | −4.04 / −5.61 |

\* on its 345-snapshot subset; `solv1_exact` on the same subset is 0.1556.

### 5.3 Ranked table — tape A (candles only, 36 days), 3 assets pooled, paired vs `solv1_candle`; top-10 + baselines

dev1 = folds 1–7 (10-02 → 10-16 UTC), dev2 = folds 8–14 (10-16 → 10-28 20:28Z). The SOL-v1 dev/forward boundary
(2026-10-28 20:52:57Z) falls inside the last fold; only four SOL closes (21:00–21:45Z) lie after it, so there is no separate
"val" column — the forward test is §7. All 47 rows and the per-asset split: `tables_full.md` T1.

| # | model | family | Brier | log-loss | cal. slope | Δ vs B1 | t_c / t_h | Δ dev1 (t_c) | Δ dev2 (t_c) | SPA t |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | ens_stack (folds 3–14) | ensemble | 0.1294 | 0.3853 | 0.99 | +0.0010 | 4.2 / 4.3 | +0.0012 (4.2) | +0.0008 (2.3) | 4.16 |
| 2 | har_kf | variance | 0.1302 | 0.3886 | 1.04 | +0.0009 | 5.1 / 5.3 | +0.0011 (4.7) | +0.0006 (2.6) | 5.11 |
| 3 | egarch11_kf | variance | 0.1302 | 0.3881 | 1.04 | +0.0009 | 6.1 / 6.3 | +0.0010 (5.1) | +0.0007 (3.6) | 6.01 |
| 4 | ewma_0.94_kf | variance | 0.1303 | 0.3886 | 1.04 | +0.0008 | 5.7 / 5.9 | +0.0010 (5.2) | +0.0006 (2.8) | 5.78 |
| 5 | garch11_kf | variance | 0.1303 | 0.3885 | 1.04 | +0.0008 | 6.0 / 6.1 | +0.0009 (4.7) | +0.0007 (3.7) | 5.89 |
| 6 | ewma_0.97_kf | variance | 0.1303 | 0.3889 | 1.04 | +0.0008 | 4.1 / 4.3 | +0.0010 (4.2) | +0.0005 (1.7) | 4.20 |
| 7 | ewma_0.94_k1 | variance | 0.1305 | 0.3893 | 1.11 | +0.0006 | 4.2 / 4.3 | +0.0007 (3.8) | +0.0004 (2.2) | 4.29 |
| 8 | bocpd_kf | change-point | 0.1305 | 0.3895 | 1.04 | +0.0005 | 3.6 / 3.7 | +0.0006 (3.0) | +0.0004 (2.0) | 3.48 |
| 9 | rv_60_kf | variance | 0.1305 | 0.3899 | 1.05 | +0.0005 | 2.5 / 2.6 | +0.0008 (2.9) | +0.0003 (0.8) | 2.54 |
| 10 | ens_avg | ensemble | 0.1305 | 0.3891 | 1.16 | +0.0005 | 4.8 / 4.8 | +0.0004 (2.8) | +0.0006 (4.1) | 4.67 |
| 11 | rv_15_kf | variance | 0.1306 | 0.3895 | 1.05 | +0.0005 | 5.8 / 6.0 | +0.0006 (5.4) | +0.0003 (2.6) | 5.72 |
| 12 | hgb_d2 (best ML) | ML | 0.1306 | 0.3902 | 1.05 | +0.0004 | 2.0 / 1.9 | +0.0006 (2.1) | +0.0002 (0.7) | 1.87 |
| 23 | ar_1 (best drift) | drift | 0.1310 | 0.3909 | 1.16 | +0.0001 | 1.2 / 1.3 | +0.0000 (0.0) | +0.0001 (1.6) | 1.26 |
| 26 | mc_sign_1 | Markov | 0.1310 | 0.3909 | 1.17 | +0.0001 | 1.1 / 1.2 | −0.0000 (−0.1) | +0.0001 (1.6) | 1.18 |
| 29 | **solv1_candle** (B1) | baseline | 0.1311 | 0.3910 | 1.17 | 0 | — | 0 | 0 | — |
| 30 | rf | ML | 0.1311 | 0.3949 | 1.09 | −0.0000 | −0.0 / −0.0 | +0.0002 (0.5) | −0.0002 (−0.6) | −0.03 |
| 31 | cusum_kf | change-point gate | 0.1312 | 0.3932 | 1.07 | −0.0001 | −0.7 / −0.7 | +0.0003 (1.1) | −0.0006 (−1.9) | −0.75 |
| 34 | hmm_2_kf | HMM | 0.1315 | 0.3922 | 1.08 | −0.0004 | −1.6 / −1.7 | −0.0009 (−2.4) | +0.0001 (0.3) | −1.62 |
| 35 | **partial_logit** (B3) | baseline | 0.1316 | 0.3952 | 1.00 | −0.0006 | −1.8 / −1.8 | −0.0002 (−0.5) | −0.0009 (−2.1) | −1.75 |
| 37 | mc_vol_2_kf (best Markov-vol) | Markov | 0.1317 | 0.3935 | 1.09 | −0.0006 | −2.5 / −2.5 | −0.0006 (−1.7) | −0.0006 (−1.8) | −2.45 |
| 45 | kalman_lt | drift | 0.1409 | 0.4192 | 0.75 | −0.0098 | −9.9 / −10.5 | −0.0079 (−6.0) | −0.0119 (−7.9) | −10.53 |

### 5.4 Multiple-comparison correction (all 47 configurations × 4 zones; stationary bootstrap over closes, block 4, B = 4000, per-zone B = 2000)

| test | closes | strategies | best (studentised) | Δ | t | naive p | RC p stud. | SPA-c p stud. | **RC p non-stud.** | **SPA-c p non-stud.** | # Δ > 0 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **B SOL vs mid** (primary) | 88 | 188 | `ens_stack|mid` | +0.0037 | 1.55 | 0.065 | 0.706 | 0.656 | **0.946** | **0.815** | 113 |
| ↳ final zone only | 78 | 47 | `solv1_exact` | +0.0031 | 1.01 | — | 0.538 | 0.417 | 0.728 | 0.404 | — |
| ↳ mid zone only | 68 | 47 | `ens_stack` | +0.0037 | 1.56 | — | 0.331 | 0.331 | 0.591 | 0.591 | — |
| ↳ early zone only | 67 | 47 | `mc_vol_2_kf` | +0.0020 | 0.44 | — | 0.837 | 0.750 | 0.873 | 0.728 | — |
| **B pooled 3 assets vs mid** | 92 | 188 | `bocpd_kf|mid` | +0.0029 | 1.30 | 0.093 | 0.765 | 0.685 | **0.969** | **0.850** | 69 |
| ↳ final / mid / early | 90 / 68 / 67 | 47 | `garch11_kf` / `bocpd_kf` / `hmm_3_kf` | −0.0007 / +0.0029 / +0.0028 | −0.50 / 1.30 / 0.91 | — | 1.00 / 0.46 / 0.55 | 1.00 / 0.42 / 0.50 | 1.00 / 0.68 / 0.74 | 1.00 / 0.55 / 0.62 | — |
| **B SOL vs `solv1_exact`** | 92 | 184 | `garch11_kf|early` | +0.0037 | 2.65 | 0.006 | 0.162 | 0.148 | **0.850** | **0.561** | 67 |
| ↳ final / mid / early | 92 / 68 / 67 | 46 | `hmm_2_kf` / `bocpd_k1` / `garch11_kf` | −0.0000 / +0.0030 / +0.0037 | −0.02 / 1.81 / 2.68 | — | 1.00 / 0.31 / 0.06 | 1.00 / 0.31 / 0.06 | 1.00 / 0.90 / 0.64 | 1.00 / 0.86 / 0.26 | — |
| A SOL vs `solv1_candle` | 2581 | 180 | `ens_stack|mid` | +0.0022 | 4.32 | 0.000 | 0.000 | 0.000 | 0.519 | **0.008** | 93 |
| ↳ final / mid / early | 2581 | 45 | `har_kf` / `ens_stack` / `ar_1` | +0.0005 / +0.0022 / +0.0002 | 2.59 / 4.31 / 1.28 | — | 0.10 / 0.00 / 0.68 | 0.08 / 0.00 / 0.57 | 0.66 / 0.07 / 0.97 | 0.34 / **0.003** / 0.76 | — |
| A pooled vs `solv1_candle` | 2581 | 180 | `egarch11_kf|pooled` | +0.0009 | 6.01 | 0.000 | 0.000 | 0.000 | 0.508 | **0.011** | 102 |
| ↳ final / mid / early | 2581 | 45 | `garch11_kf` / `ens_avg` / `rv_15_kf` | +0.0005 / +0.0011 / +0.0001 | 4.66 / 5.80 / 1.33 | — | 0.00 / 0.00 / 0.66 | 0.00 / 0.00 / 0.56 | 0.44 / 0.06 / 0.98 | **0.013** / **0.001** / 0.81 | — |

Reading: on tape B the best of 188 strategies is what one expects from 188 draws of noise (113 of 188 have a positive mean —
the mid and the models are nearly interchangeable). On tape A the fitted-κ family survives at p ≈ 0.01 in the **mid zone
(T−2 to T−10 min)** and the final zone pooled; the early zone (T−14) shows nothing anywhere.

### 5.5 Zone of skill

* **vs the market mid (SOL):** every top model's Δ is concentrated at **T−5 and T−10 min** (+0.004 to +0.007 per close, t 0.9–1.5
  for bocpd/garch), ≈ 0 at T−1/T−2 min, negative at T−14 min (where the market mid, hours after open, already knows more than
  a vol forecast). Pooled across assets the T−1/T−2 min diffs turn *negative* (−0.002 to −0.004, t −1 to −1.6) and only
  T−10 min stays positive (+0.004 to +0.006, t 1.4–1.9). None of this reaches significance even before correction.
* **vs the candle baseline (tape A):** the κ-calibration gain is a **mid-zone (T−2..T−10) effect** (+0.0011 to +0.0022, t 4–6),
  one-third as large at T−1 (+0.0005), zero at T−14. The policy's κ schedule (SOL_V1 §2) is already a `_kf`-type correction fitted
  on the feed; this is consistent with the live policy (`solv1_exact`, slope 1.08) being closer to calibrated than its
  candle-only proxy (1.17).

So the only skill found is "the candle-only formula is over-confident at 2–10 min and a fitted κ fixes it", which the live
policy already has. There is no zone where anything beats the market.

### 5.6 Honest caveats

* Tape B is **one day** (10-27 22:48Z → 10-28 21:45Z): 88–92 closes per asset, a single regime. A real +0.003 Brier edge vs the
  mid (t ≈ 1.3 here) would need ~6× the closes to reach t = 3 and more to survive 188 comparisons — the forward hook exists
  to collect exactly that, with the models frozen.
* Tape A outcomes are the minute-close proxy of the 60-s average (SOL_V1 §1: proxy agrees with the official feed on 89/90 and
  with Kalshi on 44/45 SOL closes), not the official index; the ±0.001 effects there could be partly proxy noise, though they
  are the same sign in all 3 assets and both halves.
* The `kf` κ is fitted per zone on prior folds (walk-forward), so it is legitimately out-of-sample, but it is one parameter
  per zone fitted on ~thousands of windows — the gain is exactly what one extra calibration parameter buys.
* The economic replay uses the recorded SOL quote/book tape (30-s open-phase quotes + 1-Hz final-minute books); fills in the
  open phase are against a 30-s-old book, as in `sol_v1_replay.py`.
* The stacker's first scored fold is 3 on tape A (32 955 of 38 715 points) and it is scored on tape B from block 1 (history =
  all tape-A OOS predictions before the block).

## 6. What was tried (raw count)

**47 model configurations** (§2 table, nothing added or removed after the first run) × **4 zones** = 188 strategies in the
primary SPA; 3 benchmarks (mid, `solv1_exact`, `solv1_candle`); 5 SPA families (B SOL vs mid, B pooled vs mid, B SOL vs
`solv1_exact`, A SOL vs `solv1_candle`, A pooled vs `solv1_candle`) each with 3 per-zone sub-tests; 2 economic variants ×
47 models. One full run (`results_full_20261028_151552.json`), one SPA re-run after the studentisation fix
(`results_full_20261028_152045.json`; predictions unchanged — same `preds_full.npz`), one `--quick` SOL-only smoke run
(`results_quick.json`, last 3 tape-A folds; not used for any claim). No hyper-parameter search beyond the pre-declared grid;
no configuration was dropped from the tables.

## 7. Forward scoring (wired, scored-only; no change to the policy)

Nothing survived §4 vs the mid, so per the pre-declared fallback the hook logs the **top-3 by tape-B SOL Δ vs mid** —
`bocpd_kf`, `bocpd_k1`, `garch11_kf` — **plus the tape-A champion `har_kf`** (§5.0-4), all fitted once on every tape-A window up
to 2026-10-28 21:28:21Z (`forward_models.pkl`, `train_end_ts` 1793222101) and **frozen**: the hook only re-runs each model's
causal filter over refreshed candles (`Model.extend`), never refits.

* **Mechanism.** `scripts/tournament.py forward` tails `data/sol-v1/snapshots.jsonl` (the live policy's `entry_eval` rows —
  read-only), refreshes 1-min candles via `predhft.candles.fetch_1m` every 5 min (Coinbase public REST, bounded), and appends one
  row per evaluation to **`data/tournament/forward.jsonl`**: `t, ticker, close_ts, secs_left, K, x_now, mid, yes_bid/ask,
  p_solv1_model, p_solv1_used, solv1_decision, sigma_1s_feed, candle_idx_ts, train_end_ts, forward (t ≥ train_end), zoo{model: p}`.
  It touches nothing under `data/sol-v1/` and never calls the policy. `sol_v1.py` is unchanged (`git status`: the tournament adds only new, untracked files —
  `model_zoo.py`, `tournament.py`, `tournament_tables.py`, `test_model_zoo.py`, this doc, `reports/tournament/`, `data/tournament/`; no tracked source file is modified).
* **Command** (needs a task / terminal alongside the SOL-v1 runner):
  `cd D:/prediction-hft && uv run --with scikit-learn,pandas,pyarrow,hmmlearn,statsmodels,arch,scipy python scripts/tournament.py forward`
  (`--once --from-start` replays the whole snapshot file once; done at 15:21 PT: 2 550 rows, 2 184 of them after `train_end_ts`).
* **Scoring:** `python scripts/tournament.py forward-score` — per close first, then mean / close-SE / t across closes of
  Brier(mid) − Brier(model) and Brier(`p_solv1_model`) − Brier(model), pooled and by zone, with the dev-period rows excluded.
  At 15:21 PT (4 forward closes settled) `har_kf` +0.0004 ± 0.006 vs mid, the BOCPD pair −0.004 to −0.006 — meaningless at n = 4,
  recorded only as the first checkpoint.
* **Pre-declared promotion rule (unchanged from §4, restated for the hook):** a hook model is proposed for a *paper* swap of
  `p_model` only after **≥ 100 forward SOL closes** where (i) its forward paired Brier vs the mid in the T−2..T−10 zone is better
  by ≥ 1 close-clustered SE, (ii) it is better than SOL-v1's own forward paired Brier vs the mid over the same closes, and
  (iii) the all-zones forward Δ vs `p_solv1_model` is ≥ 0. Four models are watched, so the forward SPA (same bootstrap, 4 × 4
  strategies) must also give p ≤ 0.05 — the hook is itself a multiple comparison. Nothing is automatic: a positive opens a
  SOL_V1 §10-style review, not a deployment.

## 8. Code and tests

* `src/predhft/model_zoo.py` — grids, points, the 47 models, ensembles, scoring, cluster SE, calibration slope, stationary
  bootstrap, SPA / RC (studentised and not), `decide_with_p` / `replay_policy` (policy execution path with injected p),
  `Model.extend` for the hook.
* `scripts/tournament.py` — tapes A/B, folds, staged + resumable run (`tapes.pkl` → `preds_full.npz` per fold →
  `results_full.json` metrics/SPA → economics per model → `forward_models.pkl`; `--stages`, `--force`), `forward`,
  `forward-score`; `scripts/tournament_tables.py` — the markdown tables from the parquet.
* `tests/test_model_zoo.py` — 18 tests: fit/predict shapes and [0, 1] bounds for every model; **no-leakage**: perturbing every
  candle that closes after the decision time leaves features and all 44 non-feed, non-ensemble model probabilities bit-identical, fit
  parameters ignore post-split returns, training labels close before the fold; Markov transition rows sum to 1 (sign and vol
  chains, orders 1–3), HMM posteriors/transition rows are distributions; candle terms = policy `settlement_terms`; `p_formula`
  = policy `p_settle_ge`; cluster SE; calibration slope; stationary-bootstrap block structure; SPA size on null data
  (non-studentised ≤ 4/30 rejections, studentised ≥ it) and power on a planted edge; NaN handling; `decide_with_p` with the
  policy's own p reproduces `sol_v1.decide` exactly (decision, side, qty, limit; cached-terms path identical).
* Full suite: 209 tests green (191 + 18) — `uv run --with pytest,scikit-learn,pandas,pyarrow,hmmlearn,statsmodels,arch,scipy python -m pytest -q tests`.
* Nothing committed; live SOL-v1 policy, runners and `data/sol-v1/` untouched; CPU only; no LLM calls.
