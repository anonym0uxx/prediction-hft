"""P17 — 1-minute candle store + chart-reader ("technical analysis") features for Kalshi KX*15M markets.

Pure offline/CPU; no LLM; no orders. Two public entry points so s7_live.py can later call them without
any change to the decision logic:

    fetch_1m(asset, start, end)      -> pandas DataFrame of 1-min candles (UTC), cached under data/candles/
    features_at(df, t, window_open)  -> dict of features computable strictly from candles that CLOSED at or before t

Candle source: Coinbase Exchange public REST (no key), max 300 candles / request, ~3 req/s politeness.
Columns: ts (int, unix s, candle OPEN time), open, high, low, close, volume.

Settlement proxy (documented in docs/P17_CANDLES_2026-10-27.md): the Kalshi 15M market settles on the
60-s average of the CF Benchmarks index in the minute before the close vs the minute before the open.
Offline we approximate each 60-s average with the OHLC4 of the 1-min candle that spans that minute
(candle opening at boundary-60 s).  OHLC4 is an approximation of the time-average; see the doc for the
measured error vs a tick-level average.
"""
from __future__ import annotations
import math, pathlib, time, datetime as dt
from typing import Iterable
import numpy as np
import pandas as pd

CB_BASE = "https://api.exchange.coinbase.com/products"
ROOT = pathlib.Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "candles"
PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
COLS = ["ts", "open", "high", "low", "close", "volume"]
_REQ_GAP_S = 0.34   # ~3 req/s


def _norm_asset(asset: str) -> str:
    a = asset.upper().replace("-USD", "")
    return a


def _product(asset: str) -> str:
    a = _norm_asset(asset)
    return PRODUCT.get(a, f"{a}-USD")


def cache_path(asset: str) -> pathlib.Path:
    return CACHE / f"{_norm_asset(asset)}_1m.parquet"


def _to_ts(x) -> int:
    if isinstance(x, (int, float)): return int(x)
    if isinstance(x, dt.datetime):
        if x.tzinfo is None: x = x.replace(tzinfo=dt.timezone.utc)
        return int(x.timestamp())
    return int(pd.Timestamp(x, tz="UTC").timestamp())


def _get_chunk(client, product: str, start: int, end: int) -> list[list]:
    """One Coinbase request (<= 300 candles). Returns rows [ts, low, high, open, close, volume]."""
    s = dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    e = dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    for attempt in range(5):
        r = client.get(f"{CB_BASE}/{product}/candles", params={"granularity": 60, "start": s, "end": e}, timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(1.5 * (attempt + 1)); continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return []


def fetch_1m(asset: str, start, end, use_cache: bool = True, client=None) -> pd.DataFrame:
    """1-minute candles for `asset` ('BTC' or 'BTC-USD') covering [start, end) UTC. Missing minutes are NOT filled
    (Coinbase omits minutes with no trades; the study treats them as gaps). Results are merged into the parquet cache."""
    start_ts, end_ts = _to_ts(start) // 60 * 60, _to_ts(end) // 60 * 60
    product = _product(asset); path = cache_path(asset)
    have = pd.DataFrame(columns=COLS)
    if use_cache and path.exists():
        have = pd.read_parquet(path)
    need: list[tuple[int, int]] = []
    if len(have):
        got = set(have.ts.astype(int).tolist())
        # request only missing stretches (coarse: 300-min blocks with any missing minute, but tolerate sparse gaps already fetched)
        fetched_marker = path.with_suffix(".ranges.json")
        import json
        ranges = json.loads(fetched_marker.read_text()) if fetched_marker.exists() else []
        covered = lambda a, b: any(ra <= a and b <= rb for ra, rb in ranges)
        for blk in range(start_ts, end_ts, 300 * 60):
            b_end = min(blk + 300 * 60, end_ts)
            if not covered(blk, b_end): need.append((blk, b_end))
    else:
        need = [(blk, min(blk + 300 * 60, end_ts)) for blk in range(start_ts, end_ts, 300 * 60)]
    if need:
        import httpx, json
        own = client is None
        client = client or httpx.Client(headers={"User-Agent": "predhft-p17/0.1"})
        rows = []
        try:
            for a, b in need:
                rows += _get_chunk(client, product, a, b - 60)   # Coinbase end is inclusive
                time.sleep(_REQ_GAP_S)
        finally:
            if own: client.close()
        new = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])[COLS] if rows else pd.DataFrame(columns=COLS)
        allc = pd.concat([have, new], ignore_index=True)
        allc = allc.astype({"ts": "int64", "open": "float64", "high": "float64", "low": "float64", "close": "float64", "volume": "float64"})
        allc = allc.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        CACHE.mkdir(parents=True, exist_ok=True)
        allc.to_parquet(path, index=False)
        fetched_marker = path.with_suffix(".ranges.json")
        ranges = json.loads(fetched_marker.read_text()) if fetched_marker.exists() else []
        ranges = _merge_ranges(ranges + [list(x) for x in need])
        fetched_marker.write_text(json.dumps(ranges))
        have = allc
    out = have[(have.ts >= start_ts) & (have.ts < end_ts)].sort_values("ts").reset_index(drop=True)
    return out.astype({"ts": "int64"})


def _merge_ranges(r: list) -> list:
    r = sorted([list(x) for x in r])
    out: list = []
    for a, b in r:
        if out and a <= out[-1][1]: out[-1][1] = max(out[-1][1], b)
        else: out.append([a, b])
    return out


def coverage(df: pd.DataFrame) -> dict:
    if not len(df): return {"n": 0}
    ts = df.ts.astype(int)
    span = (int(ts.max()) - int(ts.min())) // 60 + 1
    return {"n": int(len(df)), "expected": int(span), "coverage": round(len(df) / span, 4),
            "first": dt.datetime.fromtimestamp(int(ts.min()), dt.timezone.utc).isoformat(),
            "last": dt.datetime.fromtimestamp(int(ts.max()), dt.timezone.utc).isoformat()}


# ----------------------------------------------------------------------------- settlement proxy
def ohlc4(row) -> float:
    return float(row["open"] + row["high"] + row["low"] + row["close"]) / 4.0


def minute_avg_proxy(df_idx: pd.DataFrame, boundary_ts: int, method: str = "ohlc4") -> float | None:
    """Proxy for the 60-s average of the index in the minute ENDING at boundary_ts, i.e. the candle opening at boundary_ts-60.
    df_idx must be indexed by ts.  method: 'ohlc4' | 'close' | 'hl2'."""
    t = boundary_ts - 60
    if t not in df_idx.index: return None
    r = df_idx.loc[t]
    if method == "close": return float(r["close"])
    if method == "hl2": return float(r["high"] + r["low"]) / 2
    return ohlc4(r)


def window_outcomes(df: pd.DataFrame, period_s: int = 900, method: str = "ohlc4") -> pd.DataFrame:
    """One row per 15-min window [open_ts, close_ts]: avg_open (minute before open), avg_close (minute before close),
    y = 1 if avg_close > avg_open (Kalshi YES), plus the window-level move in bp."""
    d = df.set_index("ts")
    ts = d.index.values
    firsts = (int(ts.min()) // period_s + 1) * period_s
    lasts = int(ts.max()) // period_s * period_s
    rows = []
    for o in range(firsts, lasts - period_s + 1, period_s):
        c = o + period_s
        a0 = minute_avg_proxy(d, o, method); a1 = minute_avg_proxy(d, c, method)
        if a0 is None or a1 is None: continue
        rows.append({"open_ts": o, "close_ts": c, "avg_open": a0, "avg_close": a1, "y": int(a1 > a0), "move_bp": 1e4 * (a1 / a0 - 1)})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- features
def _ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x, dtype=float).ewm(span=n, adjust=False).mean().values


def _rsi(close: np.ndarray, n: int = 14) -> float:
    if len(close) < n + 1: return float("nan")
    d = np.diff(close[-(n * 4 + 1):]) if len(close) > n * 4 + 1 else np.diff(close)
    up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    au, ad = up[:n].mean(), dn[:n].mean()          # Wilder smoothing
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n; ad = (ad * (n - 1) + dn[i]) / n
    if ad == 0: return 100.0
    return 100.0 - 100.0 / (1.0 + au / ad)


def _resample(h: pd.DataFrame, step_s: int, t_end: int) -> pd.DataFrame:
    """Aggregate closed 1-min candles into step_s bars aligned so the LAST bar ends exactly at t_end (no partial bar)."""
    ts = h.ts.values.astype(int)
    bar = (t_end - 60 - ts) // step_s            # 0 = most recent full bar
    g = h.assign(_b=bar).groupby("_b", sort=True)
    out = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(), "close": g.close.last(), "volume": g.volume.sum(), "n": g.size()})
    out = out[out.n >= max(1, int(0.8 * step_s / 60))].sort_index(ascending=False)   # chronological
    return out.reset_index(drop=True)


def _trend_state(bars: pd.DataFrame, span: int = 9) -> tuple[float, float, float]:
    """(ema_slope_sign, hh_hl_structure, slope_bp) on a bar series. Structure: +1 higher-high & higher-low over last 3 bars vs prior 3, -1 LL&LH, else 0."""
    if len(bars) < max(span + 4, 7): return float("nan"), float("nan"), float("nan")
    c = bars.close.values.astype(float); e = _ema(c, span)
    slope_bp = 1e4 * (e[-1] / e[-4] - 1)
    hi, lo = bars.high.values, bars.low.values
    hh = hi[-3:].max() > hi[-6:-3].max(); hl = lo[-3:].min() > lo[-6:-3].min()
    ll = lo[-3:].min() < lo[-6:-3].min(); lh = hi[-3:].max() < hi[-6:-3].max()
    struct = 1.0 if (hh and hl) else (-1.0 if (ll and lh) else 0.0)
    return float(np.sign(slope_bp)), struct, slope_bp


def _adx(bars: pd.DataFrame, n: int = 14) -> float:
    if len(bars) < 2 * n + 1: return float("nan")
    hi, lo, c = bars.high.values.astype(float), bars.low.values.astype(float), bars.close.values.astype(float)
    up = hi[1:] - hi[:-1]; dn = lo[:-1] - lo[1:]
    pdm = np.where((up > dn) & (up > 0), up, 0.0); ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum.reduce([hi[1:] - lo[1:], np.abs(hi[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])])
    def wilder(x):
        s = x[:n].sum(); out = [s]
        for v in x[n:]: s = s - s / n + v; out.append(s)
        return np.array(out)
    atr, pd_, nd_ = wilder(tr), wilder(pdm), wilder(ndm)
    pdi, ndi = 100 * pd_ / (atr + 1e-12), 100 * nd_ / (atr + 1e-12)
    dx = 100 * np.abs(pdi - ndi) / (pdi + ndi + 1e-12)
    if len(dx) < n: return float("nan")
    adx = dx[:n].mean()
    for v in dx[n:]: adx = (adx * (n - 1) + v) / n
    return float(adx)


def _variance_ratio(c: np.ndarray, q: int = 15, span: int = 120) -> float:
    """Lo-MacKinlay VR(q): var(q-period log return) / (q * var(1-period)). >1 trending (positive autocorrelation), <1 mean-reverting."""
    x = np.log(c[-(span + 1):])
    if len(x) < q * 4: return float("nan")
    r1 = np.diff(x); rq = x[q:] - x[:-q]
    v1 = np.var(r1)
    return float(np.var(rq) / (q * v1)) if v1 > 0 else float("nan")


TA_FEATS = ["ret_1", "ret_5", "ret_15", "ret_60", "ema9_slope", "ema21_slope", "ema_cross", "ema_gap_bp", "rsi14",
            "vwap_dev_session_bp", "vwap_dev_60_bp", "rv_15", "rv_60", "rv_ratio", "range_pos_15", "range_pos_60",
            "engulf", "inside_bar", "three_row", "prev_win_dir", "prev_win_move_bp", "tod_bucket", "vol_z_5"]
MTF_FEATS = ["tf1_dir", "tf5_dir", "tf15_dir", "tf60_dir", "tf5_struct", "tf15_struct", "tf60_struct", "tf5_slope_bp", "tf15_slope_bp", "tf60_slope_bp", "align_score", "align_abs", "struct_score"]
REGIME_FEATS = ["adx14_5m", "vr15", "trending", "mom_x_trend", "partial_x_trend", "prevwin_x_trend", "ret15_x_vr"]
VOLUME_FEATS = ["vol_rel_5_60", "vol_rel_15_60", "vol_conf_5", "vol_conf_15", "vol_conf_partial"]
XASSET_FEATS = ["ref_ret_1", "ref_ret_5", "ref_ret_15", "ref_align_score", "ref_partial_move_bp", "rel_str_15", "rel_str_60", "ratio_mom_60", "ref_lead_1_5"]
STREAK_FEATS = ["streak3_sum", "streak3_same", "last_outcome"]
BASE_FEATS = ["partial_move_bp", "mins_elapsed", "hour_utc"]
FEATURE_NAMES = TA_FEATS + MTF_FEATS + REGIME_FEATS + VOLUME_FEATS + XASSET_FEATS + STREAK_FEATS + BASE_FEATS
DIMENSIONS = {"ta": TA_FEATS, "mtf": MTF_FEATS, "regime": REGIME_FEATS, "volume": VOLUME_FEATS, "xasset": XASSET_FEATS, "streak": STREAK_FEATS}


def _nan_dict() -> dict:
    return {k: float("nan") for k in FEATURE_NAMES}


def features_at(df: pd.DataFrame, t, window_open=None, ref_df: pd.DataFrame | None = None, prior_outcomes: Iterable[int] | None = None) -> dict:
    """Features using only candles whose CLOSE time <= t (candle open ts <= t-60). t / window_open: unix seconds or datetimes.
    window_open defaults to the last quarter-hour boundary <= t. ref_df: reference asset (BTC) candles for the cross-asset
    dimension (NaN if None). prior_outcomes: iterable of the last completed Kalshi window outcomes (1/0), newest last.
    Returns NaN where history is insufficient. No look-ahead: never touches a candle opening after t-60."""
    t = _to_ts(t)
    wo = (t // 900) * 900 if window_open is None else _to_ts(window_open)
    h = df[df.ts <= t - 60].tail(1500)
    f = _nan_dict()
    if len(h) < 30: return f
    c = h.close.values.astype(float); hi = h.high.values.astype(float); lo = h.low.values.astype(float); op = h.open.values.astype(float); v = h.volume.values.astype(float)
    ts = h.ts.values.astype(int); last = c[-1]
    def ret(n, arr=c):
        return 1e4 * (arr[-1] / arr[-1 - n] - 1) if len(arr) > n else float("nan")
    f["ret_1"], f["ret_5"], f["ret_15"], f["ret_60"] = ret(1), ret(5), ret(15), ret(60)
    e9, e21 = _ema(c, 9), _ema(c, 21)
    f["ema9_slope"] = 1e4 * (e9[-1] / e9[-4] - 1); f["ema21_slope"] = 1e4 * (e21[-1] / e21[-4] - 1)
    f["ema_cross"] = float(np.sign(e9[-1] - e21[-1])); f["ema_gap_bp"] = 1e4 * (e9[-1] / e21[-1] - 1)
    f["rsi14"] = _rsi(c, 14)
    day0 = (t // 86400) * 86400; typ = (hi + lo + c) / 3; m = ts >= day0
    if m.sum() >= 1 and v[m].sum() > 0: f["vwap_dev_session_bp"] = 1e4 * (last / (np.sum(typ[m] * v[m]) / np.sum(v[m])) - 1)
    if len(c) >= 60 and v[-60:].sum() > 0: f["vwap_dev_60_bp"] = 1e4 * (last / (np.sum(typ[-60:] * v[-60:]) / np.sum(v[-60:])) - 1)
    lr = np.diff(np.log(c))
    if len(lr) >= 15: f["rv_15"] = 1e4 * float(np.std(lr[-15:]))
    if len(lr) >= 60: f["rv_60"] = 1e4 * float(np.std(lr[-60:]))
    if f["rv_60"] == f["rv_60"] and f["rv_60"] > 0: f["rv_ratio"] = f["rv_15"] / f["rv_60"]
    def rpos(n):
        if len(c) < n: return float("nan")
        H, L = hi[-n:].max(), lo[-n:].min()
        return (last - L) / (H - L) if H > L else 0.5
    f["range_pos_15"], f["range_pos_60"] = rpos(15), rpos(60)
    b1, b2 = c[-1] - op[-1], c[-2] - op[-2]
    f["engulf"] = float(np.sign(b1)) if (abs(b1) > abs(b2) and np.sign(b1) == -np.sign(b2) and max(op[-1], c[-1]) >= max(op[-2], c[-2]) and min(op[-1], c[-1]) <= min(op[-2], c[-2])) else 0.0
    f["inside_bar"] = 1.0 if (hi[-1] <= hi[-2] and lo[-1] >= lo[-2]) else 0.0
    s = np.sign(c[-3:] - op[-3:]); f["three_row"] = float(s[0]) if (s[0] != 0 and (s == s[0]).all()) else 0.0
    di = h.set_index("ts")
    p_open, p_close = minute_avg_proxy(di, wo - 900), minute_avg_proxy(di, wo)
    if p_open and p_close: f["prev_win_move_bp"] = 1e4 * (p_close / p_open - 1); f["prev_win_dir"] = float(np.sign(p_close - p_open))
    hr = (t % 86400) // 3600; f["hour_utc"] = float(hr); f["tod_bucket"] = float(0 if hr < 8 else 1 if hr < 13 else 2 if hr < 21 else 3)
    a0 = minute_avg_proxy(di, wo)
    if a0: f["partial_move_bp"] = 1e4 * (last / a0 - 1)
    f["mins_elapsed"] = (t - wo) / 60.0
    if len(v) >= 65: f["vol_z_5"] = float((v[-5:].mean() - v[-65:-5].mean()) / (v[-65:-5].std() + 1e-9))
    # ---- (a) multi-timeframe alignment
    b5, b15, b60 = _resample(h, 300, t), _resample(h, 900, t), _resample(h, 3600, t)
    f["tf1_dir"] = float(np.sign(f["ema9_slope"]))
    for name, bars in (("tf5", b5), ("tf15", b15), ("tf60", b60)):
        d_, st_, sl_ = _trend_state(bars)
        f[f"{name}_dir"], f[f"{name}_struct"], f[f"{name}_slope_bp"] = d_, st_, sl_
    dirs = np.array([f["tf1_dir"], f["tf5_dir"], f["tf15_dir"], f["tf60_dir"]], dtype=float)
    if not np.isnan(dirs).any(): f["align_score"] = float(dirs.sum()); f["align_abs"] = float(abs(dirs.sum()))
    sts = np.array([f["tf5_struct"], f["tf15_struct"], f["tf60_struct"]], dtype=float)
    if not np.isnan(sts).any(): f["struct_score"] = float(sts.sum())
    # ---- (c) regime
    f["adx14_5m"] = _adx(b5, 14); f["vr15"] = _variance_ratio(c, 15, 120)
    if f["adx14_5m"] == f["adx14_5m"]:
        f["trending"] = 1.0 if f["adx14_5m"] >= 25 else 0.0
        f["mom_x_trend"] = f["ret_15"] * f["trending"]; f["partial_x_trend"] = f["partial_move_bp"] * f["trending"]; f["prevwin_x_trend"] = f["prev_win_dir"] * f["trending"]
    if f["vr15"] == f["vr15"]: f["ret15_x_vr"] = f["ret_15"] * (f["vr15"] - 1.0)
    # ---- (d) volume
    if len(v) >= 60 and v[-60:].mean() > 0:
        base = v[-60:].mean(); f["vol_rel_5_60"] = v[-5:].mean() / base; f["vol_rel_15_60"] = v[-15:].mean() / base
        f["vol_conf_5"] = f["ret_5"] * f["vol_rel_5_60"]; f["vol_conf_15"] = f["ret_15"] * f["vol_rel_15_60"]
        n_in = int(max(1, min(15, (t - wo) // 60)))
        f["vol_conf_partial"] = f["partial_move_bp"] * (v[-n_in:].mean() / base)
    # ---- (b) cross-asset (reference = BTC)
    if ref_df is not None and len(ref_df):
        rh = ref_df[ref_df.ts <= t - 60].tail(1500)
        if len(rh) >= 70:
            rc = rh.close.values.astype(float)
            f["ref_ret_1"], f["ref_ret_5"], f["ref_ret_15"] = ret(1, rc), ret(5, rc), ret(15, rc)
            rdi = rh.set_index("ts"); ra0 = minute_avg_proxy(rdi, wo)
            if ra0: f["ref_partial_move_bp"] = 1e4 * (rc[-1] / ra0 - 1)
            r5, r15, r60 = _resample(rh, 300, t), _resample(rh, 900, t), _resample(rh, 3600, t)
            re9 = _ema(rc, 9); rd = [np.sign(re9[-1] - re9[-4])] + [_trend_state(b)[0] for b in (r5, r15, r60)]
            rd = np.array(rd, dtype=float)
            if not np.isnan(rd).any(): f["ref_align_score"] = float(rd.sum())
            f["rel_str_15"] = f["ret_15"] - f["ref_ret_15"]; f["rel_str_60"] = ret(60) - ret(60, rc)
            # ratio momentum: asset/ref ratio now vs 60 min ago, minus its 15-min change (spread between slow and fast ratio momentum)
            if len(c) > 60 and len(rc) > 60: f["ratio_mom_60"] = 1e4 * ((c[-1] / rc[-1]) / (c[-61] / rc[-61]) - 1)
            # lead-lag: ref's last 1..5-min move not yet matched by the asset (ref_ret_5 - asset ret_5), signed
            f["ref_lead_1_5"] = f["ref_ret_5"] - f["ret_5"]
    # ---- (e) prior Kalshi window outcome streak
    if prior_outcomes is not None:
        po = [int(x) for x in list(prior_outcomes)[-3:]]
        if len(po) == 3:
            sgn = [1 if x else -1 for x in po]
            f["streak3_sum"] = float(sum(sgn)); f["streak3_same"] = float(sgn[0]) if len(set(sgn)) == 1 else 0.0; f["last_outcome"] = float(sgn[-1])
    return f


__all__ = ["fetch_1m", "features_at", "window_outcomes", "minute_avg_proxy", "coverage", "FEATURE_NAMES", "DIMENSIONS", "cache_path",
           "TA_FEATS", "MTF_FEATS", "REGIME_FEATS", "VOLUME_FEATS", "XASSET_FEATS", "STREAK_FEATS", "BASE_FEATS"]
