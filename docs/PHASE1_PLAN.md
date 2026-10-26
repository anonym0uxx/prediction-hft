# Phase 1 Plan — Prediction-Market Quant Bot (Kalshi primary, Polymarket US secondary)

Status: DRAFT for Alon's review · 2026-10-26 · paper-only, CPU-only, zero spend, no GPU, no credentials.
Moves into the dedicated repo when the URL exists. Mining is untouched.

---

## 0. Objective and the gate that ends Phase 1

Build and run a paper-trading system that produces an **audited, re-runnable** answer to one question:

> At what bankroll does this system's measured edge × measured capacity produce the daily $ target, after fees, with what drawdown?

Phase 1 ends (and Phase 2 — small real capital — may begin) only when ALL of these hold:

| Gate | Threshold | Why |
|---|---|---|
| G1 Duration | ≥ 28 consecutive paper days, no manual intervention in the strategy loop | Every live case study below that failed, failed inside the first month after a good short paper run |
| G2 Sample | ≥ 500 resolved paper positions across ≥ 3 strategy families | Nick Rae: 217 trades looked great, died live |
| G3 Edge | Net PnL after fees > 0 with 95 % bootstrap CI excluding zero, **per strategy family** | Predict&Profit: P&L positive-looking while model had negative skill |
| G4 Skill | Model Brier/log-loss beats the market's own price at entry time, per family (skill ≠ profit) | Both weather post-mortems |
| G5 Fill realism | Paper fill model validated: ≥ 80 % of paper maker fills would have filled under the strict rule (price traded *through* our quote, not *at* it) | Over-optimistic fills are the #1 paper→live gap |
| G6 Capacity | Measured fillable $/day per family ≥ the size implied by target bankroll; report the saturation curve | UCD paper: capacity, not edge, is Kalshi's binding constraint |
| G7 Ops | Zero unhandled crashes, kill-switch tested, every trade reconstructible from logged book state | Predict&Profit: 7 silent defects in 4 months |

Any gate failing → Phase 1 continues or the family is dropped. No "waiver" path for G3/G4.

---

## 1. Evidence base — real, tracked outcomes (what we build on and what we avoid)

### Profitable, documented
| Case | What | Tracked result | Lesson we take |
|---|---|---|---|
| @defiance_cr (Polymarket, 2024; interview + open-sourced code) | Automated two-sided market making, volatility-ranked market selection | $10k start → ~$200/day → **$700–800/day at peak**; profit fell sharply when Polymarket cut liquidity rewards; shut down & open-sourced | Market-making works; selection by **low volatility + reward/fee structure**; the edge was partly subsidy — ours must survive on spread + FLB alone |
| Anonymous sports MM (Polymarket "Meet Your Market Maker", 2025) | Sharp-line-anchored maker quoting, "sliding scale of liquidity" around fair | Up to $300k in play per NFL Sunday; one month flat then **+$60–65k**; a −$100k 10-day hockey streak | Variance at this edge size is brutal; bankroll ≫ daily target; quote ladder scaled by distance from fair |
| Polymarket arbitrage population (arXiv 2508.03474, IMDEA) | Intra-market rebalancing + combinatorial arbs | **$39.6M** extracted in a year; a top account > $2M | Real but already crowded by bots; our scan found the same arbs are fee-negative at taker on Kalshi |
| Polymarket leaderboard forensics (Finance Magnates / IOSG / rootdata 2026) | — | **14 of top 20 wallets are bots**; ~5 % bot-like wallets drive 75 % of volume; 823 wallets > $100k since Jan 2025 | The counterparty is other bots, not only retail; speed alone is not our edge |
| Kalshi — UCD "Makers and Takers" (Jan 2026, 300k contracts) | Transaction-level returns | **Makers out-earn takers**; <10¢ buyers lose > 60 %; >50¢ contracts earn small positive return | Core strategy: maker-only, sell longshots, never buy them |
| Kalshi — Roosevelt Institute (May 2026) | Aggregate flow | **$583.5M** net retail *taker* losses since 2021 | That is the pool a maker trades against |
| Tokyo CARF "Who Wins and Who Loses" (2026, $67B Polymarket volume) | Actor-level PnL | Top 1 % of winners capture 76.5 % of profit; ~69 % of users lose; **top 0.1 % earners post 47 % of their volume as makers vs 17 % for the bottom 95 %** | Maker share of volume is the strongest single predictor of being a winner |

### Unprofitable / humbling, documented (equally valuable)
| Case | Tracked result | Failure mode → our countermeasure |
|---|---|---|
| Nick Rae weather bot (Kalshi, 2026) | Paper: 217 trades, 76.5 % win, +$1,773 sim; **live on $424: not capital-ready**; a "BOUNDARY_FADE" sub-strategy averaged −$19/trade | Short paper runs + survivorship → G1/G2; audit each sub-strategy separately → G3 per family |
| Predict&Profit weather bot (Kalshi, 4 months live) | **Lost money; Brier worse than guessing** despite plausible P&L path; 7 silent defects found in audit; DST bug | Skill audit independent of PnL → G4; 49 automated tests; timestamps in UTC with DST tests → G7 |
| Turbine 500-strategy KXHIGHNY backtest | **70 / 500 profitable; median ROI −41.6 %**; winners used forecast *plus* live-observation confirmation; worst variants fought the market on one variable | Multiple-testing discipline (we will test many variants: pre-register, hold out, and penalize); weather only as nowcast confirmation, not forecast-vs-market disagreement |
| Polymarket NBA arbitrage study (arXiv 2605.00864) | Executable arbs exist (290 episodes, 101 bp median) but 77 % capped at ~15 shares | Arbitrage is not a business line; it is a free-option add-on |
| Prediction Arena (arXiv 2604.07355, 57-day live LLM cohort) and "When do prophets profit" (2607.06166) | Frontier LLMs trading live: results mixed; only **proper Kelly-style betting** converts forecast accuracy into profit | LLM layer deferred to Phase 2+; sizing is as important as the signal |

---

## 2. Design principles derived from the evidence

1. **Maker-only.** Taker fee (≈0.07·p·(1−p)) exceeds any edge we can find. We post; we never cross except to flatten risk.
2. **Short-dated, recurring markets only** for capital turnover: hourly/daily settlement classes identified in the venue scan (BTC/SPX hourly strikes, game markets, daily weather, daily data-feed markets). Long-dated politics excluded.
3. **Edge must be measured against the market's own price at entry, not against resolution alone** (skill audit, G4).
4. **Capacity first.** Every strategy reports fillable $/day and a saturation curve before edge is even discussed.
5. **Fills are modeled pessimistically** (strict trade-through rule + queue position estimate). If a strategy only works under optimistic fills, it doesn't work.
6. **Pre-registration.** Every strategy variant is written down with its hypothesis and pass criteria before the backtest runs; Turbine's 430/500 losers are what un-registered search produces.
7. **Separation of concerns**: data recorder → book reconstruction → pricing models → quoting engine → paper matcher → risk → reporting. Each testable alone.
8. **Latency honesty.** This is a *low-latency maker* (sub-second quote refresh on WebSocket events), not microsecond HFT. The 15-minute crypto flips and in-play last-minute arbs are speed games owned by colocated bots; excluded from Phase 1.

---

## 3. Architecture (CPU only; runs on the Windows host beside mining)

```
kalshi WS (orderbook_delta, trade, ticker)  ─┐
kalshi REST (markets, events, candlesticks) ─┼─▶ Recorder ──▶ Parquet/SQLite (append-only, UTC)
pm-us WS/REST (stub until key)              ─┘        │
external feeds: Coinbase/Deribit (BTC), NWS/ASOS obs + NBM/GEFS (weather),      │
                sharp-book odds (sports), AAA/ERCOT/charts (nowcast)             ▼
                                                            Book reconstruction (L2, per market)
                                                                         │
                     Pricing models (per strategy family) ◀──────────────┤
                                 │                                        │
                      Quoting engine (Feil–Nendel inventory/TTR control)  │
                                 │                                        ▼
                      Paper matcher (strict fill rule, fees, settlement) ◀┘
                                 │
                 Risk layer (per-family & global caps, kill-switch, loss limits in code)
                                 │
            Reporting: nightly Telegram summary (no LLM) + weekly audit pack (Brier, CI, capacity)
```

- Language: Python 3.12 (uv), async; one process per venue recorder, one strategy host; Windows Task Scheduler for restart-on-failure (same pattern as the mining supervisor).
- Storage: `D:/prediction-quant/data/` Parquet partitions by day; ~1–3 GB/day expected at full Kalshi book coverage of the liquid subset.
- No secrets in Phase 1 (public data only). PM-US key, when it exists, goes in Windows Credential Manager via the same elevated helper pattern used for mining.

---

## 4. Strategy families (pre-registered hypotheses)

| ID | Family | Markets | Hypothesis (falsifiable) | Reference model | Pass criterion (paper) |
|---|---|---|---|---|---|
| S1 | Longshot harvesting (maker) | All liquid multi-outcome events; offers on the <15¢ side | Realized win rate of 5–15¢ contracts is below price by ≥ 3¢ after fees at 24 h and 6 h horizons (replicating UCD) | Whelan 2024 multi-outcome Kelly; FLB curve by category | G3/G4 pass; capacity ≥ $2k/day fillable |
| S2 | Digital-option MM on BTC hourly strikes | KXBTCD/KXBTC | Model-fair (Deribit IV + jump adj.) vs Kalshi mid differs by ≥ 2¢ on ≥ 20 % of strike-hours; maker quotes at fair±1¢ earn positive expectancy | Jump-diffusion/NIG digital; Feil–Nendel quoting | Positive after fees; inventory never > cap at settlement |
| S3 | Digital-option MM on SPX/NDX hourly strikes | KXINXU/KXNASDAQ100U | Same as S2 with intraday vol seasonality; retail prices flat vol | Todorov intraday vol; delayed index data acceptable for paper | Same |
| S4 | Weather nowcast confirmation | KXHIGH{7 cities}, KXRAIN | In the final 6 h, live ASOS obs + NBM move fair faster than the book reprices; forecast-vs-market disagreement alone has **no** edge (Turbine) | NBM/GEFS + station persistence; EMOS-lite calibration | Positive after fees with ≥ 150 resolved positions |
| S5 | Data-feed nowcast | KXAAAGASD, KXDIESELD, KXTXERCOTPEAKD, YouTube/Netflix charts | Settlement source observable before close; tracker vs price gap ≥ fees | Kalman-style tracker + change-point detection | Positive; capacity reported (expected small) |
| S6 (observe only) | Sports maker vs sharp line | NBA/NFL game, spread, total | Kalshi deviates from Shin-devigged sharp consensus by ≥ 3¢ on popular sides; makers on the unpopular side profit | Shin devig; Stern/Markov in-play for later | Paper-quote but **no capital plan** until a sharp-odds feed is licensed (cost item) |

Excluded in Phase 1: 15-min crypto flips, Mentions/politics (worst calibration = most informed flow), long-dated futures, any taker strategy, any LLM layer.

---

## 5. Backtest protocol (weeks 1–2, runs in parallel with the recorder)

1. Pull settled-market history + hourly candlesticks (`/series/{s}/markets/{t}/candlesticks`) for every series in §4, 12 months back where available.
2. Replicate UCD's favorite-longshot curve by category and horizon (24 h, 6 h, 1 h). Publish our reproduction as the first report — if we cannot reproduce published results from public data, nothing else is trustworthy.
3. Walk-forward evaluation for S1–S5: fit on months 1–8, test on 9–12; strict fill rule; Kalshi fee formula; settlement at official result.
4. Metrics per family: net PnL, PnL/contract, Brier & log-loss vs market-at-entry, max drawdown, turnover, **capacity curve** (PnL vs size posted).
5. Multiple-testing control: Bonferroni/White's reality check across variants; pre-registered variants only.

## 6. Paper-live protocol (weeks 3–6)

- Recorder live from day 1 (week 1) so the paper engine has real books; paper strategies switched on at week 3 after backtests pass.
- Fill rule: maker fill only if a trade prints *through* our price (or at our price with modeled queue position < traded size). Taker fills (risk flattening only) at the touch + fee + 1 tick slippage.
- Risk: per-family notional cap, global daily loss limit (paper), auto-disable family on 3-day negative drift beyond CI, settlement-inventory cap at T−5 min.
- Reporting: nightly Telegram line per family (positions, PnL, Brier, fills/quotes ratio); weekly audit pack as Markdown in the repo.

## 7. Deliverables & timeline (CPU only; ~6 weeks)

| Week | Deliverable |
|---|---|
| 1 | Repo scaffold, recorder (Kalshi WS/REST), storage, 49+ unit tests pattern, FLB reproduction report |
| 2 | Backtest harness + S1 & S4 backtests; capacity curves |
| 3 | Paper matcher, risk layer, Feil–Nendel quoting engine; S1 live-paper |
| 4 | S2/S3 pricing models + external feeds; S5 trackers; live-paper |
| 5–6 | Full paper run, weekly audits, G1–G7 scorecard |
| End | **Phase 1 report**: edge, capacity and drawdown per family → bankroll required for the target; recommendation Go/No-Go for Phase 2 |

## 8. Risks and honest expectations

- Most plausible outcome from the evidence: S1 shows a small, real, capacity-limited edge; S2/S3 depend on how many bots already price Kalshi strikes off Deribit (unknown until measured); S4 is the family most likely to be killed by its own audit; S5 is small and reliable.
- $200–400/day requires bankroll ≈ target ÷ (edge/day on deployed capital); with maker edges of 0.3–1 % per turnover and daily turnover, that implies **$30k–130k deployed** — Phase 1's job is to replace this range with a measured number.
- Competition is bots (14/20 top Polymarket wallets). Our advantages: Kalshi's slower, retail-heavier flow; strict audit discipline the failed cases lacked; no subsidy dependence.

## 9. Needed from Alon
1. Repo URL (empty is fine).
2. Later: Polymarket US app account + API key stored via the secret-store helper (not needed for weeks 1–4).
3. Later: the bankroll figure you'd actually deploy — it sets the capacity gate G6.
4. Decision whether to license a sharp-odds feed for S6 (≈$30–100/mo) or leave sports observe-only.
