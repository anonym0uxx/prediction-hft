# LayerX "Automated Polymarket Trading Bot" vs. predhft — lessons, fill-realism audit, P21 scanner (2026-10-27)

Source: `docs/external/layerx_polymarketbots_2026-02.md`. Our numbers: paper tape since `data/RESET.json` (79 settled, −$5.83 at 05:30Z), `data/s7p13-*/books.jsonl`, `data/cfb/*.jsonl`, `data/struct/`.

## 1. Strategy-by-strategy, hard on us

| LayerX | What killed it (his words) | Our analogue | Same or different? |
|---|---|---|---|
| Crypto 15-Min: buy the 0.80–0.99 side in the last 60 s | −37.8%: "no edge identification", "95c markets are priced correctly" | P12/P13/P16: buy the favoured side ≤ .95 in T-55..T-10 s | **Identical trade.** The only claimed difference is *why* we think the favourite is cheap. |
| CEX momentum | paper profits were bid/ask artefacts; 36.7% live | P10/P6 hourly model-vs-market taker | Same class of error: our 0.97 belief floor and t-scale 0.8 made paper "edge" that REBASE falsified (−6.4 ± 10.9 c/ct, 165 windows). |
| V1→V4 manual tuning | "if you keep tuning you have no edge" | P1→P6→P10→P12→P13→P16→P18→P20 in 48 h | We tuned at least as much; our difference is procedural (PREREG, frozen constants, stop rules), not evidential. |
| Bregman/Frank-Wolfe structural arb | "rare but high-confidence", still paper | none until today | He is ahead of us here; P21 (§4) closes the gap for Kalshi's ladders. |

**Same.** Every P12/P13 bet is a late favourite purchase; pooled tape 19 wins vs 18.8 market-implied (REBASE §1.2); P13 −10 c/ct (n=7, pre-feed); "many small wins, few −80c losses" — his profile.

**Where we are genuinely different, and whether it is evidenced.**
1. *Official settlement feed.* He priced off CEX spot against a settlement he could not observe. We read the exact CF Benchmarks tick and the settlement-aligned partial 60-s average at 1 Hz (`cfb_feed.py`), i.e. at T-10 s we know 50/60 of the number that settles the contract. Real information he never had. **Evidence the market lags it: none yet.** Zero P13/P16 trades with `spot_src=cfb` (every candidate failed `fav_gt_95_or_size` or the 5c edge); the 21 cfb-era trades are P12 longshot buys (price .29, hit .33, +$7.01), an unregistered cell. §4(d) is the first direct test and it is negative: when the feed has locked the outcome (16/28 BTC windows, median 34 s before close) the winner is offered at **1.00 in 100% of 114 snapshots**. The market does not lag the feed at the lock; any edge lives in the contested band where p_cal is a model, not a fact.
2. *Calibrated p_cal and the P20 gate.* He had no probability model; we size on p_cal and refuse when `p_lcb − price − fee < 2c`. P20 thresholds were chosen in-sample on 15 + 8 trades; zero out-of-sample bets. Argued, not evidenced.
3. *Supply, not accuracy, is binding.* ~8% of windows offer our side ≤ .95; he traded every window. That is why we are −2% not −38%, but n grows ~2 bets/day and P16/P20 will not reach n_required (384) for months. We have bought time, not edge.

## 2. "Stop tuning" vs. P18 Thompson and PREREG

P18 is a 36-arm search over {edge, timing, cap, order} of the same rule — by his lesson, V5. Defensible only because every arm is replay-scored on every close, n ≥ 30 before selection, and a 300-close stop rule hands off to model work. The bootstrap already shows no arm positive (best −0.1 ± 12.4 c/ct). Keep P18 as a *measurement*; promote nothing; its likeliest output is the stop. PREREG is the one discipline he lacked — keep it honest by not registering a new entry each time one fails (P1, P6, P10, P12, P13 failed or paused within two days).

## 3. Paper fill realism audit (his "fantasy profits" lesson)

What `s7_live.py` books (lines 382–435): decision on snapshot A; a **second orderbook fetch** B; fill only if B still satisfies the rule (proj_gate: ask ≤ cap, size ≥ lot, A→B latency ≤ 1.5 s); **price = B's ask + $0.01, capped .99**; qty ≤ B's top-level size (50% if < 50, `risk.py`); fee = 0.07·C·P·(1−P) rounded UP to the cent per order; `pnl = win·qty − cost − fee` on the official `result`. Signal and execution both use the ask; his bid/ask bug is absent.

Conservative: books tick at $0.001 (fills at .076, .092 on tape), so +1c is ten ticks; the cent-ceiling fee overstates the schedule effective 2026-07-07 (trade fee ceil to $0.000001, balance aligned to $0.0001 for direct members) — `fees.py` is an upper bound, tested.

Remaining optimism, measured on 2,430 consecutive final-minute snapshot pairs (median 0.91 s apart, `s7p13-*`, favoured side ≤ .95, size ≥ 10): the ask moves **> 1c against us 17.1%** of polls, touch size drops below lot **7.5%**; move quantiles 50/75/90/95/99% = 0/0.3/3/6/18c. Scaled to a ~0.3 s order round-trip after re-check, **~6% of booked fills would miss**, and directionally so (misses when the ask jumps toward 1, i.e. winners): bound ≈ 0.06 × (1 − price) ≈ **0.6 c/ct** overstatement at .90 — small against the 5c P16 requirement. Secondary: 25 stopped-family trades were booked 1.5–7.8 s after decision (fresh price, stale signal).

**Fix applied (log-only, behaviour unchanged):** `s7_live.py` writes a `post_fill_check` row to `books.jsonl` on the first poll after each paper buy (booked price, current ask/size, `still_fillable`), turning the 6% estimate into a per-family measurement. All `books.jsonl` readers skip the row.

## 4. P21 structural scanner — what exists on Kalshi and what the first 15 minutes say

`src/predhft/struct_scan.py` (+ `tests/test_struct_scan.py`, 8 tests; full suite 92 passing). Zero orders, ≤ 1 req/s (own 1.0 s interval + shared limiter, priority lane removed), 10 requests per 60-s cycle.

Logic: all open KXBTCD/KXBTC/KXBTC15M (and ETH) markets at one close settle on one number A; each market is a half-open cents interval of A (`interval()`, per `rules_primary`). Every constant-payoff portfolio is enumerated (~279k/cycle): (a) YES+NO on one market (impossible on Kalshi's unified book unless crossed); (b) nested pairs = ladder monotonicity, incl. **15M-up ⊂ hourly above-K when K < the 15M open reference** — the one strictly logical cross-horizon constraint (only at coinciding :00 closes; nothing links 15M to daily otherwise, so skipped); (c) equal/complement pairs across series (KXBTC "greater K" ≡ KXBTCD "above K"; "less K" ≡ ¬"above K−0.01"), disjoint-union triples (bucket + above b = above a), full range partition (ΣYES asks < 1, ΣNO asks < n−1); (d) resolution lag from the P13 tape + feed (locked iff the remaining average must move > 1.5× the feed's trailing-24h empirical envelope at that horizon). Top-of-book ties get depth via one batched `/markets/orderbooks`; `executable()` walks the asks unit by unit to the largest size whose marginal cost + exact fee < payoff.

Live run 05:40–05:55Z: 15 cycles, 144 requests (0.16 req/s), 6–8 closes, 1,419 markets, ~279k portfolios/cycle, 5–9 top-of-book ties per cycle (`crosshz_union`/`ladder`/`crosshz_disjoint`), **0 executable hits** → median persistence and net $/hit undefined. Ties were 0–1c gross on the list; the three 14–18c "violations" the markets list showed were stale list quotes refuted by the live book (list top-of-book lags the book — never trust it for execution). (d): 0 hits over all cfb-era windows. The maker quoting the KXBTCD ladder and KXBTC buckets is consistent to within the fee — LayerX's "rare", measured.

## 5. His §9 future directions mapped to our stack

| His item | Status here | Verdict |
|---|---|---|
| 9.1 Live Bregman execution | n/a (P21 scored-only) | Only if P21 shows ≥ 1 executable hit/day; today's rate is 0. |
| 9.1 Cross-market constraint detection | **done today** (P21 b/c, exact interval algebra, no optimiser needed on a 1-D settlement) | Keep as monitor. |
| 9.1 Latency optimisation | re-check + 1.5 s guard; 0.6 s median decision latency | Websocket order books (we have the auth key) would cut the 6% miss; cheap, do it. |
| 9.1 Order-book ML for execution quality | `post_fill_check` rows start the dataset | Fit nothing before ≥ 200 rows. |
| 9.2 Market making | s1-maker stopped at −5.2 c/ct projected (adverse selection) | Variant "quote only when official-feed p_cal is ≥ 10c from mid" is viable *only* as a join at p_cal − 2c inside T-30 s; P18 join arms already replay-score exactly this. Promote only if join arms show mean > 0 on ≥ 60 closes. |
| 9.2 Event-driven | none for crypto; weather J1 is our event-driven line | Keep weather; no crypto event calendar beats a 60-s average. |
| 9.2 Sentiment | P15 scored-only, timestamp-disciplined | Already stricter than his proposal; P17 offline says features add nothing at T-1m. Keep scored-only. |
| 9.2 Cross-platform (Kalshi vs Polymarket US vs PredictIt) | nothing | **Build the data now, trade later.** Spec: per 15M/hourly close record both venues' top-3 books at 1 Hz in the final 2 min with server timestamps, settlement source + exact timestamps per venue (Kalshi: CF Benchmarks 60-s average; Polymarket crypto: Chainlink/Binance print — not the same number), fee schedules, and withdrawal/settlement delays. The "arb" is only risk-free when settlement definitions coincide; most do not, so the dataset should first measure basis between the two settlement series. |
| 9.2 Options replication | — | Reject: a ladder of binaries is already a digital-option strip; replicating a vanilla from it adds basis and fee on every strike for no counterparty who wants it. |
| 9.3 Backtesting framework | `ts_levers.replay_all`, `bet_gate` tape replay, P17 study | Have it; the gap is sample size, not tooling. |
| 9.3 A/B testing | P18 (arms replayed, one live) | Have it. |
| 9.3 Bayesian auto-tuning | P18 Thompson | Have it — and §2 says it is the thing to be most suspicious of. |
| 9.3 Risk dashboard | `risk.py` ledger, halts shadow-logged in paper | Have the ledger; a drawdown/VaR view is a one-file script, low value until live. |

## 6. Ranked enhancements (expected $/day at the current $10-lot paper scale; uncertainty is honest, not decorative)

| # | Enhancement | E[$/day] | Uncertainty | Promotes when |
|---|---|---|---|---|
| 1 | **Direct information test**: score, every close, p_cal from the official partial average vs the market mid at T-30/T-20/T-10 s (Brier, and "winner offered ≤ .95 while p_lcb ≥ .97") — `quote_path` already records the pre-final-minute path; extend to the final minute from P13 books | $0 directly; decides whether the whole line exists | high | ≥ 300 closes/asset; Brier(p_cal) < Brier(mid) at 95% in some bucket |
| 2 | Websocket order books for the P13 runners (auth key exists) | +$0.05–0.3 (recovers the ~6% missed fills and the 1.5 s guard rejections) | medium | `post_fill_check.still_fillable` < 90% on ≥ 50 fills |
| 3 | Cross-platform data capture (Kalshi vs Polymarket US) per §5 spec, scored-only | $0 now; the only candidate for a non-tuning edge if settlement series coincide | high (legal access, settlement basis) | basis dataset shows |Kalshi − Poly| > fee + spread for ≥ 60 s in ≥ 5% of closes |
| 4 | P20 gate run to its 60-close interim | +$0.2–1 if H1, −$0.5 if not | high | mean − 1.96 se_cl > 0 at 60 closes |
| 5 | Market-making variant: join at p_cal − 2c only when official-feed p_cal is ≥ 10c from mid, T-30..T-10 s | −$0.5…+$1 | very high; s1-maker lost −5.2 c/ct to adverse selection with less information | P18 join arms mean > 0 on ≥ 60 closes with the feed on |
| 6 | P21 scanner kept running | ~$0 (0 executable hits in 15 min; ties are 0–1c gross) | low | ≥ 1 executable hit/day for 7 days |
| 7 | Event-driven crypto, sentiment, options replication | ≤ $0 | — | rejected (§5) |

## 7. Takeaways

1. Our trade is his trade; our difference is an information source whose market lag is, so far, unproven and at the lock provably absent.
2. Paper fills are honest to within ~0.6 c/ct; now measured per fill.
3. Kalshi's crypto ladders are fee-consistent; structural arb there is a monitor, not a P&L line.
4. The scarce resource is settled bets per day, not ideas. Anything that does not raise n (websocket books, cross-platform data capture) or test the information thesis directly is tuning.
