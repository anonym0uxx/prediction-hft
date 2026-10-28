# Robust betting — P19 quote-path recorder review + P20 bet-acceptance gate (2026-10-27 22:10 PT)

Code: `src/predhft/open_quotes.py` (rewritten), `src/predhft/quote_path.py` + `scripts/score_quote_path.py` (new), `src/predhft/feed_tail.py` (new), `src/predhft/bet_gate.py` (new), hook in `src/predhft/s7_live.py` (`--bet-gate`, default OFF). Tests `tests/test_bet_gate.py` (13), `tests/test_quote_path.py` (11); suite 84 passed. Nothing committed; no running runner touched.

## 1. Review of the first `open_quotes.py` (P19 v1) — defects, all fixed by replacement

1. **Best bid/ask by list position.** `levels[-1]` assumed Kalshi returns levels ascending. It does today, but that is not a contract, and the same code path served the cents `orderbook` fallback where `[1, qty]` (1 cent) was read as `p > 1.0 → p/100`… and `[1, qty]` exactly equal to 1 was read as **$1.00**. Now `parse_book` sorts by price, derives YES asks from NO bids (`1 − no_bid`), ignores 0/1 prices and malformed levels, and is tested on ascending / reversed / shuffled fixtures.
2. **No outcome join.** The docstring promised `scripts/score_open_quotes.py`; it did not exist, so the rows could never become a Brier or a calibration. `scripts/score_quote_path.py` joins Kalshi results (cached in `data/ts/results.json`, shared with ts_levers).
3. **One snapshot per window, no depth.** A single T-14.5 top-of-book cannot answer "when is entry possible": the final minute showed that entry supply is a time-path property (TS doc §4.2). Now 30-s cadence from open to T-60 s, top-3 depth per side, plus official value/partial/age, replica/age and trailing sigma on every row. The final minute stays with the runners' `books.jsonl` (≈1 s cadence).
4. **Missed markets, no retry.** The one real run produced BTC and SOL but **not ETH** (the ETH market was not yet listed at T-14.5 min); the window was silently lost. Now the market list is cached per `(series, close_ts)` and re-requested on the next sample until found; listing failures are written as error rows.
5. **Rate-limit interaction.** v1 used `KalshiPublic(min_interval_s=0.12)`, i.e. it leaned on the shared cross-process slot (0.5 s) and would have competed with the runners had it ever sampled inside the final minute. Now `min_interval_s=0.5`, no `PREDHFT_PRIORITY` lane, hard stop at T-60 s (`--stop-s`), 3 orderbook requests per 30 s ≈ 0.1 req/s.
6. **Smaller:** `volume` read the wrong field (`volume_fp`), `cfb_age_s` could be negative (stale `now`), the partial average is structurally `null` at T-14.5 yet was the only official field kept. Fixed; rows carry `"v": 2`, v1 rows remain loadable.

Smoke test: `uv run python -m predhft.open_quotes --once` wrote 3 rows (BTC/ETH/SOL, T-7.2 min, depth, sigma 4.6 bp/min, cfb age 0.9 s). Note the 15M books quote in **0.1 c ticks** (`0.979/0.982`); the runners' +1 c slippage is therefore conservative.

## 2. P20 — the bet-acceptance gate

`bet_gate.accept(candidate, ctx) -> (ok, reasons, details)` is pure: no I/O, every guard evaluated (no short-circuit), every number returned. The runner builds the candidate right after the P16 edge check and before `risk.size` (risk.py and its one-bet-per-close invariants are untouched; the gate can only remove bets).

| guard | pre-registered rule | threshold | why |
|---|---|---|---|
| a edge_lcb | point edge ≥ 5c (P16) **and** `p_lcb − price − fee ≥ 2c`; `p_lcb = Φ(|d| / sd_hi)`, `sd_hi = max(sd(r_eff)·(1+1.645·0.17), 2.35 bp·(r_eff/0.917)^1.5)`, `r_eff = max(r_clock, 1 − partial_n/60)` | 5c / 2c | sigma estimation error (0.17 rel), partial lag, and the realised 2.35 bp T-55 projection error (REBASE §2) as a floor the model cannot talk its way under |
| b agreement | our side's market mid ≥ .60 **and** `p_cal − mid ≤ .20`; else override only if official feed ∧ partial_n ≥ 30 ∧ p_lcb ≥ .90 ∧ mid ≥ .35 | .60 / .20 | every recorded loss was a ≤ .64 side with 24–71c disagreement; the override is the only place we claim to know something the market does not (half the settlement average already printed) |
| c feed | spot_src = cfb, age ≤ 2.0 s, partial_n ∈ [expected−3, expected+2] | 2 s | correctness: a stale or mis-windowed partial makes p_cal fiction |
| d liquidity | size at our ask ≥ lot, price = ask+1 tick ≤ .96, `edge_lcb − adverse ≥ 2c`, `adverse = 100·φ(z)·r·σ₁bp·√(1.5/60)/sd` | 1.5 s | the probability-space cost of a one-sigma index move during our measured latency |
| e regime | `|30-s official move| ≤ 2.5·σ₁bp·√0.5` | K = 2.5 | REBASE 18:30Z: a 3.9 bp swing in 10 s at the quietest hour; unevaluable → pass, flagged |
| f hit-rate | rolling 30 settled gate bets per asset, Wilson 90% LB ≥ .80 once n ≥ 20; below → scored-only + `GATE ALERT`, shadow bets logged, auto-resume | .80 | 27/30 passes (LB .808), 26/30 trips (.768); 18/20 trips at warm-up |

Logging: every decision → `data/<family>/gate.jsonl` (candidate, all guard details, reasons); rejections → `runner.log` as `GATE REJECT …`, counted in the NOFILL/supply reasons as `gate:<guard>`; accepted bets carry `gate: {…}` on the trade row. `books.jsonl` snapshots now also carry `cfb_age_s` (so future replays can evaluate guard c exactly).

## 3. Tape replay (236 settled 15M windows, 2026-10-27 05:43Z → 2026-10-28 03:45Z; `uv run python -m predhft.bet_gate replay --no-fetch`)

| variant | n bets | closes | hit | Wilson LB90 | mean c/ct | se (close-cl) | top rejection reasons |
|---|---|---|---|---|---|---|---|
| P16 reference (no gate, replay) | 8 | 7 | 0.50 | 0.29 | −13.4 | 12.5 | |
| **gate_full** | 1 | 1 | 1.00 | 0.38 | +12.2 | — | feed 29, agreement 24, edge_lcb 5, liquidity 5 |
| gate_no_feed (guard c off) | 1 | 1 | 1.00 | 0.38 | +12.2 | — | agreement 24, edge_lcb 5, liquidity 5 |
| favourite_only (guard b alone) | 4 | 4 | 1.00 | 0.71 | +14.8 | 1.5 | agreement 24 |
| no_override | 1 | 1 | 1.00 | 0.38 | +12.2 | — | same as full |
| actual paper trades (P12+P13 15M, settled) | 54 | 31 | 0.52 | 0.43 | +1.7 | 5.5 | |

Reading it honestly:

- The agreement guard separates the 8 reference replays perfectly (4 winners pass, 4 losers fail) — **in-sample**: the thresholds were set after seeing them and the 15 TS entries. It is a hypothesis for P20, not a result.
- The LCB guard rejects the three SOL winners (p_point .88–.93 at σ₁ ≈ 8.5 bp/min: `sd_hi` 8.8 bp vs d 6.5–7.6 bp → p_lcb .77–.81 < price + 2c). That is the price of demanding the bound rather than the point; it may well be too conservative, and PREREG P20 says exactly what evidence would relax it.
- **Supply is the binding constraint, not accuracy.** In the 45 windows recorded with the official feed (since 16:00 PT), the P16 rule produced **one** candidate in total — and the gate accepted it (BTC 19:45 ET YES @.87, p_lcb .925, mid .855, won). Guard c "rejects 29" only because the earlier tape is replica-era. A gate cannot create bets; the quote-path recorder exists to find out whether earlier entry is possible at all.
- Guard c could not be evaluated exactly on the tape (no `cfb_age_s` in old snapshots; replay passes with `age_unknown_flag`). Guard e was evaluable on most candidates from the in-window spot history (≥ 15 s), otherwise flagged.

## 4. What "every bet profitable" can and cannot mean

A bet bought at .87 on a perfectly calibrated .87 event loses 13 % of the time; no checklist changes that. What can be engineered is (i) a positive, measured expected value per bet after fee and after an uncertainty haircut, (ii) a hit rate that is high *because the bets are favourites*, and (iii) a process that stops an asset the moment its realised hit rate falls below what the prices paid require. P20 targets hit ≥ 85 % and mean ≥ +3 c/ct on gate-accepted bets, measured out-of-sample with close-clustered se; the honest n for +3c at 30c/ct noise is ≈ 384 closes, with an interim read at 60. Today's tape would have produced about one such bet per day across three assets; whether that is a business is a supply question the P19 quote path is now recording.

## 5. Commands for the parent

```bash
cd D:/prediction-hft
uv run --with pytest python -m pytest -q tests                       # 84 passed
# P19 quote-path recorder: replace the PredHFT-OpenQuotes task command with
uv run python -m predhft.open_quotes                                   # 30-s cadence, stops at T-60 s; --once for a smoke test
uv run python scripts/score_quote_path.py                              # after >= 1 day: reports/quote_path/score.md (+ .json); --no-fetch for offline
# P20 gate: add --bet-gate to the three s7p13-KX*15M runner tasks (they already carry --proj-gate [--ts-levers]) and restart them
uv run python -m predhft.s7_live --series KXBTC15M --product BTC-USD --period 900 --t-open 0.92 --t-close 0.15 --lot 10 --sigma ewma94 --dist t --thr-mode dyn --proj-gate --ts-levers --bet-gate --root data/s7p13-KXBTC15M   # idem ETH/SOL
uv run python -m predhft.bet_gate replay [--no-fetch] [--json reports/gate_replay.json]   # tape counterfactual, any time
uv run python -m predhft.bet_gate settle-shadow                        # settles shadow rows after a hit-rate trip (schedule hourly if guard f ever trips)
```

Not verified: the live `--bet-gate` path has not run against the exchange (runners hold `runner.lock`; the block is exercised only by the pure tests and the replay, which share the guard code); guard c age semantics on the live tape; anything about P19 beyond one 3-row sample.
