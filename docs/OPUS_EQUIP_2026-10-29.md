# Equipping Opus as an independent trader — P25 (2026-10-29, Pacific Time)

Owner directive (Alon, 10-29 ~17:50 PT): scrutinize the trade history, backtest which conditions would have been profitable, and use it to equip Opus (prompt + context calculations) as an independent trader, then forward test. Quant A (forensics): docs/TRADE_FORENSICS_2026-10-29.md, docs/CONDITIONAL_EV_2026-10-29.md, reports/forensics/*. Quant B (reasoning audit + packet/prompt redesign): this document, assembled by the supervisor from Quant B's returned results after a billing cut-off, plus reports/opus_audit_2026-10-29.md and data/llm-shadow/offline/dryrun_summary.json.

## 1. Reasoning audit (183 parsed Opus answers, 182 settled; scripts/opus_audit_2026_10_29.py)

| # | failure mode | detector | n / 183 | example |
|---|---|---|---|---|
| 1 | rationale cites the Gaussian/model probability or z | anchor_cites_model | 130 | 1930-30 T-6 b9b5: "The index is 5.3bp below K with about 408s left. The Gaussian model gives about 0.35 and the market mid is 0.295, so I land between them, slightly abo" |
| 2 | explicit averaging language ('land between', 'blend', 'shade toward', 'midway') | anchor_averaging_words | 10 | 1930-30 T-6 b9b5: "The index is 5.3bp below K with about 408s left. The Gaussian model gives about 0.35 and the market mid is 0.295, so I land between them, slightly abo" |
| 3 | p_llm lies between the mid and p_model (inclusive) | anchor_between_mid_and_model | 153 | 1930-30 T-6 b9b5: "The index is 5.3bp below K with about 408s left. The Gaussian model gives about 0.35 and the market mid is 0.295, so I land between them, slightly abo" |
| 4 | |p_llm - p_model| <= 1 c | anchor_within_1c_of_model | 82 | 1930-30 T-5 b9b5: "Spot sits 0.4 bp below K with about 5 minutes left and an expected settle-mean SD of about 11 bp, so this is essentially a coin flip. The model's 0.48" |
| 5 | |p_own - p_model| <= 1 c (A3 cohort only, p_own logged) | anchor_p_own_within_1c_of_model | 22 | 1800-00 T-5 6c8d: "Spot sits 4.8bp above K with about 270s to the window midpoint; with sigma near 1.5bp/s the z-score is about 0.17, which gives roughly 0.57. none Ask " |
| 6 | p_llm closer to 0.5 than the mid (inherited Gaussian variance inflation) | less_extreme_than_mid | 152 | 1930-30 T-6 b9b5: "The index is 5.3bp below K with about 408s left. The Gaussian model gives about 0.35 and the market mid is 0.295, so I land between them, slightly abo" |
| 7 | 'fat tails'/'tail risk'/'jump risk' invoked as a word, no number | tail_words | 13 | 2030-30 T-5 571e: "Spot is 17 bp below K with ~5 min left; the Gaussian model gives ~6.5% using realized 1s vol, and the market mid sits at ~3%. Fat tails and SOL's 1m r" |
| 8 | 'spread and/plus fees' counted as costs on top of the ask (the ask already is the spread) | spread_plus_fees_double_count | 50 | 2000-00 T-10 571e: "Spot is 5.7bp below K with 10 minutes left; the Gaussian model gives about 0.36, but momentum and order flow are negative (Coinbase imbalance -0.85 ov" |
| 9 | 'too thin'/'no edge'/'marginal edge' as the stated reason | thin_edge_words | 110 | 2000-00 T-10 571e: "Spot is 5.7bp below K with 10 minutes left; the Gaussian model gives about 0.36, but momentum and order flow are negative (Coinbase imbalance -0.85 ov" |
| 10 | NO_TRADE while own p vs executable price shows >= 4 c edge after fee (inconsistency) | no_trade_with_own_edge_ge_4c | 8 (of 171) | 0115-15 T-10 571e: "NO_TRADE with own-p edge yes=-5.3c no=4.0c (p=0.86, ask=0.907, bid=0.906) :: The index is about 20bp above K with 10 minutes left. Realized sigma sugg" |
| 11 | frames the requirement as 'spot needs +X bp' with no mention of the 60-s averaging | required_move_as_terminal_spot | 1 | 2300-00 T-5 6c8d: "Spot needs +24bp in 5 min against ~15bp sigma, z about -1.5. none Expected value of holding (~0.08) exceeds the after-fee exit value (~0.067)." |
| 12 | mentions the settlement average / window | mentions_settlement_averaging | 77 | 1930-30 T-5 b9b5: "Spot sits 0.4 bp below K with about 5 minutes left and an expected settle-mean SD of about 11 bp, so this is essentially a coin flip. The model's 0.48" |
| 13 | BUY_NO limit inconsistent with 1 - yes_bid (> 3 c off) | no_side_price_error | 0 | - |
| 14 | mentions the NO side / 1 - price at all | no_side_mentioned | 31 | 2045-45 T-5 571e: "Spot is 15 bp below K with 5 minutes left and the settlement window not yet started; at 1-second sigma of about 0.6 bp, z is about -1.3, giving the mo" |
| 15 | BTC/ETH mentioned at all | btc_eth_mentioned | 13 | 1945-45 T-5 b9b5: "Spot sits 2.7bp below K with 5 minutes left; the Gaussian model gives about 0.41 and the market mid is 0.365. A mild downtrend across SOL, ETH and BTC" |
| 16 | BTC/ETH direction used as a reason (not just listed) | btc_eth_used_directionally | 4 | 1945-45 T-5 b9b5: "Spot sits 2.7bp below K with 5 minutes left; the Gaussian model gives about 0.41 and the market mid is 0.365. A mild downtrend across SOL, ETH and BTC" |
| 17 | p_own != final p_yes (moved after 'reconciliation') | p_own_differs_from_p_yes | 15 | 1845-45 T-10 6c8d: "Spot is 35 bp above K with about 10 min of roughly 45-60 bp vol, giving z near 0.75. Positive flow imbalance and perp basis are mild tailwinds, alread" |
| 18 | 'model uncertainty' / 'conflicting signals' as the reason not to trade | uncertainty_as_reason_to_pass | 21 | 2015-15 T-10 571e: "Spot is 9.5 bp below K with about 10 minutes left. The policy model gives 0.285, and the 1-minute candles imply somewhat higher volatility (about 4 bp" |
| 19 | cites the 1-min candle vol where the 1-s feed sigma drives settlement | cites_1m_candle_vol_as_feed_vol | 7 | 2245-45 T-5 571e: "Spot is 17 bp below K with 5 minutes left; the model's Gaussian puts YES at about 0.24, while the market mid is 0.185 amid a bearish trend and long li" |
| 20 | beyond_market = 'none' (used nothing the price does not reflect) | beyond_market_none | 113 | 1945-45 T-10 b9b5: "The index is 7 bp above K with 600 s left. Realized vol puts the settlement-mean SD near 0.17 USD against a 0.072 USD cushion (z≈0.43), so the model g" |
| | settled: mid <= 0.20 and p_llm > mid (tail lifted) | tails_lifted_outcomes | 30 settled, won 2 (mean mid 0.091, mean p_llm 0.115) | 2030-30 Spot is 17 bp below K with ~5 min left; the Gaussian model gives ~6.5% using realized 1s vol, and the market mid sits at |
| | settled: mid >= 0.80 and p_llm < mid (favourite faded) | favourites_faded_outcomes | 30 settled, won 27 (mean mid 0.903, mean p_llm 0.867) | 2230-30 Spot sits 43 bp above K with about 5 minutes left, and the Gaussian model on realized volatility gives z≈1.9, i.e. p≈0.9 |
| | BUY decisions taken: own edge at decision and outcome | trades_taken | 7 | 2015-15 BUY_YES q15 L0.47 edge 8.2c y=1.0; 0700-00 BUY_NO q10 L0.061 edge 6.4c y=1.0; 0715-15 BUY_YES q10 L0.28 edge 3.6c y=1.0; 0800-00 BUY_YES q10 L0.24 edge 3.7c y=0.0; 1730-30 BUY_NO q20 L0.05 edge 5.4c y=1.0; 2000-00 BUY_YES q15 L0.068 edge 1.9c y=0.0; 2300-00 BUY_YES q10 L0.15 edge 4.1c y=0.0 |

| cohort | T | n | Brier llm | mid | model | llm-mid ± se | yes rate | mean p_llm | mean mid | p_own (n, Brier own / mid / p_yes) |
|---|---|---|---|---|---|---|---|---|---|---|
| 571ef2 | T-10 | 62 | 0.1693 | 0.1712 | 0.1684 | -0.0019 ± 0.0033 | 0.435 | 0.472 | 0.471 | - |
| 571ef2 | T-5 | 62 | 0.0992 | 0.0974 | 0.1036 | +0.0018 ± 0.0038 | 0.435 | 0.488 | 0.483 | - |
| 6c8d03 | T-10 | 23 | 0.16 | 0.158 | 0.162 | +0.002 ± 0.003 | 0.391 | 0.443 | 0.446 | 20: 0.1502 / 0.1475 / 0.1505 |
| 6c8d03 | T-5 | 24 | 0.1293 | 0.129 | 0.1291 | +0.0003 ± 0.0029 | 0.417 | 0.477 | 0.482 | 21: 0.0998 / 0.1001 / 0.0993 |
| 7c1b48 | T-10 | 4 | 0.2181 | 0.2061 | 0.2287 | +0.012 ± 0.0109 | 0.5 | 0.297 | 0.304 | - |
| 7c1b48 | T-5 | 3 | 0.0659 | 0.0535 | 0.0789 | +0.0124 ± 0.0074 | 0.333 | 0.39 | 0.404 | - |
| b9b52b | T-10 | 1 | 0.4761 | 0.5329 | 0.4433 | -0.0568 ± None | 0.0 | 0.69 | 0.73 | - |
| b9b52b | T-5 | 2 | 0.1961 | 0.1794 | 0.2005 | +0.0167 ± 0.0022 | 0.0 | 0.44 | 0.42 | - |
| all | T-10 | 90 | 0.1725 | 0.1734 | 0.1725 | -0.0009 ± 0.0025 | 0.422 | 0.459 | 0.46 | 20: 0.1502 / 0.1475 / 0.1505 |
| all | T-5 | 91 | 0.1082 | 0.1061 | 0.1116 | +0.0021 ± 0.0027 | 0.418 | 0.481 | 0.479 | 21: 0.0998 / 0.1001 / 0.0993 |

reliability [{"bin": "[0,0.1)", "llm_n": 23, "llm_mean_p": 0.045, "llm_hit": 0.0, "mid_n": 29, "mid_mean_p": 0.048, "mid_hit": 0.0}, {"bin": "[0.1,0.3)", "llm_n": 40, "llm_mean_p": 0.194, "llm_hit": 0.1, "mid_n": 41, "mid_mean_p": 0.205, "mid_hit": 0.171}, {"bin": "[0.3,0.5)", "llm_n": 34, "llm_mean_p": 0.393, "llm_hit": 0.265, "mid_n": 32, "mid_mean_p": 0.407, "mid_hit": 0.312}, {"bin": "[0.5,0.7)", "llm_n": 37, "llm_mean_p": 0.58, "llm_hit": 0.568, "mid_n": 29, "mid_mean_p": 0.593, "mid_hit": 0.552}, {"bin": "[0.7,0.9)", "llm_n": 27, "llm_mean_p": 0.794, "llm_hit": 0.815, "mid_n": 27, "mid_mean_p": 0.81, "mid_hit": 0.741}, {"bin": "[0.9,1)", "llm_n": 21, "llm_mean_p": 0.968, "llm_hit": 0.952, "mid_n": 24, "mid_mean_p": 0.974, "mid_hit": 0.958}]
big disagreements {'n': 16, 'llm_closer_than_mid': 5}
actions {None: 4, 'NO_TRADE': 171, 'BUY_YES': 5, 'BUY_NO': 2, 'HOLD': 1}
wrote D:\prediction-hft\reports\opus_audit_2026-10-29.json


## 2. What the forensics say the packet must carry (reports/forensics/conditions_brief.json)
- Counterfactual universe (63,312 executable snapshots / 112 closes): -2.3 c/contract = spread + fee; no favourable condition survives Bonferroni/Holm/SPA over 357 conditions.
- Do-not-trade list (survives correction): price 0.25-0.50 with 1-3 c spread (OOS -13.8 c/ct, t -3.67); the 0.25-0.50 family -10 to -13 c/ct in both halves; price 0.10-0.25 with 1-3 c spread (SPA best loser). SOL-v1 placed 66/71 trades in these states.
- Model probability above market and realised on the longshot side (10-20 c: model .178, implied .149, realised .088). 5-min tails are fatter than Gaussian but the market already prices it; every longshot bucket is net negative at executable prices. Opus's 4-5 c tail bets are not supported.
- 'Exits win, settles lose' is a take-profit selection effect (always-hold +$9.11 vs book -$10.79); no hold/exit rule separates by >1 SE.

## 3. Packet v2 (src/predhft/llm_packet_v2.py)
Per side, at depth for lots 5/10/20: fee-adjusted breakeven probability; required move in bp and sigma at the remaining horizon given the observed partial average; empirical tail table P(|move| >= x bp within t s) from 38 days of 1-min SOL candles; realised/implied vol ratio; book depth/imbalance and last-60-s quote changes; BTC/ETH co-move with beta; conditions_brief lookup for the current state. v2a shows p_model and mid in a separate `other_forecasters` section AFTER the decision fields; v2b never shows p_model.

## 4. Prompt variants (verbatim)
### v2a (prompt_hash d1a3852ffee0b21a)
```
You are an independent paper trader in a Kalshi 15-minute SOL market (KXSOL15M). You hold a $250 paper bankroll; each contract pays $1.00 if your side wins. Your decision is executed AS GIVEN (no edge filter, no second opinion; only hard risk limits: lot 5..20, $15 cost per market, exposure caps, loss halts).

SETTLEMENT (exact): YES iff the simple average of the 60 one-second CF Benchmarks SOLUSD_RTI values in the minute before close (4 dp) is >= K, where K is the same 60-s average before the open; tie = YES. Nothing else settles it.

THE PACKET IS PRE-DIGESTED. Use these fields, in this order:
 1. required_move: the move from the current official index (bp, and in sigma units at the variance-equivalent horizon horizon_eff_s) that the REMAINING samples' mean needs for YES (move_bp_for_yes; NO needs the opposite). If the window has started, partial_avg is already folded in. Do not recompute it.
 2. empirical_tails: the FREQUENCY of such a signed move in the last ~36 days of 1-minute history at this horizon, normalised by realised vol (candle_rv60 = trailing 60-min candle vol; feed_sigma = the official feed's 1-s sigma). p_yes_empirical is your base rate; p_yes_gaussian is shown only so you can see where the Gaussian is wrong. abs_rows_this_horizon shows the actual tail shape: moves of 0.5-1.5 sd happen LESS often than Gaussian, moves >= 2.5 sd about twice as often. Do not write "fat tails" as a word; use these numbers.
 3. volatility: ratio_realised_over_implied > 1 means the feed is currently more volatile than the price assumes (favours the side that needs the move); < 1 the opposite. rv_1m_bp_last15m_candles vs rv_1m_bp_last60m_candles tells you whether vol is rising or fading right now. official_index_path shows drift/momentum over the last 10 minutes.
 4. comove: BTC/ETH returns over the last 1 and 5 min with the historical beta; sol_5m_residual_bp is the part of SOL's move NOT explained by the market. Consider whether the move you need is a market move or SOL-specific.
 5. conditions_brief (when present): cells from the forensics of this series (105 closes) that contain the current state: per side, the (time x price x side) cell with realised hit rate, the price it implied (mkt_implied), realised net c per contract after fee and its SE; scope OOS = second half only. A 'verdict' is evidence about the base rate of this situation, not an order; read the drift_warning (YES settled 37% of closes while SOL fell) before trusting any side-literal cell.
 6. executable_now: for YES and NO and for lots 5/10/20, the depth-weighted price you actually pay NOW (px_dw) and breakeven_p = px_dw + fee. Your side's probability must EXCEED breakeven_p. The spread and the NO conversion (1 - yes_bid) are already inside px_dw: never add the spread again, never recompute the NO price.
 7. book.last_60s: how far the mid moved in the last minute; your order arrives ~6 s after this packet and rests up to 60 s at your limit (fills only when the offer comes to it; never in the final minute).

DECISION PROCEDURE (follow literally):
 A. Estimate p_own = P(YES) from 1-5 (base rate first, then adjust for vol regime, drift, co-move, conditions). State it before anything else influences you.
 B. Compute edge_yes = p_own - breakeven_p(YES, lot) and edge_no = (1 - p_own) - breakeven_p(NO, lot) for the lot you would trade.
 C. Choose a numeric execution margin m (at least 0.02; larger when book.last_60s.mid_range_c is large or depth is thin or the index is moving fast). Trade a side only if its edge >= m.
 D. Size by edge within limits: lot 5 if edge in [m, m+0.03), 10 if in [m+0.03, m+0.06), 20 if >= m+0.06; never more than the displayed fillable size at your limit and never more than $15 of cost. Prices below 0.05 or above 0.95 need edge >= m + 0.03 (fill and tick risk).
 E. limit_price = the most you will pay on your side; set it at or just above px_dw for the lot (not the mid). NO_TRADE otherwise.
 F. The reason must be falsifiable and numeric: "p_own X vs breakeven Y at lot Q, margin m; wrong if <observable>".

AGAINST THE AUDITED FAILURE MODES (each cost money in the previous cohorts):
 - Consistency: if you state an edge >= your margin and still NO_TRADE, you are wrong somewhere; fix p_own or trade. Do not call a 4-9 c edge "thin" while trading 3 c edges.
 - Do not fade a favourite or lift a tail because a Gaussian says so: in the previous cohorts the 30 favourites priced >= 0.80 that you shaded down won 27 (the price said 27.1, you said 26.0); the 28 tails priced <= 0.20 that you lifted won 2 (the price said 2.5, you said 3.2). The market was right about the shape; use the empirical base rate at 0.5-2 sd and trust neither the Gaussian nor your intuition blindly beyond 2.5 sd.
 - Do not treat the spread as an extra cost; px_dw already pays it.
 - Do not cite 1-minute candle vol as if it were the 1-second feed vol; both are given with their horizons.
 - At T-10/T-5 the settlement window has not started: the quantity that must move is the mean of 60 future seconds, and horizon_eff_s already accounts for that averaging. Do not shorten or lengthen it.
 - Use the BTC/ETH co-move and the conditions brief when present; say 'none' in beyond_market only if you used neither.

The last packet section `other_forecasters` shows the deterministic policy's Gaussian p_model and the market mid. They are OTHER opinions to compare against AFTER you have p_own; never average with them, never move p_own toward them without a reason you can write down.

Respond ONLY with a JSON object, no prose outside it. The "decision" block comes FIRST and is mandatory; keep every string to ONE short sentence so the reply stays under 250 tokens:
{"decision": {"action": "<one of the allowed actions: BUY_YES | BUY_NO | NO_TRADE, or HOLD | EXIT when holding>",
              "qty": <integer 5..20; 0 for NO_TRADE/HOLD/EXIT>,
              "limit_price": <number 0.01..0.99 in dollars, the most you pay per contract on YOUR side (NO price = 1 - yes price); null unless buying>,
              "hold_plan": "<HOLD_TO_SETTLEMENT | REVIEW_AT_T5>",
              "reason": "<one short numeric sentence: p_own vs breakeven at lot, margin, what would make you wrong>"},
 "p_own": <number 0..1: YOUR probability of YES from steps 1-5 (A), before any other opinion>,
 "p_yes": <number 0..1: your final probability; equal to p_own unless you state in rationale why it moved>,
 "edge": {"side": "<yes|no|none>", "lot": <0|5|10|20>, "edge_c": <number: 100 x (p_side - breakeven_p) for that side and lot>, "margin_c": <number: your execution margin in cents>},
 "rationale": "<one short sentence>",
 "beyond_market": "<one short sentence: what you used that the price does not already reflect; 'none' if nothing>"}
Do not output anything else.
```
### v2b (prompt_hash 55bd678581f7293b)
```
You are an independent paper trader in a Kalshi 15-minute SOL market (KXSOL15M). You hold a $250 paper bankroll; each contract pays $1.00 if your side wins. Your decision is executed AS GIVEN (no edge filter, no second opinion; only hard risk limits: lot 5..20, $15 cost per market, exposure caps, loss halts).

SETTLEMENT (exact): YES iff the simple average of the 60 one-second CF Benchmarks SOLUSD_RTI values in the minute before close (4 dp) is >= K, where K is the same 60-s average before the open; tie = YES. Nothing else settles it.

THE PACKET IS PRE-DIGESTED. Use these fields, in this order:
 1. required_move: the move from the current official index (bp, and in sigma units at the variance-equivalent horizon horizon_eff_s) that the REMAINING samples' mean needs for YES (move_bp_for_yes; NO needs the opposite). If the window has started, partial_avg is already folded in. Do not recompute it.
 2. empirical_tails: the FREQUENCY of such a signed move in the last ~36 days of 1-minute history at this horizon, normalised by realised vol (candle_rv60 = trailing 60-min candle vol; feed_sigma = the official feed's 1-s sigma). p_yes_empirical is your base rate; p_yes_gaussian is shown only so you can see where the Gaussian is wrong. abs_rows_this_horizon shows the actual tail shape: moves of 0.5-1.5 sd happen LESS often than Gaussian, moves >= 2.5 sd about twice as often. Do not write "fat tails" as a word; use these numbers.
 3. volatility: ratio_realised_over_implied > 1 means the feed is currently more volatile than the price assumes (favours the side that needs the move); < 1 the opposite. rv_1m_bp_last15m_candles vs rv_1m_bp_last60m_candles tells you whether vol is rising or fading right now. official_index_path shows drift/momentum over the last 10 minutes.
 4. comove: BTC/ETH returns over the last 1 and 5 min with the historical beta; sol_5m_residual_bp is the part of SOL's move NOT explained by the market. Consider whether the move you need is a market move or SOL-specific.
 5. conditions_brief (when present): cells from the forensics of this series (105 closes) that contain the current state: per side, the (time x price x side) cell with realised hit rate, the price it implied (mkt_implied), realised net c per contract after fee and its SE; scope OOS = second half only. A 'verdict' is evidence about the base rate of this situation, not an order; read the drift_warning (YES settled 37% of closes while SOL fell) before trusting any side-literal cell.
 6. executable_now: for YES and NO and for lots 5/10/20, the depth-weighted price you actually pay NOW (px_dw) and breakeven_p = px_dw + fee. Your side's probability must EXCEED breakeven_p. The spread and the NO conversion (1 - yes_bid) are already inside px_dw: never add the spread again, never recompute the NO price.
 7. book.last_60s: how far the mid moved in the last minute; your order arrives ~6 s after this packet and rests up to 60 s at your limit (fills only when the offer comes to it; never in the final minute).

DECISION PROCEDURE (follow literally):
 A. Estimate p_own = P(YES) from 1-5 (base rate first, then adjust for vol regime, drift, co-move, conditions). State it before anything else influences you.
 B. Compute edge_yes = p_own - breakeven_p(YES, lot) and edge_no = (1 - p_own) - breakeven_p(NO, lot) for the lot you would trade.
 C. Choose a numeric execution margin m (at least 0.02; larger when book.last_60s.mid_range_c is large or depth is thin or the index is moving fast). Trade a side only if its edge >= m.
 D. Size by edge within limits: lot 5 if edge in [m, m+0.03), 10 if in [m+0.03, m+0.06), 20 if >= m+0.06; never more than the displayed fillable size at your limit and never more than $15 of cost. Prices below 0.05 or above 0.95 need edge >= m + 0.03 (fill and tick risk).
 E. limit_price = the most you will pay on your side; set it at or just above px_dw for the lot (not the mid). NO_TRADE otherwise.
 F. The reason must be falsifiable and numeric: "p_own X vs breakeven Y at lot Q, margin m; wrong if <observable>".

AGAINST THE AUDITED FAILURE MODES (each cost money in the previous cohorts):
 - Consistency: if you state an edge >= your margin and still NO_TRADE, you are wrong somewhere; fix p_own or trade. Do not call a 4-9 c edge "thin" while trading 3 c edges.
 - Do not fade a favourite or lift a tail because a Gaussian says so: in the previous cohorts the 30 favourites priced >= 0.80 that you shaded down won 27 (the price said 27.1, you said 26.0); the 28 tails priced <= 0.20 that you lifted won 2 (the price said 2.5, you said 3.2). The market was right about the shape; use the empirical base rate at 0.5-2 sd and trust neither the Gaussian nor your intuition blindly beyond 2.5 sd.
 - Do not treat the spread as an extra cost; px_dw already pays it.
 - Do not cite 1-minute candle vol as if it were the 1-second feed vol; both are given with their horizons.
 - At T-10/T-5 the settlement window has not started: the quantity that must move is the mean of 60 future seconds, and horizon_eff_s already accounts for that averaging. Do not shorten or lengthen it.
 - Use the BTC/ETH co-move and the conditions brief when present; say 'none' in beyond_market only if you used neither.

No model probability is given on purpose: the market mid (inside executable_now) is the only other opinion, and it is what you trade against.

Respond ONLY with a JSON object, no prose outside it. The "decision" block comes FIRST and is mandatory; keep every string to ONE short sentence so the reply stays under 250 tokens:
{"decision": {"action": "<one of the allowed actions: BUY_YES | BUY_NO | NO_TRADE, or HOLD | EXIT when holding>",
              "qty": <integer 5..20; 0 for NO_TRADE/HOLD/EXIT>,
              "limit_price": <number 0.01..0.99 in dollars, the most you pay per contract on YOUR side (NO price = 1 - yes price); null unless buying>,
              "hold_plan": "<HOLD_TO_SETTLEMENT | REVIEW_AT_T5>",
              "reason": "<one short numeric sentence: p_own vs breakeven at lot, margin, what would make you wrong>"},
 "p_own": <number 0..1: YOUR probability of YES from steps 1-5 (A), before any other opinion>,
 "p_yes": <number 0..1: your final probability; equal to p_own unless you state in rationale why it moved>,
 "edge": {"side": "<yes|no|none>", "lot": <0|5|10|20>, "edge_c": <number: 100 x (p_side - breakeven_p) for that side and lot>, "margin_c": <number: your execution margin in cents>},
 "rationale": "<one short sentence>",
 "beyond_market": "<one short sentence: what you used that the price does not already reflect; 'none' if nothing>"}
Do not output anything else.
```

## 5. Retrospective dry-run (NOT forward; last 40 recorded closes; 148 Opus calls; data/llm-shadow/offline/*)
| variant / time | n | Brier llm | Brier mid | Brier model | llm-mid (SE) | med abs(p_own-p_model) | actions |
|---|---|---|---|---|---|---|---|
| v2a T-10 | 40 | 0.1532 | 0.1501 | 0.1571 | +0.0031 (0.0026) | 0.028 | NO_TRADE 40 |
| v2a T-5 | 34 | 0.1310 | 0.1308 | 0.1324 | +0.0002 (0.0018) | 0.012 | NO_TRADE 34 |
| v2b T-10 | 40 | 0.1525 | 0.1501 | 0.1571 | +0.0024 (0.0026) | 0.027 | NO_TRADE 39, BUY_NO 1 (lost -$1.83) |
| v2b T-5 | 34 | 0.1305 | 0.1308 | 0.1324 | -0.0003 (0.0019) | 0.015 | NO_TRADE 34 |

Live cohort 6c8d on the same closes: T-10 n=23 +0.002 vs mid, median abs(p-p_model) 0.011, 83% less extreme than the mid; direct arm 3 settled positions, 45 ct, -$3.13. Read: neither variant beats the mid (within 1 SE); both beat p_model slightly; anchoring diagnostics improve 2.5-3x; v2b marginally better and anchor-free.

## 6. Deployment
v2b live as cohort P25-v2b from 2026-10-29 20:47 PT (`--prompt-variant v2b`); v2a in reserve; hard risk limits and resting-limit execution unchanged; 100-settled-close read per PREREG P25. This is an anchoring/plumbing choice, not a performance claim.

## 7. Unverified
Dry-run is in-sample. Candle feed vs official RTI identity assumed for the tail table. No forward evidence for either variant.
