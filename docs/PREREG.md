# Pre-registered evaluation rules (written 2026-10-26, before the no-lookahead hindcast and before any paper result after the 17:10 PT reset)

Nothing below may be changed after seeing results. New families get a new numbered entry; old entries are never edited (append "superseded by #n").

## P1 — S7 crypto taker, T-5..T-2 window (families s7live, s7live-KXETHD, s7live-KXSOLD)
- Rule: buy the side whose model probability (TWAP horizon, BRTI replica spot, trailing 120-min sigma) exceeds the touch by >= 0.05; one position per asset-hour; fill at touch + 1 tick; hold to settlement.
- Hindcast evaluation cell: T-3m, thr 0.05, hour-clustered se (reports/twap/*-nolookahead.log). Pass = mean net per contract > 0 at 95% (mean - 1.96 se > 0) for BTC. ETH/SOL inherit only if BTC passes.
- Paper evaluation: n_required = (1.96 * sd / edge)^2 independent asset-hours, sd from the paper tape, edge = 3c. No threshold/horizon changes before n_required asset-hours; if 95% CI excludes +0 on the negative side at any n >= 60, the family is paused.

## P2 — S7 final-minute (family s7final-KXBTCD)
- Rule: T-1.0..T-0.15 min, partial-TWAP conditional pricing, thr 0.05, lot 10, 3 nearest strikes. Same pass criteria as P1; evaluated separately; cannot be pooled with P1.

## P3 — S1 maker FLB (family s1-maker)
- Rule: join ask at 5-30c / join bid at 50-95c on weather/rain/gas/crypto-daily series 6-36 h out; fills from prints after post beyond queue-ahead (strict and lenient both recorded; STRICT is the evaluation fill rule).
- Pass = per-fill net P&L (strict fills) > 0 at 95% with event-clustered se and n >= 200 strict fills. Index series excluded (trend, not FLB).

## P4 — Weather J1 (families weather-j1, rain-j1)
- Tradable states: monotone-locked only (max already above / min already below, 1F METAR slack). Pass = false-locked rate < 1% on >= 300 locked states AND mean net per contract > fee + 1c.
- Pre-CLI window measurement: opportunities/day with gap >= 5c at local hours 11-16 across stations, from scoreboard rows; decision to keep/retire after 14 days.

## P5 — S7 index (s7index-*), S6 sports, J3, J4
- Scored only until BSS vs TOUCH > 0 at 95% with >= 100 resolved rows (clustered). No trading before that.

## P6 — S7 sigma-regime cell (supersedes P1 for trading; P1 unconditional rule PAUSED 2026-10-26, failed)
- Rule: T-3.5..T-2.5 only, thr .05, lot 10 paper, trade only when trailing-120-min sigma (1-min Coinbase log returns) < fixed threshold BTC 4.39 / ETH 5.71 / SOL 5.75 bp/min. sigma-hi rows scored only (family *-sigma-hi).
- n_required 188 trade-hours (ETH/SOL weight 0.6). Pass: mean net > 0 at 95% hour-clustered AND sigma-lo model Brier < mid Brier. Pause at n >= 60 if CI < 0.
## P7 — Weather morning-low locks: >= 300 locked-low states observed 06:00-12:00 local; false-lock < 1%; report count/day of gap >= 5c with size >= 20; keep/retire after 14 days.
## P8 — Rain METAR tip: >= 100 tip events; pass = mean (1 - ask) at tip+5 min >= 5c with ask_size >= 20, event-clustered 95%.
## P9 — Final-minute books skill: >= 100 settlement hours; pass = model Brier < mid Brier at 95% hour-clustered on T-1.0..T-0.15 near-money snaps (family s7final-KXBTCD-books); report sigma_mkt/sigma_trailing with CI.
## Sizing policy (EDGE_PLAN C1-C5): live stakes use shrunk edge e* = e*tau^2/(tau^2+se^2), tau = 3c; k = 0.25*min(1, e*/(2 se*)); k <= 0.125 until 2x n_required; promotion ladder S0 scored -> S1 paper -> S2 live-micro (lot 5, 14 d, >= 60 fills, realized >= 0.5x paper) -> S3 live.

## P10 — S7 BTC redesign (docs/S7_REDESIGN_2026-10-26.md §4; supersedes P6 for trading, P6 sigma gate REMOVED as in-sample selection)
- KXBTCD only; one decision per hour in T-3.5..T-2.5; spot = BRTI replica; sigma = EWMA(0.94) of 1-min Coinbase log returns refreshed every poll; p = StudentT(nu=8)(z/0.8); threshold = 0.02 + |z|phi(z)*0.17 + spread/2; taker at touch+1 tick, lot 10, resting size >= lot, spread <= 6c, mid in .03-.97; strike = argmax edge/sqrt(p(1-p)); one position per asset-hour.
- Parameters (nu, scale, lambda, thr formula) FROZEN. n_required 222 trade-hours (5c edge) / 616 (3c). Pass: mean net > 0 at 95% hour-clustered. Pause if 95% CI upper < 0 at n >= 60.
- ETH/SOL: identical model, scored-only (families s7p10-KXETHD/-KXSOLD) until their own books-Brier beats mid at 95% on >= 100 hours.
- Secondary scored-only maker family s7p10-KXBTCD-maker5: post p_side - 0.02 at T-5..T-3.5, cancel at T-3, fill iff strictly traded through; evaluate adverse selection (filled vs unfilled P&L).

- P10 amendment 2026-10-26 22:20 PT (user decision, before any ETH/SOL paper result): ETH/SOL trade the P10 model on paper at lot 10 for sampling; evaluated as separate families (s7p10-KXETHD, s7p10-KXSOLD) with the same pass/pause rules; hindcast prior for ETH is negative (-2.9c +-6.6).
