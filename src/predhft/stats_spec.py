"""Statistical specification for the P12 forward confirmation (docs/AUDIT_ADDENDUM2_2026-10-28.md §4). Pure functions, no I/O, no scipy.

Estimand  theta = sum(net P&L) / sum(filled contracts) over the post-freeze P12 cohort  (ratio estimator; $/contract).
H0: theta <= 0.   A rejection establishes theta > 0, NOT theta >= 2 c; 2 c/contract is the minimum economically worthwhile
edge used only as the POWER-PLANNING alternative.

Variance: linearisation of the ratio with cluster-robust (Liang-Zeger) sums, clusters = settlement close (BTC/ETH/SOL share a
close) or hour block. Information I = 1 / Var(theta). Design effect DEFF = Var_cluster / Var_fill-iid; effective fills = n / DEFF.
Boundaries: Lan-DeMets alpha-spending with the O'Brien-Fleming-type function alpha(t) = 2 (1 - Phi(z_{1-alpha/2} / sqrt t)),
computed by recursive numerical integration of the joint normal law of the sequential z-statistics at the given information
fractions (Armitage-McPherson-Rowe), not by reusing textbook values.
"""
from __future__ import annotations
import collections, math, random

SQ2 = math.sqrt(2.0)


def Phi(x: float) -> float: return 0.5 * (1 + math.erf(x / SQ2))
def phi(x: float) -> float: return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def PhiInv(p: float) -> float:
    """Acklam rational approximation (|err| < 1.2e-9)."""
    p = min(max(p, 1e-12), 1 - 1e-12)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p)); return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - 0.02425:
        q = math.sqrt(-2 * math.log(1 - p)); return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5; r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ------------------------------------------------------------------------------------------------------------ ratio estimator
def ratio_estimate(pnl: list[float], qty: list[float], clusters: list) -> dict:
    """theta = sum pnl / sum qty with linearised cluster-robust variance. Returns $/contract quantities plus DEFF and ICC diagnostics.
    u_i = pnl_i - theta * qty_i (influence of fill i on the numerator of the ratio); Var(theta) = Var(sum u) / (sum qty)^2.
    Fill-iid variance uses sum u_i^2; cluster variance uses cluster sums with the G/(G-1) small-sample factor."""
    n = len(pnl); Q = float(sum(qty))
    if n == 0 or Q <= 0: return {"n_fills": n, "contracts": Q, "theta": None}
    theta = sum(pnl) / Q; u = [p - theta * q for p, q in zip(pnl, qty)]
    v_iid = sum(x * x for x in u) / (Q * Q) * (n / (n - 1) if n > 1 else float("nan"))
    by = collections.defaultdict(float); cnt = collections.Counter()
    for x, c in zip(u, clusters): by[c] += x; cnt[c] += 1
    G = len(by)
    v_cl = (sum(s * s for s in by.values()) * G / (G - 1)) / (Q * Q) if G > 1 else None
    deff = (v_cl / v_iid) if (v_cl is not None and v_iid > 0) else None
    # intra-cluster correlation of the per-contract influence (ANOVA estimator on fills with >1 per cluster, equal-weight approximation)
    icc = None
    if G > 1 and n > G:
        m = n / G; grand = sum(u) / n
        msb = sum(cnt[c] * ((by[c] / cnt[c]) - grand) ** 2 for c in by) / (G - 1)
        msw = sum((x - by[c] / cnt[c]) ** 2 for x, c in zip(u, clusters)) / (n - G)
        m0 = (n - sum(k * k for k in cnt.values()) / n) / (G - 1)
        icc = (msb - msw) / (msb + (m0 - 1) * msw) if (msb + (m0 - 1) * msw) > 0 else None
    return {"n_fills": n, "contracts": Q, "n_clusters": G, "theta": theta, "se_iid": math.sqrt(v_iid) if v_iid == v_iid else None,
            "se_cluster": (math.sqrt(v_cl) if v_cl is not None else None), "information": (1 / v_cl if v_cl else None), "deff": deff,
            "n_effective_fills": (n / deff if deff and deff > 0 else None), "icc_influence": icc, "mean_cluster_size": n / G,
            "z_h0_theta_le_0": (theta / math.sqrt(v_cl) if v_cl else None)}


def cluster_bootstrap(records: list[dict], stat, cluster_key, B: int = 2000, seed: int = 20261028) -> dict:
    """Resample whole clusters with replacement (dependence inside a cluster preserved); returns the point estimate, bootstrap SE and
    percentile 2.5/97.5 of `stat(records_subset)` (stat must return a float or None)."""
    rng = random.Random(seed); by = collections.defaultdict(list)
    for r in records: by[cluster_key(r)].append(r)
    keys = list(by); G = len(keys); point = stat(records)
    if G < 2 or point is None: return {"point": point, "se": None, "lo95": None, "hi95": None, "n_clusters": G, "B": 0}
    vals = []
    for _ in range(B):
        samp = []
        for _ in range(G): samp.extend(by[keys[rng.randrange(G)]])
        v = stat(samp)
        if v is not None: vals.append(v)
    vals.sort(); m = sum(vals) / len(vals); se = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else None
    return {"point": point, "se": se, "lo95": vals[int(0.025 * len(vals))], "hi95": vals[min(len(vals) - 1, int(0.975 * len(vals)))], "n_clusters": G, "B": len(vals)}


# ------------------------------------------------------------------------------------------------------------ group-sequential
def obf_spending(t: float, alpha: float = 0.025) -> float:
    """Lan-DeMets O'Brien-Fleming-type one-sided spending function alpha*(t) = 2(1 - Phi(z_{1-alpha/2}/sqrt t))."""
    if t <= 0: return 0.0
    if t >= 1: return alpha
    return 2 * (1 - Phi(PhiInv(1 - alpha / 2) / math.sqrt(t)))


def _crossing_prob(bounds: list[float], fracs: list[float], new_b: float, grid: int = 1200, lo: float = -8.0, hi: float = 8.0) -> float:
    """P(cross at the LAST look | no earlier crossing) under H0 for sequential z-statistics with information fractions `fracs`,
    boundaries `bounds` (earlier looks) and candidate `new_b` at the last look. Recursive numerical integration on a grid of the
    sub-density of Z_k (Armitage-McPherson-Rowe)."""
    h = (hi - lo) / grid; xs = [lo + (i + 0.5) * h for i in range(grid)]
    # sub-density after look 1 (Z_1 ~ N(0,1), survive iff Z_1 < b_1)
    dens = [phi(x) if x < bounds[0] else 0.0 for x in xs]
    for k in range(1, len(fracs)):
        t_prev, t_k = fracs[k - 1], fracs[k]; bk = bounds[k] if k < len(bounds) else new_b
        r = math.sqrt(t_prev / t_k); s = math.sqrt(1 - t_prev / t_k)      # Z_k = r Z_{k-1} + s * eps
        if k == len(fracs) - 1:
            # probability mass of Z_k >= bk
            return sum(d * h * (1 - Phi((bk - r * x) / s)) for d, x in zip(dens, xs))
        new = []
        for y in xs:
            if y >= bk: new.append(0.0); continue
            new.append(sum(d * h * phi((y - r * x) / s) / s for d, x in zip(dens, xs)))
        dens = new
    return 1 - Phi(new_b)


def lan_demets_bounds(fracs: list[float], alpha: float = 0.025, spending=obf_spending) -> list[dict]:
    """One-sided efficacy boundaries z_k at information fractions `fracs` such that the cumulative crossing probability under H0
    equals spending(t_k). Bisection on each boundary given the previous ones."""
    bounds: list[float] = []; out = []; spent_prev = 0.0
    for k, t in enumerate(fracs):
        target = spending(t, alpha) - spent_prev
        if k == 0:
            b = PhiInv(1 - target) if target > 0 else 8.0
        else:
            lo_b, hi_b = 0.5, 8.0
            for _ in range(60):
                mid = (lo_b + hi_b) / 2
                if _crossing_prob(bounds, fracs[:k + 1], mid) > target: lo_b = mid
                else: hi_b = mid
            b = (lo_b + hi_b) / 2
        bounds.append(b); spent_prev = spending(t, alpha)
        out.append({"look": k + 1, "info_fraction": t, "cum_alpha_spent": spent_prev, "z_boundary": b, "nominal_one_sided_p": 1 - Phi(b)})
    return out


def fixed_sample_n(delta: float, sd: float, alpha: float = 0.025, power: float = 0.8) -> float:
    """Independent observations needed for one-sided alpha and power against delta with sd per observation."""
    return ((PhiInv(1 - alpha) + PhiInv(power)) * sd / delta) ** 2


def gsd_power(bounds: list[dict], drift_at_full: float, sims: int = 200000, seed: int = 7) -> float:
    """Monte-Carlo power of the boundary set when the standardised drift at full information is `drift_at_full`
    (= delta * sqrt(I_max)); Brownian-motion increments between looks."""
    rng = random.Random(seed); hits = 0; fr = [b["info_fraction"] for b in bounds]; zb = [b["z_boundary"] for b in bounds]
    for _ in range(sims):
        w = 0.0; prev = 0.0
        for t, z in zip(fr, zb):
            w += rng.gauss(0.0, math.sqrt(t - prev)) + drift_at_full * (t - prev); prev = t
            if w / math.sqrt(t) >= z: hits += 1; break
    return hits / sims
