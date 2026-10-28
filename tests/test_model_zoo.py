"""Model-zoo tests (docs/TOURNAMENT_2026-10-28.md): fit/predict shapes and bounds, causal (no-leakage) features and filters,
Markov transition rows, the policy-formula bridge, cluster SE, and SPA/Reality-Check sanity on null data. CPU, synthetic, fast."""
from __future__ import annotations
import math, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import numpy as np
import pytest
from predhft import sol_v1 as S
from predhft import model_zoo as Z

pytest.importorskip("sklearn")
ASSETS = ("BTC", "ETH", "SOL")
T0 = 1_790_000_000 - (1_790_000_000 % 900)        # grid start on a 15-min boundary
N_MIN = 20 * 1440                                  # 20 synthetic days of 1-min candles


def _grids(seed=0, n=N_MIN, vol_1m=6e-4):
    rng = np.random.default_rng(seed); out = {}
    for k, a in enumerate(ASSETS):
        r = rng.standard_normal(n) * vol_1m * (1 + 0.5 * np.sin(np.arange(n) / 700))     # slow vol cycle
        x = 100.0 * (k + 1) * np.exp(np.cumsum(r)); ts = T0 + 60 * np.arange(n)
        out[a] = Z.make_grid(a, ts, x)
    return out


def _points(grids, dec_min=(14, 10, 5, 2, 1), start_idx=500):
    """Tape-A-like points: strike = close of the candle before the window open, label = close at the window close >= strike."""
    pts = []
    for a, g in grids.items():
        for o in range(int(g.ts[start_idx]) // 900 * 900 + 900, int(g.ts[-1]) - 900, 900):
            c = o + 900; io = int(np.searchsorted(g.ts, o)); ic = int(np.searchsorted(g.ts, c))
            if ic >= len(g.ts): break
            K = float(g.x[io - 1]); y = int(g.x[ic - 1] >= K)
            for dm in dec_min:
                t = c - dm * 60; idx = g.idx_last_closed(t); m, V = Z.candle_terms(t, c)
                ref = {b: (0.0, 0.0, 0.0) for b in grids if b != a}
                pts.append(Z.Point(a, c, float(t), float(c - t), K, float(g.x[idx]), 0, None, m, V, idx, y, float(g.x[ic - 1]), "proxy", ref=ref))
    pts.sort(key=lambda p: (p.t, p.asset)); return pts


@pytest.fixture(scope="module")
def world():
    grids = _grids(); Z.set_grids(grids); pts = _points(grids)
    split = float(grids["SOL"].ts[14 * 1440]); end = float(grids["SOL"].ts[-1]) + 60
    train = [p for p in pts if p.close_ts <= split]; test = [p for p in pts if split <= p.t < end]
    return {"grids": grids, "pts": pts, "ctx": Z.FitContext(grids, split, end, train), "train": train, "test": test, "split": split}


# ------------------------------------------------------------------ grid / candle terms
def test_make_grid_fills_gaps_and_idx_last_closed_is_causal():
    ts = T0 + 60 * np.array([0, 1, 2, 4, 5]); x = np.array([1.0, 2.0, 3.0, 5.0, 6.0])
    g = Z.make_grid("SOL", ts, x)
    assert len(g.ts) == 6 and g.x[3] == 3.0 and g.r[3] == 0.0                # missing minute forward-filled, zero return
    t = T0 + 60 * 4 + 30                                                      # 04:30 -> candle 3 (open 03:00) closed at 04:00; candle 4 is still open
    assert g.idx_last_closed(t) == 3 and g.ts[g.idx_last_closed(t)] + 60 <= t
    assert g.idx_last_closed(T0 + 59) == -1


def test_candle_terms_match_policy_decomposition_before_window():
    c = T0 + 900; t = c - 600                                                 # 10 min before close, no window tick observed
    m, V = Z.candle_terms(t, c)
    series = [(s, 100.0) for s in range(t - 1000, t + 1)]
    terms = S.settlement_terms(series, c, t)
    assert m == terms["m"] == 60 and abs(V - terms["V"]) < 1e-9


def test_p_formula_is_the_policy_probability():
    c = T0 + 900; t = c - 300; m, V = Z.candle_terms(t, c)
    pt = Z.Point("SOL", c, t, 300.0, 100.0, 100.2, 0, None, m, V, 10, None, None, "x")
    sig = 1e-4
    ref = S.p_settle_ge({"n": 0, "partial": None, "m": m, "x_now": 100.2, "V": V}, 100.0, sig, 1.15, 4)["p"]
    assert abs(Z.p_formula(pt, sig, 1.15) - ref) < 1e-12
    assert Z.p_formula(pt, sig, 1.15, drift_log=0.01) > Z.p_formula(pt, sig, 1.15) > Z.p_formula(pt, sig, 1.15, drift_log=-0.01)


# ------------------------------------------------------------------ zoo: shapes, bounds
def test_zoo_fit_predict_shapes_and_bounds(world):
    ctx, test = world["ctx"], world["test"]
    zoo = [m for m in Z.build_zoo() if m.family != "ensemble" and m.name != "solv1_exact"]
    assert Z.n_configurations(Z.build_zoo()) == 47          # 47 models + mid = 48 columns (doc §2 table sums to 48 incl. mid)
    sub = test[:600]
    for m in zoo:
        p = m.fit(ctx).predict(sub, ctx)
        assert isinstance(p, np.ndarray) and p.shape == (len(sub),), m.name
        assert np.all(np.isfinite(p)) and np.all(p >= 0) and np.all(p <= 1), m.name
    exact = Z.SolV1Exact().predict(sub, ctx); assert np.all(np.isnan(exact))      # no feed sigma on tape A -> NaN, never a guess


def test_ensembles_shapes_and_stack_gate():
    n = 300; rng = np.random.default_rng(1)
    preds = {nm: rng.uniform(0.05, 0.95, n) for nm in Z.ENS_MEMBERS}
    pa = Z.EnsAvg().predict_from(preds); assert pa.shape == (n,) and np.all((pa > 0) & (pa < 1))
    st = Z.EnsStack().fit_from(preds, (rng.uniform(size=n) < 0.5).astype(int), n_prior_folds=1)
    assert np.all(np.isnan(st.predict_from(preds)))                            # < min_prior_folds -> abstain (NaN)
    st = Z.EnsStack().fit_from(preds, (rng.uniform(size=n) < 0.5).astype(int), n_prior_folds=3)
    ps = st.predict_from(preds); assert ps.shape == (n,) and np.all((ps > 0) & (ps < 1))


# ------------------------------------------------------------------ no leakage
def test_features_at_decision_time_ignore_future_candles(world):
    """Perturb every candle that closes AFTER the decision time: features and all model probabilities must not change."""
    grids = world["grids"]; ctx = world["ctx"]; test = world["test"]
    pts = [p for p in test if p.asset == "SOL"][:120]
    X0 = Z.feature_matrix(pts, grids)
    zoo = [m for m in Z.build_zoo() if m.family != "ensemble" and m.name != "solv1_exact"]
    base = {m.name: m.fit(ctx).predict(pts, ctx).copy() for m in zoo}
    # earliest decision in the subset; mutate everything the grid has not closed by then
    t_min = min(p.t for p in pts); g = grids["SOL"]; cut = g.idx_last_closed(t_min) + 1
    pert = {a: gg for a, gg in grids.items()}
    x2 = g.x.copy(); rng = np.random.default_rng(7); x2[cut:] *= np.exp(rng.standard_normal(len(x2) - cut) * 0.01)
    r2 = np.zeros(len(x2)); r2[1:] = np.log(x2[1:] / x2[:-1]); pert["SOL"] = Z.Grid("SOL", g.ts, x2, r2)
    Z.set_grids(pert); Z._FIT_CACHE.clear(); Z._FEAT_CACHE.clear()
    try:
        early = [p for p in pts if p.t == t_min]
        X1 = Z.feature_matrix(early, pert)
        i0 = [i for i, p in enumerate(pts) if p.t == t_min]
        np.testing.assert_allclose(X1, X0[i0], equal_nan=True)
        ctx2 = Z.FitContext(pert, ctx.start_ts, ctx.end_ts, ctx.train_points)
        for m in zoo:
            p1 = m.fit(ctx2).predict(early, ctx2)
            np.testing.assert_allclose(p1, base[m.name][i0], rtol=1e-9, atol=1e-12, err_msg=m.name)
    finally:
        Z.set_grids(grids); Z._FIT_CACHE.clear(); Z._FEAT_CACHE.clear()


def test_fit_context_training_returns_end_before_fold_start(world):
    ctx = world["ctx"]; g = world["grids"]["SOL"]; i0 = ctx.i0("SOL")
    assert g.ts[i0 - 1] + 60 < ctx.start_ts <= g.ts[i0] + 60          # last training return closed strictly before the fold start
    assert all(p.close_ts <= ctx.start_ts for p in ctx.train_points)   # training labels are known before the fold starts
    assert len(ctx.train_returns("SOL")) == i0


def test_fit_params_do_not_see_fold_returns(world):
    """Fitting with the post-split returns replaced by garbage must give identical parameters (fit uses r[:i0] only)."""
    grids = world["grids"]; ctx = world["ctx"]; g = grids["SOL"]; i0 = ctx.i0("SOL")
    r_bad = g.r.copy(); r_bad[i0:] = 0.05
    pert = dict(grids); pert["SOL"] = Z.Grid("SOL", g.ts, g.x, r_bad); ctx2 = Z.FitContext(pert, ctx.start_ts, ctx.end_ts, ctx.train_points)
    for mk in (lambda: Z.GARCHModel("garch"), lambda: Z.MarkovVol(2), lambda: Z.HARModel(), lambda: Z.ARModel(3), lambda: Z.MarkovSign(2)):
        a = mk(); b = mk()
        pa = a.fit_params("SOL", ctx.train_returns("SOL"), ctx) if isinstance(a, Z.VarianceModel) else a.fit_params("SOL", ctx.train_returns("SOL"))
        pb = b.fit_params("SOL", ctx2.train_returns("SOL"), ctx2) if isinstance(b, Z.VarianceModel) else b.fit_params("SOL", ctx2.train_returns("SOL"))
        assert repr(pa) == repr(pb), type(a).__name__


# ------------------------------------------------------------------ Markov chains
def test_markov_transition_rows_sum_to_one():
    rng = np.random.default_rng(3); r = rng.standard_normal(5000) * 5e-4
    for k in (1, 2, 3):
        s = (r > 0).astype(int); counts = np.ones((2,) * (k + 1))
        for t in range(k, len(r)): counts[tuple(s[t - k:t])][s[t]] += 1
        P = counts / counts.sum(axis=-1, keepdims=True)
        np.testing.assert_allclose(P.sum(axis=-1), 1.0)
        ms = Z.MarkovSign(k); tb = ms.fit_params("SOL", r)["table"]
        assert len(tb) == 2 ** k and all(len(v) == 15 for v in tb.values())
        mv = Z.MarkovVol(k); pv = mv.fit_params("SOL", r, None)
        assert len(pv["table"]) == 3 ** k and all(np.all(np.diff(v) > 0) for v in pv["table"].values())   # cumulative variance increases with H
    # the vol chain's transition matrix: recompute from the model's own bucketing and check rows
    thr = pv["thr"]; b = np.minimum(mv._buckets(r, thr), 2); counts = np.ones((3,) * (k + 1))
    for t in range(k + 4, len(r)): counts[tuple(b[t - k:t])][b[t]] += 1
    np.testing.assert_allclose((counts / counts.sum(axis=-1, keepdims=True)).sum(axis=-1), 1.0)


def test_hmm_filter_posteriors_are_distributions(world):
    ctx = world["ctx"]; m = Z.HMMModel(2).fit(ctx)
    post = m.state["SOL"]["post"]; A = m.params["SOL"]["A"]
    np.testing.assert_allclose(post.sum(axis=1), 1.0, atol=1e-9); np.testing.assert_allclose(A.sum(axis=1), 1.0, atol=1e-9)


def test_variance_sum_scales_with_horizon(world):
    ctx = world["ctx"]; pts = [p for p in world["test"] if p.asset == "SOL"][:50]
    for m in (Z.RVModel(15), Z.EWMAModel(0.94), Z.GARCHModel("garch"), Z.HMMModel(2)):
        m.fit(ctx); vs, dr = m.var_sum("SOL", pts, m.params["SOL"], m.state["SOL"], m.lo["SOL"])
        assert vs.shape == (len(pts),) and np.all(vs > 0) and dr.shape == (len(pts),)
        H = np.array([p.H for p in pts]); per = vs / H
        assert np.all(per > 1e-10) and np.all(per < 1e-3)


# ------------------------------------------------------------------ scoring / SE
def test_cluster_se_matches_iid_formula_with_singleton_clusters():
    rng = np.random.default_rng(5); v = rng.standard_normal(400)
    se = Z.cluster_se(v, np.arange(400)); assert abs(se - v.std(ddof=1) / math.sqrt(400)) < 1e-9 * 10
    assert Z.cluster_se(v, np.zeros(400)) is None


def test_calibration_slope_near_one_for_calibrated_probabilities():
    rng = np.random.default_rng(6); p = rng.uniform(0.02, 0.98, 20000); y = (rng.uniform(size=20000) < p).astype(int)
    assert abs(Z.calibration_slope(p, y) - 1.0) < 0.05
    assert Z.calibration_slope(Z.expit(2 * Z.logit(p)), y) < 0.7                   # over-confident -> slope < 1


# ------------------------------------------------------------------ SPA / Reality Check
def test_stationary_bootstrap_indices_are_valid_and_blocky():
    rng = np.random.default_rng(0); idx = Z.stationary_bootstrap_indices(200, 50, 4.0, rng)
    assert idx.shape == (50, 200) and idx.min() >= 0 and idx.max() < 200
    cont = np.mean((idx[:, 1:] - idx[:, :-1]) % 200 == 1); assert 0.65 < cont < 0.85     # ~1 - 1/4 continuation rate


def test_spa_null_data_gives_large_p_and_rejects_rarely():
    """K strategies with zero true edge: SPA / RC p-values should be large; the best raw p is small (max of K t-stats)."""
    rng = np.random.default_rng(11); n, K = 120, 40
    D = rng.standard_normal((n, K)) * 0.01
    res = Z.spa_test(D, B=500, mean_block=4.0, names=[f"s{k}" for k in range(K)], seed=1)
    assert res["n_strategies"] == K and res["n_closes"] == n
    assert res["p_spa_lower"] <= res["p_spa_consistent"] <= res["p_spa_upper"] + 1e-12
    assert res["p_rc_white_nonstud"] > 0.2 and res["p_spa_consistent_nonstud"] > 0.2
    assert res["best_raw_p_one_sided"] < res["p_rc_white"]           # the naive best-of-K p is anti-conservative
    # size of the calibrated (non-studentised) test on iid null data: ~3 % at the 5 % level; studentised is looser (noisy omega)
    rej_ns = rej_st = 0
    for s in range(30):
        D = np.random.default_rng(100 + s).standard_normal((n, K)) * 0.01
        r = Z.spa_test(D, B=300, names=None, seed=s); rej_ns += r["p_spa_consistent_nonstud"] < 0.05; rej_st += r["p_spa_consistent"] < 0.05
    assert rej_ns <= 4
    assert rej_st >= rej_ns                                           # documents why the non-studentised p is the one that decides


def test_spa_detects_a_real_edge():
    rng = np.random.default_rng(12); n, K = 150, 30
    D = rng.standard_normal((n, K)) * 0.01; D[:, 7] += 0.004           # one strategy with mean 0.4 bp per close, t ~ 4.9
    res = Z.spa_test(D, B=500, names=[f"s{k}" for k in range(K)], seed=2)
    assert res["best"] == "s7" and res["best_nonstud"] == "s7"
    assert res["p_spa_consistent"] < 0.05 and res["p_rc_white"] < 0.05 and res["p_spa_consistent_nonstud"] < 0.05 and res["p_rc_white_nonstud"] < 0.05


def test_spa_handles_nan_columns_and_drops_sparse_strategies():
    rng = np.random.default_rng(13); D = rng.standard_normal((100, 3)) * 0.01; D[:80, 2] = np.nan; D[::2, 1] = np.nan
    res = Z.spa_test(D, B=100, names=["a", "b", "c"], seed=3)
    assert res["dropped_lt_30_closes"] == ["c"] and res["n_strategies"] == 2 and all(np.isfinite(v) for v in res["t_stats"].values())


# ------------------------------------------------------------------ economic replay path (injected p == policy p must reproduce the policy)
def test_decide_with_p_reproduces_policy_decision_when_p_is_the_policy_p():
    c = T0 + 900; now = c - 400.0; ns = int(now)
    series = [(s, 100.0 * (1 + 0.0003 * math.sin(s / 7.0))) for s in range(ns - 1000, ns + 1)]
    book = {"yes_bid": 0.40, "yes_ask": 0.42, "bids": [[0.40, 200], [0.39, 500]], "asks": [[0.42, 200], [0.43, 500]], "src": "test"}
    size_fn = S.kelly_size_fn()
    ref = S.decide(now, c, 100.0, series, now - 0.5, book, now - 0.2, size_fn, S.CFG, latency_s=0.3, now_sec=ns)
    got = Z.decide_with_p(ref["p_model"], now, c, 100.0, series, now - 0.5, book, now - 0.2, size_fn, S.CFG, 0.3, ns, True)
    assert got["decision"] == ref["decision"] and abs(got["p_yes"] - ref["p_yes"]) < 1e-12
    if ref["decision"] == "enter": assert got["side"] == ref["side"] and got["qty"] == ref["qty"] and abs(got["limit"] - ref["limit"]) < 1e-9
    # the cached-terms path is identical
    sig, _, _ = S.realized_sigma_1s(series, ns); terms = S.settlement_terms(series, c, ns)
    got2 = Z.decide_with_p(ref["p_model"], now, c, 100.0, series, now - 0.5, book, now - 0.2, size_fn, S.CFG, 0.3, ns, True, (sig, terms))
    assert got2["decision"] == got["decision"] and got2.get("ev") == got.get("ev")
