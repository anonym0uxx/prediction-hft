"""P25 pre-digested packet tests (docs/PREREG.md §P25): fee-adjusted breakevens at depth-weighted prices for lots 5/10/20 with the
NO conversion, required move given a partial window (bp and sigma units), market-implied sigma round trip, empirical tail table
build / lookup on synthetic Gaussian and fat-tailed data, quote-change summary, co-move residual, conditions-brief lookup,
packet ordering (other_forecasters last in v2a, p_model absent in v2b), prompt variants / cohort hashes, parser edge capture,
and forecast_once tagging rows with the variant's prompt_hash (no network)."""
from __future__ import annotations
import re
import json, math, pathlib, random, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import pandas as pd
import pytest
from predhft import llm_packet_v2 as P2, llm_shadow as L, sol_v1 as S
from predhft.fees import kalshi_taker_fee

T = 1_800_000_000 - (1_800_000_000 % 900)
BOOK = {"yes_bid": 0.46, "yes_ask": 0.48, "bids": [[0.46, 4.0], [0.45, 10.0], [0.44, 100.0]], "asks": [[0.48, 6.0], [0.49, 6.0], [0.50, 100.0]]}
SNAP = {"t": T - 300.0, "close_ts": T, "secs_left": 300.0, "K": 100.0, "x_now": 100.2, "partial": None, "n_obs": 0, "m_rem": 60, "V": 260.5, "sigma_1s": 2.0e-4, "sigma_src": "realized", "kappa": 1.15,
        "p_model": 0.55, "p_yes": 0.55, "blend_w": 0.0, "z": 0.3, "sd_mean": 0.3, "mu": 100.2, **BOOK, "book_src": "ws", "cfb_age_s": 0.3, "book_age_s": 0.5, "latency_s": 0.01, "decision": "abstain", "reasons": ["no_positive_ev"],
        "kind": "entry_eval", "ticker": "KXSOL15M-TEST-00", "version": "SOL-v1"}


# ------------------------------------------------------------------ fees / breakevens / executable
def test_breakeven_is_price_plus_per_contract_fee():
    for px, q in ((0.30, 10), (0.50, 5), (0.05, 20), (0.95, 20)):
        assert P2.breakeven_p(px, q) == pytest.approx(px + kalshi_taker_fee(q, px) / q)
    assert P2.fee_pc(0.5, 10) == pytest.approx(0.0175, abs=1e-3)          # 0.07 x .5 x .5 = 1.75 c
    assert P2.fee_pc(0.5, 0) == 0.0 and P2.fee_pc(1.0, 10) == 0.0

def test_executable_walks_depth_for_lots_and_converts_no_side():
    y5 = P2.executable(BOOK, "yes", 5); y10 = P2.executable(BOOK, "yes", 10); y20 = P2.executable(BOOK, "yes", 20)
    assert y5["px_dw"] == 0.48 and y5["fillable"] == 5 and not y5["partial"]
    assert y10["px_dw"] == pytest.approx((6 * 0.48 + 4 * 0.49) / 10, abs=1e-6)
    assert y20["px_dw"] == pytest.approx((6 * 0.48 + 6 * 0.49 + 8 * 0.50) / 20, abs=1e-6) and y20["fillable"] == 20
    assert y5["breakeven_p"] < y10["breakeven_p"] < y20["breakeven_p"]
    n5 = P2.executable(BOOK, "no", 5)
    assert n5["px_dw"] == pytest.approx((4 * 0.54 + 1 * 0.55) / 5, abs=1e-6)       # NO asks = 1 - YES bids: 0.54 x4, then 0.55
    assert n5["breakeven_p"] == pytest.approx(n5["px_dw"] + P2.fee_pc(n5["px_dw"], 5), abs=1e-6)
    thin = P2.executable({"bids": [[0.46, 2.0]], "asks": []}, "no", 10)
    assert thin["fillable"] == 2 and thin["partial"] and thin["px_dw"] == pytest.approx(0.54)
    empty = P2.executable({"bids": [], "asks": []}, "yes", 5)
    assert empty["fillable"] == 0 and empty["px_dw"] is None and empty["breakeven_p"] is None

def test_depth_imbalance_and_spread():
    d = P2.depth_imbalance(BOOK)
    assert d["spread_c"] == 2.0 and d["bid_qty_top3"] == 114.0 and d["ask_qty_top3"] == 112.0 and d["imbalance"] == pytest.approx((114 - 112) / 226, abs=1e-3)


# ------------------------------------------------------------------ required move / implied sigma
def test_required_move_without_partial_is_distance_to_K_in_bp_and_sigma_units():
    r = P2.required_move(100.0, 100.2, None, 0, 60, 2e-4, 260.5)
    assert r["move_bp_for_yes"] == pytest.approx(1e4 * ((100.0 - 0.5e-4) / 100.2 - 1), abs=0.01) and r["move_bp_for_no"] == -r["move_bp_for_yes"]
    assert r["sd_remaining_mean_bp"] == pytest.approx(1e4 * 2e-4 * math.sqrt(260.5), abs=0.01)
    assert r["sigma_units_for_yes"] == pytest.approx(r["move_bp_for_yes"] / r["sd_remaining_mean_bp"], abs=1e-3)
    assert r["p_gauss_kappa1"] == pytest.approx(1 - P2.Phi(r["sigma_units_for_yes"]), abs=5e-4) and r["horizon_eff_s"] == 260.5

def test_required_move_folds_in_partial_average():
    # 20 samples observed at 99.0 (below K=100): the remaining 40 must average (60*100 - 20*99)/40 = 100.5 for YES
    r = P2.required_move(100.0, 100.0, 99.0, 20, 40, 2e-4, 100.0)
    assert r["avg_needed"] == pytest.approx((60 * (100.0 - 0.5e-4) - 20 * 99.0) / 40, abs=1e-4)
    assert r["move_bp_for_yes"] == pytest.approx(1e4 * (r["avg_needed"] / 100.0 - 1), abs=0.01) and r["move_bp_for_yes"] > 49
    d = P2.required_move(100.0, 100.0, 100.3, 60, 0, 2e-4, 0.0)
    assert d["determined"] and d["yes_wins"] and d["p_gauss_kappa1"] == 1.0
    d2 = P2.required_move(100.0, 100.0, 99.9999, 60, 0, 2e-4, 0.0)
    assert d2["determined"] and not d2["yes_wins"]

def test_implied_sigma_round_trip_and_unidentified_cases():
    sig = 3e-4; V = 260.5; x = 100.2; K = 100.0; mu = 100.2
    mid = P2.Phi((mu - K) / (sig * x * math.sqrt(V)))
    out = P2.implied_sigma(mid, mu, K, x, V, 60, 2 * sig)
    assert out["sigma_implied_1s_bp"] == pytest.approx(1e4 * sig, rel=1e-3) and out["ratio_realised_over_implied"] == pytest.approx(2.0, rel=1e-3)
    assert P2.implied_sigma(0.505, mu, K, x, V, 60, sig)["sigma_implied_1s_bp"] is None       # mid ~ 0.5: not identified
    assert P2.implied_sigma(0.3, mu, K, x, V, 60, sig)["sigma_implied_1s_bp"] is None         # mid on the wrong side of the distance
    assert P2.implied_sigma(None, mu, K, x, V, 60, sig)["ratio_realised_over_implied"] is None


# ------------------------------------------------------------------ empirical tails
def _synthetic(n=20000, seed=1, df=None, base=100.0):
    rnd = random.Random(seed); ts = [T - 60 * (n - i) for i in range(n)]; c = [base]
    for _ in range(n - 1):
        z = rnd.gauss(0, 1) if df is None else rnd.gauss(0, 1) / math.sqrt(sum(rnd.gauss(0, 1) ** 2 for _ in range(df)) / df)
        c.append(c[-1] * math.exp(0.001 * z))
    return pd.DataFrame({"ts": ts, "open": c, "high": c, "low": c, "close": c, "volume": [1.0] * n})

def test_tail_table_gaussian_data_matches_gaussian_and_lookup_is_monotone():
    t = P2.build_tail_table(_synthetic(), horizons=(5, 9))
    assert t["horizons_min"] == ["5", "9"] and t["table"]["5"]["n"] > 15000
    assert P2.tail_survival(t, 5.0, 0.0) == pytest.approx(0.5, abs=0.03)
    assert P2.tail_survival(t, 5.0, 1.0) == pytest.approx(1 - P2.Phi(1.0), abs=0.03)
    assert P2.tail_survival(t, 5.0, -1.0) == pytest.approx(P2.Phi(1.0), abs=0.03)
    assert P2.tail_survival(t, 5.0, -9.0) >= 0.999 and P2.tail_survival(t, 5.0, 9.0) <= 0.001 and P2.tail_survival(t, 5.0, 4.0) <= 0.001
    s = [P2.tail_survival(t, 5.0, k) for k in (-2, -1, -0.5, 0, 0.5, 1, 2)]
    assert all(a >= b for a, b in zip(s, s[1:]))
    assert P2.nearest_horizon(t, 8.6) == "9" and P2.nearest_horizon(t, 4.0) == "5"
    assert t["table"]["5"]["abs_rows"]["2.0"]["empirical"] == pytest.approx(0.0455, abs=0.02)

def test_tail_table_fat_tailed_data_shows_more_mass_beyond_2_5_sd():
    t = P2.build_tail_table(_synthetic(df=3, seed=2), horizons=(5,))
    r = t["table"]["5"]["abs_rows"]
    assert r["2.5"]["empirical"] > 1.5 * r["2.5"]["gaussian"] and r["3.0"]["empirical"] > 2 * r["3.0"]["gaussian"]

def test_tail_table_requires_contiguous_minutes_and_betas():
    d = _synthetic(3000); d2 = d.copy(); d2["ts"] = d2["ts"] + 7     # misaligned other asset -> no beta
    t = P2.build_tail_table(d, d, d2, horizons=(5,))
    assert t["betas"]["SOL_BTC"]["beta"] == pytest.approx(1.0, abs=1e-6) and t["betas"]["SOL_BTC"]["corr"] == pytest.approx(1.0, abs=1e-6) and "SOL_ETH" not in t["betas"]
    gap = d.iloc[list(range(0, 1500)) + list(range(1600, 3000))]      # a 100-minute hole: windows spanning it are dropped
    tg = P2.build_tail_table(gap, horizons=(5,))
    assert tg["table"]["5"]["n"] < t["table"]["5"]["n"]

def test_empirical_block_uses_horizon_eff_and_both_vol_normalisations():
    t = P2.build_tail_table(_synthetic(), horizons=(4, 5, 9))
    req = P2.required_move(100.0, 100.2, None, 0, 60, 2e-4, 260.5)
    e = P2.empirical_block(t, req, 20.0, 2e-4)
    assert e["horizon_used_min"] == 4.0 and e["candle_rv60"]["vol_1m_bp"] == 20.0 and e["feed_sigma"]["vol_1m_bp"] == pytest.approx(1e4 * 2e-4 * math.sqrt(60), abs=1e-2)
    k = req["move_bp_for_yes"] / (20.0 * math.sqrt(260.5 / 60))
    assert e["candle_rv60"]["k_for_yes"] == pytest.approx(k, abs=1e-3) and e["candle_rv60"]["p_yes_empirical"] == pytest.approx(P2.tail_survival(t, 260.5 / 60, k))
    assert e["candle_rv60"]["p_yes_empirical"] + e["candle_rv60"]["p_no_empirical"] == pytest.approx(1.0)
    assert P2.empirical_block(None, req, 20.0, 2e-4) is None and P2.empirical_block(t, {"determined": True}, 20.0, 2e-4) is None


# ------------------------------------------------------------------ book tape / co-move / brief
def test_quote_changes_summarises_last_60s():
    now = 1000.0; tape = [{"recv_t": now - 70, "yes_bid": 0.10, "yes_ask": 0.12}] + [{"recv_t": now - 50 + i * 10, "yes_bid": 0.40 + 0.01 * i, "yes_ask": 0.42 + 0.01 * i} for i in range(5)] + [{"recv_t": now - 1, "yes_bid": 0.44, "yes_ask": 0.46}]
    q = P2.quote_changes(tape, now, 60.0)
    assert q["n_updates"] == 6 and q["n_mid_changes"] == 4 and q["mid_then"] == 0.41 and q["mid_now"] == 0.45 and q["mid_change_c"] == 4.0 and q["mid_range_c"] == 4.0 and q["largest_jump_c"] == 1.0 and q["spread_now_c"] == 2.0
    assert P2.quote_changes(tape[:1], now) is None

def test_comove_residual_uses_historical_beta():
    cs = {"SOL": {"returns_1m_bp": [1.0, 2.0, 30.0], "ret_5m_bp": 50.0, "rv_1m_bp": 20.0}, "BTC": {"returns_1m_bp": [0.5, 1.0, 10.0], "ret_5m_bp": 20.0, "rv_1m_bp": 10.0}}
    c = P2.comove_block(cs, {"SOL_BTC": {"beta": 1.4, "corr": 0.8}})
    assert c["SOL"]["ret_1m_bp"] == 30.0 and c["BTC"]["beta_hist_1m"] == 1.4 and c["BTC"]["sol_5m_explained_by_beta_bp"] == 28.0 and c["BTC"]["sol_5m_residual_bp"] == 22.0
    assert P2.comove_block(None, None) is None

def test_conditions_lookup_matches_ranges_and_values():
    brief = {"note": "forensics", "buckets": [{"name": "T5_fav", "match": {"decision_secs_left": 300, "mid": [0.8, 1.0]}, "n": 40, "hit": 0.9, "implied": 0.86, "net_c_per_ct": 3.1, "se": 2.0, "verdict": "edge_yes"},
                                           {"name": "T10_any", "match": {"decision_secs_left": 600}, "n": 80, "hit": 0.5, "implied": 0.5, "net_c_per_ct": -1.0, "se": 3.0, "verdict": "no_edge"}]}
    b = P2.conditions_lookup(brief, {"decision_secs_left": 300.0, "mid": 0.9})
    assert b["name"] == "T5_fav" and b["net_c_per_ct"] == 3.1 and b["brief_note"] == "forensics"
    assert P2.conditions_lookup(brief, {"decision_secs_left": 600.0, "mid": 0.9})["name"] == "T10_any"
    assert P2.conditions_lookup(brief, {"decision_secs_left": 300.0, "mid": 0.5}) is None
    flat = [{"name": "lo", "mid_lo": 0.0, "mid_hi": 0.2, "n": 3}]
    assert P2.conditions_lookup(flat, {"mid": 0.1})["name"] == "lo" and P2.conditions_lookup(flat, {"mid": 0.3}) is None
    assert P2.conditions_lookup(None, {"mid": 0.1}) is None and P2.conditions_lookup({"buckets": "garbage"}, {"mid": 0.1}) is None


# ------------------------------------------------------------------ packet / prompts / rows
def _base(now):
    return L.assemble_packet(SNAP, now, [(s, 100.2 + (0.01 if s % 2 else -0.01)) for s in range(T - 1000, T - 300)], None, None, None, 300.0, direct={"allowed_actions": ["BUY_YES", "BUY_NO", "NO_TRADE"]})

def test_packet_v2a_puts_other_forecasters_last_and_v2b_has_no_p_model():
    now = T - 298.0; t = P2.build_tail_table(_synthetic(4000), horizons=(4, 5))
    a = P2.build_packet_v2(SNAP, now, 300.0, "v2a", _base(now), tape=[], table=t, brief=None, direct={"allowed_actions": ["BUY_YES"]})
    b = P2.build_packet_v2(SNAP, now, 300.0, "v2b", _base(now), tape=[], table=t, brief=None, direct={"allowed_actions": ["BUY_YES"]})
    keys = list(a.keys())
    assert keys[-1] == "other_forecasters" and keys.index("executable_now") < keys.index("required_move") < keys.index("empirical_tails") < keys.index("direct_arm")
    assert a["other_forecasters"]["policy_gaussian_p_model"] == 0.55 and "policy_model" not in a
    assert "other_forecasters" not in b and "policy_model" not in b
    sb = json.dumps(b)
    assert "p_model" not in sb and not re.search(r"(?<![0-9.])0\.55(?![0-9])", sb)       # the model probability does not leak anywhere in v2b
    assert a["executable_now"]["yes"]["10"]["breakeven_p"] == pytest.approx(P2.executable(BOOK, "yes", 10)["breakeven_p"])
    assert a["required_move"]["move_bp_for_yes"] == pytest.approx(1e4 * ((100.0 - 0.5e-4) / 100.2 - 1), abs=0.01)
    assert a["empirical_tails"]["horizon_used_min"] == 4.0 and a["volatility"]["sigma_realised_1s_bp"] == 2.0
    assert a["direct_arm"] == {"allowed_actions": ["BUY_YES"]} and a["conditions_brief"] is None
    msgs = L.messages_for(a, "v2a"); body = msgs[1]["content"]
    assert body.index('"executable_now"') < body.index('"other_forecasters"') and msgs[0]["content"] == L.SYSTEM_PROMPT_V2A
    assert len(body) < 9000                                                 # packet budget (~2.3k tokens); the system prompt is ~1.6k

def test_prompt_variants_have_distinct_hashes_and_v1_is_unchanged():
    assert L.prompt_hash_for("v1") == L.PROMPT_HASH == "6c8d03af884f0000"
    hs = {L.prompt_hash_for(v) for v in L.PROMPTS}; assert len(hs) == 3
    for v in ("v2a", "v2b"):
        p = L.PROMPTS[v]
        assert '"decision"' in p and '"p_own"' in p and "breakeven" in p and "FAILURE MODES" in p and "DECISION PROCEDURE" in p
    assert "other_forecasters" in L.SYSTEM_PROMPT_V2A and "other_forecasters" not in L.SYSTEM_PROMPT_V2B
    with pytest.raises(KeyError): L.prompt_hash_for("v9")

def test_parse_response_captures_edge_block():
    txt = '{"decision": {"action": "BUY_NO", "qty": 10, "limit_price": 0.33, "hold_plan": "HOLD_TO_SETTLEMENT", "reason": "r"}, "p_own": 0.60, "p_yes": 0.60, "edge": {"side": "no", "lot": 10, "edge_c": 7.5, "margin_c": 2}, "rationale": "x", "beyond_market": "none"}'
    pr = L.parse_response(txt)
    assert pr["p_llm"] == 0.60 and pr["p_own"] == 0.60 and pr["edge"] == {"side": "no", "lot": 10, "edge_c": 7.5, "margin_c": 2} and pr["decision_raw"]["action"] == "BUY_NO"
    assert L.parse_response('{"p_yes": 0.4, "edge": "none"}')["edge"] is None

class _FakeClient:
    model = "fake/model"; red = L.Redactor("sk-nou...9xyz")
    def complete(self, messages, budget=None):
        assert messages[0]["content"] == L.SYSTEM_PROMPT_V2B and '"executable_now"' in messages[1]["content"] and "p_model" not in messages[1]["content"]
        return {"ok": True, "text": '{"decision": {"action": "NO_TRADE", "qty": 0, "limit_price": null, "hold_plan": "HOLD_TO_SETTLEMENT", "reason": "p_own 0.55 vs breakeven 0.5 margin 2c"}, "p_own": 0.55, "p_yes": 0.55, "edge": {"side": "none", "lot": 0, "edge_c": 1.0, "margin_c": 2}, "rationale": "r", "beyond_market": "none"}',
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "latency_s": 0.1, "status": 200, "error": None, "attempts": 1, "finish_reason": "stop"}

def test_forecast_once_tags_variant_cohort_and_v2_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "gather_context", lambda now, red, root: ([], {}, None, None, []))
    monkeypatch.setattr(L, "gather_v2_context", lambda now, ticker, red, errs: ([], P2.build_tail_table(_synthetic(4000), horizons=(4, 5)), None))
    row = L.forecast_once(_FakeClient(), L.Budget(tmp_path / "b.json", 10), SNAP, 300.0, T - 298.0, tmp_path, variant="v2b")
    assert row["prompt_variant"] == "v2b" and row["prompt_hash"] == L.prompt_hash_for("v2b") and row["p_own"] == 0.55 and row["edge_stated"]["margin_c"] == 2
    assert row["v2_fields"]["breakeven_yes_10"] == pytest.approx(P2.executable(BOOK, "yes", 10)["breakeven_p"]) and row["v2_fields"]["p_yes_empirical_feed"] is not None
    logged = json.loads((tmp_path / "forecasts.jsonl").read_text().splitlines()[-1]); assert logged["prompt_hash"] == L.prompt_hash_for("v2b") and "sk-nou" not in json.dumps(logged)
    row1 = L.forecast_once(_FakeClient.__new__(_FakeClient), L.Budget(tmp_path / "b2.json", 10), SNAP, 300.0, T - 298.0, tmp_path, dry_run=True)
    assert row1["prompt_variant"] == "v1" and row1["prompt_hash"] == L.PROMPT_HASH and "v2_fields" not in row1


# ---------------------------------------------------------------- Quant A brief (actual shape: 'states' with bucket labels)
def _qa_brief():
    mk = lambda scope, state, n_closes, hit, imp, net, se, verdict: {"scope": scope, "state": state, "n": 100 * n_closes, "n_closes": n_closes, "hit": hit, "mkt_implied": imp, "model_p": None, "net_c": net, "se_c": se, "verdict": verdict}
    return {"meta": {"generated_pt": "2026-10-29 19:57 PT", "drift_warning": "YES settled 37%"}, "states": [
        mk("full_sample_IN_SAMPLE", {"tb": "T-2-5m", "pxb": "px.25-.50", "side": "yes"}, 35, 0.134, 0.371, -25.3, 6.4, "unfavourable"),
        mk("second_half_OOS", {"tb": "T-2-5m", "pxb": "px.25-.50", "side": "yes"}, 15, 0.121, 0.358, -25.3, 8.6, "insufficient"),
        mk("second_half_OOS", {"tb": "T-2-5m", "pxb": "px.50-.75", "side": "no"}, 15, 0.874, 0.638, 22.0, 9.3, "insufficient"),
        mk("second_half_OOS", {"tb": "T-2-5m", "edgeb": "medge5-15c", "side": "yes"}, 19, 0.121, 0.199, -8.9, 8.0, "insufficient"),
        mk("second_half_OOS", {"tb": "T-2-5m", "pick": "with_model"}, 40, 0.244, 0.232, 0.23, 4.1, "indeterminate"),
        mk("second_half_OOS", {"volb": "vol.7-1.2bp", "pick": "with_model"}, 26, 0.352, 0.364, -2.5, 3.2, "indeterminate"),
        mk("second_half_OOS", {"volb": "vol.7-1.2bp", "pick": "against_model"}, 5, 0.3, 0.3, 0.0, 9.0, "insufficient"),
        mk("second_half_OOS", {"tb": "T-5-10m", "pxb": "px.25-.50", "side": "yes"}, 30, 0.3, 0.35, -5.0, 4.0, "indeterminate")]}

_BOOK = {"yes_bid": 0.46, "yes_ask": 0.48, "bids": [[0.46, 4.0], [0.45, 10.0], [0.44, 100.0]], "asks": [[0.48, 6.0], [0.49, 6.0], [0.50, 100.0]]}

def test_qa_bucket_labels_match_quant_a_definitions():
    assert P2.b_time(298.8) == "T-2-5m" and P2.b_time(300) == "T-5-10m" and P2.b_time(599) == "T-5-10m" and P2.b_time(600) == "T-10-14m" and P2.b_time(59) == "T-10-60s"
    assert P2.b_px(0.099) == "px<.10" and P2.b_px(0.25) == "px.25-.50" and P2.b_px(0.9) == "px>=.90"
    assert P2.b_edge(0) == "medge<=0" and P2.b_edge(0.05) == "medge0-5c" and P2.b_edge(0.051) == "medge5-15c" and P2.b_edge(0.2) == "medge>15c"
    assert P2.b_spread(0.01) == "spr<=1c" and P2.b_spread(0.02) == "spr1-3c" and P2.b_spread(0.04) == "spr>3c"
    assert P2.b_depth(49) == "depth<50" and P2.b_depth(50) == "depth50-200" and P2.b_depth(200) == "depth>=200"
    assert P2.b_vol(0.69e-4) == "vol<.7bp" and P2.b_vol(1.0e-4) == "vol.7-1.2bp" and P2.b_vol(1.2e-4) == "vol>=1.2bp"
    assert P2.b_partial(0) == "partial0" and P2.b_partial(30) == "partial1-30" and P2.b_partial(31) == "partial31-59"
    assert P2.b_hour(5) == "h00-05PT" and P2.b_hour(6) == "h06-11PT" and P2.b_hour(18) == "h18-23PT"
    assert P2.b_mid(0.5) == "mid.30-.70" and P2.b_mid(0.9) == "mid>=.90" and P2.b_absz(None) == "z_na" and P2.b_absz(-1.5) == "|z|1-2"

def test_qa_state_buckets_per_side_uses_lot10_price_plus_tick_and_hides_model_when_absent():
    st = P2.qa_state_buckets(298.8, _BOOK, 1.0e-4, 0, 19, 0.47, 0.55, 0.3)
    assert st["tb"] == "T-2-5m" and st["sprb"] == "spr1-3c" and st["volb"] == "vol.7-1.2bp" and st["partb"] == "partial0" and st["hourb"] == "h18-23PT" and st["midb"] == "mid.30-.70" and st["zb"] == "|z|<.5"
    y = st["sides"]["yes"]; n = st["sides"]["no"]
    # yes lot 10: 6 @ .48 + 4 @ .49 = .484 avg, + 1 tick = .494 ; no lot 10: 4 @ .54 + 6 @ .55 = .546, + tick = .556
    assert y["px_exec_lot10_plus_tick"] == pytest.approx(0.494, abs=1e-3) and y["pxb"] == "px.25-.50" and y["depb"] == "depth<50" and y["fav"] == "longshot"
    assert n["px_exec_lot10_plus_tick"] == pytest.approx(0.556, abs=1e-3) and n["pxb"] == "px.50-.75" and n["fav"] == "favourite"
    assert y["edgeb"] == "medge5-15c" and y["pick"] == "with_model" and n["edgeb"] == "medge<=0" and n["pick"] == "against_model"
    st_b = P2.qa_state_buckets(298.8, _BOOK, 1.0e-4, 0, 19, 0.47, None, None)
    assert "edgeb" not in st_b["sides"]["yes"] and "pick" not in st_b["sides"]["yes"] and st_b["zb"] is None
    empty = P2.qa_state_buckets(298.8, {"yes_bid": None, "yes_ask": None, "bids": [], "asks": []}, None, 0, None, None, None, None)
    assert empty["sides"] == {"yes": None, "no": None} and empty["sprb"] is None and empty["volb"] is None

def test_conditions_lookup_qa_selects_matching_cells_both_scopes_and_filters_insufficient_marginals():
    b = _qa_brief()
    st = P2.qa_state_buckets(298.8, _BOOK, 1.0e-4, 0, 19, 0.47, 0.55, 0.3)
    r = P2.conditions_lookup(b, {"qa": st})
    states = [(c["scope"], json.dumps(c["state"], sort_keys=True)) for c in r["cells"]]
    assert ("OOS", json.dumps({"tb": "T-2-5m", "pxb": "px.25-.50", "side": "yes"}, sort_keys=True)) in states
    assert ("IN_SAMPLE", json.dumps({"tb": "T-2-5m", "pxb": "px.25-.50", "side": "yes"}, sort_keys=True)) in states
    assert ("OOS", json.dumps({"tb": "T-2-5m", "pxb": "px.50-.75", "side": "no"}, sort_keys=True)) in states
    assert ("OOS", json.dumps({"tb": "T-2-5m", "edgeb": "medge5-15c", "side": "yes"}, sort_keys=True)) in states
    assert ("OOS", json.dumps({"tb": "T-2-5m", "pick": "with_model"}, sort_keys=True)) in states
    assert ("OOS", json.dumps({"volb": "vol.7-1.2bp", "pick": "with_model"}, sort_keys=True)) in states
    assert ("OOS", json.dumps({"volb": "vol.7-1.2bp", "pick": "against_model"}, sort_keys=True)) not in states       # insufficient marginal dropped
    assert ("OOS", json.dumps({"tb": "T-5-10m", "pxb": "px.25-.50", "side": "yes"}, sort_keys=True)) not in states   # other time bucket
    cell = next(c for c in r["cells"] if c["scope"] == "IN_SAMPLE")
    assert cell["n_closes"] == 35 and cell["hit"] == 0.134 and cell["mkt_implied"] == 0.371 and cell["net_c"] == -25.3 and cell["se_c"] == 6.4 and cell["verdict"] == "unfavourable"
    assert r["drift_warning"] == "YES settled 37%" and r["state_buckets"]["sides"]["yes"]["pxb"] == "px.25-.50"
    # v2b state (no p_model): only the price x time x side cells, nothing model-relative, no leak of the model's sign
    rb = P2.conditions_lookup(b, {"qa": P2.qa_state_buckets(298.8, _BOOK, 1.0e-4, 0, 19, 0.47, None, None)})
    assert all("pick" not in c["state"] and "edgeb" not in c["state"] for c in rb["cells"]) and len(rb["cells"]) == 3
    assert P2.conditions_lookup(b, {"qa": P2.qa_state_buckets(None, _BOOK, None, 0, None, None, None, None)}) is None

def test_build_packet_v2_carries_qa_brief_cells_and_v2b_cells_are_model_free():
    now = T - 298.8; t = P2.build_tail_table(_synthetic(4000), horizons=(4, 5))
    snap = {**SNAP, "secs_left": 298.8, "sigma_1s": 1.0e-4, "mid": 0.47, "yes_bid": 0.46, "yes_ask": 0.48, "book": _BOOK, "p_model": 0.55, "z": 0.3}
    a = P2.build_packet_v2(snap, now, 300.0, "v2a", _base(now), tape=[], table=t, brief=_qa_brief(), direct={"allowed_actions": ["BUY_YES"]})
    b = P2.build_packet_v2(snap, now, 300.0, "v2b", _base(now), tape=[], table=t, brief=_qa_brief(), direct={"allowed_actions": ["BUY_YES"]})
    assert a["conditions_brief"]["cells"] and any("pick" in c["state"] for c in a["conditions_brief"]["cells"])
    assert b["conditions_brief"]["cells"] and all("pick" not in c["state"] and "edgeb" not in c["state"] for c in b["conditions_brief"]["cells"])
    assert "p_model" not in json.dumps(b) and "with_model" not in json.dumps(b)
