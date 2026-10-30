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
