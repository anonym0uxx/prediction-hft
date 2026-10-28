"""Model zoo for the KX*15M algorithm tournament (docs/TOURNAMENT_2026-10-28.md). Pure CPU, no I/O above the forward hook.

Every parametric model maps into the live policy's settlement-average formula (sol_v1.p_settle_ge) by supplying its own
(drift, remaining variance); ML models output p directly. All models share one interface:

    m.fit(ctx)              ctx: FitContext (per-asset return grids, fold boundary, training points with outcomes)
    m.predict(points, ctx)  -> np.ndarray of P(YES) for the given decision points (same fold)

Nothing here touches sol_v1.py; the policy's pure functions are imported and reused as-is.
"""
from __future__ import annotations
import bisect, collections, dataclasses, math
import numpy as np
from . import sol_v1 as S

WINDOW_S = S.WINDOW_S
EPS = 1e-4
ZONES = {"final": (0.0, 120.0), "mid": (120.0, 600.0), "early": (600.0, 1e9)}
WARM_MIN = 3000          # causal filter warm-up (minutes) before a fold's first index
KFIT_DAYS = 10           # kappa for the `kf` variants is fitted on the most recent 10 training days before the fold


def zone_of(secs_left: float) -> str:
    for z, (a, b) in ZONES.items():
        if a < secs_left <= b or (z == "final" and secs_left <= a): return z
    return "early"


def clip_p(p):
    return np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)


def logit(p):
    p = clip_p(p); return np.log(p / (1 - p))


def expit(x):
    return 1.0 / (1.0 + np.exp(-np.asarray(x, dtype=float)))


# ------------------------------------------------------------------------------------------------------------- data model
@dataclasses.dataclass
class Grid:
    """One asset's 1-min grid: ts[i] = candle OPEN time (closed at ts[i]+60); x = close; r = log return vs previous minute (0 where
    a minute is missing and forward-filled)."""
    asset: str
    ts: np.ndarray
    x: np.ndarray
    r: np.ndarray

    def idx_last_closed(self, t: float) -> int:
        """Index of the last candle that CLOSED at or before t (open ts <= t-60). -1 if none."""
        return int(np.searchsorted(self.ts, t - 60, side="right")) - 1


def make_grid(asset: str, ts: np.ndarray, close: np.ndarray) -> Grid:
    ts = np.asarray(ts, dtype=np.int64); close = np.asarray(close, dtype=float)
    full = np.arange(ts.min(), ts.max() + 60, 60, dtype=np.int64)
    pos = np.searchsorted(ts, full); pos = np.clip(pos, 0, len(ts) - 1)
    have = ts[pos] == full
    x = np.where(have, close[pos], np.nan)
    # forward fill
    idx = np.where(~np.isnan(x), np.arange(len(x)), 0); np.maximum.accumulate(idx, out=idx); x = x[idx]
    r = np.zeros(len(x)); r[1:] = np.log(x[1:] / x[:-1])
    return Grid(asset, full, x, r)


@dataclasses.dataclass
class Point:
    asset: str
    close_ts: int
    t: float
    secs_left: float
    K: float
    x_now: float
    n_obs: int
    partial: float | None
    m: int
    V: float
    idx: int                     # grid index of the last closed candle at t
    y: int | None
    avg_close: float | None
    outcome_src: str
    mid: float | None = None
    spread: float | None = None
    sigma_feed: float | None = None    # tape B: policy's realised 1-s vol
    book: dict | None = None
    tape: str = "A"
    fold: int = -1
    ref: dict = dataclasses.field(default_factory=dict)   # cross-asset returns {asset: (ret1_bp, ret5_bp, partial_bp)}

    @property
    def hours_left(self): return self.secs_left / 3600.0
    @property
    def H(self) -> int:           # remaining whole minutes of price evolution to the last window second
        return max(1, int(math.ceil(self.secs_left / 60.0)))


def V_from_leads(leads) -> float:
    leads = sorted(leads); m = len(leads)
    if m == 0: return 0.0
    tot = sum(l * (2 * (m - j) + 1) for j, l in enumerate(leads, start=1)); return tot / (m * m)


def candle_terms(t: float, close_ts: int) -> tuple[int, float]:
    """(m, V) for a decision at t before the settlement window with the latest price at t (no window tick observed yet)."""
    a = max(0.0, (close_ts - WINDOW_S) - t)
    return WINDOW_S, V_from_leads([a + j for j in range(WINDOW_S)])


def p_formula(pt: Point, sigma_1s: float, kappa: float = 1.0, drift_log: float = 0.0, strike_dp: int | None = None) -> float:
    """The policy's probability with an injected per-second sigma and an expected log drift of the remaining samples."""
    x = pt.x_now * math.exp(drift_log)
    terms = {"n": pt.n_obs, "partial": pt.partial, "m": pt.m, "x_now": x, "V": pt.V}
    dp = strike_dp if strike_dp is not None else (4 if pt.asset == "SOL" else 2)
    pr = S.p_settle_ge(terms, pt.K, max(sigma_1s, S.CFG["vol_floor_1s"]), kappa, dp)
    return float(pr["p"]) if pr["p"] is not None else 0.5


def z_resid(pt: Point, sigma_1s: float, drift_log: float = 0.0) -> float | None:
    """Standardised settlement residual (avg_close - mu)/sd_mean under the injected sigma (kappa = 1)."""
    if pt.avg_close is None or pt.m == 0: return None
    x = pt.x_now * math.exp(drift_log)
    terms = {"n": pt.n_obs, "partial": pt.partial, "m": pt.m, "x_now": x, "V": pt.V}
    pr = S.p_settle_ge(terms, pt.K, max(sigma_1s, S.CFG["vol_floor_1s"]), 1.0)
    if pr["sd_mean"] is None or pr["sd_mean"] <= 0: return None
    return (pt.avg_close - pr["mu"]) / pr["sd_mean"]


# ------------------------------------------------------------------------------------------------------------- features
def rv(grid: Grid, idx: np.ndarray, n: int) -> np.ndarray:
    """RMS of the last n 1-min log returns ending at idx (inclusive); NaN if not enough history."""
    r2 = np.concatenate([[0.0], np.cumsum(grid.r ** 2)])
    idx = np.asarray(idx); lo = idx + 1 - n
    out = np.full(len(idx), np.nan); ok = lo >= 0
    out[ok] = np.sqrt((r2[idx[ok] + 1] - r2[lo[ok]]) / n)
    return out


def ret_bp(grid: Grid, idx: np.ndarray, n: int) -> np.ndarray:
    idx = np.asarray(idx); out = np.full(len(idx), np.nan); ok = idx - n >= 0
    out[ok] = 1e4 * (grid.x[idx[ok]] / grid.x[idx[ok] - n] - 1); return out


def sigma_rv15_1s(grid: Grid, pts: list[Point]) -> np.ndarray:
    idx = np.array([p.idx for p in pts]); s = rv(grid, idx, 15) / math.sqrt(60.0)
    return np.where(np.isnan(s), S.CFG["sigma_fallback_1s"], np.maximum(s, S.CFG["vol_floor_1s"]))


FEATURES = ["z_base", "partial_move_bp", "secs_left", "rv1_bp", "rv5_bp", "rv15_bp", "rv_ratio", "ref1_ret1", "ref1_ret5", "ref1_partial",
            "ref2_ret1", "ref2_ret5", "hour_sin", "hour_cos"]


def feature_matrix(pts: list[Point], grids: dict[str, Grid]) -> np.ndarray:
    X = np.full((len(pts), len(FEATURES)), np.nan)
    by_asset = collections.defaultdict(list)
    for i, p in enumerate(pts): by_asset[p.asset].append(i)
    for a, ii in by_asset.items():
        g = grids[a]; sub = [pts[i] for i in ii]; idx = np.array([p.idx for p in sub])
        sig = sigma_rv15_1s(g, sub)
        rv1, rv5, rv15, rv60 = rv(g, idx, 1), rv(g, idx, 5), rv(g, idx, 15), rv(g, idx, 60)
        for j, (i, p) in enumerate(zip(ii, sub)):
            # z_base: standardised distance of the martingale forecast to the strike under rv15 sigma (kappa 1)
            terms = {"n": p.n_obs, "partial": p.partial, "m": p.m, "x_now": p.x_now, "V": p.V}
            pr = S.p_settle_ge(terms, p.K, sig[j], 1.0)
            z = pr["z"] if pr["z"] is not None else 0.0
            z = max(min(z, 8.0), -8.0) if math.isfinite(z) else (8.0 if z > 0 else -8.0)
            refs = sorted(k for k in p.ref if k != p.asset)
            r1 = p.ref.get(refs[0], (np.nan, np.nan, np.nan)) if refs else (np.nan, np.nan, np.nan)
            r2 = p.ref.get(refs[1], (np.nan, np.nan, np.nan)) if len(refs) > 1 else (np.nan, np.nan, np.nan)
            hr = (p.t % 86400) / 86400 * 2 * math.pi
            X[i] = [z, 1e4 * (p.x_now / p.K - 1), p.secs_left, 1e4 * rv1[j], 1e4 * rv5[j], 1e4 * rv15[j],
                    (rv5[j] / rv60[j]) if (rv60[j] and rv60[j] > 0) else np.nan, r1[0], r1[1], r1[2], r2[0], r2[1], math.sin(hr), math.cos(hr)]
    return X


# ------------------------------------------------------------------------------------------------------------- contexts
@dataclasses.dataclass
class FitContext:
    grids: dict[str, Grid]
    start_ts: float                       # fold start: nothing at/after this instant may be used for fitting
    end_ts: float                         # last instant predicted in this fold (bounds causal filters)
    train_points: list[Point]             # tape-A points with outcomes, all strictly before start_ts
    oos_history: dict[str, np.ndarray] = dataclasses.field(default_factory=dict)  # earlier folds' OOS predictions (stacker)
    oos_points: list[Point] = dataclasses.field(default_factory=list)

    def i0(self, asset: str) -> int:
        """First grid index whose candle closes at/after start_ts (returns at index >= i0 are NOT training data)."""
        return int(np.searchsorted(self.grids[asset].ts, self.start_ts - 60, side="left"))

    def i1(self, asset: str) -> int:
        return min(len(self.grids[asset].ts) - 1, int(np.searchsorted(self.grids[asset].ts, self.end_ts, side="right")))

    def train_returns(self, asset: str) -> np.ndarray:
        return self.grids[asset].r[: self.i0(asset)]

    def kfit_points(self, asset: str) -> list[Point]:
        lo = self.start_ts - KFIT_DAYS * 86400
        return [p for p in self.train_points if p.asset == asset and p.t >= lo]

    def filter_range(self, asset: str) -> tuple[int, int]:
        lo = int(np.searchsorted(self.grids[asset].ts, self.start_ts - KFIT_DAYS * 86400 - 60, side="left"))
        return max(0, lo - WARM_MIN), self.i1(asset)


# ------------------------------------------------------------------------------------------------------------- base classes
class Model:
    name: str = "base"; family: str = "base"; cfg: dict = {}

    def fit(self, ctx: FitContext): return self
    def predict(self, pts: list[Point], ctx: FitContext) -> np.ndarray: raise NotImplementedError
    def extend(self, grids: dict[str, Grid]): return self      # forward hook: refresh causal state on new candles (no refit)


_FIT_CACHE: dict = {}        # (model base name, asset, fold start, fold end) -> (params, lo, filter state); k1/kf variants share fits
_FEAT_CACHE: dict = {}       # id(points list) -> feature matrix (lists are kept alive by the driver for the fold)


def features_cached(pts: list[Point], grids: dict[str, Grid]) -> np.ndarray:
    key = (id(pts), len(pts))
    if key not in _FEAT_CACHE:
        if len(_FEAT_CACHE) > 8: _FEAT_CACHE.clear()
        _FEAT_CACHE[key] = feature_matrix(pts, grids)
    return _FEAT_CACHE[key]


class VarianceModel(Model):
    """Supplies a per-minute variance forecast summed over the horizon; p from the policy formula. kappa: 1 or fitted."""
    family = "variance"

    def __init__(self, name: str, kappa_mode: str = "k1", **cfg):
        self.name = f"{name}_{kappa_mode}"; self.kappa_mode = kappa_mode; self.cfg = dict(cfg); self.kappa = {"final": 1.0, "mid": 1.0, "early": 1.0}
        self.params: dict = {}; self.state: dict = {}

    # subclass API ------------------------------------------------------------------------------------------------
    def fit_params(self, asset: str, r_train: np.ndarray, ctx: FitContext) -> dict: return {}
    def filter(self, asset: str, grid: Grid, lo: int, hi: int, params: dict) -> dict: return {}
    def var_sum(self, asset: str, pts: list[Point], params: dict, state: dict, lo: int) -> tuple[np.ndarray, np.ndarray]:
        """(sum of per-minute variance over H minutes, cumulative drift in log units) per point."""
        raise NotImplementedError

    # shared ------------------------------------------------------------------------------------------------------
    def fit(self, ctx: FitContext):
        self.params, self.state, self.lo = {}, {}, {}; self.lo_ts = {}
        base = self.name.rsplit("_", 1)[0]
        for a, g in ctx.grids.items():
            key = (base, a, ctx.start_ts, ctx.end_ts)
            if key not in _FIT_CACHE:
                params = self.fit_params(a, ctx.train_returns(a), ctx); lo, hi = ctx.filter_range(a)
                _FIT_CACHE[key] = (params, lo, self.filter(a, g, lo, hi, params))
                if len(_FIT_CACHE) > 400: _FIT_CACHE.pop(next(iter(_FIT_CACHE)))
            self.params[a], self.lo[a], self.state[a] = _FIT_CACHE[key]; self.lo_ts[a] = int(g.ts[self.lo[a]])
        if self.kappa_mode == "kf":
            zs = collections.defaultdict(list)
            for a in ctx.grids:
                kp = ctx.kfit_points(a)
                if not kp: continue
                sig, drift = self._sigma_drift(a, kp)
                for p, s, d in zip(kp, sig, drift):
                    z = z_resid(p, s, d)
                    if z is not None and math.isfinite(z): zs[zone_of(p.secs_left)].append(z)
            for zn in self.kappa:
                v = zs.get(zn)
                self.kappa[zn] = float(np.clip(np.sqrt(np.mean(np.square(v))), 0.5, 3.0)) if v and len(v) >= 50 else 1.0
        return self

    def extend(self, grids: dict[str, Grid]):
        """Forward hook: re-run the causal filter over the refreshed grid with the FITTED params/kappa (no refit, no new training
        data). Filters start at the same wall-clock minute as at fit time so the state is identical on the overlap."""
        for a, g in grids.items():
            if a not in self.params: continue
            lo = int(np.searchsorted(g.ts, self.lo_ts.get(a, int(g.ts[0])), side="left")); hi = len(g.ts) - 1
            self.lo[a] = lo; self.state[a] = self.filter(a, g, lo, hi, self.params[a])
        return self

    def _sigma_drift(self, asset: str, pts: list[Point]) -> tuple[np.ndarray, np.ndarray]:
        vs, drift = self.var_sum(asset, pts, self.params[asset], self.state[asset], self.lo[asset])
        H = np.array([p.H for p in pts], dtype=float)
        sig = np.sqrt(np.maximum(vs, 1e-14) / (60.0 * H))
        return sig, drift

    def predict(self, pts: list[Point], ctx: FitContext) -> np.ndarray:
        out = np.empty(len(pts)); by = collections.defaultdict(list)
        for i, p in enumerate(pts): by[p.asset].append(i)
        for a, ii in by.items():
            sub = [pts[i] for i in ii]; sig, drift = self._sigma_drift(a, sub)
            for i, p, s, d in zip(ii, sub, sig, drift): out[i] = p_formula(p, s, self.kappa[zone_of(p.secs_left)], d)
        return out


def _horizon_steps(pts: list[Point]) -> np.ndarray:
    return np.array([p.H for p in pts], dtype=int)


# ------------------------------------------------------------------------------------------------------------- baselines
class MidModel(Model):
    name = "mid"; family = "baseline"
    def predict(self, pts, ctx): return np.array([p.mid if p.mid is not None else np.nan for p in pts])


class SolV1Candle(Model):
    """B1: policy formula with sigma = rv15 (1-min) / sqrt(60), policy kappa, no drift."""
    name = "solv1_candle"; family = "baseline"
    def predict(self, pts, ctx):
        out = np.empty(len(pts)); by = collections.defaultdict(list)
        for i, p in enumerate(pts): by[p.asset].append(i)
        for a, ii in by.items():
            sub = [pts[i] for i in ii]; sig = sigma_rv15_1s(ctx.grids[a], sub)
            for i, p, s in zip(ii, sub, sig): out[i] = p_formula(p, s, S.kappa_for(p.secs_left))
        return out


class SolV1Exact(Model):
    """B2: the live policy's p_model (official-feed realised 1-s vol, policy kappa). NaN where the feed sigma is absent (tape A)."""
    name = "solv1_exact"; family = "baseline"
    def predict(self, pts, ctx):
        return np.array([p_formula(p, p.sigma_feed, S.kappa_for(p.secs_left)) if p.sigma_feed is not None else np.nan for p in pts])


class PartialLogit(Model):
    name = "partial_logit"; family = "baseline"
    def _X(self, pts, ctx):
        X = features_cached(pts, ctx.grids)
        pm = X[:, 1]; rv15 = X[:, 5]; mins = X[:, 2] / 60.0
        z = pm / np.maximum(rv15, 0.1) / np.sqrt(np.maximum(mins, 0.5))
        return np.column_stack([pm, z, mins])
    def fit(self, ctx):
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.impute import SimpleImputer
        from sklearn.preprocessing import StandardScaler
        pts = ctx.train_points
        self.pipe = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=1.0, max_iter=500))
        self.pipe.fit(self._X(pts, ctx), [p.y for p in pts]); return self
    def predict(self, pts, ctx): return self.pipe.predict_proba(self._X(pts, ctx))[:, 1]


# ------------------------------------------------------------------------------------------------------------- V1/V2: RV, EWMA
class RVModel(VarianceModel):
    def __init__(self, n: int, kappa_mode="k1"): super().__init__(f"rv_{n}", kappa_mode, n=n)
    def var_sum(self, asset, pts, params, state, lo):
        g_idx = np.array([p.idx for p in pts]); v = rv(GRIDS[asset], g_idx, self.cfg["n"]) ** 2
        v = np.where(np.isnan(v), S.CFG["sigma_fallback_1s"] ** 2 * 60, v)
        return v * _horizon_steps(pts), np.zeros(len(pts))


class EWMAModel(VarianceModel):
    def __init__(self, lam: float, kappa_mode="k1"): super().__init__(f"ewma_{lam}", kappa_mode, lam=lam)
    def filter(self, asset, grid, lo, hi, params):
        lam = self.cfg["lam"]; r2 = grid.r[lo:hi + 1] ** 2; v = np.empty(len(r2)); v0 = float(np.mean(r2[:60])) if len(r2) >= 60 else float(np.mean(r2))
        cur = v0
        for i, x in enumerate(r2): cur = lam * cur + (1 - lam) * x; v[i] = cur
        return {"v": v}
    def var_sum(self, asset, pts, params, state, lo):
        v = state["v"][np.array([p.idx for p in pts]) - lo]
        return v * _horizon_steps(pts), np.zeros(len(pts))


# ------------------------------------------------------------------------------------------------------------- V3: HAR
class HARModel(VarianceModel):
    def __init__(self, kappa_mode="k1"): super().__init__("har", kappa_mode)
    HS = (1, 2, 5, 10, 14)
    def fit_params(self, asset, r, ctx):
        n = len(r); r2 = r ** 2; c = np.concatenate([[0.0], np.cumsum(r2)])
        def rvn(k, i): return (c[i + 1] - c[i + 1 - k]) / k
        i = np.arange(400, n - 15, 3)
        f = np.column_stack([np.ones(len(i)), np.log(rvn(15, i) + 1e-12), np.log(rvn(60, i) + 1e-12), np.log(rvn(360, i) + 1e-12)])
        betas = {}
        for h in self.HS:
            y = np.log((c[i + 1 + h] - c[i + 1]) / h + 1e-12)
            b, *_ = np.linalg.lstsq(f, y, rcond=None); resid = y - f @ b
            betas[h] = (b, float(np.mean(np.exp(resid))))     # smearing bias correction
        return {"betas": betas}
    def var_sum(self, asset, pts, params, state, lo):
        g = GRIDS[asset]; idx = np.array([p.idx for p in pts])
        f = np.column_stack([np.ones(len(idx)), np.log(rv(g, idx, 15) ** 2 + 1e-12), np.log(rv(g, idx, 60) ** 2 + 1e-12), np.log(rv(g, idx, 360) ** 2 + 1e-12)])
        f = np.where(np.isnan(f), np.log(S.CFG["sigma_fallback_1s"] ** 2 * 60), f)
        H = _horizon_steps(pts); out = np.empty(len(pts))
        for j, h in enumerate(H):
            hk = min(self.HS, key=lambda k: abs(k - h)); b, sm = params["betas"][hk]
            out[j] = math.exp(float(f[j] @ b)) * sm * h
        return out, np.zeros(len(pts))


# ------------------------------------------------------------------------------------------------------------- V4: GARCH / EGARCH
class GARCHModel(VarianceModel):
    def __init__(self, kind: str = "garch", kappa_mode="k1"): super().__init__(f"{kind}11", kappa_mode, kind=kind)
    SCALE = 1e4
    def fit_params(self, asset, r, ctx):
        from arch import arch_model
        import warnings
        x = r[-20000:] * self.SCALE
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if self.cfg["kind"] == "garch": am = arch_model(x, mean="Zero", vol="GARCH", p=1, q=1, dist="normal")
            else: am = arch_model(x, mean="Zero", vol="EGARCH", p=1, o=1, q=1, dist="normal")
            res = am.fit(disp="off", show_warning=False)
        p = {k: float(v) for k, v in res.params.items()}
        return p
    def filter(self, asset, grid, lo, hi, params):
        x = grid.r[lo:hi + 1] * self.SCALE; n = len(x); h = np.empty(n + 1)
        if self.cfg["kind"] == "garch":
            w, a, b = params["omega"], params["alpha[1]"], params["beta[1]"]
            h[0] = w / max(1 - a - b, 1e-3)
            for i in range(n): h[i + 1] = w + a * x[i] ** 2 + b * h[i]
        else:
            w, a, g, b = params["omega"], params["alpha[1]"], params.get("gamma[1]", 0.0), params["beta[1]"]
            lh = np.empty(n + 1); lh[0] = w / max(1 - b, 1e-3); c = math.sqrt(2 / math.pi)
            for i in range(n):
                e = x[i] / math.sqrt(math.exp(lh[i])); lh[i + 1] = w + a * (abs(e) - c) + g * e + b * lh[i]
            h = np.exp(lh)
        return {"h_next": h[1:]}          # h_next[i] = conditional variance of return i+1 given returns <= i (bp^2)
    def var_sum(self, asset, pts, params, state, lo):
        hn = state["h_next"][np.array([p.idx for p in pts]) - lo]; H = _horizon_steps(pts); out = np.empty(len(pts))
        if self.cfg["kind"] == "garch":
            w, a, b = params["omega"], params["alpha[1]"], params["beta[1]"]; per = min(a + b, 0.9999); lr = w / (1 - per)
            for j in range(len(pts)):
                k = np.arange(H[j]); out[j] = float(np.sum(lr + per ** k * (hn[j] - lr)))
        else:
            w, a, g, b = params["omega"], params["alpha[1]"], params.get("gamma[1]", 0.0), params["beta[1]"]; b = min(b, 0.9999)
            vlog = a * a * (1 - 2 / math.pi) + g * g
            for j in range(len(pts)):
                k = np.arange(H[j]); el = w * (1 - b ** k) / (1 - b) + b ** k * math.log(hn[j]); vl = vlog * (1 - b ** (2 * k)) / (1 - b * b)
                out[j] = float(np.sum(np.exp(el + 0.5 * vl)))
        return out / self.SCALE ** 2, np.zeros(len(pts))


# ------------------------------------------------------------------------------------------------------------- V5: Markov chain on vol regime
class MarkovVol(VarianceModel):
    def __init__(self, order: int, kappa_mode="k1"): super().__init__(f"mc_vol_{order}", kappa_mode, order=order)
    def _buckets(self, grid_r, thr):
        c = np.concatenate([[0.0], np.cumsum(grid_r ** 2)]); n = len(grid_r); i = np.arange(n)
        rv5 = np.sqrt(np.where(i >= 4, (c[i + 1] - np.maximum(c[i - 4], 0)) / 5, np.nan))
        return np.digitize(rv5, thr)       # 0,1,2 (nan -> 3 -> clipped below)
    def fit_params(self, asset, r, ctx):
        k = self.cfg["order"]; c = np.concatenate([[0.0], np.cumsum(r ** 2)]); i = np.arange(4, len(r)); rv5 = np.sqrt((c[i + 1] - c[i - 4]) / 5)
        thr = np.quantile(rv5, [1 / 3, 2 / 3]); b = np.minimum(self._buckets(r, thr), 2)
        er2 = np.array([float(np.mean(r[b == s] ** 2)) if np.any(b == s) else float(np.mean(r ** 2)) for s in range(3)])
        counts = np.ones((3,) * (k + 1))
        for t in range(k + 4, len(r)): counts[tuple(b[t - k:t])][b[t]] += 1
        P = counts / counts.sum(axis=-1, keepdims=True)
        # table[state tuple][H] = expected sum of r^2 over the next H minutes
        table = {}
        import itertools
        for st in itertools.product(range(3), repeat=k):
            dist = {st: 1.0}; acc = []; tot = 0.0
            for h in range(15):
                nd = collections.defaultdict(float); ev = 0.0
                for s, pr in dist.items():
                    for nxt in range(3):
                        q = pr * P[s][nxt]; ev += q * er2[nxt]; nd[s[1:] + (nxt,)] += q
                dist = nd; tot += ev; acc.append(tot)
            table[st] = np.array(acc)
        return {"thr": thr, "table": table}
    def var_sum(self, asset, pts, params, state, lo):
        g = GRIDS[asset]; k = self.cfg["order"]; b = np.minimum(self._buckets(g.r, params["thr"]), 2); out = np.empty(len(pts))
        for j, p in enumerate(pts):
            st = tuple(int(v) for v in b[p.idx - k + 1: p.idx + 1]); out[j] = params["table"][st][min(p.H, 15) - 1]
        return out, np.zeros(len(pts))


# ------------------------------------------------------------------------------------------------------------- V6: Gaussian HMM
class HMMModel(VarianceModel):
    SCALE = 1e4
    def __init__(self, n_states: int, kappa_mode="k1"): super().__init__(f"hmm_{n_states}", kappa_mode, n_states=n_states)
    def fit_params(self, asset, r, ctx):
        from hmmlearn.hmm import GaussianHMM
        import warnings
        x = (r[-30000:] * self.SCALE).reshape(-1, 1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hm = GaussianHMM(n_components=self.cfg["n_states"], covariance_type="diag", n_iter=60, random_state=0, tol=1e-3).fit(x)
        return {"A": hm.transmat_, "mu": hm.means_[:, 0], "var": hm.covars_[:, 0, 0] if hm.covars_.ndim == 3 else hm.covars_[:, 0], "pi": hm.startprob_}
    def filter(self, asset, grid, lo, hi, params):
        x = grid.r[lo:hi + 1] * self.SCALE; A, mu, var = params["A"], params["mu"], params["var"]; k = len(mu)
        post = np.empty((len(x), k)); a = np.full(k, 1.0 / k)
        for i, xi in enumerate(x):
            lik = np.exp(-0.5 * (xi - mu) ** 2 / var) / np.sqrt(var)
            a = (a @ A) * lik; s = a.sum(); a = a / s if s > 0 else np.full(k, 1.0 / k); post[i] = a
        return {"post": post}
    def var_sum(self, asset, pts, params, state, lo):
        A, mu, var = params["A"], params["mu"], params["var"]; er2 = var + mu ** 2
        post = state["post"][np.array([p.idx for p in pts]) - lo]; H = _horizon_steps(pts)
        vs = np.empty(len(pts)); dr = np.empty(len(pts))
        for j in range(len(pts)):
            d = post[j] @ A; tv = 0.0; td = 0.0
            for h in range(H[j]):
                w = 1.0 if h < H[j] - 1 else 0.5
                m_ = float(d @ mu); tv += float(d @ er2) - m_ ** 2; td += w * m_; d = d @ A
            vs[j] = tv; dr[j] = td
        return vs / self.SCALE ** 2, dr / self.SCALE


# ------------------------------------------------------------------------------------------------------------- V7: BOCPD
class BOCPDModel(VarianceModel):
    RMAX = 300
    def __init__(self, hazard: float = 1 / 120, kappa_mode="k1"): super().__init__("bocpd", kappa_mode, hazard=hazard)
    def fit_params(self, asset, r, ctx): return {"var0": float(np.var(r[-20000:]))}
    def filter(self, asset, grid, lo, hi, params):
        x = grid.r[lo:hi + 1]; hz = self.cfg["hazard"]; R = self.RMAX; v0 = params["var0"]
        mu0, k0, a0 = 0.0, 1.0, 2.0; b0 = (a0 - 1) * v0
        mu = np.full(R, mu0); kap = np.full(R, k0); al = np.full(R, a0); be = np.full(R, b0)
        p = np.zeros(R); p[0] = 1.0; out = np.empty(len(x))
        for i, xi in enumerate(x):
            # predictive Student-t for each run length
            sc2 = be * (kap + 1) / (al * kap); nu = 2 * al
            lp = _lgamma_vec((nu + 1) / 2) - _lgamma_vec(nu / 2) - 0.5 * np.log(nu * math.pi * sc2) - (nu + 1) / 2 * np.log1p((xi - mu) ** 2 / (nu * sc2))
            pred = np.exp(lp)
            growth = p * pred * (1 - hz); cp = float(np.sum(p * pred * hz))
            newp = np.empty(R); newp[0] = cp; newp[1:] = growth[:-1]; newp[-1] += growth[-1]
            s = newp.sum(); p = newp / s if s > 0 else np.eye(R)[0]
            # posterior update (shift)
            nmu = np.empty(R); nk = np.empty(R); na = np.empty(R); nb = np.empty(R)
            nmu[1:] = (kap[:-1] * mu[:-1] + xi) / (kap[:-1] + 1); nk[1:] = kap[:-1] + 1; na[1:] = al[:-1] + 0.5
            nb[1:] = be[:-1] + kap[:-1] * (xi - mu[:-1]) ** 2 / (2 * (kap[:-1] + 1))
            nmu[0], nk[0], na[0], nb[0] = mu0, k0, a0, b0
            mu, kap, al, be = nmu, nk, na, nb
            out[i] = float(np.sum(p * be * (kap + 1) / (kap * np.maximum(al - 1, 0.5))))   # predictive variance mixture
        return {"v": out}
    def var_sum(self, asset, pts, params, state, lo):
        v = state["v"][np.array([p.idx for p in pts]) - lo]; return v * _horizon_steps(pts), np.zeros(len(pts))


def _lgamma_vec(x):
    from scipy.special import gammaln
    return gammaln(x)


# ------------------------------------------------------------------------------------------------------------- V8: CUSUM gate
class CUSUMGate(VarianceModel):
    def __init__(self, k: float = 0.5, h: float = 5.0, lookback: int = 10, kappa_mode="k1"): super().__init__("cusum", kappa_mode, k=k, h=h, lookback=lookback)
    def fit_params(self, asset, r, ctx): return {"var0": float(np.mean(r[-20000:] ** 2))}
    def filter(self, asset, grid, lo, hi, params):
        x = grid.r[lo:hi + 1] ** 2 / params["var0"] - 1; k, h = self.cfg["k"], self.cfg["h"]; sp = sm = 0.0; alarm = np.zeros(len(x), dtype=bool)
        for i, s in enumerate(x):
            sp = max(0.0, sp + s - k); sm = max(0.0, sm - s - k)
            if sp > h or sm > h: alarm[i] = True; sp = sm = 0.0
        return {"alarm": alarm}
    def var_sum(self, asset, pts, params, state, lo):
        g = GRIDS[asset]; idx = np.array([p.idx for p in pts]); al = state["alarm"]; lb = self.cfg["lookback"]
        recent = np.array([bool(al[max(0, i - lo - lb + 1): i - lo + 1].any()) for i in idx])
        v5, v60 = rv(g, idx, 5) ** 2, rv(g, idx, 60) ** 2
        v = np.where(recent, v5, v60); v = np.where(np.isnan(v), S.CFG["sigma_fallback_1s"] ** 2 * 60, v)
        return v * _horizon_steps(pts), np.zeros(len(pts))


# ------------------------------------------------------------------------------------------------------------- drift models
class DriftModel(Model):
    """Supplies an expected log drift of the remaining samples; sigma = rv15 candle baseline, policy kappa."""
    family = "drift"
    def drift(self, asset: str, pts: list[Point], ctx: FitContext) -> np.ndarray: raise NotImplementedError
    def fit(self, ctx): self.params = {a: self.fit_params(a, ctx.train_returns(a)) for a in ctx.grids}; return self
    def fit_params(self, asset, r): return {}
    def predict(self, pts, ctx):
        out = np.empty(len(pts)); by = collections.defaultdict(list)
        for i, p in enumerate(pts): by[p.asset].append(i)
        for a, ii in by.items():
            sub = [pts[i] for i in ii]; sig = sigma_rv15_1s(ctx.grids[a], sub); dr = self.drift(a, sub, ctx)
            for i, p, s, d in zip(ii, sub, sig, dr): out[i] = p_formula(p, s, S.kappa_for(p.secs_left), d)
        return out


class MarkovSign(DriftModel):
    def __init__(self, order: int): self.name = f"mc_sign_{order}"; self.cfg = {"order": order}
    def fit_params(self, asset, r):
        k = self.cfg["order"]; s = (r > 0).astype(int); counts = np.ones((2,) * (k + 1))
        for t in range(k, len(r)): counts[tuple(s[t - k:t])][s[t]] += 1
        P = counts / counts.sum(axis=-1, keepdims=True); m_up = float(np.mean(r[r > 0])); m_dn = float(np.mean(r[r <= 0]))
        import itertools
        table = {}
        for st in itertools.product(range(2), repeat=k):
            dist = {st: 1.0}; acc = []; tot = 0.0
            for h in range(15):
                nd = collections.defaultdict(float); ev = 0.0
                for s_, pr in dist.items():
                    for nxt, mv in ((1, m_up), (0, m_dn)):
                        q = pr * P[s_][nxt]; ev += q * mv; nd[s_[1:] + (nxt,)] += q
                dist = nd; tot += ev; acc.append(tot)
            table[st] = np.array(acc)
        return {"table": table}
    def drift(self, asset, pts, ctx):
        g = ctx.grids[asset]; k = self.cfg["order"]; s = (g.r > 0).astype(int); out = np.empty(len(pts))
        for j, p in enumerate(pts):
            st = tuple(int(v) for v in s[p.idx - k + 1: p.idx + 1]); tb = self.params[asset]["table"][st]; H = min(p.H, 15)
            out[j] = tb[H - 1] - 0.5 * (tb[H - 1] - (tb[H - 2] if H > 1 else 0.0))
        return out


class ARModel(DriftModel):
    def __init__(self, p: int): self.name = f"ar_{p}"; self.cfg = {"p": p}
    def fit_params(self, asset, r):
        p = self.cfg["p"]; X = np.column_stack([np.ones(len(r) - p)] + [r[p - j - 1: len(r) - j - 1] for j in range(p)]); y = r[p:]
        b, *_ = np.linalg.lstsq(X, y, rcond=None); return {"b": b}
    def drift(self, asset, pts, ctx):
        g = ctx.grids[asset]; p = self.cfg["p"]; b = self.params[asset]["b"]; out = np.empty(len(pts))
        for j, pt in enumerate(pts):
            lags = list(g.r[pt.idx - p + 1: pt.idx + 1][::-1]); tot = 0.0
            for h in range(pt.H):
                f = b[0] + float(np.dot(b[1:], lags[:p])); tot += f * (1.0 if h < pt.H - 1 else 0.5); lags = [f] + lags[:-1]
            out[j] = tot
        return out


class KalmanModel(DriftModel):
    def __init__(self, kind: str, q: float): self.name = f"kalman_{kind}_{q}" if kind == "ll" else f"kalman_{kind}"; self.cfg = {"kind": kind, "q": q}
    def drift(self, asset, pts, ctx):
        g = ctx.grids[asset]; q = self.cfg["q"]; out = np.empty(len(pts)); idx = np.array([p.idx for p in pts]); lo = max(0, int(idx.min()) - 600); hi = int(idx.max())
        y = np.log(g.x[lo:hi + 1])
        if self.cfg["kind"] == "ll":
            P = (-q + math.sqrt(q * q + 4 * q)) / 2; K = (P + q) / (P + q + 1); L = np.empty(len(y)); L[0] = y[0]
            for i in range(1, len(y)): L[i] = L[i - 1] + K * (y[i] - L[i - 1])
            for j, p in enumerate(pts): out[j] = L[p.idx - lo] - math.log(p.x_now)
        else:
            # local linear trend, state (level, slope), obs var 1, level var q, slope var q/100 (relative units)
            F = np.array([[1, 1], [0, 1]]); Q = np.diag([q, q / 100]); Hm = np.array([[1.0, 0.0]]); s = np.array([y[0], 0.0]); Pm = np.eye(2); slope = np.empty(len(y))
            for i in range(len(y)):
                s = F @ s; Pm = F @ Pm @ F.T + Q; innov = y[i] - s[0]; Sv = Pm[0, 0] + 1.0; Kg = Pm[:, 0] / Sv; s = s + Kg * innov; Pm = Pm - np.outer(Kg, Pm[0]); slope[i] = s[1]
            for j, p in enumerate(pts): out[j] = slope[p.idx - lo] * (p.H - 0.5)
        return out


# ------------------------------------------------------------------------------------------------------------- ML
class SKModel(Model):
    family = "ml"
    def __init__(self, name: str, make): self.name = name; self._make = make; self.cfg = {"estimator": name}
    def fit(self, ctx):
        pts = ctx.train_points; X = features_cached(pts, ctx.grids); y = np.array([p.y for p in pts])
        self.pipe = self._make(); self.pipe.fit(X, y); return self
    def predict(self, pts, ctx): return self.pipe.predict_proba(features_cached(pts, ctx.grids))[:, 1]


def _logit_pipe(C):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    return lambda: make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=C, max_iter=1000))


def _hgb_pipe(depth):
    from sklearn.ensemble import HistGradientBoostingClassifier
    return lambda: HistGradientBoostingClassifier(max_depth=depth, max_iter=150, learning_rate=0.05, min_samples_leaf=50, random_state=0)


def _rf_pipe():
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.impute import SimpleImputer
    return lambda: make_pipeline(SimpleImputer(strategy="median"), RandomForestClassifier(n_estimators=300, min_samples_leaf=50, max_features="sqrt", n_jobs=8, random_state=0))


# ------------------------------------------------------------------------------------------------------------- ensembles
ENS_MEMBERS = ["solv1_candle", "garch11_k1", "hmm_2_k1", "hgb_d3"]


class EnsAvg(Model):
    name = "ens_avg"; family = "ensemble"; cfg = {"members": ENS_MEMBERS}
    def predict_from(self, preds: dict[str, np.ndarray]) -> np.ndarray:
        return expit(np.mean([logit(preds[m]) for m in ENS_MEMBERS], axis=0))


class EnsStack(Model):
    name = "ens_stack"; family = "ensemble"; cfg = {"members": ENS_MEMBERS, "min_prior_folds": 2}
    def fit_from(self, hist: dict[str, np.ndarray], y: np.ndarray, n_prior_folds: int):
        from sklearn.linear_model import LogisticRegression
        self.ok = n_prior_folds >= self.cfg["min_prior_folds"] and all(m in hist for m in ENS_MEMBERS) and len(y) >= 200
        if self.ok:
            X = np.column_stack([logit(hist[m]) for m in ENS_MEMBERS]); self.lr = LogisticRegression(C=1.0, max_iter=500).fit(X, y)
        return self
    def predict_from(self, preds: dict[str, np.ndarray]) -> np.ndarray:
        if not getattr(self, "ok", False): return np.full(len(next(iter(preds.values()))), np.nan)
        return self.lr.predict_proba(np.column_stack([logit(preds[m]) for m in ENS_MEMBERS]))[:, 1]


# ------------------------------------------------------------------------------------------------------------- registry
GRIDS: dict[str, Grid] = {}      # set by the driver (set_grids) so RV-type models can index without carrying the context


def set_grids(grids: dict[str, Grid]):
    GRIDS.clear(); GRIDS.update(grids)


def build_zoo() -> list[Model]:
    """The ENTIRE pre-declared universe (docs/TOURNAMENT_2026-10-28.md §2). Order matters only for the ensembles (members first)."""
    zoo: list[Model] = [SolV1Candle(), SolV1Exact(), PartialLogit()]
    for km in ("k1", "kf"):
        zoo += [RVModel(15, km), RVModel(60, km), EWMAModel(0.94, km), EWMAModel(0.97, km), HARModel(km), GARCHModel("garch", km), GARCHModel("egarch", km),
                MarkovVol(1, km), MarkovVol(2, km), MarkovVol(3, km), HMMModel(2, km), HMMModel(3, km), BOCPDModel(kappa_mode=km), CUSUMGate(kappa_mode=km)]
    zoo += [MarkovSign(1), MarkovSign(2), MarkovSign(3), ARModel(1), ARModel(3), ARModel(5), KalmanModel("ll", 0.1), KalmanModel("ll", 1.0), KalmanModel("lt", 0.1)]
    zoo += [SKModel("logit_C0.1", _logit_pipe(0.1)), SKModel("logit_C1", _logit_pipe(1.0)), SKModel("hgb_d2", _hgb_pipe(2)), SKModel("hgb_d3", _hgb_pipe(3)), SKModel("rf", _rf_pipe())]
    zoo += [EnsAvg(), EnsStack()]
    names = [m.name for m in zoo]; assert len(names) == len(set(names)), "duplicate model names"
    return zoo


def n_configurations(zoo: list[Model]) -> int:
    return len(zoo)


# ------------------------------------------------------------------------------------------------------------- scoring
def brier(p, y): return (clip_p(p) - np.asarray(y, dtype=float)) ** 2
def logloss(p, y):
    p = clip_p(p); y = np.asarray(y, dtype=float); return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def calibration_slope(p, y) -> float | None:
    """Slope of logit(y) on logit(p) by logistic regression (1 = calibrated). None if degenerate."""
    p = np.asarray(p, dtype=float); y = np.asarray(y, dtype=float); ok = np.isfinite(p)
    if ok.sum() < 50 or len(set(y[ok])) < 2: return None
    from sklearn.linear_model import LogisticRegression
    X = logit(p[ok]).reshape(-1, 1)
    try: return float(LogisticRegression(C=1e6, max_iter=1000).fit(X, y[ok]).coef_[0, 0])
    except Exception: return None


def cluster_se(vals: np.ndarray, clusters) -> float | None:
    vals = np.asarray(vals, dtype=float); ok = np.isfinite(vals); vals = vals[ok]; clusters = np.asarray(clusters)[ok]
    n = len(vals)
    if n < 2: return None
    m = vals.mean(); by = collections.defaultdict(float)
    for v, c in zip(vals, clusters): by[c] += v - m
    G = len(by)
    if G < 2: return None
    return float(math.sqrt(sum(s * s for s in by.values()) * G / (G - 1)) / n)


def per_close_mean(vals: np.ndarray, closes: np.ndarray) -> dict:
    out = collections.defaultdict(list)
    for v, c in zip(vals, closes):
        if np.isfinite(v): out[int(c)].append(v)
    return {c: float(np.mean(v)) for c, v in out.items()}


# ------------------------------------------------------------------------------------------------------------- SPA / Reality Check
def stationary_bootstrap_indices(n: int, B: int, mean_block: float, rng: np.random.Generator) -> np.ndarray:
    """Politis-Romano stationary bootstrap: B x n index matrix (circular)."""
    q = 1.0 / mean_block; out = np.empty((B, n), dtype=np.int64)
    starts = rng.integers(0, n, size=(B, n)); cont = rng.random((B, n)) >= q
    for b in range(B):
        idx = starts[b, 0]
        for t in range(n):
            if t > 0: idx = (idx + 1) % n if cont[b, t] else starts[b, t]
            out[b, t] = idx
    return out


def spa_test(D: np.ndarray, B: int = 4000, mean_block: float = 4.0, seed: int = 20261028, names: list[str] | None = None) -> dict:
    """Hansen (2005) SPA + White (2000) Reality Check for 'some strategy beats the benchmark'. D: n_closes x n_strategies
    matrix of loss differentials (benchmark loss - strategy loss; positive = strategy better), NaN where a strategy has no
    observation in that close (handled by nan-means; strategies with < 30 closes are dropped and listed)."""
    D = np.asarray(D, dtype=float); n, K = D.shape; names = names or [str(k) for k in range(K)]
    cnt = np.sum(np.isfinite(D), axis=0); keep = cnt >= 30
    dropped = [names[k] for k in range(K) if not keep[k]]; D = D[:, keep]; names = [nm for nm, k_ in zip(names, keep) if k_]; K = D.shape[1]
    if K == 0: return {"error": "no strategy with >= 30 closes", "dropped": dropped}
    rng = np.random.default_rng(seed); idx = stationary_bootstrap_indices(n, B, mean_block, rng)
    dbar = np.nanmean(D, axis=0)
    boot = np.empty((B, K))
    for b in range(B): boot[b] = np.nanmean(D[idx[b]], axis=0)
    omega = np.sqrt(n) * np.nanstd(boot, axis=0, ddof=1); omega = np.where(omega > 0, omega, np.inf)
    tstat = np.sqrt(n) * dbar / omega
    T = max(0.0, float(np.max(tstat)))
    thr = math.sqrt(2 * math.log(math.log(max(n, 3))))
    g_c = np.where(tstat <= -thr, dbar, 0.0)           # SPA consistent recentring
    g_l = np.minimum(dbar, 0.0)                          # lower
    g_u = np.zeros(K)                                    # upper (= Reality Check recentring at the sample mean)
    def pval(g, stud=True):
        sc = omega if stud else 1.0
        Tb = np.max(np.sqrt(n) * (boot - dbar + g) / sc, axis=1); Tb = np.maximum(Tb, 0.0)
        Tobs = T if stud else max(0.0, float(np.max(np.sqrt(n) * dbar)))
        return float(np.mean(Tb >= Tobs))
    best = int(np.argmax(tstat)); best_ns = int(np.argmax(dbar))
    raw_p = float(np.mean(np.sqrt(n) * (boot[:, best] - dbar[best]) / omega[best] >= tstat[best]))
    return {"n_closes": int(n), "n_strategies": int(K), "dropped_lt_30_closes": dropped, "B": B, "mean_block_closes": mean_block,
            "best": names[best], "best_mean_diff": float(dbar[best]), "best_t": float(tstat[best]), "best_raw_p_one_sided": raw_p,
            "p_rc_white": pval(g_u), "p_spa_consistent": pval(g_c), "p_spa_lower": pval(g_l), "p_spa_upper": pval(g_u),
            # non-studentised versions (White 2000 original form). At n ~ 100 closes the block-bootstrap omega is noisy and the
            # studentised max is anti-conservative under the null (~14 % size at 5 %, K = 40, measured in tests); the
            # non-studentised max keeps ~3 % size on iid null and ~8 % under AR(1) rho = 0.3. Report both; the calibrated one decides.
            "best_nonstud": names[best_ns], "p_rc_white_nonstud": pval(g_u, False), "p_spa_consistent_nonstud": pval(g_c, False), "p_spa_lower_nonstud": pval(g_l, False),
            "n_with_positive_mean": int(np.sum(dbar > 0)), "t_stats": {nm: float(t) for nm, t in zip(names, tstat)}}


# ------------------------------------------------------------------------------------------------------------- economic replay with an injected p
def decide_with_p(p_model: float, now: float, close_ts: int, K: float, series, cfb_recv_t, book: dict, book_recv_t, size_fn, cfg=S.CFG,
                  latency_s: float = 0.3, now_sec: int | None = None, blend: bool = True, pre: tuple | None = None) -> dict:
    """sol_v1.decide with the probability replaced by `p_model`; execution terms (z, sd_mean for the adverse haircut, sigma for
    the edge requirement) come from the policy's own decomposition so that ONLY the probability differs. blend=False -> w = 1.
    pre = (sigma_1s, terms) already computed by the policy's own functions for this (close, now) (pure cache; same values)."""
    secs_left = close_ts - now; st = S.staleness(now, cfb_recv_t, book_recv_t, cfg)
    if now_sec is None: now_sec = int(math.floor(now))
    if pre is None:
        sig, nret, sig_src = S.realized_sigma_1s(series, now_sec, cfg["vol_window_s"], cfg["vol_min_n"], cfg["vol_floor_1s"], cfg["sigma_fallback_1s"])
        terms = S.settlement_terms(series, close_ts, now_sec)
    else: sig, terms = pre
    kap = S.kappa_for(secs_left, cfg); pr = S.p_settle_ge(terms, K, sig, kap)
    yb = book.get("yes_bid"); ya = book.get("yes_ask"); mid = ((yb + ya) / 2) if (yb is not None and ya is not None) else None
    spread = (ya - yb) if mid is not None else None
    if p_model is None or not np.isfinite(p_model) or pr["p"] is None:
        return {"decision": "abstain", "reasons": ["no_p"], "p_yes": None, "mid": mid, "secs_left": secs_left}
    p_model = float(min(max(p_model, EPS), 1 - EPS))
    p_used, w_used = (S.p_used_from(p_model, mid, spread, secs_left, cfg) if blend else (p_model, 1.0))
    rec = {"t": now, "close_ts": close_ts, "secs_left": round(secs_left, 2), "K": K, "p_model": p_model, "p_yes": p_used, "blend_w": w_used, "mid": mid, "decision": "abstain",
           "reasons": list(st["reasons"]), "ev": None, "qty": 0}
    if not (cfg["entry_min_secs_left"] <= secs_left <= cfg["entry_max_secs_left"]): rec["reasons"].append("outside_entry_window")
    if rec["reasons"]: return rec
    evs = {s: S.entry_ev(p_used, s, book, cfg["lot"], pr["z"], pr["sd_mean"], terms["x_now"], sig, latency_s, cfg, kap) for s in ("yes", "no")}
    rec["ev"] = evs
    ok = [e for e in evs.values() if e["why"] == "ok" and e["px_dw"] is not None and cfg["min_px"] <= e["px_dw"] <= cfg["max_px"]]
    if not ok:
        rec["reasons"].append("no_positive_ev" if all(e["why"] in ("no_offer", "no_depth_within_limit") for e in evs.values()) else "edge_lt_req"); return rec
    e = max(ok, key=lambda e: e["ev_usd"])
    qty, why = size_fn(e["side"], e["p_side"], e["px_dw"], e["depth_within_limit"], e["fee_pc"], None); qty = int(min(qty, e["qty_obtainable"]))
    rec["size_reason"] = why
    if qty < cfg["min_lot"]: rec["reasons"].append("size_lt_min_lot"); return rec
    rec.update({"decision": "enter", "side": e["side"], "qty": qty, "limit": e["limit"], "px_dw": e["px_dw"], "ev_pc": e["ev_pc"], "p_side": e["p_side"]})
    return rec


def replay_policy(closes: dict[int, list[dict]], p_of: dict, results: dict[int, str], series, now_sec_at, cfg=S.CFG, blend: bool = True, bankroll: float = 250.0,
                  pre: dict | None = None) -> dict:
    """Trading replay of SOL-v1's execution path with injected probabilities. closes: close_ts -> time-ordered snapshots
    ({t, K, yes_bid, yes_ask, bids, asks, recv_t, src}); p_of[(close_ts, t)] -> p for that snapshot; results[close_ts] -> 'yes'/'no'.
    Mirrors scripts/sol_v1_replay.py §3-4 (same paper_fill / hold_or_exit / Kelly sizing / per-close portfolio accounting).
    pre: optional shared cache {(close_ts, t): (sigma_1s, terms)} filled lazily with the policy's own functions (same values)."""
    size_fn = S.kelly_size_fn(bankroll=bankroll, lot_cap=cfg["lot"], min_lot=cfg["min_lot"]); trades = []; evals = 0; enters = 0
    if pre is None: pre = {}
    for c in sorted(closes):
        if c not in results: continue
        rows = closes[c]; pos = None
        for i, r in enumerate(rows):
            ns, cfb_recv_t = now_sec_at(r["t"])
            if ns is None: continue
            p = p_of.get((c, r["t"]))
            if p is None: continue
            key = (c, r["t"])
            if key not in pre:
                sig, _, _ = S.realized_sigma_1s(series, ns, cfg["vol_window_s"], cfg["vol_min_n"], cfg["vol_floor_1s"], cfg["sigma_fallback_1s"])
                pre[key] = (sig, S.settlement_terms(series, c, ns))
            book = {"yes_bid": r["yes_bid"], "yes_ask": r["yes_ask"], "bids": r["bids"], "asks": r["asks"], "src": r["src"]}
            rec = decide_with_p(p, r["t"], c, r["K"], series, cfb_recv_t, book, r["recv_t"], size_fn, cfg, 0.3, ns, blend, pre[key])
            if pos is None:
                evals += 1
                if rec["decision"] != "enter": continue
                enters += 1
                f = S.paper_fill(rec["side"], rec["qty"], rec["limit"], book)
                if f["qty"] <= 0: continue
                pos = {"close_ts": c, "t": r["t"], "secs_left": rec["secs_left"], "side": rec["side"], "qty": f["qty"], "px": f["px"], "cost": f["cost"], "fee": f["fee"], "p_side": rec["p_side"], "p_model": rec["p_model"], "mid": rec["mid"], "ev_pc": rec["ev_pc"], "exit": None}
            else:
                if rec.get("p_yes") is None: continue
                h = S.hold_or_exit(rec["p_yes"], pos["side"], book, pos["qty"], cfg["exit_margin_c"])
                if h["decision"] == "exit":
                    pos["exit"] = {"t": r["t"], "proceeds": h["exit_proceeds"], "px": h["exit_px"]}; pos["pnl"] = h["exit_proceeds"] - pos["cost"] - pos["fee"]; trades.append(pos); pos = None
        if pos is not None:
            win = 1.0 if ((results[c] == "yes") == (pos["side"] == "yes")) else 0.0; pos["win"] = win; pos["pnl"] = win * pos["qty"] - pos["cost"] - pos["fee"]; trades.append(pos)
    cl = sorted(c for c in closes if c in results); pnl_by = collections.defaultdict(float)
    for t in trades: pnl_by[t["close_ts"]] += t["pnl"]
    rets = np.array([pnl_by.get(c, 0.0) / bankroll for c in cl]); eq = np.cumsum([pnl_by.get(c, 0.0) for c in cl]) if cl else np.array([0.0])
    dd = float(np.min(eq - np.maximum.accumulate(eq))) if len(eq) else 0.0
    q = sum(t["qty"] for t in trades); net = sum(t["pnl"] for t in trades)
    pc = [100 * t["pnl"] / t["qty"] for t in trades]
    return {"closes": len(cl), "evals": evals, "enter_signals": enters, "trades": len(trades), "contracts": int(q), "net_usd": round(net, 2), "fees_usd": round(sum(t["fee"] for t in trades), 2),
            "c_per_ct": (round(100 * net / q, 2) if q else None), "se_c_per_ct_close": (round(cluster_se(np.array(pc), [t["close_ts"] for t in trades]), 2) if len(trades) > 1 and cluster_se(np.array(pc), [t["close_ts"] for t in trades]) is not None else None),
            "hit": (round(float(np.mean([t["pnl"] > 0 for t in trades])), 3) if trades else None), "max_drawdown_usd": round(dd, 2),
            "sharpe_per_15min": (round(float(rets.mean() / rets.std()), 3) if len(rets) > 1 and rets.std() > 0 else None), "exits": sum(1 for t in trades if t["exit"]),
            "by_zone": {z: {"n": sum(1 for t in trades if zone_of(t["secs_left"]) == z), "net": round(sum(t["pnl"] for t in trades if zone_of(t["secs_left"]) == z), 2)} for z in ZONES}}


__all__ = ["Grid", "Point", "FitContext", "Model", "VarianceModel", "DriftModel", "build_zoo", "n_configurations", "set_grids", "make_grid", "candle_terms", "V_from_leads",
           "p_formula", "feature_matrix", "FEATURES", "brier", "logloss", "calibration_slope", "cluster_se", "per_close_mean", "spa_test", "decide_with_p", "replay_policy",
           "zone_of", "ZONES", "EnsAvg", "EnsStack", "ENS_MEMBERS", "MidModel"]
