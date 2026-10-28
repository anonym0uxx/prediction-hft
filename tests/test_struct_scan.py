"""P21 structural scanner: fee formula, interval mapping, ladder / cross-horizon / partition detectors, depth-limited sizing, lock rule."""
import math
from predhft import struct_scan as ss
from predhft.fees import kalshi_taker_fee

def M(ticker, stt, fl=None, cp=None, yb=0.0, ya=1.0, close="2026-10-28T06:00:00Z"):
    return {"ticker": ticker, "strike_type": stt, "floor_strike": fl, "cap_strike": cp, "yes_bid_dollars": f"{yb:.4f}", "yes_ask_dollars": f"{ya:.4f}", "close_time": close}

# ---------------------------------------------------------------- fees (schedule effective 2026-07-07: 0.07*C*P*(1-P), rounded up)
def test_fee_formula_matches_schedule_table():
    # published table: 100 contracts at 0.50 -> $1.75; at 0.05 -> $0.34; at 0.99 -> $0.07; 1 contract at 0.50 -> $0.02 (cent-aligned member)
    assert math.isclose(ss.fee_exact(100, 0.50), 1.75)
    assert math.isclose(ss.fee_exact(100, 0.05), 0.3325)              # direct member: $0.0001 alignment -> 0.3325 (table shows cent-rounded 0.34)
    assert math.isclose(ss.fee_exact(100, 0.05, precision=0.01), 0.34)
    assert math.isclose(ss.fee_exact(1, 0.50, precision=0.01), 0.02)
    assert math.isclose(ss.fee_exact(100, 0.99), 0.0693)
    assert ss.fee_exact(100, 0.99, precision=0.01) == kalshi_taker_fee(100, 0.99) == 0.07
    assert ss.fee_exact(10, 0.5) <= kalshi_taker_fee(10, 0.5)        # fees.py (cent ceil) is an upper bound of the exact rule
    assert ss.fee_pc(0.5) == 0.0175 and ss.fee_pc(0.0) == 0.0 and ss.fee_pc(1.0) == 0.0

# ---------------------------------------------------------------- settlement intervals
def test_intervals_follow_rules_text():
    assert ss.interval(M("a", "greater", fl=80799.99)) == (8080000, ss.INF)             # above 80799.99 <=> A >= 80800.00
    assert ss.interval(M("b", "less", cp=70200)) == (-ss.INF, 7020000)                 # below 70200 <=> A <= 70199.99
    assert ss.interval(M("c", "between", fl=80800, cp=80899.99)) == (8080000, 8090000)
    assert ss.interval(M("d", "greater_or_equal", fl=80849.15)) == (8084915, ss.INF)   # 15M: at least the open reference
    assert ss.interval(M("e", "weird")) is None
    # bucket + above(b) == above(a): disjoint union holds exactly on the cents grid
    a, b, c = ss.interval(M("a", "greater", fl=80799.99)), ss.interval(M("c", "between", fl=80800, cp=80899.99)), ss.interval(M("z", "greater", fl=80899.99))
    assert b == (a[0], c[0]) and a[1] == c[1] == ss.INF

# ---------------------------------------------------------------- ladder monotonicity
def test_ladder_detector_flags_crossed_pair_and_not_consistent_ladder():
    good = [M("T1", "greater", fl=100, yb=0.60, ya=0.62), M("T2", "greater", fl=200, yb=0.30, ya=0.32), M("T3", "greater", fl=300, yb=0.05, ya=0.07)]
    ports = ss.candidates(good)
    ladders = [p for p in ports if p["kind"] == "ladder"]
    assert len(ladders) == 3 and all(p["net_pc"] < 0 for p in ladders)           # outer YES ask + inner NO ask > 1 everywhere
    # violation: market bids 0.40 for 'above 200' while offering 'above 100' at 0.35 -> buy YES T1 @.35, buy NO T2 @.60 -> cost .95, payoff >= 1
    bad = [M("T1", "greater", fl=100, yb=0.33, ya=0.35), M("T2", "greater", fl=200, yb=0.40, ya=0.42)]
    hit = [p for p in ss.candidates(bad) if p["kind"] == "ladder"][0]
    assert hit["legs"] == [("T1", "yes"), ("T2", "no")] and math.isclose(hit["cost"], 0.95) and hit["payoff"] == 1.0
    assert math.isclose(hit["gross_pc"], 0.05) and hit["net_pc"] > 0 and hit["net_pc"] < 0.05

def test_cross_horizon_15m_vs_hourly_ladder():
    # 15M up: A >= 80849.15 ; hourly above 80799.99: A >= 80800 -> 15M-up implies above-80799.99 (nested)
    ms = [M("UP", "greater_or_equal", fl=80849.15, yb=0.70, ya=0.72), M("H", "greater", fl=80799.99, yb=0.62, ya=0.64)]
    hit = [p for p in ss.candidates(ms) if p["kind"] == "ladder"][0]
    assert hit["legs"] == [("H", "yes"), ("UP", "no")] and math.isclose(hit["cost"], 0.64 + 0.30) and hit["net_pc"] > 0
    # the other direction (K above the reference) nests the other way and is consistent here
    ms2 = [M("UP", "greater_or_equal", fl=80849.15, yb=0.70, ya=0.72), M("H2", "greater", fl=80899.99, yb=0.20, ya=0.22)]
    lad = [p for p in ss.candidates(ms2) if p["kind"] == "ladder"]
    assert lad[0]["legs"] == [("UP", "yes"), ("H2", "no")] and lad[0]["net_pc"] < 0

def test_equal_complement_union_and_partition():
    ms = [M("R-T", "less", cp=70200, yb=0.10, ya=0.12), M("R-B", "between", fl=70200, cp=70299.99, yb=0.30, ya=0.33), M("R-G", "greater", fl=70299.99, yb=0.55, ya=0.57),
          M("D_1", "greater", fl=70199.99, yb=0.86, ya=0.88), M("D_2", "greater", fl=70299.99, yb=0.56, ya=0.58)]
    ports = ss.candidates(ms); kinds = {p["kind"] for p in ports}
    assert {"crosshz_equal", "crosshz_complement", "crosshz_union", "partition_yes", "partition_no", "ladder", "yes_no"} <= kinds
    eq = [p for p in ports if p["kind"] == "crosshz_equal"]; assert any(set(l[0] for l in p["legs"]) == {"R-G", "D_2"} for p in eq)
    comp = [p for p in ports if p["kind"] == "crosshz_complement"][0]; assert set(l[0] for l in comp["legs"]) == {"R-T", "D_1"} and math.isclose(comp["cost"], 1.00)
    uns = [p for p in ports if p["kind"] == "crosshz_union" and p["payoff"] == 1.0]
    un = [p for p in uns if p["legs"] == [("R-B", "yes"), ("D_2", "yes"), ("D_1", "no")]][0]; assert math.isclose(un["cost"], 0.33 + 0.58 + 0.14)
    assert any(p["legs"] == [("R-B", "yes"), ("R-G", "yes"), ("D_1", "no")] for p in uns)      # R-G is the same event as D_2
    py = [p for p in ports if p["kind"] == "partition_yes"][0]; assert math.isclose(py["cost"], 0.12 + 0.33 + 0.57) and py["payoff"] == 1.0
    pn = [p for p in ports if p["kind"] == "partition_no"][0]; assert pn["payoff"] == 2.0 and math.isclose(pn["cost"], 0.90 + 0.70 + 0.45)
    # a YES+NO on one market costs ask + (1 - bid) >= 1 whenever the book is not crossed
    assert all(p["gross_pc"] <= 0 for p in ports if p["kind"] == "yes_no")

# ---------------------------------------------------------------- depth-limited sizing
def test_executable_size_walks_depth_and_stops_at_fee_breakeven():
    # leg A asks: 30 @ .40, 50 @ .45, 100 @ .60 ; leg B asks: 40 @ .50, 10 @ .52, 100 @ .70 ; payoff 1
    A = [(0.40, 30), (0.45, 50), (0.60, 100)]; B = [(0.50, 40), (0.52, 10), (0.70, 100)]
    ex = ss.executable([A, B], 1.0)
    # units 1-30: .90 + fee(.0168+.0175=.0343) < 1 ok; 31-40: .95 + .0348 < 1 ok; 41-50: .97 + fee(.0173+.0175) = 1.005 -> stop
    assert ex["size"] == 40 and ex["depth"] == [180, 150]
    assert math.isclose(ex["gross"], 30 * 0.10 + 10 * 0.05) and 0 < ex["net"] < ex["gross"]
    assert math.isclose(ex["vwap"][0], (30 * 0.40 + 10 * 0.45) / 40) and math.isclose(ex["vwap"][1], 0.50)
    # depth cap: thin leg limits size even when prices stay good
    assert ss.executable([[(0.40, 5)], [(0.50, 1000)]], 1.0)["size"] == 5
    assert ss.executable([[], [(0.5, 10)]], 1.0)["size"] == 0

def test_asks_from_unified_book():
    ob = {"orderbook_fp": {"yes_dollars": [["0.3000", "10"], ["0.3500", "20"]], "no_dollars": [["0.6000", "7"], ["0.5500", "3"]]}}
    assert ss.asks_from_book(ob, "yes") == [(0.40, 7.0), (0.45, 3.0)]        # YES asks = 1 - NO bids, best first
    assert ss.asks_from_book(ob, "no") == [(0.65, 20.0), (0.70, 10.0)]

# ---------------------------------------------------------------- (d) lock rule
def test_lock_state_uses_remaining_average_and_envelope():
    env = {s: 10.0 for s in ss.ENV_S}
    snap = {"spot": 80000.0, "K": 79990.0, "partial_twap": 80010.0, "partial_n": 50, "mins_left": 10 / 60}
    L = ss.lock_state(snap, env)          # k_eff = (79990 - (5/6)*80010) * 6 = 79890 -> required move 13.8 bp vs bound 15 -> not locked
    assert L["side"] == "yes" and not L["locked"] and 13 < L["req_bp"] < 14 and L["bound_bp"] == 15.0
    L2 = ss.lock_state({**snap, "K": 79980.0}, env)                        # k_eff = 79830 -> 21 bp > 15 -> locked
    assert L2["locked"] and L2["side"] == "yes"
    assert ss.lock_state({**snap, "partial_n": 2}, env) is None
    hits = ss.lag_hits_from_snaps([{**snap, "K": 79980.0, "t": 1.0, "ticker": "X", "spot_src": "cfb", "yes_ask": 0.97, "ask_size": 50, "yes_bid": 0.95, "bid_size": 5, "close_ts": 0},
                                   {**snap, "K": 79980.0, "t": 2.0, "ticker": "X", "spot_src": "cfb", "yes_ask": 0.98, "ask_size": 50, "yes_bid": 0.95, "bid_size": 5, "close_ts": 0},
                                   {**snap, "K": 79980.0, "t": 3.0, "ticker": "X", "spot_src": "cfb", "yes_ask": 1.0, "ask_size": 0, "yes_bid": 0.99, "bid_size": 5, "close_ts": 0}], env)
    assert len(hits) == 1 and hits[0]["persist_s"] == 1.0 and hits[0]["n_obs"] == 2 and math.isclose(hits[0]["gross_pc"], 0.03)
