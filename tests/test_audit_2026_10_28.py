"""AUDIT_2026-10-28: book representation (None = side absent), first-failure rejection taxonomy, downstream None handling,
Thompson objective/allocation-history invariants, cfb raw capture, P12 freeze fingerprint."""
import json, math, pathlib
import pytest
from predhft import reject_taxonomy as RT
from predhft import s7_live as S7
from predhft import ts_levers as T
from predhft import bet_gate as BG
from predhft import cfb_feed as CF
from predhft.fees import kalshi_taker_fee

CLOSE = 1_800_000_000

def ob(yes=None, no=None):
    return {"orderbook_fp": {"yes_dollars": [[str(p), str(q)] for p, q in (yes or [])], "no_dollars": [[str(p), str(q)] for p, q in (no or [])]}}

def row(secs_left, yes_bid, yes_ask, bid_size=100.0, ask_size=100.0, d_bp=5.0, K=100.0, n=None, src="cfb", age=0.5, **kw):
    px = K * (1 + d_bp * 1e-4); n = (max(0, int(60 - secs_left)) if n is None else n)
    r = {"t": CLOSE - secs_left, "mins_left": secs_left / 60, "ticker": "KXBTC15M-TEST", "K": K, "spot": px, "partial_twap": px, "partial_n": n, "sigma1": 0.0004,
         "yes_bid": yes_bid, "yes_ask": yes_ask, "bid_size": bid_size, "ask_size": ask_size, "close_ts": CLOSE, "spot_src": src, "cfb_age_s": age}
    r.update(kw); return r

# ------------------------------------------------------------------------------------------------------ book representation
def test_book_sides_absent_no_side_means_no_yes_offer():
    b = S7.book_sides(ob(yes=[(0.999, 416689.19)], no=[]))
    assert b["yes_bid"] == 0.999 and b["yes_bid_present"] and b["yes_ask"] is None and not b["yes_ask_present"] and b["yes_ask_size"] == 0.0
    assert b["bids"] == [(0.999, 416689.19)] and b["asks"] == []
    yb, ya, ybs, yas = S7.book_touch(ob(yes=[(0.999, 416689.19)], no=[]))         # legacy sentinels unchanged for the rule arithmetic
    assert (yb, ya, ybs, yas) == (0.999, 1.0, 416689.19, 0.0)

def test_book_sides_two_sided_subpenny_depth_and_complement():
    b = S7.book_sides(ob(yes=[(0.042, 119.54), (0.04, 10), (0.03, 5), (0.02, 1)], no=[(0.957, 2558.6), (0.95, 20)]))
    assert b["yes_bid"] == 0.042 and b["yes_ask"] == pytest.approx(0.043) and b["yes_ask_size"] == 2558.6
    assert len(b["bids"]) == 3 and [(pytest.approx(p), q) for p, q in b["asks"]] == [(0.043, 2558.6), (0.05, 20)]
    assert b["asks"][0][0] == 1 - 0.957                                              # ADDENDUM2 §2: exact legacy float, not round(.., 4)
    assert S7.book_touch(ob(yes=[], no=[])) == (0.0, 1.0, 0.0, 0.0)

def test_side_present_handles_new_and_legacy_rows():
    assert RT.side_present({"yes_bid": 0.999, "yes_ask": 1.0, "bid_size": 5.0, "ask_size": 0.0}) == (True, False)       # legacy sentinel
    assert RT.side_present({"yes_bid": 0.0, "yes_ask": 0.02, "bid_size": 0.0, "ask_size": 9.0}) == (False, True)
    assert RT.side_present({"yes_bid": 0.999, "yes_ask": None, "bid_size": 5.0, "ask_size": 0.0, "yes_bid_present": True, "yes_ask_present": False}) == (True, False)
    assert RT.side_offer({"yes_bid": 0.999, "yes_ask": None, "bid_size": 5.0, "ask_size": 0.0, "yes_bid_present": True, "yes_ask_present": False}, "yes") == (None, 0.0)
    assert RT.side_offer({"yes_bid": 0.30, "yes_ask": 0.33, "bid_size": 7.0, "ask_size": 9.0}, "no") == (pytest.approx(0.70), 7.0)

# ------------------------------------------------------------------------------------------------------ taxonomy: one reason per check
def test_pinned_favourite_with_no_offer_is_economic_not_defect_and_not_fav_cap():
    c = RT.classify(row(30, 0.999, None, bid_size=4e5, ask_size=0.0, yes_bid_present=True, yes_ask_present=False), "KXBTC15M")
    assert c["reason"] == "no_economic_opportunity:no_offer_pinned" and c["side"] == "yes" and c["offer"] is None
    assert "risk_constraint:fav_cap" not in c["flags"]
    c2 = RT.classify(row(30, 0.999, 1.0, bid_size=4e5, ask_size=0.0), "KXBTC15M")                                       # legacy encoding, same answer
    assert c2["reason"] == "no_economic_opportunity:no_offer_pinned"

def test_one_sided_unpinned_book_is_a_data_defect():
    c = RT.classify(row(30, 0.60, None, bid_size=50, ask_size=0.0, yes_bid_present=True, yes_ask_present=False), "KXBTC15M")
    assert c["reason"] == "data_defect:one_sided_book_unpinned"
    assert RT.classify(row(30, None, None, 0.0, 0.0, yes_bid_present=False, yes_ask_present=False), "KXBTC15M")["reason"] == "data_defect:book_empty"

def test_class_order_feed_before_timing_before_risk_before_economic():
    stale = row(70, 0.90, 0.92, src="replica")
    c = RT.classify(stale, "KXBTC15M")
    assert c["reason"] == "feed_health:replica_fallback" and "timing_exclusion:outside_final_minute" in c["flags"]
    c = RT.classify(row(30, 0.96, 0.97), "KXBTC15M", arm_window_s=(10, 25))
    assert c["reason"] == "timing_exclusion:ts_arm_window" and "risk_constraint:fav_cap" in c["flags"]
    c = RT.classify(row(30, 0.96, 0.97, d_bp=0.2), "KXBTC15M")
    assert c["reason"] == "risk_constraint:fav_cap" and "no_economic_opportunity:d_small" in c["flags"]
    c = RT.classify(row(30, 0.90, 0.92, ask_size=4.0), "KXBTC15M")
    assert c["reason"] == "risk_constraint:depth_lt_lot"

def test_candidate_and_edge_use_executable_price_plus_tick_and_fee_once():
    r = row(30, 0.70, 0.72, d_bp=3.0)
    c = RT.classify(r, "KXBTC15M")
    price = 0.73; fee = kalshi_taker_fee(10, price) / 10
    assert c["edge_c"] == pytest.approx(round(100 * (c["p_side"] - price - fee), 2), abs=0.02)
    assert c["cls"] == "candidate" and c["flags"] == []
    c = RT.classify(row(30, 0.90, 0.92, d_bp=1.2), "KXBTC15M")        # offered, within cap, |d| ok, but p_cal too close to price
    assert c["reason"] == "no_economic_opportunity:edge_lt_min"

def test_partial_warmup_is_timing_but_missing_partial_later_is_feed_health():
    assert RT.classify(row(59, 0.80, 0.82, n=1), "KXBTC15M")["reason"] == "timing_exclusion:outside_runner_window"
    assert "timing_exclusion:partial_warmup" in RT.classify(row(59, 0.80, 0.82, n=1), "KXBTC15M")["flags"]
    assert RT.classify(row(30, 0.80, 0.82, n=0, partial_twap=None), "KXBTC15M")["reason"] == "feed_health:partial_missing"

def test_every_check_gets_exactly_one_primary_and_primary_matches_flag_order():
    rows = [row(30, 0.999, 1.0, 4e5, 0.0), row(30, 0.96, 0.97), row(30, 0.70, 0.72, d_bp=3.0), row(70, 0.9, 0.92), row(30, 0.9, 0.92, d_bp=0.1)]
    for r in rows:
        c = RT.classify(r, "KXBTC15M")
        assert c["reason"].count(":") == 1 and c["cls"] in RT.CLASSES
        assert RT.primary_from_flags(c["flags"]) == c["reason"]

# ------------------------------------------------------------------------------------------------------ downstream None handling
def test_ts_levers_normalise_keeps_rows_with_absent_side_and_bet_gate_quotes():
    r = {"t": CLOSE - 30, "mins_left": 0.5, "ticker": "X", "K": 100.0, "spot": 100.05, "partial_twap": 100.05, "partial_n": 30, "sigma1": 0.0004,
         "yes_bid": 0.999, "yes_ask": None, "bid_size": 5.0, "ask_size": 0.0, "close_ts": CLOSE, "spot_src": "cfb"}
    out = T.normalise([r], "KXBTC15M", basis_applied=True)
    assert len(out) == 1 and out[0]["ya"] == 1.0 and out[0]["yas"] == 0.0 and out[0]["yb"] == 0.999
    ask, sz, _, _ = T._side_quotes(out[0], "yes"); assert ask == 1.0 and sz == 0.0            # nobody offers YES -> no take fill
    q = BG._quotes({"yes_bid": None, "yes_ask": 0.03, "bid_size": 0.0, "ask_size": 40.0}); assert q["yb"] == 0.0 and q["ybs"] == 0.0 and q["ya"] == 0.03

# ------------------------------------------------------------------------------------------------------ Thompson invariants
def test_thompson_objective_is_net_cents_per_contract_and_no_trade_closes_are_not_observations():
    S = [{"t": CLOSE - 50, "mins_left": 50 / 60, "ticker": "KXBTC15M-T", "K": 100.0, "spot": 100.05, "partial_twap": 100.05, "partial_n": 10, "sigma1": 0.0005,
          "yes_bid": 0.78, "yes_ask": 0.80, "bid_size": 50, "ask_size": 50, "close_ts": CLOSE, "spot_src": "cfb"}]
    o = T.replay_window(S, "yes", T.ARM_BY_ID["E3-early-C95-take"])
    assert o["pnl_c"] == pytest.approx(100 * (1 - 0.81) - 100 * kalshi_taker_fee(T.LOT, 0.81) / T.LOT, abs=0.01)     # win - price - fee, not a win indicator
    state = {"obs": {"E3-early-C95-take": {"1": {}, "2": {"KXBTC15M": 12.0}, "3": {}}}}
    assert T.cluster_obs(state, "E3-early-C95-take") == [12.0]                                                         # empty closes: neither win nor loss

def test_log_selection_appends_allocation_history(tmp_path):
    p = tmp_path / "sel.jsonl"; arm = T.ARM_BY_ID["E3-early-C95-take"]
    T.log_selection("s7p13-KXBTC15M", CLOSE, arm, {"probs": {arm.id: 0.4}, "size_mult": 0.5, "n": 3, "mean": -1.2, "sd": 4.0, "p_best": 0.4}, path=p)
    T.log_selection("s7p13-KXBTC15M", CLOSE + 900, arm, {}, path=p)
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["arm"] == arm.id and rows[0]["probs"][arm.id] == 0.4 and rows[0]["close_ts"] == CLOSE and rows[1]["n"] is None

# ------------------------------------------------------------------------------------------------------ cfb raw capture
def test_cfb_raw_capture_preserves_verbatim_message_with_local_receipt(tmp_path):
    raw = '{"type":"cfbenchmarks_value","msg":{"index_id":"BRTI","data":"{\\"value\\":\\"80000.12\\",\\"time\\":1793140392000}"}}'
    CF._raw_write("BTC", 1793140392.05, raw, root=tmp_path)
    files = list((tmp_path / "raw").glob("BTC.*.raw.jsonl")); assert len(files) == 1
    r = json.loads(files[0].read_text().splitlines()[0]); assert r["raw"] == raw and r["t_local"] == 1793140392.05

# ------------------------------------------------------------------------------------------------------ P12 freeze fingerprint
def test_p12_frozen_decision_functions_fingerprint():
    """P12 (s7p15: dist=t nu=8 scale=0.8, dyn threshold) is frozen for the forward evaluation window defined in AUDIT_2026-10-28 §6.
    If any of these values change, the frozen version has changed and the evaluation window must restart."""
    p, z = S7.p_yes_t(100.03, 100.0, 0.0004, 0.5, 8.0, 0.8, 100.02, 30)
    assert (round(p, 6), round(z, 6)) == (0.997481, 3.061709)
    assert round(S7.dyn_thr(z, 0.02), 6) == 0.031913
    assert round(S7.p_yes(100.03, 100.0, 0.0004, 0.5, 100.02, 30), 6) == 0.998900
    p2, z2 = S7.p_yes_t(100.0, 100.0, 0.0006, 0.6, 8.0, 0.8, 99.995, 24)
    assert (round(p2, 6), round(z2, 6)) == (0.440224, -0.124224)
    assert round(S7.dyn_thr(z2, 0.03), 6) == 0.043360
