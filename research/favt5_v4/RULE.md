# FavT5 v4 — frozen rule (v4.0, 2026-11-22)

Mirror of a Polymarket profile's 5-minute crypto longshot strategy, ported to Kalshi's 15-minute crypto series.
**Shadow-only: no order route, no live mode, no automatic promotion — same standing rule as v3.**
Rule changes (band, side logic, cohorts, budget) only by owner order, same as v3.

## Provenance (verified, not just the tweet)

- Tweet: @PolyDekos, 2026-11-22 — "buys 5-minute Bitcoin markets at 20–35¢ and collects $1. Made $103,235 across 39,521 markets."
- Profile: `polymarket.com/profile/0xb9587c373c773d023f47c2f85a30b3d5bddcf513` ("blue.birds", joined 2026-10).
- Live-page verification (2026-11-22): **Total PnL +$103.9K**, markets traded **39,928**, total volume **$13.2M**, biggest win **$223.33**.
  → ~0.79% net margin on churned volume; ~$330 average spend per market-turnover (consistent with the $300–900 claim historically).
- Current positions snapshot showed entries at 19.6¢–76¢ on BTC *and* ETH 5-minute up/down, recent share sizes much smaller than the claimed $300–900 (e.g. 9.8 shares @ 65¢, 41.9 @ 29.9¢) — so the tweet's 20–35¢ band is a simplification; the bot buys underpriced probability on both cheap and moderately-priced sides.
- Polymarket 5-minute up/down settle on Chainlink reference at window end vs open; trades are fee-free. Kalshi's analogue series is 15-minute up/down because **Kalshi has no 5-minute series (KXBTC5M/KXBTC5MIN/etc. → 404)**.

## The rule (as implemented in predhft/favt5_v4_shadow.py)

Series: **KXBTC15M + KXETH15M** • Cohorts (the timing experiment): **early_t300** (T−5 min), **late_t090** (T−90 s)

1. At decision D = close − H, read the freshest sequence-valid book (≤ 3 s old, two-sided).
2. `mid = (bid+ask)/2`; **side = YES if mid < 0.5 else NO** (the market's less-likely side; ties → NO).
3. Offer = best displayed ask of that side; trade only if **offer ∈ [0.20, 0.35]** (skips logged with the offer seen).
4. `limit = offer + 0.01`; **budget $50.00**: `qty = floor(50 / (limit + 0.07·limit·(1−limit)))`; holds to settlement. No stop, no exit, one decision per market per cohort.

Fill reality: shadow fills walk displayed offers ≤ limit (measured arrival + replays at +1s/+5s/+30s); per-fill exchange fee rounding.
Settlement: official exchange result (paid $1/contract), fees charged per fill.

**Kalshi taker fee math (the difference from Polymarket):** at 25¢ entry the fee ≈ 1.31¢/contract → breakeven hit rate ≈ **26.3%** (vs 25% fee-free).
The experiment is precisely: does the longshot side in this band win more than breakeven on Kalshi's 15-minute series, and at which timing.

## Review gates

- First read after **200 settled decisions or 7 days**, cohorts compared (early vs late, BTC vs ETH), net after fees per trade and total.
- Any live promotion: separate owner order only (v3's freeze language applies).

## Ops

- Task `PredHFT-FavT5-v4-Shadow` module `predhft.favt5_v4_shadow` in worktree `D:/prediction-hft-v4` (branch `favt5-v4`).
- Ledger `data/favt5-v4/fills.jsonl` (start | meta | decision | fill | settle | alert); hourly paper report cron via `favt5_v4_hourly.py`.
