# Inference architecture — where LLM subagents provide edge (and where they must not touch)

Author stance: 15-yr prediction-market quant. Target: Kalshi (now), Polymarket US (later). Inference = Hermes-spawned subagents
(delegate_task), no self-hosted or third-party inference endpoint. Paper first; every inference is a scored forecast before it is a trade.

## 0. The only five sources of edge in a prediction market
| # | Edge | Who wins | LLM useful? |
|---|---|---|---|
| E1 | Speed: book lags the settlement variable | fastest feed + colocated execution | **No** (adds 20–120 s) — S7 |
| E2 | Behavioral bias and crowd flow: favorite-longshot, round-number, recency, herding into one side | maker with patience + fee discipline; fade crowded flow | **No for direction; sentiment/flow used as a *flow* feature** — S1 |
| E3a | Information (resolving): verify the resolving fact before the crowd reprices | whoever reads the resolution source first and correctly | **Yes — primary (J1)** |
| E3b | Information (shifting): translate a news shock into probability deltas before the book reprices | fastest *reader* of wires/official feeds on slow-repricing markets | **Yes (J4)** |
| E4 | Aggregation/calibration: combine market + outside evidence better than the mid | careful forecaster with reference classes + extremizing | **Yes — secondary, must be hindcast-proven** |
| E5 | Liquidity provision: earn spread, manage inventory to resolution | Feil–Nendel style quoting, inventory limits | **No** (LLM only sets slow priors) |

Published live-money evidence (Prediction Arena 2604.07355; six-month fleet record 2609.05663; Prophets 2607.06166; Raven-Agent
2607.03015): generic "research then bet" agents lose (−16…−31 % on Kalshi); the surviving designs (a) restrict the LLM to belief
formation, (b) bet only where a number was *knowable*, (c) keep sizing/fees/execution mechanical. This architecture encodes exactly that.

## 1. The inference layer has three jobs, never "predict the price"
### J1 — Verifier (E3, the money-maker)
Question to the subagent: *"Given the market's exact rules and the named resolution source, is the outcome already determined or
near-determined by publicly available data right now? Return p, evidence, source timestamp."*
Target series (Kalshi, liquid, rules name a public source):
- Weather daily high/low/rain (KXHIGH*, KXLOW*, KXRAIN*): resolution = NWS daily climate report; the observation (ASOS/METAR, 1–5 min)
  is public hours earlier. After the day's max is set (typically 15:00–17:00 local) P(above strike) is ~0/1 while books still show 5–30¢.
- Economic prints (CPI, NFP, claims, Fed funds, GDP): the number is public at 08:30 ET; markets stay quoted for minutes.
- Sports finals and in-game states (KXNBAGAME/NFL/MLB totals and spreads): final score public before the book is fully swept.
- Nowcastable counts (Rotten Tomatoes, YouTube views, Spotify, AAA gas, box office): the page *is* the resolution source.
- Mentions (KXTRUMPMENTION etc.): transcript/live-caption scanning; a word said = resolved.
The verifier does not forecast; it reads. Its errors are false "resolved" calls → guard with a dual-source requirement and a
confidence field; trade only p ≥ 0.9 or ≤ 0.1 with two agreeing sources, or p blended via J3 otherwise.

### J2 — Rules reader (E3, asymmetric, cheap)
For every market in the universe once at listing: extract settlement source, timezone, rounding, "close vs high", tie handling,
early-resolution clauses, void conditions. Output a structured `rules_card`. Flags: (a) crowd-vs-rules divergences (market trades
as if X but resolves on Y), (b) markets where the settlement variable is observable earlier than the crowd assumes (feeds J1),
(c) toxic markets (ambiguous rules, discretionary resolution) → excluded from all families. Cost: one inference per market lifetime.

### J3 — Reference-class forecaster (E4, probation until proven)
Only for markets ≥ 24 h to resolution without a J1 path. The subagent returns a *structured* forecast: base rate and reference class,
key scheduled catalysts, 3 strongest for/against evidence items with dates, p, and a self-reported confidence. It never sees the
current market price (prevents anchoring; we fuse afterwards). It is scored, not trusted: see §3.

### J4 — News-shock repricer (E3b)
Python feed watchers (wires/RSS, official accounts, ESPN injury API, BLS/BEA/Fed calendars, court dockets, campaign/poll releases)
emit items; a subagent maps each to affected markets and returns {market, direction, delta_logit, confidence, evidence_ts}. Only
markets with a measured repricing half-life > 10 min (from candlestick history) are eligible — the fill must beat the book's own
repricing. Fusion/sizing mechanical. Scored like J3 (p* vs market at ts). Barred on minute-scale strikes.

### Sentiment: flow feature, never a direction signal
Social/news sentiment is not information about the outcome (the market already aggregates it; minute–hour horizons show no
predictive power). It *is* information about retail order flow: herding pushes price away from fair value and is faded (E2).
Primary flow signal = Kalshi trade/volume imbalance and one-sided book growth; social sentiment is a secondary, scored input.
Used to skew S1 quotes and to shrink J3/J4 beliefs when the crowd is already stampeding in the same direction.

## 2. Pipeline (slow layer = inference, fast layer = Python)
```
Universe (Kalshi /markets, /series, /events)                           every 15 min
  → Rules reader J2 (once per market)  → rules_card, exclusion flags
  → Trigger engine (Python, no LLM):
       • calendar triggers: data-release times, game end, local 15:00 for weather highs, show air times
       • observation triggers: ASOS/METAR max ≥ strike, score final, print published (dedicated fetchers where a feed exists)
       • staleness trigger: market mid unchanged N min after a trigger while implied p ≠ 0/1
       • J3 schedule: once per market per day, ≥ 24 h out, liquidity ≥ threshold
       • J4 news feed watchers → item dedupe → eligible-market filter (repricing half-life > 10 min)
  → Inference dispatcher (Hermes delegate_task, parallel ≤ 10, strict JSON output_schema, 90 s budget, cache by (market, evidence_ts))
  → Belief fusion (Python): logit p* = a_s·logit(p_market) + b_s·logit(p_agent) + c_s, coefficients fit per series class on
       hindcast; shrink toward market when series skill is unproven; J1 with dual-source p ≥ .9 bypasses fusion (p* = p_agent)
  → Decision (Python): trade iff |p* − price| > fee(price) + half-spread + margin_s; size = f·Kelly with f ≤ 0.25, caps per
       market/series/day; maker order first, taker only on J1 resolved-state signals where time-to-sweep is short
  → Execution: paper ledger now (same code path later signs Kalshi orders); kill switches: daily loss, inference error rate, API latency
  → Scoreboard: every inference stored as (ts, market, p_agent, p_market_at_ts, p*, outcome) → Brier skill vs market per series
```
Cost/latency budget: J1 fires only on triggers (≈ 50–200/day across weather/sports/econ), J2 ≈ 100–300/day at listing, J3 ≤ 100/day.
Subagent tokens ≈ 20–60k each → bounded, cached, no per-tick inference. S7/S1 run without inference.

## 3. Gates (pre-registered, per series class)
- G1 skill: Brier skill score vs market mid at inference time > 0 with 95 % CI excluding 0, n ≥ 100 scored forecasts.
- G2 fee-positive paper PnL over ≥ 200 trades or 28 days (whichever later), computed against actual touch size.
- G3 error audit: J1 false-resolved rate < 1 %; any discretionary-resolution loss → series excluded.
- Promotion: paper → live only after G1∧G2∧G3 and the Phase-1 legal/account gate. Demotion automatic if the 30-day rolling BSS < 0.
- Anti-leak rules: forecaster never sees market price or other agents' outputs; evidence timestamps must precede the inference ts;
  hindcasts use only sources dated before the market timestamp (hindcast protocol from references/algorithms.md).

## 4. What inference is explicitly barred from
- Direction calls on minute-scale crypto/index strikes (no information at that horizon; the literature finds none).
- Position sizing, fee math, order placement, risk limits (mechanical only).
- "Sentiment" as a direction signal; at most a slow regime feature in J3 after it shows skill in the scoreboard.
- Any output not conforming to the JSON schema (dropped, counted as an inference error).
- Sentiment as a direction signal; social sentiment enters only as a scored flow feature.

## 5. Why this is the edge-providing version
The crowd's weakness on Kalshi is not reasoning — it is **attention and reading**: thousands of markets, public resolution sources
that update before the book, rules the crowd skims. A fleet of cheap, parallel, schema-bound readers attacking exactly those gaps,
with the money decisions kept mechanical and every belief scored against the market, is the one LLM design with a live-money record
of working (Prediction Arena's only profitable run was selective and small; weather/econ "knowable number" markets were where
models beat the crowd). It also compounds: rules_cards and scoreboards are durable assets; the forecaster improves by fitting
fusion coefficients, not by prompting harder.

## 6. Build order
1. Rules reader + universe store (J2) — immediately useful to S1 (toxic-market exclusion).
2. Weather verifier (J1) with ASOS/METAR fetcher + staleness trigger, paper; this is the cleanest knowable-number class.
3. Scoreboard + fusion fit on Kalshi settled history.
4. Econ-print and sports-final verifiers.
5. J3 forecaster on probation; promote only via G1.
