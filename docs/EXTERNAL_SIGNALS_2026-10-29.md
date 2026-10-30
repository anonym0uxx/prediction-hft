# EXTERNAL SIGNALS — free, latency-appropriate context for the Opus paper trader (2026-10-29)

Status: **research + scored-only feeds + packet section. Paper only. Nothing committed. No risk limit, sizing, gate or policy code
changed; the live P25-v2b cohort (prompt_hash 55bd678581f7293b) is untouched and its packet is byte-identical.** The new section
only exists in a new prompt variant `v2c` (P26, prompt_hash **ee484a634efe25a7**), which is not live until the owner switches the task.

Code: `src/predhft/ext_feeds/` (`__init__.py` packet block + I/O, `polymarket.py`, `hyperliquid.py`, `binance_flow.py`, `events.py`,
`__main__.py` runner), `src/predhft/llm_packet_v2.py` (`external=` argument, `external_signals` key for v2c), `src/predhft/llm_shadow.py`
(`SYSTEM_PROMPT_V2C`, `PROMPTS["v2c"]`, `ext_fields` on the row), `scripts/ext_signals_backfill.py`, `tests/test_ext_feeds.py` (8 tests).
Data: `data/ext/<signal>.jsonl` + `.latest.json`, `data/ext/backfill_cache.json`; report `reports/ext_signals/backfill_latest.json` + `backfill_rows.jsonl`.
Runner (started 2026-10-29 21:10 PT as a plain background process; not yet a scheduled task): `uv run python -m predhft.ext_feeds`.

Owner directive answered here: everything so far tuned on inputs we already had; what **free, legal, 1-15-minute-relevant external**
information exists, what is it worth against a Kalshi mid that already embeds spot, and feed the useful part to Opus as context.

---

## 0. The honest frame

The thing Opus must beat is the KXSOL15M mid at T-10 / T-5. Every statistical model in the tournament (47 configurations,
docs/TOURNAMENT_2026-10-28.md) lost to it; Opus itself is indistinguishable from it (|ΔBrier| ≤ 0.002). The mid is made by market
makers who watch Binance, OKX and Coinbase tick by tick, so **anything that is a function of the current spot price, the last few
minutes of spot returns, or the current perp premium is already in the mid by construction**. An external feed can only add
information if it is (a) a *different crowd's* price for the same event, (b) something that moves the *distribution* (vol regime,
scheduled events, exchange incidents) rather than the point estimate, or (c) a lead of the official index that the Kalshi book has
not yet absorbed — and (c) decays in seconds, which is useless at a 5-10-minute decision time.

The literature agrees with that frame:
* Order-flow imbalance predicts the next price change strongly at the *seconds-to-one-minute* horizon and its explanatory power
  decays fast with the horizon (Cont, Kukanov & Stoikov, *J. Financial Econometrics* 12(1), 2014, "The price impact of order book
  events"; crypto replication: Koutmos & Wei, *Rev. Quant. Finance & Accounting* 2023, "Nowcasting bitcoin's crash risk with order
  imbalance" — informative for *crash risk*, i.e. the tail, more than for the sign).
* Price discovery in BTC happens on the large unregulated venues and on perps; regulated USD spot (the CF Benchmarks constituents)
  follows within seconds to a minute (Alexander & Heck, *J. Financial Stability* 50, 2020; Makarov & Schoar, *J. Financial Economics*
  135(2), 2020, on cross-exchange arbitrage and how fast gaps close).
* Funding / basis predicts returns, if at all, at daily-to-weekly horizons (NBER w32936, "Perpetual futures pricing", 2024; the
  "carry" literature); liquidation cascades matter as a *tail-risk amplifier* on the hour of a large move (Faulkner, working paper
  April 2026, "Forced liquidation cascades in unregulated perpetual futures markets").
* Social sentiment (Twitter/Reddit) has daily-horizon evidence at best (Kraaijeveld & De Smedt, *J. Int. Financial Markets,
  Institutions & Money* 65, 2020); nothing credible at 15 minutes; our own P15 sentiment stack showed no edge (docs/SENTIMENT_DESIGN_2026-10-27.md).
* US macro releases raise crypto realised vol in the minutes around the print (SSRN 4478184 "Bitcoin's price alchemy"; *Finance
  Research Letters* 101, 2026, on FOMC intraday reactions) — a *vol* effect, not a direction.
* Cross-platform prediction markets price the same event differently and lead/lag each other (Ng, Peng, Tao & Zhou, SSRN 5331995,
  2025, "Price discovery and trading in prediction markets"; arXiv 2601.01706 on law-of-one-price violations across platforms).
  Polymarket's own 15-minute crypto product is large (Dune, April 2026: $292M/week at its February peak, since cannibalised by 5-minute
  markets) and was changed to a 60-second Chainlink TWAP in August 2026 after settlement-sniping (CoinDesk, 7 Aug 2026; Polymarket
  changelog 14 Aug 2026) — which, by coincidence, makes it the **same contract structure as KXSOL15M** (60-s average at close vs at open).

So the only candidate with a *moderate* prior is the Polymarket SOL 15-minute market on the same window. Everything else is
low-to-zero prior and is included only because it is free, cheap and the stated owner directive asked for breadth; the packet
marks every block with its age and the prompt says a stale block is absent evidence.

---

## 1. Candidate table (ranked by honest prior of incremental information beyond the Kalshi mid at T-10 / T-5)

Verified from this machine (US residential, no VPN) on 2026-10-29 ~20:55 PT with one request each; "latency" = time from the event
to the value being readable by us.

| # | Signal | Source / endpoint | Auth | Rate limit (published) | Latency | Cost | Legal / ToS note | Mechanism at a 15-min SOL bet | Prior beyond the mid | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **Polymarket "SOL Up or Down 15m"** — Up price, spread, depth, volume on the **same quarter-hour window** | Gamma `events?slug=sol-updown-15m-<window_start>` (tokens, volume); CLOB `book?token_id=` / `prices-history` ; data-api `trades` | none | not published for these public reads; we poll the book every 10 s (2 books) and Gamma once per window + 60 s | book is real time (ws available), our poll ≤ 10 s | $0 | Public read-only data. Polymarket restricts US persons from *trading*; we place no orders, hold no account, no VPN. Settlement is Chainlink 60-s TWAP vs TWAP at open ≈ our CF 60-s average rule; measured index-basis flip rate 2/181 closes (1.1 %). | A second order book pricing the same event, made by a different crowd (Polymarket's fast-market flow is mostly automated, Dune 2026). If the two books disagree, the average of two noisy forecasts is less noisy than either; disagreements may also carry the direction of Binance-watching bots that are not quoting on Kalshi. | **moderate-low**: backfill (§5) says the Polymarket price alone is *worse* than the Kalshi mid (Brier +0.004/+0.006), the 50/50 average is 0.0016 ± 0.0040 better at T-10 (not significant), and in the 107 T-10 closes where they disagreed by ≥ 5 c the Polymarket side won 42 % vs the mid's 35 % implied (+6.6 ± 4.8 pp; at T-5 +3.6 ± 5.6 pp). Information, maybe; not an edge. | **implemented** (`polymarket.py`) |
| 1b | Polymarket "SOL Up or Down 5m" covering the window's **last 5 minutes** (TWAP at close vs price at T-5) | same, slug `sol-updown-5m-<close-300>` | none | as above | exists only from ~2 min before T-5 | $0 | as above | The crowd's price for the *last leg* conditional on the price at T-5: pure drift opinion for the remaining minutes. | low (it is a 5-minute coin flip priced by bots; informative mainly as a sanity check at T-5) | implemented as `leg5m_*` |
| 2 | **Binance spot SOLUSDT aggressor imbalance / CVD** (60 s, 300 s) | `data-api.binance.vision/api/v3/aggTrades?limit=1000` | none | 6000 request-weight/min per IP; aggTrades weight 4; we use ~20 weight/min | sub-second | $0 | Binance documents `data-api.binance.vision` as its no-authentication **market-data-only** host. `api.binance.com`/`fapi` answer HTTP 451 from here and are **not** used; no VPN. Read-only. | Order-flow imbalance → next-minutes price change (CKS 2014); Binance is the price-discovery venue, the official index (Coinbase/Kraken/Bitstamp/Gemini/crypto.com) follows. | **low**: backfill (§5) finds no sign or Brier effect at 5 or 10 min (t = +0.3 / −1.3 at T-10, −0.4 / −0.9 at T-5; in-sample fitted Brier gain 0.003 ± 0.004 = the two fitted parameters). The literature's horizon for OFI is seconds. | **implemented** (`binance_flow.py`) |
| 3 | Binance mid minus official index, **deviation from its 15-min median** (lead-lag nowcast) | same host, `ticker/bookTicker` + `data/cfb/SOL.latest.json` | none | weight 2 | sub-second | $0 | as #2 | If Binance has moved and the CF constituents have not yet printed it, the index catches up in seconds (Alexander & Heck 2020). | **very low** at T-5/T-10 (the gap closes long before close; the persistent ~20 bp USDT/USD basis is removed by the median). Kept because it costs nothing and tells Opus whether the index value it sees is already behind Binance *right now*. | implemented (`basis_dev_bp`) |
| 4 | **Hyperliquid SOL perp**: hourly funding, premium (mark vs oracle), OI + 15-min OI change, 24-h notional | `POST api.hyperliquid.xyz/info {"type":"metaAndAssetCtxs"}` | none | 1200 weight/min per IP; this call weight 20; we use 60/min | ~1 s (block time) | $0 | Public info endpoint; Hyperliquid's app is geo-restricted for US persons, we send no transactions and hold no address. | Leveraged positioning: OI building while premium is negative/positive flags a crowded side whose liquidation would amplify a move (tail, not sign). Hyperliquid is the largest on-chain perp venue (~$0.4-0.5 B/day SOL notional on the day of the probe). | **very low for direction**; a weak *tail/vol* hint. Funding is hourly and predicts nothing at 15 min. | **implemented** (`hyperliquid.py`) |
| 5 | **Scheduled US macro releases** (next high-impact USD event; one inside / 30 min before the window) | ForexFactory `nfs.faireconomy.media/ff_calendar_thisweek.json` | none | unpublished; we fetch hourly | n/a (schedule) | $0 | Public weekly JSON for personal, non-redistributed use; we store derived flags only. `nextweek.json` returns 404, so late in the week the "next" field can be null. | A release in the window raises realised vol → favours the side that needs the move and fattens both tails; zero directional content. | **low but real on a handful of windows per week**; zero on the rest. | **implemented** (`events.py`) |
| 6 | **Constituent-exchange status** (Coinbase, Kraken) | `status.coinbase.com/api/v2/status.json`, `status.kraken.com/api/v2/status.json` | none | statuspage.io public | minutes (operator-posted) | $0 | public | An incident on a constituent changes how SOLUSD_RTI is formed (fewer books, wider dispersion) and how fast it follows Binance. Kraken was "minor: Partially Degraded Service" during the probe. | **near-zero on average, material on rare days** | implemented (`events.py`) |
| 7 | OKX perp funding / premium / OI change / long-short ratio / liquidations; Coinbase trade-tape imbalance; Fear & Greed | already in `context_feed.py` (`packet.context`) | — | — | — | — | — | — | already in the packet; P15 found no edge in the scored form | existing, unchanged |
| 8 | Options IV / skew | Deribit `get_book_summary_by_currency?currency=SOL&kind=option` returns **empty** (no SOL options); DVOL is BTC/ETH only; OKX `opt-summary?uly=SOL-USD` exists but is thin | none | — | — | $0 | public | IV is a daily-horizon vol gauge; the packet already has realised vs market-implied 1-s sigma from the Kalshi price itself, which is the horizon-correct implied vol. | **very low**; no SOL IV source worth the call | not implemented |
| 9 | Bybit trades / liquidations / OI | `api.bybit.com/v5/...` | none | — | — | — | **HTTP 403 from here** (geo-block) — excluded, no VPN | — | — | excluded |
| 10 | Binance futures liquidations (`forceOrder`), funding, OI | `fapi.binance.com` | none | — | — | — | **HTTP 451 from here** — excluded, no VPN (the `.vision` mirror serves spot only) | liquidation prints are the cleanest cascade signal in the literature | would be low-moderate for the *tail* in the final minutes; unavailable | excluded |
| 11 | Coinglass aggregated liquidations / OI across exchanges | REST | **API key** (free tier needs registration) | — | — | $0 | violates "no new credentials" | as #10 | — | excluded by constraint |
| 12 | dYdX v4 SOL-USD perp (oracle, funding, OI) | `indexer.dydx.trade/v4/perpetualMarkets` | none | public | ~1 s | $0 | public | redundant with #4 and OKX | very low | not implemented (works; redundant) |
| 13 | CF Benchmarks constituent trades (Kraken, Bitstamp, Gemini public trade endpoints) | public REST | none | per venue | ~1 s | $0 | public | an exact replica of the index already exists (`index_replica.py`); constituent aggressor flow is a smaller, noisier version of #2 | very low | not implemented |
| 14 | Kalshi related markets (KXSOL hourly / KXSOLD daily) | Kalshi public | existing | existing | — | — | — | different events; the only logical link is at coinciding :00 closes (docs/LAYERX_LESSONS_2026-10-27.md §4) | ~zero | not implemented |
| 15 | Social: X/Twitter | paid API | — | — | — | **$100+/month** | excluded (paid) | daily-horizon evidence only | ~zero at 15 min | excluded |
| 16 | Social: Reddit RSS (existing, read-only), StockTwits (existing) | existing feeds | existing read-only account — **never posts/votes/comments; untouched** | — | minutes | $0 | permanent guard rail respected; nothing changed | P15: scored, no edge | ~zero | existing, unchanged |
| 17 | Social: Farcaster (Warpcast search), Nostr relays, public Telegram / Discord | Warpcast `client.warpcast.com/v2/search-casts?q=solana` works; Nostr needs a websocket client; Telegram/Discord need a bot/account | none / account | unpublished | minutes | $0 | Telegram/Discord scraping needs credentials → excluded; Farcaster/Nostr are public | throughput for "SOL" at a 15-minute resolution is a handful of posts; no literature at this horizon | ~zero | not implemented |
| 18 | Google Trends | pytrends (unofficial) | none | aggressive throttling | **hourly at best, delayed** | $0 | grey (unofficial scraper) | attention predicts daily returns/volume at best | zero at 15 min | not implemented |
| 19 | On-chain SOL flows (whale transfers, exchange inflows, DEX volume) | Solana public RPC (works), DefiLlama (works, daily) | none | public RPC ~100 req/10 s | minutes-hours to be meaningful | $0 | public | exchange-inflow signals are daily; whale transfers are noisy and the price reaction is already in spot | ~zero at 15 min | not implemented |
| 20 | Stablecoin flows, token unlocks, ETF flows | daily sources | — | — | daily | — | — | regime, not a 15-min sign | zero | not implemented |

**Plainly: #8-#20 are unlikely to add information at this horizon; #2-#6 are cheap context with low priors; only #1 has a
moderate prior, and the backfill puts it at "a slightly worse second opinion whose disagreements lean a little its way".**

---

## 2. What was implemented

`src/predhft/ext_feeds/` — one process, four threads, read-only public HTTP, no credentials read anywhere, every row stamped with
receipt time `t` and `version: P26-ext-v1`; `data/ext/<name>.jsonl` (append) + `<name>.latest.json` (atomic replace).

| feed | cadence | fields written | notes |
|---|---|---|---|
| `polymarket` | 10 s | `close_ts, window_start_ts, slug, up_bid, up_ask, up_mid, spread_c, up_bid_qty_top3, up_ask_qty_top3, book_ts_ms, last_trade_px/t, volume_usd, mid_change_5m_c, leg5m_slug/up_mid/up_bid/up_ask` | slug = `sol-updown-15m-<close-900>`; the market for the window that contains `now` is the Kalshi close being decided; Gamma metadata cached per window; `matched_window` is computed at packet time against the snapshot's `close_ts` |
| `hyperliquid` | 20 s | `funding_hourly, funding_annualised_pct, premium_bp, oi_coins, oi_usd, oi_chg_15m_bp, day_ntl_vlm_usd, mark_px, oracle_px, mid_px` | `recentTrades` returns only the last 10 prints (~10 s), so no trade-side flow is derived (would need the ws `trades` stream) |
| `binance_flow` | 15 s | `imb_60s, imb_300s, notional_60s/300s, cvd_60s/300s_usd, n_trades_*, oldest_trade_age_s, bid, ask, mid, binance_minus_official_bp, basis_med_15m_bp, basis_dev_bp, official_age_s` | 1000 aggTrades cover ~3-5 min of SOLUSDT at the probe's activity; `n_trades_300s == 1000` means the 300-s window was truncated (imbalance then covers `oldest_trade_age_s`) |
| `events` | 300 s (calendar hourly) | `next_high_usd_title, next_high_usd_in_min, high_usd_in_window, high_usd_within_30m_before, window_close_ts, calendar_age_s, coinbase_status, kraken_status` | flags are for the window containing `now` |

Sample rows (2026-10-29 21:19 PT, window closing 04:30Z):
```
polymarket   {"t": 1793333941.9, "close_ts": 1793334600, "slug": "sol-updown-15m-1793333700", "volume_usd": 58.8, "up_bid": 0.88, "up_ask": 0.9, "up_mid": 0.89, "spread_c": 2.0, "up_bid_qty_top3": 290.0, "up_ask_qty_top3": 942.8, "last_trade_px": 0.47}
             (Kalshi KXSOL15M at the same second: yes 0.901 / 0.902 — same window, Polymarket 1-2 c lower and 2 c wide)
binance_flow {"t": 1793333931.9, "imb_60s": 0.282, "notional_60s": 239957, "cvd_60s_usd": 67729, "imb_300s": 0.403, "notional_300s": 1042087, "n_trades_300s": 1000, "mid": 97.555, "binance_minus_official_bp": 22.09, "basis_med_15m_bp": 22.18, "basis_dev_bp": -0.09}
hyperliquid  {"t": 1793333941.9, "funding_hourly": -1.21e-05, "funding_annualised_pct": -10.63, "premium_bp": -7.4, "oi_usd": 533388871, "oi_chg_15m_bp": 51.27, "day_ntl_vlm_usd": 439067933, "mark_px": 97.494, "oracle_px": 97.555}
events       {"t": 1793333901.9, "next_high_usd_title": null, "high_usd_in_window": false, "high_usd_within_30m_before": false, "coinbase_status": "none:All Systems Operational", "kraken_status": "minor:Partially Degraded Service"}
```

### 2.1 Packet section (`llm_packet_v2.build_packet_v2(..., external=)`, key `external_signals`, **v2c only**)

`ext_feeds.packet_block(now, close_ts)` reads the four `latest.json` files and emits, per source, the kept fields plus `age_s`,
`stale` (thresholds: Polymarket 45 s, Hyperliquid 90 s, Binance 90 s, events 3 h) and a one-sentence `note`; the Polymarket block
adds `matched_window` (its `close_ts` == the snapshot's). A missing file is `{stale: true, age_s: null}`. ~2.2 kB of JSON (~600 tokens).
The section is appended after `context` and before `direct_arm`; for v2a/v2b the key is never added (test
`test_external_section_only_in_v2c_packet`). The row logs an `ext_fields` summary (Polymarket mid/bid/ask/age/stale/matched, Binance
imbalances and basis deviation, Hyperliquid funding/premium/OI change, event flags) so the scorer can pair it with outcomes later.

### 2.2 Prompt change (exact)

`SYSTEM_PROMPT_V2C` = `SYSTEM_PROMPT_V2B` with **one line** inserted after item 7 of "THE PACKET IS PRE-DIGESTED" (test asserts
`SYSTEM_PROMPT_V2C.replace(_V2C_EXT_LINE, "", 1) == SYSTEM_PROMPT_V2B`):

> 8. external_signals (P26, context from other venues; every block carries age_s and stale - a stale or window-mismatched block is absent evidence): polymarket_sol15m = Polymarket's 'SOL Up or Down 15m' market on this SAME quarter-hour window (Chainlink 60-s TWAP at close vs at open; Up ~ YES), an independent crowd's price with its spread and depth; hyperliquid_sol_perp = funding, premium and 15-min open-interest change on the largest on-chain perp; binance_spot_flow = Binance spot aggressor imbalance / CVD over 60 and 300 s and the Binance-minus-official-index basis deviation; events = next high-impact US macro release and constituent-exchange status. None of these settles the contract; they do not replace steps 1-7.

Decision procedure, failure-mode list, output schema: unchanged. Packet version string `P26-llm-v2c`.
Hashes: v1 `6c8d03af884f0000`, v2a `d1a3852ffee0b21a`, v2b `55bd678581f7293b` (all unchanged), **v2c `ee484a634efe25a7`**.

---

## 3. Legal / ToS / rate-limit summary

* No credentials anywhere in `ext_feeds`; no orders; no VPN; nothing posted. The Reddit read-only account is not referenced by this work.
* Polymarket: public Gamma/CLOB/data-api reads (the same endpoints the public site uses). Polymarket's ToS restrict *trading* by US persons;
  data reads carry no account and no funds. Poll budget ~15 requests/min total.
* Binance: only the documented market-data-only host; `api.binance.com` is geo-blocked here (451) and never called. ~5 requests/min.
* Hyperliquid: public `info` endpoint, 60 weight/min of 1200. dYdX, Kraken, Bitstamp, Gemini probed but not used.
* ForexFactory JSON: personal use, hourly fetch, derived flags only. Statuspage JSON: public.
* Bybit (403), Binance futures (451), Coinglass (key), X (paid), Telegram/Discord (account) are **excluded** under the constraints.

---

## 4. Backfill method (`scripts/ext_signals_backfill.py`)

* Closes: the 182 KXSOL15M closes with a two-sided Kalshi quote within 20 s of T-10 / T-5 in `data/open_quotes/quotes.jsonl`
  (2026-10-27 21:45 PT → 2026-10-29 21:15 PT); outcome from the official feed (exact rule) with the Kalshi result cross-check
  (`llm_shadow_score.settle`); 0 unsettled.
* Polymarket: Gamma by slug → Up token → CLOB `prices-history` (fidelity 1 ≈ one point per minute) → last point at or before the
  Kalshi quote time (median age 41 s — **this handicaps Polymarket by ~40 s of staleness**; the live feed is ≤ 10 s). Polymarket's own
  resolution (`outcomePrices`) gives the index-basis flip rate.
* Binance: `aggTrades` for the 300 s before each decision time (startTime/endTime paging), imbalance over 300 s and 60 s.
* Statistics: one observation per close per decision time (so close-clustered SE = sd/√n); paired Brier(mid) − Brier(x); in-sample
  logistic `y ~ a + b·logit(mid) + c·x` (Newton); disagreement cells; a sign test (hit of the Polymarket-favoured side minus the
  mid's implied rate). **All fitted coefficients are in-sample and labelled so.**

## 5. Backfill result (reports/ext_signals/backfill_latest.json, generated 2026-10-29 21:18 PT)

Index basis: Polymarket (Chainlink TWAP) and our CF settlement disagreed on **2 of 181 closes (1.1 %)** — the two contracts are the same event to within that.

| decision | n | Brier mid | Brier Polymarket | Brier ½(mid+poly) | paired mid − poly | paired mid − avg | mean \|poly − mid\| | corr(logits) | in-sample logit b_mid / b_poly (t_poly) |
|---|---|---|---|---|---|---|---|---|---|
| T-10 | 180 | 0.1773 | 0.1810 | 0.1758 | −0.0037 ± 0.0080 | **+0.0016 ± 0.0040 (t 0.4)** | 8.7 c | 0.91 | 0.72 / 0.45 (1.28) |
| T-5 | 181 | 0.1186 | 0.1249 | 0.1183 | −0.0062 ± 0.0073 | +0.0004 ± 0.0036 (t 0.1) | 7.4 c | 0.95 | 0.73 / 0.24 (0.76) |

Disagreement cells (Polymarket-favoured side; "excess" = realised hit − the mid's implied rate for that side):

| decision | \|poly − mid\| ≥ 5 c | poly side hit | mid implied | poly said | excess ± SE | \|poly − mid\| ≥ 10 c | excess ± SE | any ≥ 1 c | excess ± SE |
|---|---|---|---|---|---|---|---|---|---|
| T-10 | n 107 | 0.421 | 0.354 | 0.485 | **+6.6 ± 4.8 pp** | n 58 | +10.9 ± 6.1 pp | n 169 | +2.3 ± 3.2 pp |
| T-5 | n 76 | 0.368 | 0.333 | 0.482 | +3.6 ± 5.6 pp | n 44 | +6.1 ± 6.3 pp | n 151 | +2.9 ± 3.0 pp |

Restricting to Polymarket points ≤ 30 s old (n 29 / 25) gives the same picture with wider errors (mid − avg −0.002 ± 0.009 / +0.003 ± 0.008).
Polymarket prices are on average closer to 0.5 than Kalshi's (mean |p − 0.5| 0.205 vs 0.228 at T-10) — the thinner book is less
decisive, which is most of why its Brier is worse, and why the truth sits between the two when they disagree.

Binance aggressor imbalance (in-sample logit with the mid as the other regressor):

| decision | imb_300s: c (t) | Brier gain in-sample | corr(imb, y − mid) | imb_60s: c (t) | Brier gain in-sample | corr |
|---|---|---|---|---|---|---|
| T-10 | +0.19 (0.32) | 0.0026 ± 0.0035 | +0.02 | −0.48 (−1.25) | 0.0035 ± 0.0047 | −0.09 |
| T-5 | −0.25 (−0.37) | 0.0026 ± 0.0040 | −0.03 | −0.42 (−0.86) | 0.0030 ± 0.0043 | −0.07 |

**Read.** (i) The Polymarket price *alone* is a worse forecaster than the Kalshi mid by 0.4-0.6 Brier points (not significant).
(ii) Averaging the two is at best 0.0016 ± 0.0040 better — indistinguishable from zero at n = 180; the in-sample logit puts ~0.4 of
the weight on Polymarket at T-10 (t 1.3) and ~0.25 at T-5 (t 0.8). (iii) When they disagree by ≥ 5 c (which is 59 % of T-10
closes — the two books are *not* tightly arbitraged), the Polymarket side wins more often than the Kalshi price implies (+6.6 ± 4.8 pp
at T-10), but far less often than Polymarket itself says (48.5 %): the honest estimate sits between the two, nearer Kalshi.
(iv) Binance order flow has no detectable sign effect at 5 or 10 minutes; its 60-s imbalance even points the wrong way (t −1.25),
consistent with the literature's seconds-scale horizon. Nothing here is a trading rule; it is the prior Opus is handed with the data.

---

## 6. PREREG — P26 entry (also appended to docs/PREREG.md)

See docs/PREREG.md §P26. Summary: cohort `P26-v2c` (prompt_hash ee484a634efe25a7) = P25-v2b packet + `external_signals` + the one
prompt line above; same model, temperature, decision times, budget, hard limits, execution model and direct arm as P25; primary and
secondary endpoints, the 100-close read, PROMOTION-CANDIDATE / STOP / CONTINUE rules **identical to P25**, plus two P26-specific
diagnostics: (a) the fraction of answers whose `beyond_market` cites an external block, and (b) paired Brier(mid) − Brier(p_own)
split by whether the Polymarket block was fresh-and-matched vs stale/mismatched (pre-declared split, not a selection rule).
H1: with an independent second market in view, p_own stops re-pricing the Kalshi mid and the Brier difference vs the mid is ≥ 0;
H0: Opus averages the two prices and lands between them, indistinguishable from the mid as before. Switching the live task to v2c
requires an owner directive and a PT-timestamped note in PREREG; the switch is a cohort boundary.

## 7. Unverified / open

* The feeds have run for ~20 minutes as a plain background process (started 21:10 PT); they are not yet a Windows scheduled task
  and the live Opus task has not been restarted (parent's call). Overnight stability (Polymarket slug roll at each quarter hour,
  Hyperliquid/Binance availability, log growth ≈ 15 MB/day total) is not yet observed.
* Polymarket book depth is in shares; `volume_usd` is Gamma's figure (lagging). Whether the live 10-s book is a better signal than the
  ~40-s-stale backfill price is a guess until `ext_fields` rows accumulate; the P26 split (b) measures it.
* `basis_dev_bp` needs ≥ 10 rows (2.5 min) of tape before it appears; after a restart it is null for that long.
* The Hyperliquid `premium` field is taken from the venue; if it is ever null the code falls back to mark/oracle − 1.
* No offline Opus dry-run of v2c was performed (budget; the P25 dry-run was in-sample for the prompt anyway). The first live v2c
  decisions should be eyeballed for the model treating `external_signals` as an anchor instead of context — the prompt line tells it
  not to, but P25's audit shows it re-prices whatever it is shown.
* The ForexFactory `nextweek` file is 404; from Friday afternoon PT the `next_high_usd_*` fields can be null even when Monday has a release.
