"""SOL-v1 unit tests: probability-model limits, EV/fees, depth-weighted price, partial fills, hold/exit, inventory cap, staleness gates."""
from __future__ import annotations
import math, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import pytest
from predhft import sol_v1 as S
from predhft.fees import kalshi_taker_fee

T = 1_800_000_000 - (1_800_000_000 % 900)          # a 15-min boundary
def flat_series(start: int, end: int, v: float = 100.0, bump: float = 0.0):
    """(sec, value) for start..end-1 with alternating +/- bump so realised vol is non-zero."""
    return [(s, v + (bump if s % 2 else -bump)) for s in range(start, end)]

# ------------------------------------------------------------------ probability model
def test_remaining_variance_factor_matches_closed_forms():
    series = flat_series(T - 2000, T - 60)                 # nothing observed inside the window; latest sec = T-61
    terms = S.settlement_terms(series, T, T - 61)
    assert terms["n"] == 0 and terms["m"] == 60 and terms["partial"] is None
    # leads 1..60: V = (1/m^2) sum_j l_j (2(m-j)+1) = (m+1)(2m+1)/(6m) for consecutive leads starting at 1
    m = 60; assert abs(terms["V"] - (m + 1) * (2 * m + 1) / (6 * m)) < 1e-9
    # 10 min before the window: V = (tau - 60) + (m+1)(2m+1)/(6m) with tau = lead of the first sample (T-60) - now = 540
    terms2 = S.settlement_terms(series[:-540], T, T - 601)
    assert abs(terms2["V"] - (540 + (m + 1) * (2 * m + 1) / (6 * m))) < 1e-9

def test_p_tends_to_indicator_as_remaining_samples_vanish():
    K = 100.0
    # observed partial above K, index at K: as m -> 0 the probability -> 1 regardless of sigma
    series = flat_series(T - 2000, T - 60) + [(s, 100.02) for s in range(T - 60, T - 1)]      # 59 observed at 100.02, latest = T-2
    terms = S.settlement_terms(series, T, T - 2); assert terms["n"] == 59 and terms["m"] == 1
    pr = S.p_settle_ge(terms, K, sigma_1s=10e-4, kappa=1.0)       # 59 x 2 c of observed excess vs one remaining sample with sd 10 c
    assert pr["p"] > 0.999
    assert S.p_settle_ge(S.settlement_terms(series, T, T - 31), K, sigma_1s=10e-4)["p"] < pr["p"]   # 30 samples left: less certain
    series_full = series + [(T - 1, 100.02)]
    terms0 = S.settlement_terms(series_full, T, T - 1); assert terms0["m"] == 0
    assert S.p_settle_ge(terms0, K, 50e-4)["p"] == 1.0 and S.p_settle_ge(terms0, K, 50e-4)["why"] == "determined"
    # partial below K -> 0
    series_lo = flat_series(T - 2000, T - 60) + [(s, 99.98) for s in range(T - 60, T)]
    assert S.p_settle_ge(S.settlement_terms(series_lo, T, T - 1), K, 50e-4)["p"] == 0.0

def test_tie_rule_and_precision():
    K = 100.0; series = flat_series(T - 2000, T - 60) + [(s, 100.0) for s in range(T - 60, T)]
    # average exactly K -> YES ('>=' rule); average K - 1 ulp (4 dp) -> NO
    assert S.p_settle_ge(S.settlement_terms(series, T, T - 1), K, 1e-4)["p"] == 1.0
    series2 = flat_series(T - 2000, T - 60) + [(s, 100.0 - 1e-4) for s in range(T - 60, T)]
    assert S.p_settle_ge(S.settlement_terms(series2, T, T - 1), K, 1e-4)["p"] == 0.0
    assert S.settle_from_feed(series, T, K) == {"avg": 100.0, "result": "yes"}
    assert S.settle_from_feed(series[:-1], T, K) is None            # 59 samples: not reconstructable

def test_p_monotone_in_index_and_at_the_money_half():
    series = flat_series(T - 2000, T - 300, 100.0, 0.01)
    terms = S.settlement_terms(series, T, T - 301)
    p_atm = S.p_settle_ge(terms, terms["x_now"], 1e-4)["p"]                 # K = x_now exactly: tie adj makes it a hair above .5
    assert 0.5 <= p_atm < 0.51
    p_up = S.p_settle_ge(terms, 99.5, 1e-4)["p"]; p_dn = S.p_settle_ge(terms, 100.5, 1e-4)["p"]
    assert p_up > 0.9 > 0.1 > p_dn
    assert S.p_settle_ge(terms, 99.5, 1e-4, kappa=2.0)["p"] < p_up           # more variance -> less extreme

def test_realized_sigma_fallback_and_floor():
    sig, n, src = S.realized_sigma_1s(flat_series(T - 100, T, 100.0, 0.0), T, min_n=120)
    assert src == "fallback" and sig == S.CFG["sigma_fallback_1s"]
    sig2, n2, src2 = S.realized_sigma_1s(flat_series(T - 1000, T, 100.0, 0.0), T)
    assert src2 == "realized" and sig2 == S.CFG["vol_floor_1s"] and n2 == S.CFG["vol_window_s"] - 1
    sig3, _, _ = S.realized_sigma_1s(flat_series(T - 1000, T, 100.0, 0.05), T)
    assert abs(sig3 - math.log(100.05 / 99.95)) < 1e-6

def test_blend_weights_by_horizon():
    assert S.blend_w_for(60) == S.CFG["blend_w"]["final"] and S.blend_w_for(300) == S.CFG["blend_w"]["mid"] and S.blend_w_for(700) == S.CFG["blend_w"]["early"]
    p, w = S.p_used_from(0.7, 0.5, 0.02, 300); assert abs(p - (0.9 * 0.7 + 0.1 * 0.5)) < 1e-12 and w == 0.9
    p2, w2 = S.p_used_from(0.7, None, None, 300); assert p2 == 0.7 and w2 == 1.0           # one-sided book: model only
    p3, w3 = S.p_used_from(0.7, 0.5, 0.2, 300); assert p3 == 0.7                            # too wide: model only

# ------------------------------------------------------------------ book / execution
BOOK = {"yes_bid": 0.40, "yes_ask": 0.43, "bids": [[0.40, 6], [0.39, 10], [0.35, 50]], "asks": [[0.43, 4], [0.44, 3], [0.50, 100]]}

def test_side_levels_price_spaces():
    assert S.side_levels(BOOK, "yes", "buy")[0] == (0.43, 4)
    assert S.side_levels(BOOK, "no", "buy")[0] == (0.60, 6)                 # NO ask = 1 - best YES bid
    assert S.side_levels(BOOK, "yes", "sell")[0] == (0.40, 6)
    assert S.side_levels(BOOK, "no", "sell")[0] == (0.57, 4)                # NO bid = 1 - best YES ask

def test_depth_weighted_walk_and_partial_fill():
    w = S.walk(S.side_levels(BOOK, "yes", "buy"), 10, limit=0.45)
    assert w["qty"] == 7 and abs(w["avg_px"] - (4 * 0.43 + 3 * 0.44) / 7) < 1e-12 and w["levels_used"] == 2
    f = S.paper_fill("yes", 10, 0.45, BOOK)
    assert f["qty"] == 7 and f["partial"] and f["status"] == "partial" and abs(f["px"] - ((4 * 0.43 + 3 * 0.44) / 7 + S.TICK)) < 1e-12
    assert f["fee"] == kalshi_taker_fee(7, f["px"]) and abs(f["cost"] - 7 * f["px"]) < 1e-12
    assert S.paper_fill("yes", 10, 0.42, BOOK)["qty"] == 0                   # limit below the offer: nothing, never touch=fill
    assert S.walk(S.side_levels(BOOK, "yes", "buy"), 10, limit=0.50)["qty"] == 10

def test_fee_per_contract_is_ceil_per_order():
    assert S.fee_pc(10, 0.5) == kalshi_taker_fee(10, 0.5) / 10 == 0.018      # 0.07*10*.25 = 0.175 -> ceil to 0.18 per order
    assert S.fee_pc(1, 0.5) == 0.02 and S.fee_pc(0, 0.5) == 0.0

def test_entry_ev_sign_fees_and_limit_bound():
    cfg = dict(S.CFG); cfg["edge_base_c"] = 3.0
    # p_used = 0.60 for YES at ask 0.43: ev = .60 - .43 - fee - adverse ; z large -> tiny adverse/req terms
    e = S.entry_ev(0.60, "yes", BOOK, 10, z=3.0, sd_mean=0.05, x_now=100.0, sigma_1s=1e-4, latency_s=0.3, cfg=cfg, kappa=1.0)
    assert e["why"] == "ok" and e["qty_obtainable"] == 10 and e["px_dw"] > 0.43 and e["ev_pc"] > 0 and e["limit"] <= 0.60 - 0.03
    assert abs(e["ev_pc"] - (0.60 - e["px_dw"] - e["fee_pc"] - e["adverse_pc"])) < 1e-12
    assert e["depth_within_limit"] >= 10
    # no edge: p_used 0.44 vs ask 0.43 -> fee + requirement kill it
    e2 = S.entry_ev(0.44, "yes", BOOK, 10, z=0.1, sd_mean=0.05, x_now=100.0, sigma_1s=1e-4, latency_s=0.3, cfg=cfg)
    assert e2["why"] in ("edge_lt_req", "no_depth_within_limit") and (e2["ev_pc"] is None or e2["ev_pc"] < e2["req_pc"])
    # hypersensitive outcome (sd_mean -> 0, z finite): adverse haircut saturates -> abstain
    e3 = S.entry_ev(0.60, "yes", BOOK, 10, z=0.5, sd_mean=1e-9, x_now=100.0, sigma_1s=1e-4, latency_s=0.3, cfg=cfg)
    assert e3["adverse_pc"] == 1.0 and e3["why"] != "ok"
    assert S.entry_ev(0.9, "yes", {"bids": [], "asks": []}, 10, 2.0, 0.05, 100.0, 1e-4, 0.3, cfg)["why"] == "no_offer"

def test_hold_or_exit_rule_no_stop_loss():
    # holding 10 YES; p_side .40; bids 6@.40 + 10@.39 -> full liquidation possible at dw .396 minus fee
    h = S.hold_or_exit(0.40, "yes", BOOK, 10, margin_c=2.0)
    assert h["decision"] == "hold" and h["exit_qty"] == 10 and abs(h["exit_px"] - (6 * 0.40 + 4 * 0.39) / 10) < 1e-12
    h2 = S.hold_or_exit(0.30, "yes", BOOK, 10, margin_c=2.0)          # market pays .396-fee vs hold .30 -> exit
    assert h2["decision"] == "exit" and h2["exit_proceeds"] > h2["hold_value"] + 0.2
    h3 = S.hold_or_exit(0.05, "yes", BOOK, 100, margin_c=2.0)         # cannot liquidate the full position (66 displayed) -> hold
    assert h3["decision"] == "hold" and h3["exit_qty"] == 66
    assert S.hold_or_exit(0.90, "yes", BOOK, 10)["decision"] == "hold" # losing mark never forces an exit: no stop
    assert S.conservative_mark("yes", BOOK, 10) == pytest.approx(10 * (6 * 0.40 + 4 * 0.39) / 10 - kalshi_taker_fee(10, (6 * 0.40 + 4 * 0.39) / 10))
    assert S.conservative_mark("yes", {"bids": [], "asks": []}, 10) == 0.0 and S.conservative_mark("yes", None, 10) == 0.0

# ------------------------------------------------------------------ gates / decide / sizing
def test_staleness_gates():
    now = 1000.0
    assert S.staleness(now, now - 0.5, now - 1.0)["ok"]
    st = S.staleness(now, now - 5.0, None); assert not st["ok"] and st["reasons"] == ["stale_cfb", "stale_book"]

def test_decide_abstains_on_stale_and_outside_window_and_enters_on_edge():
    series = flat_series(T - 2000, T - 300, 100.0, 0.01); now = T - 300.5; now_sec = T - 301
    K = 100.9                                                           # 90 bp above: with ~2 bp/s vol over ~5 min p_yes ~1 %
    book = {"yes_bid": 0.30, "yes_ask": 0.32, "bids": [[0.30, 200]], "asks": [[0.32, 200]]}  # market says 31 %: NO is cheap if model says ~5 %
    size_fn = S.kelly_size_fn()
    rec = S.decide(now, T, K, series, now - 5.0, book, now - 0.2, size_fn, now_sec=now_sec)
    assert rec["decision"] == "abstain" and "stale_cfb" in rec["reasons"]
    rec = S.decide(T - 5, T, K, series, T - 5.1, book, T - 5.2, size_fn, now_sec=T - 6)
    assert "outside_entry_window" in rec["reasons"]
    rec = S.decide(now, T, K, series, now - 0.1, book, now - 0.2, size_fn, now_sec=now_sec)
    assert rec["p_model"] < 0.05 and rec["decision"] == "enter" and rec["side"] == "no" and 5 <= rec["qty"] <= 10 and rec["limit"] <= rec["p_side"] - 0.03 and rec["px_dw"] <= 0.701
    assert rec["ev"]["no"]["ev_pc"] >= rec["ev"]["no"]["req_pc"]
    assert rec["blend_w"] == S.CFG["blend_w"]["mid"]

def test_kelly_size_fn_min_lot_cap_and_thin_book():
    fn = S.kelly_size_fn(bankroll=250, frac=0.25, lot_cap=10, min_lot=5)
    q, why = fn("yes", 0.60, 0.40, avail=500, fee=0.017, p_mkt_side=None); assert q == 10
    q2, _ = fn("yes", 0.60, 0.40, avail=12, fee=0.017, p_mkt_side=None); assert q2 == 6           # thin book: half of 12
    q3, _ = fn("yes", 0.60, 0.40, avail=8, fee=0.017, p_mkt_side=None); assert q3 == 0            # 4 < min lot
    q4, _ = fn("yes", 0.41, 0.40, avail=500, fee=0.017, p_mkt_side=None); assert q4 == 0          # no edge after fee

def test_one_position_per_close_inventory(tmp_path):
    pos = S.Positions(tmp_path); assert not pos.has_close(T)
    S._append(pos.fills, {"kind": "fill", "intent_id": "sol-v1:X:yes", "ticker": "X", "close_ts": T, "status": "open", "side": "yes", "qty": 10, "cost": 3.0, "fee": 0.02, "K": 1.0})
    pos2 = S.Positions(tmp_path); assert pos2.has_close(T) and "sol-v1:X:yes" in pos2.intents
    S._append(pos.fills, {"kind": "settle", "intent_id": "sol-v1:X:yes", "ticker": "X", "close_ts": T, "pnl": 1.0})
    pos3 = S.Positions(tmp_path); assert not pos3.has_close(T) and "sol-v1:X:yes" in pos3.intents  # intent id stays claimed after settle

def test_risk_ledger_cap_blocks_second_crypto_lot_same_close(tmp_path, monkeypatch):
    """Shared inventory: risk.size refuses when the crypto per-close cost cap is already used (by any family)."""
    from predhft import risk
    monkeypatch.setattr(risk, "GLOBAL", tmp_path / "events.jsonl")
    led = risk.Ledger(tmp_path / "fam", family="sol-v1")
    q0, why0 = risk.size("KXSOL15M", 0.65, 0.40, 500, led, lot_cap=10, fee_per_contract=0.017, close_ts=T); assert 5 <= q0 <= 10, why0
    led.record_open("KXSOL15M-A", "KXSOL15M", 10 * 0.40, close_ts=T, side="yes")
    led2 = risk.Ledger(tmp_path / "fam2", family="s7p15-KXSOL15M")
    q1, why1 = risk.size("KXSOL15M", 0.95, 0.90, 500, led2, lot_cap=10, fee_per_contract=0.01, close_ts=T)
    assert q1 == 0 and "crypto close_ts cap" in why1                                           # 4.0 + 9.0 > 12 -> blocked for ANY family
    # sol-v1 itself is blocked once an s7p* family already holds this ticker at this close (symmetric one-family rule)
    led2.record_open("KXSOL15M-A", "KXSOL15M", 2.0, close_ts=T, side="no")
    q2, why2 = risk.size("KXSOL15M", 0.65, 0.40, 500, led, lot_cap=10, fee_per_contract=0.017, close_ts=T); assert q2 == 0 and "s7p15_holds_ticker" in why2
