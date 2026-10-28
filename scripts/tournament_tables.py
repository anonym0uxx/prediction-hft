"""Build the markdown tables for docs/TOURNAMENT_2026-10-28.md from reports/tournament/{predictions_full.parquet,results_full.json}."""
import sys, json, math, pathlib, collections
sys.path.insert(0, "src"); sys.path.insert(0, "scripts")
import numpy as np, pandas as pd
import predhft.model_zoo as Z
ROOT = pathlib.Path("."); OUT = ROOT / "reports" / "tournament"
df = pd.read_parquet(OUT / "predictions_full.parquet"); res = json.loads((OUT / "results_full.json").read_text(encoding="utf-8"))
models = res["models"]; fam = {m: res["model_cfg"].get(m, {}).get("family", "baseline") for m in models}
A = df[df.tape == "A"].copy(); B = df[(df.tape == "B") & (df.decision_pt == True)].copy()
half = A.fold.max() // 2
A["half"] = np.where(A.fold <= half, "dev1", "dev2")
L = []
def f(v, d=4):
    return "—" if v is None or (isinstance(v, float) and not np.isfinite(v)) else (f"{v:.{d}f}" if isinstance(v, float) else str(v))

def paired(d, nm, bench):
    pm = d[f"p:{nm}"].values; pb = d[f"p:{bench}"].values; y = d.y.values; ok = np.isfinite(pm) & np.isfinite(pb)
    if ok.sum() < 20: return None
    diff = Z.brier(pb[ok], y[ok]) - Z.brier(pm[ok], y[ok]); pc = Z.per_close_mean(diff, d.close_ts.values[ok])
    vals = np.array(list(pc.values())); cl = np.array(list(pc.keys()))
    se_c = vals.std(ddof=1) / math.sqrt(len(vals)) if len(vals) > 1 else np.nan; se_h = Z.cluster_se(vals, cl // 3600)
    return {"n": int(ok.sum()), "closes": len(vals), "diff": float(vals.mean()), "se_c": float(se_c), "se_h": (float(se_h) if se_h else np.nan),
            "t_c": float(vals.mean() / se_c) if se_c else np.nan, "t_h": float(vals.mean() / se_h) if se_h else np.nan}

def scores(d, nm):
    pm = d[f"p:{nm}"].values; y = d.y.values; ok = np.isfinite(pm)
    if ok.sum() < 20: return None
    return {"n": int(ok.sum()), "brier": float(np.mean(Z.brier(pm[ok], y[ok]))), "ll": float(np.mean(Z.logloss(pm[ok], y[ok]))), "cal": Z.calibration_slope(pm[ok], y[ok])}

# ---------------- Table 1: tape A, all models, dev1/dev2 + pooled, vs solv1_candle
rows = []
for nm in models:
    if nm == "mid": continue
    r = {"model": nm, "family": fam[nm]}
    for lab, d in (("all", A), ("dev1", A[A.half == "dev1"]), ("dev2", A[A.half == "dev2"])):
        s = scores(d, nm); p = paired(d, nm, "solv1_candle")
        r[lab] = (s, p)
    rows.append(r)
spa_a = res["spa"]["A_pooled_vs_solv1_candle"]["t_stats"]
rows.sort(key=lambda r: -(r["all"][1]["diff"] if r["all"][1] else -9))
L.append("### T1 — Tape A (candles, 36 days, 14 walk-forward folds, 3 assets pooled, T−14/−10/−5/−2/−1 min): all 47 configurations vs `solv1_candle` (B1)\n")
L.append("Δ = mean per-close Brier(B1) − Brier(model) (positive = better than B1); t = Δ / SE with SE clustered by close (t_c) and by UTC hour (t_h). dev1 = folds 1–7 (10-02 → 10-16 UTC), dev2 = folds 8–14 (10-16 → 10-28 20:28Z). Calibration slope: logistic slope of y on logit(p) (1 = calibrated, < 1 over-confident). SPA t = studentised t in the pooled-zone column of the SPA matrix.\n")
L.append("| # | model | family | n | Brier | log-loss | cal. slope | Δ vs B1 (all) | t_c / t_h | Δ dev1 (t_c) | Δ dev2 (t_c) | SPA t |")
L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
for i, r in enumerate(rows, 1):
    s, p = r["all"]; s1, p1 = r["dev1"]; s2, p2 = r["dev2"]
    if not s: L.append(f"| {i} | {r['model']} | {r['family']} | — | — | — | — | — | — | — | — | — |"); continue
    L.append(f"| {i} | {r['model']} | {r['family']} | {s['n']} | {f(s['brier'])} | {f(s['ll'])} | {f(s['cal'], 2)} | {f(p['diff'] if p else None)} | {f(p['t_c'] if p else None, 1)} / {f(p['t_h'] if p else None, 1)} | "
             f"{f(p1['diff'] if p1 else None)} ({f(p1['t_c'] if p1 else None, 1)}) | {f(p2['diff'] if p2 else None)} ({f(p2['t_c'] if p2 else None, 1)}) | {f(spa_a.get(r['model'] + '|pooled'), 2)} |")
L.append("")
# per-asset tape A top 5 + baselines
L.append("Per asset (tape A, Δ vs B1 with t_c): ")
L.append("| model | BTC Δ (t) | ETH Δ (t) | SOL Δ (t) |"); L.append("|---|---|---|---|")
for nm in [r["model"] for r in rows[:8]] + ["solv1_candle", "partial_logit", "ens_stack", "ens_avg", "hgb_d3", "rf", "logit_C1"]:
    if nm in [r["model"] for r in rows[:8]] and nm not in ("ens_stack", "ens_avg") and [r["model"] for r in rows[:8]].index(nm) > 7: continue
    cells = []
    for a in ("BTC", "ETH", "SOL"):
        p = paired(A[A.asset == a], nm, "solv1_candle"); cells.append(f"{f(p['diff'] if p else None)} ({f(p['t_c'] if p else None, 1)})")
    L.append(f"| {nm} | " + " | ".join(cells) + " |")
L.append("")

# ---------------- Table 2: tape B decision points, SOL and pooled, vs mid and vs solv1_exact, by zone
L.append("### T2 — Tape B (official feed + Kalshi books, 10-27 22:48Z → 10-28 21:15Z, 10 time-ordered blocks; decision snapshots T−14/−10/−5/−2/−1 min, T−30 s): paired vs the **market mid** and vs the **live policy** `solv1_exact` (B2)\n")
L.append("Only snapshots where a two-sided book with spread ≤ 10 c exists enter the mid-paired columns (the policy's own blend rule). Zones: final ≤ 2 min, mid 2–10 min, early > 10 min. SE clustered by close (t_c) and by UTC hour (t_h).\n")
sp_sol = res["spa"]["B_SOL_vs_mid"]["t_stats"]; sp_pool = res["spa"]["B_pooled_vs_mid"]["t_stats"]
rowsb = []
for nm in models:
    r = {"model": nm, "family": fam[nm]}
    dS = B[B.asset == "SOL"]
    r["sol_s"] = scores(dS, nm); r["sol_mid"] = paired(dS, nm, "mid"); r["pool_mid"] = paired(B, nm, "mid"); r["sol_ex"] = paired(dS, nm, "solv1_exact"); r["pool_ex"] = paired(B, nm, "solv1_exact")
    r["zones"] = {z: paired(dS[dS.zone == z], nm, "mid") for z in Z.ZONES}
    r["zones_ex"] = {z: paired(dS[dS.zone == z], nm, "solv1_exact") for z in Z.ZONES}
    rowsb.append(r)
rowsb.sort(key=lambda r: -(r["sol_mid"]["diff"] if r["sol_mid"] else -9))
L.append("| # | model | family | SOL n (closes) | SOL Brier | log-loss | cal. slope | SOL Δ vs mid | t_c / t_h | SOL Δ final / mid / early (t_c) | pooled 3-asset Δ vs mid (t_c / t_h) | SOL Δ vs B2 (t_c) | pooled Δ vs B2 (t_c) | SPA t (SOL / pooled) |")
L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for i, r in enumerate(rowsb, 1):
    s = r["sol_s"]; pm = r["sol_mid"]; pp = r["pool_mid"]; ex = r["sol_ex"]; pex = r["pool_ex"]; zs = r["zones"]
    L.append(f"| {i} | {r['model']} | {r['family']} | {s['n'] if s else '—'} ({pm['closes'] if pm else '—'}) | {f(s['brier'] if s else None)} | {f(s['ll'] if s else None)} | {f(s['cal'] if s else None, 2)} | {f(pm['diff'] if pm else None)} | {f(pm['t_c'] if pm else None, 1)} / {f(pm['t_h'] if pm else None, 1)} | "
             + " / ".join(f"{f(zs[z]['diff'] if zs[z] else None)} ({f(zs[z]['t_c'] if zs[z] else None, 1)})" for z in Z.ZONES)
             + f" | {f(pp['diff'] if pp else None)} ({f(pp['t_c'] if pp else None, 1)} / {f(pp['t_h'] if pp else None, 1)}) | {f(ex['diff'] if ex else None)} ({f(ex['t_c'] if ex else None, 1)}) | {f(pex['diff'] if pex else None)} ({f(pex['t_c'] if pex else None, 1)}) | {f(sp_sol.get(r['model'] + '|pooled'), 2)} / {f(sp_pool.get(r['model'] + '|pooled'), 2)} |")
L.append("")
# unpaired caveat numbers
dS = B[B.asset == "SOL"]; okm = np.isfinite(dS["p:mid"].values)
L.append(f"Note: the unpaired Brier column is computed over every decision snapshot with a prediction ({int(len(dS))} SOL snapshots), whereas the mid exists on {int(okm.sum())} of them (two-sided book, spread ≤ 10 c); the mid's own Brier on its subset is {f(float(np.mean(Z.brier(dS['p:mid'].values[okm], dS.y.values[okm]))))} and `solv1_exact` on the same subset is {f(float(np.mean(Z.brier(dS['p:solv1_exact'].values[okm], dS.y.values[okm]))))}. Only the paired Δ columns compare like with like.\n")

# ---------------- Table 3: zone x decision time for the top models vs mid (SOL)
L.append("### T3 — Where (if anywhere) the skill sits: SOL paired Δ vs mid by decision time (t_c), top-6 by SOL Δ + baselines\n")
B["dec"] = (B.secs_left / 60).round().astype(int); dS = B[B.asset == "SOL"]
decs = sorted(B.dec.unique())
L.append("| model | " + " | ".join(f"T−{d} min" if d >= 1 else "T−30 s" for d in decs) + " |"); L.append("|---|" + "---|" * len(decs))
for nm in [r["model"] for r in rowsb[:6]] + ["solv1_exact", "solv1_candle", "partial_logit", "ens_stack"]:
    cells = []
    for d in decs:
        p = paired(dS[dS.dec == d], nm, "mid"); cells.append(f"{f(p['diff'] if p else None)} ({f(p['t_c'] if p else None, 1)}; n={p['closes'] if p else 0})")
    L.append(f"| {nm} | " + " | ".join(cells) + " |")
L.append("")
L.append("Same, pooled 3 assets vs mid:\n")
L.append("| model | " + " | ".join(f"T−{d} min" if d >= 1 else "T−30 s" for d in decs) + " |"); L.append("|---|" + "---|" * len(decs))
for nm in [r["model"] for r in rowsb[:6]] + ["solv1_exact", "solv1_candle"]:
    cells = []
    for d in decs:
        p = paired(B[B.dec == d], nm, "mid"); cells.append(f"{f(p['diff'] if p else None)} ({f(p['t_c'] if p else None, 1)}; n={p['closes'] if p else 0})")
    L.append(f"| {nm} | " + " | ".join(cells) + " |")
L.append("")

# ---------------- Table 4: economics
L.append("### T4 — Economics (SOL tape B, every snapshot, SOL-v1 execution/EV replay path with the model's probability injected; blend = policy blend weights, raw = w 1)\n")
L.append("| model | blend: trades / contracts / net $ / c per ct ± SE(close) / hit / max DD $ / Sharpe per 15 min | raw: trades / contracts / net $ / c per ct ± SE / hit / max DD $ |")
L.append("|---|---|---|")
ec = res["economics"]
order = [r["model"] for r in rowsb if r["model"] in ec]
for nm in order:
    e = ec[nm]; b = e["blend"]; w = e["raw"]
    L.append(f"| {nm} | {b['trades']} / {b['contracts']} / {b['net_usd']} / {f(b['c_per_ct'], 1)} ± {f(b['se_c_per_ct_close'], 1)} / {f(b['hit'], 2)} / {b['max_drawdown_usd']} / {f(b['sharpe_per_15min'], 2)} | {w['trades']} / {w['contracts']} / {w['net_usd']} / {f(w['c_per_ct'], 1)} ± {f(w['se_c_per_ct_close'], 1)} / {f(w['hit'], 2)} / {w['max_drawdown_usd']} |")
L.append("")
# ---------------- SPA block
L.append("### T5 — Multiple-comparison correction (stationary bootstrap by close, mean block 4 closes, B = 4000; per-zone B = 2000)\n")
L.append("| test | closes | strategies in the max | best (studentised) | Δ | t | naive one-sided p | RC p (stud.) | SPA-c p (stud.) | **RC p (non-stud.)** | **SPA-c p (non-stud.)** | # strategies with Δ > 0 |")
L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
for lab, s in res["spa"].items():
    if "error" in s: continue
    L.append(f"| {lab} | {s['n_closes']} | {s['n_strategies']} | `{s['best']}` | {s['best_mean_diff']:+.4f} | {s['best_t']:.2f} | {s['best_raw_p_one_sided']:.3f} | {s['p_rc_white']:.3f} | {s['p_spa_consistent']:.3f} | **{s['p_rc_white_nonstud']:.3f}** | **{s['p_spa_consistent_nonstud']:.3f}** | {s['n_with_positive_mean']} |")
    for zn, sz in s.get("per_zone", {}).items():
        if "error" in sz: continue
        L.append(f"| ↳ zone {zn} only | {sz['n_closes']} | {sz['n_strategies']} | `{sz['best']}` | {sz['best_mean_diff']:+.4f} | {sz['best_t']:.2f} | {sz['best_raw_p_one_sided']:.3f} | {sz['p_rc_white']:.3f} | {sz['p_spa_consistent']:.3f} | **{sz['p_rc_white_nonstud']:.3f}** | **{sz['p_spa_consistent_nonstud']:.3f}** | {sz['n_with_positive_mean']} |")
L.append("")
(OUT / "tables_full.md").write_text("\n".join(L), encoding="utf-8")
print("\n".join(L)[:200]); print("written", len(L), "lines")
# some extra numbers for prose
print("tape A closes per asset", A.groupby("asset").close_ts.nunique().to_dict(), "y mean", A.y.mean().round(3), "B SOL closes", dS.close_ts.nunique(), "B y mean", B.y.mean().round(3))
print("B decision points per zone (SOL)", dS.zone.value_counts().to_dict(), "with mid", dS[okm].zone.value_counts().to_dict())
print("forward boundary 2026-10-28T20:52:57Z = ", 1793220777, "B closes after:", sorted(set(B[B.close_ts > 1793220777].close_ts)))
