"""P17 offline study: do candle / technical-analysis / multi-dimensional trend features predict Kalshi KX*15M outcomes beyond
the within-window partial move?  Zero-cost, CPU only, no LLM.  Writes reports/p17_candles/{results.json, oos_preds.parquet, features.parquet}.

Run: cd D:/prediction-hft && uv run --with scikit-learn,pandas,pyarrow,httpx python scripts/p17_candles_study.py
"""
from __future__ import annotations
import json, math, pathlib, sys, time, datetime as dt
import numpy as np, pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft.candles import fetch_1m, window_outcomes, features_at, FEATURE_NAMES, DIMENSIONS, minute_avg_proxy, coverage
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.impute import SimpleImputer
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "p17_candles"; OUT.mkdir(parents=True, exist_ok=True)
ASSETS = ["BTC", "ETH", "SOL"]
DECISION = {"open": 0, "T-5m": 600, "T-1m": 840}
BASE = ["partial_move_bp", "partial_z"]
ALL_DIMS = [f for d in DIMENSIONS.values() for f in d]
EDGE_C = 0.05
rng = np.random.default_rng(17)
START, END = dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc), dt.datetime(2026, 10, 28, 1, 30, tzinfo=dt.timezone.utc)


def kalshi_fee(p: float) -> float:
    return 0.07 * p * (1 - p)          # per contract, un-rounded


def build(asset: str, candles: dict) -> pd.DataFrame:
    df = candles[asset]; ref = candles["BTC"] if asset != "BTC" else None
    wins = window_outcomes(df)
    rows = []; hist: list[int] = []
    for w in wins.itertuples():
        for dname, off in DECISION.items():
            t = w.open_ts + off
            f = features_at(df, t, window_open=w.open_ts, ref_df=ref, prior_outcomes=hist)
            rem = max(15.0 - f["mins_elapsed"], 1.0)
            f["partial_z"] = f["partial_move_bp"] / (f["rv_60"] * math.sqrt(rem) + 1e-9) if f.get("rv_60") == f.get("rv_60") else float("nan")
            f.update({"asset": asset, "decision": dname, "open_ts": w.open_ts, "y": w.y, "move_bp": w.move_bp, "day": w.close_ts // 86400})
            rows.append(f)
        hist.append(int(w.y))   # only windows that CLOSED before the next window opens
    return pd.DataFrame(rows)


def logit_pipe(C=1.0):
    return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), LogisticRegression(C=C, max_iter=3000))


def hgb():
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150, min_samples_leaf=40, l2_regularization=1.0)


def walk_forward(d: pd.DataFrame, feats: list[str], model_fn) -> np.ndarray:
    """Expanding window: first 20 days train only; then 4-day test blocks, retrain before each. OOS p aligned to d (NaN on train-only days)."""
    days = np.sort(d.day.unique()); p = np.full(len(d), np.nan)
    for i in range(20, len(days), 4):
        blk = days[i:i + 4]; tr = (d.day < blk[0]).values; te = d.day.isin(blk).values
        X = d[feats].values.astype(float)
        m = model_fn(); m.fit(X[tr], d.y.values[tr]); p[te] = m.predict_proba(X[te])[:, 1]
    return p


def metrics(y, p, base_rate):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return {"n": int(len(y)), "auc": float(roc_auc_score(y, p)), "logloss": float(log_loss(y, p)), "logloss_base": float(log_loss(y, np.full(len(y), base_rate))), "brier": float(brier_score_loss(y, p))}


def calib(y, p, bins=(0, .3, .4, .5, .6, .7, 1.0)):
    out = []; idx = np.digitize(p, bins[1:-1])
    for b in range(len(bins) - 1):
        m = idx == b
        if m.sum() >= 10: out.append({"lo": bins[b], "hi": bins[b + 1], "n": int(m.sum()), "p_mean": round(float(p[m].mean()), 3), "y_mean": round(float(y[m].mean()), 3)})
    return out


def day_bootstrap_auc_diff(y, p1, p0, days, B=600):
    ud = np.unique(days); groups = {d: np.where(days == d)[0] for d in ud}; diffs = []
    for _ in range(B):
        idx = np.concatenate([groups[d] for d in rng.choice(ud, len(ud), replace=True)]); yy = y[idx]
        if yy.min() == yy.max(): continue
        diffs.append(roc_auc_score(yy, p1[idx]) - roc_auc_score(yy, p0[idx]))
    diffs = np.array(diffs)
    return {"mean": float(diffs.mean()), "lo": float(np.percentile(diffs, 2.5)), "hi": float(np.percentile(diffs, 97.5)), "p_gt0": float((diffs > 0).mean())}


def economics(y, p_model, p_mkt, edge=EDGE_C):
    """Buy favoured side when model exceeds the market proxy by >= edge after fee; pays p_mkt (no spread, no slippage)."""
    pnl = []
    for yi, pm, pk in zip(y, p_model, p_mkt):
        if pm - pk - kalshi_fee(pk) >= edge: pnl.append((yi - pk) - kalshi_fee(pk))
        elif (pk - pm) - kalshi_fee(pk) >= edge: pnl.append((pk - yi) - kalshi_fee(pk))
    pnl = np.array(pnl)
    if len(pnl) == 0: return {"n_trades": 0, "pnl_c_per_ct": None, "se_c": None, "win": None, "total_c": 0.0}
    return {"n_trades": int(len(pnl)), "pnl_c_per_ct": float(100 * pnl.mean()), "se_c": float(100 * pnl.std(ddof=1) / math.sqrt(len(pnl))) if len(pnl) > 1 else None, "win": float((pnl > 0).mean()), "total_c": float(100 * pnl.sum())}


def approx_error(candles):
    """OHLC4 / close / HL2 of the Coinbase 1-min candle vs the OFFICIAL 60-s index average (data/cfb/<A>.jsonl rows with avg60_n=60 on a minute boundary)."""
    res = {}
    for a in ASSETS:
        p = ROOT / "data" / "cfb" / f"{a}.jsonl"
        if not p.exists(): continue
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        off = [r for r in rows if r.get("avg60_n") == 60 and int(r["value_ts_ms"]) % 60000 == 0]
        cand = candles[a].set_index("ts"); errs = {"ohlc4": [], "close": [], "hl2": []}
        for r in off:
            b = int(r["value_ts_ms"]) // 1000
            for m in errs:
                v = minute_avg_proxy(cand, b, m)
                if v is not None: errs[m].append(1e4 * (v / r["avg60"] - 1))
        res[a] = {m: {"n": len(e), "bias_bp": float(np.mean(e)), "sd_bp": float(np.std(e)), "mad_bp": float(np.median(np.abs(e)))} for m, e in errs.items() if e}
    return res


def main():
    t0 = time.time()
    candles = {a: fetch_1m(a, START, END) for a in ASSETS}
    cov = {a: coverage(candles[a]) for a in ASSETS}; print(cov)
    D = pd.concat([build(a, candles) for a in ASSETS], ignore_index=True)
    D.to_parquet(OUT / "features.parquet", index=False)
    print("features built", len(D), round(time.time() - t0), "s")
    results = {"built_at": dt.datetime.now(dt.timezone.utc).isoformat(), "coverage": cov, "n_rows": int(len(D)), "approx_error": approx_error(candles), "cells": []}
    sens = {}
    for a in ASSETS:
        w1 = window_outcomes(candles[a], method="ohlc4"); w2 = window_outcomes(candles[a], method="close")
        mm = w1.merge(w2, on="open_ts", suffixes=("_o", "_c")); sens[a] = {"n": int(len(mm)), "label_disagree": float((mm.y_o != mm.y_c).mean()), "yes_rate": float(mm.y_o.mean()), "abs_move_bp_median": float(mm.move_bp_o.abs().median())}
    results["label_sensitivity"] = sens
    preds = []
    for a in ASSETS:
        for dname in DECISION:
            d = D[(D.asset == a) & (D.decision == dname)].sort_values("open_ts").reset_index(drop=True).dropna(subset=["partial_move_bp", "rv_60"]).reset_index(drop=True)
            dims = {k: v for k, v in DIMENSIONS.items() if not (k == "xasset" and a == "BTC")}
            all_feats = BASE + [f for k in dims for f in dims[k]]
            P = {"base": walk_forward(d, BASE, logit_pipe)}
            for k, fs in dims.items(): P[f"base+{k}"] = walk_forward(d, BASE + fs, lambda: logit_pipe(C=0.3))
            P["base+all_logit"] = walk_forward(d, all_feats, lambda: logit_pipe(C=0.1))
            P["base+all_hgb"] = walk_forward(d, all_feats, hgb)
            P["candles_only_logit"] = walk_forward(d, [f for f in all_feats if f not in BASE], lambda: logit_pipe(C=0.1))
            P["candles_only_hgb"] = walk_forward(d, [f for f in all_feats if f not in BASE], hgb)
            m = ~np.isnan(P["base"]); y = d.y.values[m]; days = d.day.values[m]; base_rate = float(d.y.values[~m].mean())
            cell = {"asset": a, "decision": dname, "n_train": int((~m).sum()), "n_test": int(m.sum()), "base_rate_train": base_rate, "y_mean_test": float(y.mean()), "models": {}, "dauc_vs_base": {}, "econ": {}}
            half = np.full(m.sum(), 0.5)
            for name, p in P.items():
                pm = p[m]; cell["models"][name] = metrics(y, pm, base_rate); cell["models"][name]["calib"] = calib(y, np.clip(pm, 1e-4, 1 - 1e-4))
                if name != "base": cell["dauc_vs_base"][name] = day_bootstrap_auc_diff(y, pm, P["base"][m], days)
                cell["econ"][name + "_vs_base"] = economics(y, pm, P["base"][m]) if name != "base" else None
                cell["econ"][name + "_vs_50c"] = economics(y, pm, half)
            dd = d[m]; reg = {}
            for lab, mask in (("trending", dd.trending == 1), ("ranging", dd.trending == 0)):
                sub = dd[mask]
                if len(sub) >= 60:
                    reg[lab] = {"n": int(len(sub)), "auc_ret15": float(roc_auc_score(sub.y, sub.ret_15)), "auc_prevwin": float(roc_auc_score(sub.y, sub.prev_win_move_bp)), "auc_partial": float(roc_auc_score(sub.y, sub.partial_move_bp)), "auc_align": float(roc_auc_score(sub.y, sub.align_score.fillna(0)))}
            cell["regime_split"] = reg
            results["cells"].append(cell)
            preds.append(pd.DataFrame({"asset": a, "decision": dname, "open_ts": d.open_ts.values[m], "y": y, "day": days, **{k: v[m] for k, v in P.items()}}))
            mm_ = cell["models"]
            print(f"{a} {dname:5s} n={m.sum():4d} AUC base {mm_['base']['auc']:.3f} all_logit {mm_['base+all_logit']['auc']:.3f} all_hgb {mm_['base+all_hgb']['auc']:.3f} cand_only {mm_['candles_only_logit']['auc']:.3f} | "
                  + " ".join(f"{k}:{cell['dauc_vs_base'][f'base+{k}']['mean']:+.3f}" for k in dims) + f" | econ all_logit vs base: {cell['econ']['base+all_logit_vs_base']['n_trades']} tr {cell['econ']['base+all_logit_vs_base']['pnl_c_per_ct']}")
    pd.concat(preds).to_parquet(OUT / "oos_preds.parquet", index=False)
    uni = []
    for dname in DECISION:
        d = D[D.decision == dname]
        for f in ALL_DIMS + BASE:
            x = d[f].values.astype(float); yv = d.y.values; ok = ~np.isnan(x)
            if ok.sum() < 200 or np.std(x[ok]) == 0: continue
            uni.append({"decision": dname, "feature": f, "auc": float(roc_auc_score(yv[ok], x[ok])), "n": int(ok.sum())})
    results["univariate"] = uni
    ll = {}
    for a in ("ETH", "SOL"):
        d = D[(D.asset == a) & (D.decision == "open")]
        ll[a] = {f: float(roc_auc_score(d.y, d[f].fillna(0))) for f in ("ref_ret_1", "ref_ret_5", "ref_ret_15", "ref_lead_1_5", "rel_str_15", "ratio_mom_60")}
        ll[a]["n"] = int(len(d))
    results["leadlag_open"] = ll
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=float))
    print("done", round(time.time() - t0), "s")


if __name__ == "__main__":
    main()
