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
