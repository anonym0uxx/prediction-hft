"""P20 bet gate: every guard individually (pure functions), accept() aggregation, Wilson bound, replay candidate generation."""
import math, pathlib, json
import pytest
from predhft import bet_gate as G
from predhft.fees import kalshi_taker_fee

def cand(**kw):
    """A clean favourite candidate that passes every guard: YES at .87 (ask .86), p_cal .967, d +3.9 bp, sigma 2.5 bp/min, T-46 s, official feed."""
    c = {"series": "KXBTC15M", "asset": "BTC", "ticker": "KXBTC15M-TEST", "close_ts": 1_800_000_000, "side": "yes", "price": 0.87, "ask": 0.86, "ask_size": 120.0,
         "p_cal": 0.967, "d_bp": 3.9, "sigma1": 0.00025, "mins_left": 46.2 / 60, "secs_left": 46.2, "partial_n": 14, "spot_src": "cfb", "cfb_age_s": 0.8, "mkt_side_mid": 0.855, "lot": 10}
    c.update(kw); return c

def ctx(**kw):
    d = {"move30_bp": 0.2, "recent_outcomes": []}; d.update(kw); return d

# ------------------------------------------------------------------------------------------------------------------ (a) edge
def test_p_lower_is_below_point_and_monotone_in_partial_lag():
    a = G.p_lower(3.0, 0.00025, 0.77, partial_n=14, official=True)
    assert a["p_lcb"] < a["p_point"] and a["sd_hi"] > a["sd"]
    lag = G.p_lower(3.0, 0.00025, 0.77, partial_n=2, official=True)      # partial lags the clock -> more remaining variance -> lower bound
    assert lag["p_lcb"] < a["p_lcb"] and lag["r_eff"] > a["r_eff"]
    rep = G.p_lower(3.0, 0.00025, 0.77, partial_n=14, official=False)    # replica adds basis noise
    assert rep["p_lcb"] < a["p_lcb"]

def test_empirical_sd_floor_binds_when_sigma_is_tiny():
    a = G.p_lower(2.0, 0.00001, 0.917, partial_n=5, official=True)
    assert a["sd_hi"] == pytest.approx(G.CFG["SD_EMP_T55_BP"], rel=1e-6)   # 2.35 bp floor at T-55, not the ~0 model sd

def test_guard_edge_point_and_lcb_thresholds():
    ok, d = G.guard_edge(cand()); assert ok and d["edge_lcb_c"] >= G.CFG["EDGE_LCB_MIN_C"] and d["edge_point_c"] >= G.CFG["EDGE_MIN_C"]
    ok, d = G.guard_edge(cand(price=0.95)); assert not ok                   # fill too rich for the LCB
    ok, d = G.guard_edge(cand(d_bp=-3.9)); assert not ok and d["why"] == "side_vs_projection"   # side disagrees with the projection sign
    ok, d = G.guard_edge(cand(d_bp=2.0, sigma1=0.00025, price=0.85)); assert not ok and d["edge_point_c"] >= 5 and d["edge_lcb_c"] < 2   # point edge ~5.6c but the LCB kills it

# ------------------------------------------------------------------------------------------------------------- (b) agreement
def test_guard_agreement_favourite_disagreement_longshot_override():
    ok, d = G.guard_agreement(cand(), 0.92); assert ok and d["mode"] == "favourite"
    ok, d = G.guard_agreement(cand(mkt_side_mid=0.64, p_cal=0.946), 0.9); assert not ok and d["mode"] == "disagreement"   # ETH 14:15 loss: fav .64 but 31c disagreement
    ok, d = G.guard_agreement(cand(mkt_side_mid=0.19, p_cal=0.90), 0.85); assert not ok and d["mode"] == "longshot"
    # override: official feed, >= 30 s of partial locked, p_lcb >= .90, mid >= .35
    ok, d = G.guard_agreement(cand(mkt_side_mid=0.40, p_cal=0.97, partial_n=35), 0.93); assert ok and d["mode"] == "override"
    ok, _ = G.guard_agreement(cand(mkt_side_mid=0.40, p_cal=0.97, partial_n=35, spot_src="replica"), 0.93); assert not ok
    ok, _ = G.guard_agreement(cand(mkt_side_mid=0.40, p_cal=0.97, partial_n=20), 0.93); assert not ok
    ok, _ = G.guard_agreement(cand(mkt_side_mid=0.40, p_cal=0.97, partial_n=35), 0.85); assert not ok
    ok, _ = G.guard_agreement(cand(mkt_side_mid=0.30, p_cal=0.97, partial_n=35), 0.93); assert not ok
    ok, d = G.guard_agreement(cand(mkt_side_mid=0.40, p_cal=0.97, partial_n=35), 0.93, cfg={**G.CFG, "OVERRIDE": False}); assert not ok

# ------------------------------------------------------------------------------------------------------------------ (c) feed
def test_guard_feed_official_age_and_partial_consistency():
    assert G.guard_feed(cand(), {})[0]
    ok, d = G.guard_feed(cand(spot_src="replica"), {}); assert not ok and d["why"] == "not_official"
    ok, d = G.guard_feed(cand(cfb_age_s=2.6), {}); assert not ok and d["why"] == "stale"
    ok, d = G.guard_feed(cand(cfb_age_s=None), {}); assert not ok and d["why"] == "age_unknown"
    ok, d = G.guard_feed(cand(cfb_age_s=None), {"age_unknown_ok": True}); assert ok and d["age_unknown_flag"]
    ok, d = G.guard_feed(cand(partial_n=5), {}); assert not ok and d["why"] == "partial_n_inconsistent"      # T-46 s => ~14 ticks expected
    ok, d = G.guard_feed(cand(partial_n=20), {}); assert not ok and d["why"] == "partial_n_inconsistent"
    assert G.guard_feed(cand(partial_n=11), {})[0] and G.guard_feed(cand(partial_n=15), {})[0]

# ------------------------------------------------------------------------------------------------------------- (d) liquidity
def test_guard_liquidity_size_price_cap_and_adverse_move():
    ok, d = G.guard_liquidity(cand(), 4.7, 1.6, 2.4); assert ok and d["adverse_c"] > 0
    ok, d = G.guard_liquidity(cand(ask_size=9), 4.7, 1.6, 2.4); assert not ok and d["why"] == "size_lt_lot"
    ok, d = G.guard_liquidity(cand(ask=0.96, price=0.97), 10.0, 1.6, 2.4); assert not ok and d["why"] == "price_cap"
    ok, d = G.guard_liquidity(cand(ask=0.86, price=0.86), 10.0, 1.6, 2.4); assert not ok and d["why"] == "price_cap"   # must pay ask + tick
    # high sigma, z near 0 (phi max) and small sd -> adverse move eats the edge
    ok, d = G.guard_liquidity(cand(sigma1=0.0012), 2.5, 0.0, 0.5); assert not ok and d["why"] == "adverse_move"
    assert d["adverse_c"] == pytest.approx(100 * G.phi(0.0) * (46.2 / 60) * 12.0 * math.sqrt(1.5 / 60) / 0.5, abs=0.01)

# ---------------------------------------------------------------------------------------------------------------- (e) regime
def test_guard_regime_k_sd_and_unevaluable():
    s30 = 0.00025 * 1e4 * math.sqrt(0.5)
    assert G.guard_regime(cand(), {"move30_bp": 2.4 * s30})[0]
    ok, d = G.guard_regime(cand(), {"move30_bp": -2.6 * s30}); assert not ok and d["k_sd"] == pytest.approx(2.6, abs=0.01)
    ok, d = G.guard_regime(cand(), {"move30_bp": None}); assert ok and d["regime_unevaluable"]

# --------------------------------------------------------------------------------------------------------------- (f) hit rate
def test_wilson_lb_values():
    assert G.wilson_lb(0, 0) == 0.0
    assert G.wilson_lb(27, 30) == pytest.approx(0.808, abs=0.002)    # passes the .80 floor
    assert G.wilson_lb(26, 30) == pytest.approx(0.768, abs=0.002)    # trips it
    assert G.wilson_lb(30, 30) > 0.9

def test_guard_hitrate_warming_floor_window():
    ok, d = G.guard_hitrate({"recent_outcomes": [1] * 10 + [0] * 5}); assert ok and d["warming"]            # n < 20: cannot trip
    ok, d = G.guard_hitrate({"recent_outcomes": [1] * 18 + [0] * 2}); assert not ok and d["hr_n"] == 20     # 18/20 -> LB .78 < .80
    assert G.guard_hitrate({"recent_outcomes": [1] * 19 + [0]})[0]
    ok, d = G.guard_hitrate({"recent_outcomes": [0] * 10 + [1] * 30}); assert ok and d["hr_n"] == 30 and d["hr_k"] == 30   # only the last 30 count

# ------------------------------------------------------------------------------------------------------------------- accept
def test_accept_all_guards_evaluated_and_reasons_listed():
    ok, reasons, det = G.accept(cand(), ctx()); assert ok and reasons == [] and all(det["guards"].values())
    ok, reasons, det = G.accept(cand(spot_src="replica", mkt_side_mid=0.3, ask_size=1), ctx(move30_bp=50.0))
    assert not ok and set(reasons) >= {"agreement", "feed", "liquidity", "regime"} and det["guards"]["hitrate"]
    ok, reasons, _ = G.accept(cand(spot_src="replica"), ctx(disable=["feed"])); assert ok          # replay variant switch
    ok, reasons, _ = G.accept(cand(), ctx(recent_outcomes=[1] * 18 + [0, 0])); assert reasons == ["hitrate"]

def test_accept_is_pure_and_cfg_override():
    c = cand(); before = json.dumps(c, sort_keys=True)
    G.accept(c, ctx()); assert json.dumps(c, sort_keys=True) == before
    ok, _, _ = G.accept(cand(), ctx(), cfg={"EDGE_LCB_MIN_C": 50.0}); assert not ok

# ----------------------------------------------------------------------------------------------------------- replay machinery
CLOSE = 1_800_000_000
def row(secs_left, ya, yb, d_bp=5.0, K=100.0, n=None, yas=50, ybs=50, src="cfb", sigma1=0.00025):
    px = K * (1 + d_bp * 1e-4)
    return {"t": CLOSE - secs_left, "mins_left": secs_left / 60.0, "ticker": "KXBTC15M-T", "K": K, "spot": px, "partial_twap": px, "partial_n": (n if n is not None else int(60 - secs_left)),
            "sigma1": sigma1, "yes_bid": yb, "yes_ask": ya, "bid_size": ybs, "ask_size": yas, "close_ts": CLOSE, "spot_src": src}

def test_candidates_emulate_p16_rule_and_gate_replay_window():
    pre = [row(s, 0.5, 0.49) for s in (90, 80, 70, 65)]                     # pre-window history for the 30-s move
    S = pre + [row(50, 0.86, 0.85), row(48, 0.86, 0.85), row(40, 0.86, 0.85)]
    cs = G.candidates_from_rows(S, "KXBTC15M", True)
    assert cs and cs[0]["side"] == "yes" and cs[0]["price"] == pytest.approx(0.87) and cs[0]["secs_left"] == pytest.approx(50) and cs[0]["move30_bp"] is not None
    o = G.replay_window(S, "yes", "KXBTC15M", True); assert o["traded"] and o["pnl_c"] == pytest.approx(100 * (1 - 0.87) - 100 * kalshi_taker_fee(10, 0.87) / 10, abs=1e-6)
    o = G.replay_window(S, "no", "KXBTC15M", True); assert o["traded"] and o["pnl_c"] < -80
    # pinned at .99 / wrong side -> no candidates
    assert G.candidates_from_rows([row(50, 0.995, 0.99), row(48, 0.995, 0.99)], "KXBTC15M", True) == []
    # a longshot candidate exists for the P16 rule but the gate rejects it (agreement), so the window is untraded with reasons
    L = [row(50, 0.30, 0.28, d_bp=1.5, sigma1=0.0002), row(48, 0.30, 0.28, d_bp=1.5, sigma1=0.0002)]
    o = G.replay_window(L, "yes", "KXBTC15M", True); assert not o["traded"] and "agreement" in o["reasons_hist"]

def test_summarise_cluster_se_and_hit():
    tr = [{"close_ts": 1, "pnl_c": 10, "win": 1}, {"close_ts": 1, "pnl_c": 20, "win": 1}, {"close_ts": 2, "pnl_c": -50, "win": 0}]
    s = G.summarise(tr); assert s["n"] == 3 and s["closes"] == 2 and s["hit"] == pytest.approx(2 / 3) and s["mean_cl_c"] == pytest.approx((15 - 50) / 2)

def test_recent_outcomes_and_settle_shadow(tmp_path: pathlib.Path):
    (tmp_path / "trades.jsonl").write_text(json.dumps({"close_ts": 10, "status": "settled", "result": "yes", "side": "yes", "gate": {"x": 1}}) + "\n" +
                                           json.dumps({"close_ts": 11, "status": "settled", "result": "no", "side": "yes"}) + "\n")        # no gate -> ignored
    (tmp_path / "gate.jsonl").write_text(json.dumps({"close_ts": 12, "ticker": "T1", "shadow": True, "side": "no", "price": 0.8, "fee_c": 1.0}) + "\n")
    assert G.recent_outcomes(tmp_path) == [1]
    assert G.settle_shadow(tmp_path, {"T1": {"result": "yes"}}) == 1
    assert G.recent_outcomes(tmp_path) == [1, 0]
    rows = [json.loads(l) for l in (tmp_path / "gate.jsonl").read_text().splitlines()]; assert rows[0]["pnl_c"] == pytest.approx(-81.0)
