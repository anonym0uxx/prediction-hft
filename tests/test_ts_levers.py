"""P18 Thompson lever selection: replay correctness on synthetic snapshots, posterior math, sampling floor/guards, state persistence."""
import json, math, pathlib, random
import pytest
from predhft import ts_levers as T
from predhft.fees import kalshi_taker_fee

CLOSE = 1_800_000_000
def snap(secs_left: float, ya: float, yb: float, d_bp: float = 5.0, K: float = 100.0, n: int = 10, yas: float = 50, ybs: float = 50, t0: float = CLOSE - 60.0, src: str = "cfb", sigma1: float = 0.0005) -> dict:
    """One final-minute book row as s7_live writes it. d_bp>0 -> projection says YES. partial==spot so proj==spot."""
    px = K * (1 + d_bp * 1e-4)
    return {"t": t0 + (60 - secs_left), "mins_left": secs_left / 60.0, "ticker": "KXBTC15M-TEST", "K": K, "spot": px, "partial_twap": px, "partial_n": n, "sigma1": sigma1,
            "yes_bid": yb, "yes_ask": ya, "bid_size": ybs, "ask_size": yas, "close_ts": CLOSE, "spot_src": src}

A = T.ARM_BY_ID
def fee_c(price): return 100 * kalshi_taker_fee(T.LOT, price) / T.LOT

# ------------------------------------------------------------------------------------------------------------ replay: take
def test_take_fill_and_pnl_win_and_loss():
    S = [snap(50, 0.80, 0.78), snap(48, 0.80, 0.78)]
    o = T.replay_window(S, "yes", A["E3-early-C95-take"])
    assert o and o["fill"] == "take" and o["side"] == "yes" and o["price"] == pytest.approx(0.81)
    assert o["pnl_c"] == pytest.approx(100 * (1 - 0.81) - fee_c(0.81), abs=1e-6)
    lo = T.replay_window(S, "no", A["E3-early-C95-take"])
    assert lo["pnl_c"] == pytest.approx(-81 - fee_c(0.81), abs=1e-6)

def test_take_no_side_requires_no_quotes():
    # d<0 -> buy NO; NO ask = 1 - yes_bid = 0.20 -> fill 0.21; resting size for NO ask = yes bid size
    S = [snap(50, 0.85, 0.80, d_bp=-5.0, ybs=50), snap(48, 0.85, 0.80, d_bp=-5.0, ybs=50)]
    o = T.replay_window(S, "no", A["E3-early-C95-take"])
    assert o and o["side"] == "no" and o["price"] == pytest.approx(0.21) and o["pnl_c"] > 0

def test_take_respects_cap_size_and_edge():
    assert T.replay_window([snap(50, 0.96, 0.94), snap(48, 0.96, 0.94)], "yes", A["E3-early-C95-take"]) is None       # ask > cap .95
    assert T.replay_window([snap(50, 0.80, 0.78, yas=5), snap(48, 0.80, 0.78, yas=5)], "yes", A["E3-early-C95-take"]) is None   # size < lot
    # edge: p_cal(d=5bp, sigma1 5bp/min, T-50s) is ~0.99; at ask .95 the fill .96 + fee leaves < 3c -> no trade; at .80 fine
    assert T.replay_window([snap(50, 0.95, 0.93), snap(48, 0.95, 0.93)], "yes", A["E3-early-C95-take"]) is None
    assert T.replay_window([snap(50, 0.80, 0.78), snap(48, 0.80, 0.78)], "yes", A["E7-early-C95-take"]) is not None
    # cap .97 cannot pay a 3c edge after +1 tick and fee (p <= 1): the cap lever is degenerate for take arms under E >= 3c
    assert T.replay_window([snap(50, 0.96, 0.94), snap(48, 0.96, 0.94)], "yes", A["E3-early-C97-take"]) is None

def test_take_recheck_uses_next_snapshot():
    # next snapshot (<= 4 s later) lost the offer -> live re-check fails -> no fill at that snap; nothing later -> None
    assert T.replay_window([snap(50, 0.80, 0.78), snap(48.5, 0.97, 0.78)], "yes", A["E3-early-C95-take"]) is None
    # next snapshot still offers but worse: pay the worse ask + 1 tick
    o = T.replay_window([snap(50, 0.80, 0.78), snap(48.5, 0.83, 0.78)], "yes", A["E3-early-C95-take"])
    assert o["price"] == pytest.approx(0.84)
    # next snapshot too far away (> 4 s): decision snapshot alone
    o = T.replay_window([snap(50, 0.80, 0.78), snap(40, 0.97, 0.78)], "yes", A["E3-early-C95-take"])
    assert o["price"] == pytest.approx(0.81)

def test_gates_partial_n_and_dmin():
    assert T.replay_window([snap(50, 0.80, 0.78, n=2), snap(48, 0.80, 0.78, n=2)], "yes", A["E3-early-C95-take"]) is None
    assert T.replay_window([snap(50, 0.80, 0.78, d_bp=0.5), snap(48, 0.80, 0.78, d_bp=0.5)], "yes", A["E3-early-C95-take"]) is None
    # SOL needs 2 bp
    # quiet tape (sigma1 1 bp/min) so p_cal(1.5 bp) ~ .99 and the edge exists: SOL's 2 bp D_MIN still blocks, BTC's 1 bp does not
    assert T.replay_window([snap(50, 0.80, 0.78, d_bp=1.5, sigma1=1e-4), snap(48, 0.80, 0.78, d_bp=1.5, sigma1=1e-4)], "yes", A["E3-early-C95-take"], series="KXSOL15M") is None
    assert T.replay_window([snap(50, 0.80, 0.78, d_bp=1.5, sigma1=1e-4), snap(48, 0.80, 0.78, d_bp=1.5, sigma1=1e-4)], "yes", A["E3-early-C95-take"], series="KXBTC15M") is not None
    # p_cal itself gates: at sigma1 5 bp/min, d = 1.5 bp is only p ~ .68 -> no 3c edge at ask .80
    assert T.replay_window([snap(50, 0.80, 0.78, d_bp=1.5), snap(48, 0.80, 0.78, d_bp=1.5)], "yes", A["E3-early-C95-take"], series="KXBTC15M") is None

# ------------------------------------------------------------------------------------------------------------ replay: timing
def test_timing_windows():
    early_only = [snap(50, 0.80, 0.78), snap(48, 0.80, 0.78)]
    assert T.replay_window(early_only, "yes", A["E3-early-C95-take"]) is not None
    assert T.replay_window(early_only, "yes", A["E3-mid-C95-take"]) is None
    assert T.replay_window(early_only, "yes", A["E3-late-C95-take"]) is None
    late_only = [snap(20, 0.80, 0.78), snap(18, 0.80, 0.78)]
    assert T.replay_window(late_only, "yes", A["E3-early-C95-take"]) is None
    assert T.replay_window(late_only, "yes", A["E3-mid-C95-take"]) is not None
    assert T.replay_window(late_only, "yes", A["E3-late-C95-take"]) is not None
    assert T.replay_window(late_only, "yes", T.REF_ARM) is not None
    assert T.replay_window([snap(5, 0.80, 0.78), snap(3, 0.80, 0.78)], "yes", T.REF_ARM) is None   # after T-10 nobody trades

def test_first_qualifying_snapshot_wins():
    S = [snap(54, 0.97, 0.95, sigma1=1e-4), snap(52, 0.90, 0.88, sigma1=1e-4), snap(50, 0.70, 0.68, sigma1=1e-4), snap(48, 0.70, 0.68, sigma1=1e-4)]
    o = T.replay_window(S, "yes", A["E3-early-C95-take"])
    assert o["secs_left"] == 52 and o["price"] == pytest.approx(0.91)       # .97 skipped (cap), first eligible .90 -> fill .91

# ------------------------------------------------------------------------------------------------------------ replay: join
def test_join_fills_only_when_later_ask_crosses_strictly():
    posted = snap(50, 0.80, 0.78)
    assert T.replay_window([posted, snap(40, 0.80, 0.78)], "yes", A["E3-early-C95-join"]) is None            # never crossed
    assert T.replay_window([posted, snap(40, 0.78, 0.76)], "yes", A["E3-early-C95-join"]) is None            # equal price: queue not assumed
    o = T.replay_window([posted, snap(40, 0.77, 0.75)], "yes", A["E3-early-C95-join"])
    assert o and o["fill"] == "join" and o["price"] == pytest.approx(0.78) and o["fee_c"] == 0 and o["filled_secs_left"] == 40
    assert o["pnl_c"] == pytest.approx(22.0)
    # crossing may happen after the arm's decision window (order rests to the close)
    o = T.replay_window([posted, snap(15, 0.70, 0.68)], "no", A["E3-early-C95-join"])
    assert o and o["price"] == pytest.approx(0.78) and o["pnl_c"] == pytest.approx(-78.0) and o["filled_secs_left"] == 15

def test_join_no_side_and_cap():
    # d<0 -> NO; NO bid = 1 - yes_ask = 0.15; later NO ask (1 - yes_bid) must drop strictly below .15 -> yes_bid > .85
    S = [snap(50, 0.85, 0.80, d_bp=-5.0), snap(40, 0.90, 0.88, d_bp=-5.0)]
    o = T.replay_window(S, "no", A["E3-early-C95-join"])
    assert o and o["side"] == "no" and o["price"] == pytest.approx(0.15) and o["pnl_c"] == pytest.approx(85.0)
    assert T.replay_window([snap(50, 0.98, 0.96, sigma1=1e-4), snap(40, 0.95, 0.93, sigma1=1e-4)], "yes", A["E3-early-C95-join"]) is None   # bid .96 > cap .95
    assert T.replay_window([snap(50, 0.98, 0.96, sigma1=1e-4), snap(40, 0.95, 0.93, sigma1=1e-4)], "yes", A["E3-early-C97-join"]) is not None   # cap .97 + p_cal ~ .999: 3c edge at the bid

def test_floor_candidate_only_buys_market_favourite():
    S = [snap(50, 0.30, 0.28), snap(48, 0.30, 0.28)]           # our side YES priced .30 -> market longshot
    assert T.replay_window(S, "yes", A["E3-early-C95-take"]) is not None
    assert T.replay_window(S, "yes", A["E3-early-C95-take-F50"]) is None
    assert A["E3-early-C95-take-F50"] in T.CAND_ARMS and A["E3-early-C95-take-F50"] not in T.ARMS

def test_replay_all_covers_every_arm_and_void_result():
    S = [snap(50, 0.80, 0.78), snap(48, 0.80, 0.78)]
    out = T.replay_all(S, "yes", "KXBTC15M")
    assert set(out) == {a.id for a in T.ALL_ARMS} and len(T.ARMS) == 36
    assert all(v is None for v in T.replay_all(S, "", "KXBTC15M").values())

def test_normalise_applies_basis_once_for_p15_rows():
    raw = [snap(50, 0.80, 0.78, d_bp=0.0, src="replica")]
    assert T.normalise(raw, "KXBTC15M", basis_applied=True)[0]["d_bp"] == pytest.approx(0.0, abs=1e-9)
    assert T.normalise(raw, "KXBTC15M", basis_applied=False)[0]["d_bp"] == pytest.approx(0.44, abs=1e-6)
    assert T.normalise([snap(50, 0.80, 0.78, d_bp=0.0, src="cfb")], "KXBTC15M", basis_applied=False)[0]["d_bp"] == pytest.approx(0.0, abs=1e-9)

# ------------------------------------------------------------------------------------------------------------ posterior
def test_posterior_prior_and_update_math():
    p0 = T.posterior([])
    assert p0["n"] == 0 and p0["mean"] == 0.0 and p0["sd"] == pytest.approx(T.TAU0) and p0["p_pos"] == pytest.approx(0.5)
    xs = [10.0, -5.0, 20.0, 15.0]
    p = T.posterior(xs)
    n, m = 4, 10.0; ss = sum((x - m) ** 2 for x in xs)
    sigma = max(math.sqrt((ss + T.SIGMA_PSEUDO_N * T.SIGMA_PRIOR ** 2) / (n - 1 + T.SIGMA_PSEUDO_N)), T.SIGMA_FLOOR)
    prec = 1 / T.TAU0 ** 2 + n / sigma ** 2
    assert p["mean"] == pytest.approx((sum(xs) / sigma ** 2) / prec) and p["sd"] == pytest.approx(math.sqrt(1 / prec)) and p["raw_mean"] == 10.0
    assert 0 < p["mean"] < m                                        # shrunk toward the zero prior
    big = T.posterior([10.0] * 400 + [-10.0] * 100)
    assert abs(big["mean"] - 6.0) < 0.5 and big["sd"] < 1.5          # data dominates

def test_cluster_by_close_and_idempotent_record(tmp_path):
    st = T.load_state(tmp_path / "s.json")
    out1 = {"E3-early-C95-take": {"pnl_c": 20.0}, "E5-early-C95-take": None}
    out2 = {"E3-early-C95-take": {"pnl_c": -10.0}, "E5-early-C95-take": None}
    T.record_window(st, CLOSE, "KXBTC15M", "tk1", out1, log_path=tmp_path / "log.jsonl")
    T.record_window(st, CLOSE, "KXETH15M", "tk2", out2, log_path=tmp_path / "log.jsonl")
    T.record_window(st, CLOSE, "KXETH15M", "tk2", out2, log_path=tmp_path / "log.jsonl")      # re-replay same (arm, close, series): no double count
    assert T.cluster_obs(st, "E3-early-C95-take") == [5.0]                                        # one observation per close = mean over assets
    assert T.cluster_obs(st, "E5-early-C95-take") == []
    assert len((tmp_path / "log.jsonl").read_text().splitlines()) == 3
    T.save_state(st, tmp_path / "s.json"); st2 = T.load_state(tmp_path / "s.json")
    assert st2["obs"] == st["obs"]
    rows = T.posterior_summary(st2, draws=200)
    r = next(r for r in rows if r["arm"] == "E3-early-C95-take"); assert r["n"] == 1 and r["n_trades"] == 2

def test_update_state_file_locks_and_persists(tmp_path):
    path = tmp_path / "ts" / "levers_state.json"
    T.update_state_file(CLOSE, "KXBTC15M", "tk", {"E3-early-C95-take": {"pnl_c": 12.0}}, path=path)
    T.update_state_file(CLOSE + 900, "KXBTC15M", "tk2", {"E3-early-C95-take": {"pnl_c": -3.0}}, path=path)
    st = T.load_state(path); assert sorted(T.cluster_obs(st, "E3-early-C95-take")) == [-3.0, 12.0]
    assert (path.parent / "arm_pnl.jsonl").exists() and len((path.parent / "arm_pnl.jsonl").read_text().splitlines()) == 2

# ------------------------------------------------------------------------------------------------------------ sampling
def _rows(spec):
    rows = []
    for arm in T.ARMS + [T.REF_ARM]:
        mean, sd, n = spec.get(arm.id, (0.0, 8.0, 0))
        rows.append({"arm": arm.id, "sampled": arm in T.ARMS, "live_ok": arm.live_ok, "n": n, "n_trades": n, "raw_mean": mean, "se": None, "mean": mean, "sd": sd, "p_pos": T.Phi(mean / sd), "sigma": 30.0})
    return rows

def test_negative_arm_floor_never_zero_and_young_arm_capped():
    spec = {"E3-early-C95-take": (15.0, 1.0, 100), "E5-early-C95-take": (-20.0, 1.0, 60), "E7-early-C95-take": (14.0, 1.0, 5), T.REF_ARM.id: (0.0, 1.0, 100)}
    probs = T.selection_probs(_rows(spec), live_only=True)
    assert set(probs) == {a.id for a in T.ARMS if a.live_ok}            # join arms never selected live
    assert probs["E5-early-C95-take"] >= T.P_FLOOR * 0.9                 # floored (n >= N_FLOOR, mean < 0), renormalised
    assert probs["E3-early-C95-take"] > 0.5
    assert probs["E7-early-C95-take"] <= max(1 / len(probs), 1e-9) + 1e-9 or probs["E7-early-C95-take"] < probs["E3-early-C95-take"] / 5   # n < N_MIN: capped at ref/uniform
    assert abs(sum(probs.values()) - 1) < 1e-9

def test_sample_best_returns_live_arm_and_size_rule(tmp_path):
    st = T.load_state(tmp_path / "s.json")
    arm, info = T.sample_best(st, rng=random.Random(1))
    assert arm.live_ok and arm in T.ARMS and info["size_mult"] == 0.5         # no data: half size
    for i in range(40): T.record_window(st, CLOSE + 900 * i, "KXBTC15M", f"tk{i}", {"E3-early-C95-take": {"pnl_c": 25.0}}, log_path=None)
    picks = [T.sample_best(st, rng=random.Random(i)) for i in range(60)]
    good = [info for arm, info in picks if arm.id == "E3-early-C95-take"]
    assert len(good) > 30 and all(info["size_mult"] == 1.0 for info in good)       # probability matching: the only arm with data dominates
    assert all(info["size_mult"] == 0.5 for arm, info in picks if arm.id != "E3-early-C95-take")

def test_p_best_sums_to_one_over_grid():
    rows = _rows({"E3-early-C95-take": (40.0, 2.0, 50)})
    pb = T.p_best(rows, draws=500, seed=1)
    assert abs(sum(v for k, v in pb.items() if k != T.REF_ARM.id) - 1) < 1e-9 and pb["E3-early-C95-take"] > 0.9

def test_hour_masks_pacific():
    # 2026-10-27 20:00:00 UTC = 13:00 PDT -> US day; 2026-10-27 09:00 UTC = 02:00 PDT -> off
    assert T.mask_usday(1793131200) and not T.mask_usday(1793091600)
    assert T.hour_pt(1793131200) == 13
