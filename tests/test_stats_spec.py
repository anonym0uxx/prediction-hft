"""AUDIT_ADDENDUM2_2026-10-28 §4 — statistical spec primitives: ratio estimator with cluster-robust variance, cluster bootstrap,
Lan-DeMets O'Brien-Fleming boundaries (numerical integration), fixed-sample n and sequential power."""
import math, random
import pytest
from predhft import stats_spec as SS


def test_normal_helpers():
    assert SS.Phi(0) == pytest.approx(0.5) and SS.Phi(1.959964) == pytest.approx(0.975, abs=1e-6)
    for p in (0.001, 0.025, 0.3, 0.5, 0.8, 0.975, 0.999): assert SS.Phi(SS.PhiInv(p)) == pytest.approx(p, abs=1e-8)


def test_ratio_estimator_point_and_iid_vs_cluster_variance():
    pnl = [1.0, -1.0, 2.0, -2.0, 3.0, -3.0]; qty = [10, 10, 10, 10, 10, 10]
    r = SS.ratio_estimate(pnl, qty, clusters=list(range(6)))
    assert r["theta"] == pytest.approx(0.0) and r["n_clusters"] == 6 and r["deff"] == pytest.approx(1.0, abs=1e-9)   # singleton clusters -> DEFF 1
    # perfectly correlated pairs inside clusters inflate the cluster variance
    r2 = SS.ratio_estimate([1, 1, -1, -1, 2, 2, -2, -2], [5] * 8, clusters=[0, 0, 1, 1, 2, 2, 3, 3])
    assert r2["deff"] > 1.5 and r2["n_effective_fills"] < 8 and r2["icc_influence"] > 0.9


def test_ratio_estimator_is_total_over_total_not_mean_of_ratios():
    r = SS.ratio_estimate([10.0, -1.0], [100, 1], clusters=[0, 1])
    assert r["theta"] == pytest.approx(9.0 / 101)


def test_cluster_bootstrap_preserves_clusters_and_returns_interval():
    rng = random.Random(1); recs = [{"c": i // 4, "x": rng.gauss(0, 1) + (i // 4) * 0.5} for i in range(40)]
    b = SS.cluster_bootstrap(recs, lambda S: sum(r["x"] for r in S) / len(S), lambda r: r["c"], B=300)
    assert b["n_clusters"] == 10 and b["lo95"] <= b["point"] <= b["hi95"] and b["se"] > 0
    one = SS.cluster_bootstrap(recs[:4], lambda S: 1.0, lambda r: r["c"], B=10)
    assert one["se"] is None and one["n_clusters"] == 1                                             # one cluster: no inferential SE


def test_lan_demets_obf_matches_textbook_two_equal_looks():
    b = SS.lan_demets_bounds([0.5, 1.0], alpha=0.025)
    assert b[0]["z_boundary"] == pytest.approx(2.963, abs=0.01) and b[1]["z_boundary"] == pytest.approx(1.969, abs=0.01)
    assert b[-1]["cum_alpha_spent"] == pytest.approx(0.025)


def test_lan_demets_one_third_look_is_much_stricter_than_2_80():
    b = SS.lan_demets_bounds([1 / 3, 1.0])
    assert b[0]["z_boundary"] > 3.5 and b[1]["z_boundary"] == pytest.approx(1.96, abs=0.01)


def test_spending_function_monotone_and_total_alpha():
    xs = [SS.obf_spending(t) for t in (0.1, 0.3, 0.5, 0.8, 1.0)]
    assert all(a <= b for a, b in zip(xs, xs[1:])) and xs[-1] == pytest.approx(0.025) and xs[0] < 1e-6


def test_fixed_sample_n_and_power_consistency():
    n = SS.fixed_sample_n(0.02, 0.37, alpha=0.025, power=0.8)
    assert n == pytest.approx(2686, rel=0.01)
    b = SS.lan_demets_bounds([0.5, 1.0]); drift = 0.02 * math.sqrt(n * 1.02) / 0.37
    assert 0.74 <= SS.gsd_power(b, drift, sims=40000) <= 0.86                                       # ~80 % with the small OBF inflation
    assert SS.gsd_power(b, 0.0, sims=40000) == pytest.approx(0.025, abs=0.006)                     # type-I error preserved
