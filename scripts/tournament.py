"""Algorithm tournament for KX{BTC,ETH,SOL}15M (docs/TOURNAMENT_2026-10-28.md). Offline, CPU only, paper only.

    uv run --with scikit-learn,pandas,pyarrow,hmmlearn,statsmodels,arch,scipy python scripts/tournament.py run [--quick]
    uv run ... python scripts/tournament.py forward            # scored-only hook: tail data/sol-v1/snapshots.jsonl -> data/tournament/forward.jsonl
    uv run ... python scripts/tournament.py forward-score      # paired Brier of the logged forward probabilities vs mid and SOL-v1

Outputs reports/tournament/{tapes.pkl, preds_full.npz (per-fold checkpoint), predictions_full.parquet, results_full.json, forward_models.pkl, summary_full.md}.
Staged and resumable: --stages preds,metrics,economics,forward (each skipped when already persisted; --force recomputes).
Nothing here touches the live policy (sol_v1.py) or any runner.
"""
from __future__ import annotations
import argparse, bisect, collections, datetime as dt, json, math, pathlib, pickle, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import numpy as np
import pandas as pd
from predhft import sol_v1 as S
from predhft import model_zoo as Z

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "tournament"; OUT.mkdir(parents=True, exist_ok=True)
ASSETS = ("BTC", "ETH", "SOL")
DEC_MIN_A = (14, 10, 5, 2, 1)                       # tape A decision times (minutes before close)
DEC_S_B = (840, 600, 300, 120, 60, 30)              # tape B decision times (seconds before close), nearest snapshot within 10 s
TRAIN_ONLY_DAYS = 10; FOLD_DAYS = 2; B_FOLDS = 10
PT = dt.timezone(dt.timedelta(hours=-7))


def log(*a):
    print(dt.datetime.now(PT).strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------------------------------------------------- loaders
def load_grids() -> dict[str, Z.Grid]:
    g = {}
    for a in ASSETS:
        d = pd.read_parquet(ROOT / "data" / "candles" / f"{a}_1m.parquet").sort_values("ts")
        g[a] = Z.make_grid(a, d.ts.values, d.close.values)
    # common grid start/end
    t0 = max(x.ts[0] for x in g.values()); t1 = min(x.ts[-1] for x in g.values())
    for a, x in g.items():
        m = (x.ts >= t0) & (x.ts <= t1); g[a] = Z.Grid(a, x.ts[m], x.x[m], x.r[m])
    return g


def load_cfb(asset: str):
    rows = []
    for l in (ROOT / "data" / "cfb" / f"{asset}.jsonl").open(encoding="utf-8"):
        try: d = json.loads(l)
        except Exception: continue
        if d.get("value") and d.get("value_ts_ms"): rows.append((d["t"], int(d["value_ts_ms"]) // 1000, float(d["value"])))
    rows.sort(key=lambda r: r[1]); series = []; recv = []; last = None
    for t, s, v in rows:
        if s == last: series[-1] = (s, v); recv[-1] = t; continue
        series.append((s, v)); recv.append(t); last = s
    return series, recv


def official_avg(series, boundary_ts: int, dp: int) -> float | None:
    secs = [s for s, _ in series]; lo = bisect.bisect_left(secs, boundary_ts - 60); hi = bisect.bisect_left(secs, boundary_ts)
    vals = [v for s, v in series[lo:hi]]
    return round(sum(vals) / 60, dp) if len(vals) == 60 else None


def load_kalshi_results() -> dict[tuple[str, int], dict]:
    p = ROOT / "data" / "ts" / "results.json"
    if not p.exists(): return {}
    out = {}
    for tk, r in json.loads(p.read_text(encoding="utf-8")).items():
        if r.get("result") in ("yes", "no") and r.get("close_ts"):
            a = tk[2:5]
            out[(a, int(r["close_ts"]))] = r
    return out


def load_snapshots(asset: str) -> dict[int, list[dict]]:
    """Tape B: close_ts -> time-ordered snapshots for one asset (quote path + final-minute books), like sol_v1_replay.load_snapshots."""
    by = collections.defaultdict(list)
    for l in (ROOT / "data" / "open_quotes" / "quotes.jsonl").open(encoding="utf-8"):
        try: r = json.loads(l)
        except Exception: continue
        a = r.get("asset") or (r.get("series", "")[2:5])
        if a != asset or r.get("orderbook_error") or not r.get("strike"): continue
        bids = r.get("bids"); asks = r.get("asks")
        if bids is None: bids = [[r["yes_bid"], r.get("yes_bid_qty", 0)]] if r.get("yes_bid") and r.get("yes_bid_qty") else []
        if asks is None: asks = [[r["yes_ask"], r.get("no_bid_qty", 0)]] if r.get("yes_ask") and r.get("no_bid_qty") else []
        by[int(r["close_ts"])].append({"t": r["t"], "K": float(r["strike"]), "yes_bid": r.get("yes_bid"), "yes_ask": r.get("yes_ask"), "bids": bids, "asks": asks, "recv_t": r["t"], "src": "quotes", "ticker": r["ticker"]})
    for fam in ("s7p15", "s7p13"):
        p = ROOT / "data" / f"{fam}-KX{asset}15M" / "books.jsonl"
        if not p.exists(): continue
        for l in p.open(encoding="utf-8"):
            try: r = json.loads(l)
            except Exception: continue
            if "mins_left" not in r or not r.get("K"): continue
            if "bids" in r: bids, asks = r["bids"], r["asks"]
            else:
                bids = [[r["yes_bid"], r.get("bid_size", 0)]] if (r.get("yes_bid") not in (None, 0.0) and r.get("bid_size")) else []
                asks = [[r["yes_ask"], r.get("ask_size", 0)]] if (r.get("yes_ask") not in (None, 1.0) and r.get("ask_size")) else []
            yb = r.get("yes_bid"); ya = r.get("yes_ask")
            if yb == 0.0 and not bids: yb = None
            if ya == 1.0 and not asks: ya = None
            by[int(r["close_ts"])].append({"t": r["t"], "K": float(r["K"]), "yes_bid": yb, "yes_ask": ya, "bids": bids, "asks": asks, "recv_t": r.get("book_recv_t") or r["t"], "src": fam, "ticker": r["ticker"]})
    out = {}
    for c, rows in by.items():
        rows.sort(key=lambda r: r["t"]); ded = []
        for r in rows:
            if ded and r["t"] - ded[-1]["t"] < 0.3: continue
            ded.append(r)
        out[c] = ded
    return out


# ------------------------------------------------------------------------------------------------------------- points
def ref_feats(grids, asset, idx, K_by_asset_close, close_ts) -> dict:
    out = {}
    for a, g in grids.items():
        if a == asset: continue
        r1 = Z.ret_bp(g, np.array([idx]), 1)[0]; r5 = Z.ret_bp(g, np.array([idx]), 5)[0]
        Ka = K_by_asset_close.get((a, close_ts)); pm = 1e4 * (g.x[idx] / Ka - 1) if Ka else np.nan
        out[a] = (float(r1), float(r5), float(pm))
    return out


def build_tape_a(grids, cfb: dict[str, tuple], kalshi: dict) -> tuple[list[Z.Point], dict]:
    """One Point per (asset, window, decision time). Label/strike in the OHLC4 proxy space (features and label consistent);
    the official label is recorded alongside where the feed covers the window (agreement reported)."""
    from predhft.candles import minute_avg_proxy
    pts = []; meta = collections.Counter(); K_by = {}; win_rows = {}
    for a, g in grids.items():
        df = pd.DataFrame({"ts": g.ts, "open": g.x, "high": g.x, "low": g.x, "close": g.x})       # placeholder; replaced by real candles below
        d = pd.read_parquet(ROOT / "data" / "candles" / f"{a}_1m.parquet").set_index("ts")
        first = (int(g.ts[0]) // 900 + 1) * 900; last = int(g.ts[-1]) // 900 * 900
        dp = 4 if a == "SOL" else 2
        for o in range(first, last - 900 + 1, 900):
            c = o + 900
            a0 = minute_avg_proxy(d, o); a1 = minute_avg_proxy(d, c)
            if a0 is None or a1 is None: continue
            K_by[(a, c)] = a0; win_rows[(a, c)] = (a0, a1)
    for a, g in grids.items():
        series, _ = cfb[a]; dp = 4 if a == "SOL" else 2
        for (aa, c), (a0, a1) in win_rows.items():
            if aa != a: continue
            y = int(a1 >= a0); src = "proxy"
            off_o, off_c = official_avg(series, c - 900, dp), official_avg(series, c, dp)
            y_off = int(off_c >= off_o) if (off_o is not None and off_c is not None) else None
            kr = kalshi.get((a, c)); y_k = (1 if kr["result"] == "yes" else 0) if kr else None
            meta[f"{a}_windows"] += 1
            if y_off is not None: meta[f"{a}_official_available"] += 1; meta[f"{a}_official_agrees_proxy"] += int(y_off == y)
            if y_k is not None: meta[f"{a}_kalshi_available"] += 1; meta[f"{a}_kalshi_agrees_proxy"] += int(y_k == y)
            for dm in DEC_MIN_A:
                t = c - dm * 60; idx = g.idx_last_closed(t)
                if idx < 400: continue
                m, V = Z.candle_terms(t, c)
                pts.append(Z.Point(a, c, float(t), float(c - t), a0, float(g.x[idx]), 0, None, m, V, idx, y, a1, src, tape="A",
                                   ref=ref_feats(grids, a, idx, K_by, c)))
    pts.sort(key=lambda p: (p.t, p.asset)); return pts, dict(meta)


def build_tape_b(grids, cfb, kalshi) -> tuple[list[Z.Point], dict[str, dict[int, list[dict]]], dict[str, dict[int, str]], dict]:
    pts = []; snaps = {}; results = {}; meta = collections.Counter(); K_by = {}
    now_sec_fn = {}
    for a in ASSETS:
        series, recv = cfb[a]; dp = 4 if a == "SOL" else 2
        sn = load_snapshots(a); res = {}
        for c, rows in sn.items():
            if c - 60 < series[0][0] + 900: continue
            K = rows[0]["K"]; K_by[(a, c)] = K
            kr = kalshi.get((a, c)); off = official_avg(series, c, dp)
            if kr: res[c] = {"result": kr["result"], "src": "kalshi", "avg": float(kr["expiration_value"]) if kr.get("expiration_value") else off}
            elif off is not None: res[c] = {"result": ("yes" if off >= K else "no"), "src": "official", "avg": off}
            if c in res: meta[f"{a}_closes"] += 1; meta[f"{a}_src_{res[c]['src']}"] += 1
        snaps[a] = {c: r for c, r in sn.items() if c in res}; results[a] = {c: r["result"] for c, r in res.items()}
        def now_sec_at(t, recv=recv, series=series):
            i = bisect.bisect_right(recv, t); return (series[i - 1][0] if i else None), (recv[i - 1] if i else None)
        now_sec_fn[a] = now_sec_at
        g = grids[a]
        for c, rows in snaps[a].items():
            y = 1 if res[c]["result"] == "yes" else 0
            for r in rows:
                ns, _ = now_sec_at(r["t"])
                if ns is None: continue
                terms = S.settlement_terms(series, c, ns)
                if terms["x_now"] is None or terms["m"] == 0: continue
                sig, _, _ = S.realized_sigma_1s(series, ns); idx = g.idx_last_closed(r["t"])
                if idx < 400: continue
                yb, ya = r["yes_bid"], r["yes_ask"]; mid = ((yb + ya) / 2) if (yb is not None and ya is not None and ya - yb <= S.CFG["blend_max_spread"]) else None
                pts.append(Z.Point(a, c, float(r["t"]), float(c - r["t"]), rows[0]["K"], float(terms["x_now"]), terms["n"], terms["partial"], terms["m"], float(terms["V"]), idx, y, res[c]["avg"], res[c]["src"],
                                   mid=mid, spread=(ya - yb) if mid is not None else None, sigma_feed=float(sig), book={"yes_bid": yb, "yes_ask": ya, "bids": r["bids"], "asks": r["asks"], "src": r["src"]}, tape="B",
                                   ref=ref_feats(grids, a, idx, K_by, c)))
    pts.sort(key=lambda p: (p.t, p.asset)); return pts, snaps, results, dict(meta), now_sec_fn


def mark_decision_points(pts: list[Z.Point]) -> np.ndarray:
    """Tape B: True for the snapshot nearest each pre-declared decision time within 10 s (one per (asset, close, decision time))."""
    flag = np.zeros(len(pts), dtype=bool); by = collections.defaultdict(list)
    for i, p in enumerate(pts): by[(p.asset, p.close_ts)].append(i)
    for key, ii in by.items():
        sl = np.array([pts[i].secs_left for i in ii])
        for d in DEC_S_B:
            j = int(np.argmin(np.abs(sl - d)))
            if abs(sl[j] - d) <= 10: flag[ii[j]] = True
    return flag


# ------------------------------------------------------------------------------------------------------------- folds
def tape_a_folds(pts: list[Z.Point]) -> list[tuple[float, float]]:
    t0 = min(p.t for p in pts); day0 = (int(t0) // 86400) * 86400
    start = day0 + TRAIN_ONLY_DAYS * 86400; end = max(p.t for p in pts) + 1
    folds = []; s = start
    while s < end: folds.append((float(s), float(min(s + FOLD_DAYS * 86400, end)))); s += FOLD_DAYS * 86400
    return folds


def tape_b_folds(pts: list[Z.Point]) -> list[tuple[float, float]]:
    closes = sorted({p.close_ts for p in pts}); n = len(closes); folds = []
    for k in range(B_FOLDS):
        lo = closes[k * n // B_FOLDS]; hi = closes[min(n - 1, (k + 1) * n // B_FOLDS - 1)]
        folds.append((float(lo - 900), float(hi + 1)))     # a fold starts at the open of its first close
    return folds


# ------------------------------------------------------------------------------------------------------------- run
def _stage_path(name: str) -> pathlib.Path: return OUT / name


def load_tapes(force: bool = False):
    """Stage 1: tapes (cached in reports/tournament/tapes.pkl). now_sec_fn closures are rebuilt from the cfb series."""
    grids = load_grids(); Z.set_grids(grids); cfb = {a: load_cfb(a) for a in ASSETS}; kalshi = load_kalshi_results()
    p = _stage_path("tapes.pkl")
    if p.exists() and not force:
        A, B, snaps, results, meta_a, meta_b = pickle.load(p.open("rb")); log("tapes loaded from cache", len(A), len(B))
    else:
        A, meta_a = build_tape_a(grids, cfb, kalshi); log("tape A points", len(A), meta_a)
        B, snaps, results, meta_b, _ = build_tape_b(grids, cfb, kalshi); log("tape B points", len(B), meta_b)
        pickle.dump((A, B, snaps, results, meta_a, meta_b), p.open("wb"))
    now_sec_fn = {}
    for a in ASSETS:
        series, recv = cfb[a]
        def now_sec_at(t, recv=recv, series=series):
            i = bisect.bisect_right(recv, t); return (series[i - 1][0] if i else None), (recv[i - 1] if i else None)
        now_sec_fn[a] = now_sec_at
    return grids, cfb, kalshi, A, B, snaps, results, meta_a, meta_b, now_sec_fn


def run_tournament(quick: bool = False, force: bool = False, stages: tuple = ("preds", "metrics", "economics", "forward")) -> dict:
    """Staged + resumable: every stage persists to reports/tournament/ and is skipped when its artifact exists; --force recomputes
    exactly the stages listed in --stages (tapes, preds, metrics, economics, forward)."""
    t_start = time.time()
    grids, cfb, kalshi, A, B, snaps, results, meta_a, meta_b, now_sec_fn = load_tapes(force and "tapes" in stages)
    log("grids", {a: (len(g.ts), str(dt.datetime.fromtimestamp(int(g.ts[0]), dt.timezone.utc)), str(dt.datetime.fromtimestamp(int(g.ts[-1]), dt.timezone.utc))) for a, g in grids.items()})
    if quick:
        A = [p for p in A if p.asset == "SOL"]; B = [p for p in B if p.asset == "SOL"]
    zoo = Z.build_zoo(); names = [m.name for m in zoo]; n_cfg = Z.n_configurations(zoo)
    log("configurations", n_cfg)
    folds_a = tape_a_folds(A); folds_b = tape_b_folds(B)
    if quick: folds_a = folds_a[-3:]
    log("folds A", len(folds_a), "folds B", len(folds_b))
    ens_avg = next(m for m in zoo if m.name == "ens_avg"); ens_stack = next(m for m in zoo if m.name == "ens_stack")
    base_models = [m for m in zoo if m.family != "ensemble"]
    ta = np.array([p.t for p in A]); ca = np.array([p.close_ts for p in A], dtype=float); ya = np.array([p.y for p in A])
    # ---------------------------------------------------------------- stage 2: predictions (per fold checkpoint)
    tag = "quick" if quick else "full"
    ck = _stage_path(f"preds_{tag}.npz")
    preds_a = {nm: np.full(len(A), np.nan) for nm in names}; preds_b = {nm: np.full(len(B), np.nan) for nm in names}
    fold_a = np.full(len(A), -1); fold_b = np.full(len(B), -1); done = set()
    if ck.exists() and not (force and "preds" in stages):
        z = np.load(ck, allow_pickle=True)
        if int(z["n_a"]) == len(A) and int(z["n_b"]) == len(B):
            for nm in names:
                if f"A:{nm}" in z: preds_a[nm] = z[f"A:{nm}"]; preds_b[nm] = z[f"B:{nm}"]
            fold_a = z["fold_a"]; fold_b = z["fold_b"]; done = set(z["done"].tolist()); log("resuming predictions; folds done:", sorted(done))

    def save_ck():
        np.savez(ck, n_a=len(A), n_b=len(B), fold_a=fold_a, fold_b=fold_b, done=np.array(sorted(done), dtype=object),
                 **{f"A:{nm}": v for nm, v in preds_a.items()}, **{f"B:{nm}": v for nm, v in preds_b.items()})

    def run_fold(tape, pts_all, preds, fold_arr, k, s, e, n_prior):
        if f"{tape}{k}" in done: return
        ii = [i for i, p in enumerate(pts_all) if s <= p.t < e]
        if not ii: done.add(f"{tape}{k}"); return
        pts = [pts_all[i] for i in ii]; fold_arr[ii] = k
        # training labels must be KNOWN before the fold starts: window closed at/before s (not merely decided before s)
        train = [A[j] for j in np.where(ca <= s)[0]]
        hist_mask = (ca <= s) & (fold_a >= 0)
        ctx = Z.FitContext(grids, s, e, train)
        for m in base_models:
            t0 = time.time()
            try:
                m.fit(ctx); preds[m.name][ii] = m.predict(pts, ctx)
            except Exception as ex:
                log(f"  {m.name} FAILED fold {k}: {type(ex).__name__}: {str(ex)[:120]}")
            if time.time() - t0 > 20: log(f"  {m.name} fold {k} {time.time() - t0:.0f}s")
        member = {nm: preds[nm][ii] for nm in Z.ENS_MEMBERS}
        preds["ens_avg"][ii] = ens_avg.predict_from(member)
        hist = {nm: preds_a[nm][hist_mask] for nm in Z.ENS_MEMBERS}
        okh = np.all([np.isfinite(hist[nm]) for nm in Z.ENS_MEMBERS], axis=0) if hist_mask.any() else np.array([], dtype=bool)
        ens_stack.fit_from({nm: hist[nm][okh] for nm in Z.ENS_MEMBERS}, ya[hist_mask][okh], n_prior)
        preds["ens_stack"][ii] = ens_stack.predict_from(member)
        done.add(f"{tape}{k}"); save_ck()
        log(f"tape {tape} fold {k + 1}/{len(folds_a) if tape == 'A' else len(folds_b)} points {len(pts)} train {len(train)} done {time.time() - t_start:.0f}s")

    if "preds" in stages:
        for k, (s, e) in enumerate(folds_a): run_fold("A", A, preds_a, fold_a, k, s, e, k)
        for k, (s, e) in enumerate(folds_b): run_fold("B", B, preds_b, fold_b, k, s, e, len(folds_a))
    # mid baseline on tape B
    preds_b["mid"] = np.array([p.mid if p.mid is not None else np.nan for p in B]); preds_a["mid"] = np.full(len(A), np.nan)
    dec_b = mark_decision_points(B)
    # ---------------------------------------------------------------- frames
    def frame(pts, preds, fold_arr, tape):
        df = pd.DataFrame({"tape": tape, "asset": [p.asset for p in pts], "close_ts": [p.close_ts for p in pts], "t": [p.t for p in pts], "secs_left": [p.secs_left for p in pts],
                           "zone": [Z.zone_of(p.secs_left) for p in pts], "y": [p.y for p in pts], "fold": fold_arr, "outcome_src": [p.outcome_src for p in pts], "mid": [p.mid if p.mid is not None else np.nan for p in pts]})
        for nm, v in preds.items(): df[f"p:{nm}"] = v
        return df
    dfa = frame(A, preds_a, fold_a, "A"); dfa = dfa[dfa.fold >= 0].reset_index(drop=True)
    dfb = frame(B, preds_b, fold_b, "B"); dfb["decision_pt"] = dec_b; dfb = dfb[dfb.fold >= 0].reset_index(drop=True)
    allp = pd.concat([dfa, dfb], ignore_index=True); allp.to_parquet(OUT / f"predictions_{tag}.parquet", index=False)
    model_names = names + ["mid"]
    res_path = _stage_path(f"results_{tag}.json")
    res = json.loads(res_path.read_text(encoding="utf-8")) if res_path.exists() else {}
    res.update({"stamp_pt": dt.datetime.now(PT).isoformat(timespec="seconds"), "n_configurations": n_cfg, "models": model_names, "meta_a": meta_a, "meta_b": meta_b,
                "folds_a": [(str(dt.datetime.fromtimestamp(s, dt.timezone.utc)), str(dt.datetime.fromtimestamp(e, dt.timezone.utc))) for s, e in folds_a],
                "folds_b": [(str(dt.datetime.fromtimestamp(s, dt.timezone.utc)), str(dt.datetime.fromtimestamp(e, dt.timezone.utc))) for s, e in folds_b],
                "n_points_a_oos": int(len(dfa)), "n_points_b": int(len(dfb)), "n_points_b_decision": int(dfb.decision_pt.sum()), "stages_done": sorted(set(res.get("stages_done", [])) | {"preds"}),
                "model_cfg": {m.name: {"family": m.family, **{k: (v if isinstance(v, (int, float, str, list)) else str(v)) for k, v in m.cfg.items()}} for m in zoo}})
    def persist():
        res_path.write_text(json.dumps(res, indent=1, default=_json_default), encoding="utf-8")
    persist()
    # ---------------------------------------------------------------- stage 3: metrics + SPA
    dfd = dfb[dfb.decision_pt]
    if "metrics" in stages and ("metrics" not in res["stages_done"] or force):
        res["tables"] = {}
        res["tables"]["A_vs_solv1_candle"] = score_table(dfa, model_names, "solv1_candle", by_asset=True)
        res["tables"]["B_vs_mid"] = score_table(dfd, model_names, "mid", by_asset=True)
        res["tables"]["B_vs_solv1_exact"] = score_table(dfd, model_names, "solv1_exact", by_asset=True)
        res["tables"]["B_all_snapshots_vs_mid"] = score_table(dfb, model_names, "mid", by_asset=False)
        log("score tables done", f"{time.time() - t_start:.0f}s")
        res["spa"] = {}
        strat_models = [nm for nm in names]
        for label, df_, bench in (("B_SOL_vs_mid", dfd[dfd.asset == "SOL"], "mid"), ("B_pooled_vs_mid", dfd, "mid"), ("B_SOL_vs_solv1_exact", dfd[dfd.asset == "SOL"], "solv1_exact"),
                                  ("A_SOL_vs_solv1_candle", dfa[dfa.asset == "SOL"], "solv1_candle"), ("A_pooled_vs_solv1_candle", dfa, "solv1_candle")):
            D, snames, closes = diff_matrix(df_, strat_models, bench)
            if D is None: res["spa"][label] = {"error": "no data"}; continue
            res["spa"][label] = Z.spa_test(D, B=4000, mean_block=4.0, names=snames)
            res["spa"][label]["n_strategies_submitted"] = len(snames)
            res["spa"][label]["per_zone"] = {}
            for zn in Z.ZONES:
                Dz, nz, _ = diff_matrix(df_[df_.zone == zn], strat_models, bench, zones=False)
                if Dz is not None: res["spa"][label]["per_zone"][zn] = {k: v for k, v in Z.spa_test(Dz, B=2000, mean_block=4.0, names=nz).items() if k != "t_stats"}
            log("SPA", label, {k: res["spa"][label].get(k) for k in ("best", "best_mean_diff", "best_t", "p_rc_white", "p_spa_consistent", "n_closes", "n_strategies")})
        res["stages_done"] = sorted(set(res["stages_done"]) | {"metrics"}); persist()
    # ---------------------------------------------------------------- stage 4: economics (SOL tape B), incremental per model
    if "economics" in stages and ("economics" not in res["stages_done"] or force):
        res.setdefault("economics", {})
        sol_b = [i for i, p in enumerate(B) if p.asset == "SOL" and fold_b[i] >= 0]
        pre = {(B[i].close_ts, B[i].t): (B[i].sigma_feed, {"n": B[i].n_obs, "partial": B[i].partial, "m": B[i].m, "x_now": B[i].x_now, "V": B[i].V}) for i in sol_b}
        if sol_b:
            for nm in names:
                if nm in res["economics"] and not force: continue
                p_of = {(B[i].close_ts, B[i].t): float(preds_b[nm][i]) for i in sol_b if np.isfinite(preds_b[nm][i])}
                res["economics"][nm] = {"blend": Z.replay_policy(snaps["SOL"], p_of, results["SOL"], cfb["SOL"][0], now_sec_fn["SOL"], blend=True, pre=pre),
                                        "raw": Z.replay_policy(snaps["SOL"], p_of, results["SOL"], cfb["SOL"][0], now_sec_fn["SOL"], blend=False, pre=pre)}
                persist()
            log("economics done", f"{time.time() - t_start:.0f}s")
        res["stages_done"] = sorted(set(res["stages_done"]) | {"economics"}); persist()
    # ---------------------------------------------------------------- ranking
    if "tables" in res and "spa" in res: res["ranking"] = ranking(res)
    # ---------------------------------------------------------------- stage 5: forward models: fit on everything (for the scored-only hook)
    if "forward" in stages and "ranking" in res and ("forward" not in res["stages_done"] or force):
        try:
            end = max(p.close_ts for p in A) + 1; ctx_all = Z.FitContext(grids, end, end + 7 * 86400, [p for p in A])
            fwd = {}
            for nm in forward_candidates(res):
                m = next((m for m in zoo if m.name == nm), None)
                if m is None or m.family == "ensemble": continue
                m.fit(ctx_all); fwd[nm] = m
            with (OUT / "forward_models.pkl").open("wb") as fh: pickle.dump({"fitted_at": time.time(), "train_end_ts": end, "models": fwd, "grids_end_ts": int(grids["SOL"].ts[-1])}, fh)
            res["forward_models"] = list(fwd); res["stages_done"] = sorted(set(res["stages_done"]) | {"forward"})
        except Exception as ex:
            res["forward_models_error"] = f"{type(ex).__name__}: {ex}"
    res["runtime_s"] = round(time.time() - t_start, 1); persist()
    (OUT / f"results_{tag}_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(json.dumps(res, indent=1, default=_json_default), encoding="utf-8")
    if "ranking" in res:
        (OUT / f"summary_{tag}.md").write_text(summary_md(res), encoding="utf-8"); print(summary_md(res))
    return res


def _json_default(o):
    if isinstance(o, (np.integer,)): return int(o)
    if isinstance(o, (np.floating,)): return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.ndarray): return o.tolist()
    return str(o)


def diff_matrix(df: pd.DataFrame, models: list[str], bench: str, zones: bool = True):
    """Per-close mean loss differential (Brier(bench) - Brier(model)) matrix: rows = closes, cols = strategies (model x zone + pooled)."""
    if not len(df) or f"p:{bench}" not in df: return None, None, None
    d = df[np.isfinite(df[f"p:{bench}"])]
    if not len(d): return None, None, None
    closes = sorted(d.close_ts.unique()); cidx = {c: i for i, c in enumerate(closes)}; cols = []; names = []
    lb = Z.brier(d[f"p:{bench}"].values, d.y.values)
    for nm in models:
        if nm == bench or f"p:{nm}" not in d: continue
        pm = d[f"p:{nm}"].values; ok = np.isfinite(pm)
        if ok.sum() < 30: continue
        diff = np.where(ok, lb - Z.brier(np.where(ok, pm, 0.5), d.y.values), np.nan)
        groups = [("pooled", np.ones(len(d), dtype=bool))] + ([(z, (d.zone.values == z)) for z in Z.ZONES] if zones else [])
        for zn, mask in groups:
            col = np.full(len(closes), np.nan); acc = collections.defaultdict(list)
            for v, c, mk in zip(diff, d.close_ts.values, mask):
                if mk and np.isfinite(v): acc[c].append(v)
            for c, vs in acc.items(): col[cidx[c]] = float(np.mean(vs))
            if np.sum(np.isfinite(col)) >= 30: cols.append(col); names.append(f"{nm}|{zn}" if zones else nm)
    if not cols: return None, None, None
    return np.column_stack(cols), names, closes


def score_table(df: pd.DataFrame, models: list[str], bench: str, by_asset: bool) -> dict:
    out = {}
    groups = [("pooled", df)] + ([(a, df[df.asset == a]) for a in ASSETS] if by_asset else [])
    for gname, d in groups:
        if not len(d): continue
        out[gname] = {}
        for zn in ["pooled"] + list(Z.ZONES):
            dz = d if zn == "pooled" else d[d.zone == zn]
            if not len(dz): continue
            pb = dz[f"p:{bench}"].values if f"p:{bench}" in dz else np.full(len(dz), np.nan)
            rows = {}
            for nm in models:
                if f"p:{nm}" not in dz: continue
                pm = dz[f"p:{nm}"].values; ok = np.isfinite(pm) & np.isfinite(pb)
                okm = np.isfinite(pm)
                if okm.sum() < 20: continue
                y = dz.y.values
                row = {"n": int(okm.sum()), "closes": int(dz.close_ts[okm].nunique()), "brier": float(np.mean(Z.brier(pm[okm], y[okm]))), "logloss": float(np.mean(Z.logloss(pm[okm], y[okm]))),
                       "cal_slope": Z.calibration_slope(pm[okm], y[okm])}
                if ok.sum() >= 20:
                    diff = Z.brier(pb[ok], y[ok]) - Z.brier(pm[ok], y[ok])        # positive = model better than bench
                    pc = Z.per_close_mean(diff, dz.close_ts.values[ok]); vals = np.array(list(pc.values())); cl = np.array(list(pc.keys()))
                    se_c = float(vals.std(ddof=1) / math.sqrt(len(vals))) if len(vals) > 1 else None
                    se_h = Z.cluster_se(vals, (cl // 3600)) if len(vals) > 1 else None
                    row.update({"n_paired": int(ok.sum()), "paired_closes": len(vals), f"brier_{bench}": float(np.mean(Z.brier(pb[ok], y[ok]))), "diff_vs_bench": float(vals.mean()),
                                "se_close": se_c, "se_hour": se_h, "t_close": (float(vals.mean() / se_c) if se_c else None), "t_hour": (float(vals.mean() / se_h) if se_h else None),
                                "logloss_diff_vs_bench": float(np.mean(Z.logloss(pb[ok], y[ok])) - np.mean(Z.logloss(pm[ok], y[ok])))})
                rows[nm] = row
            out[gname][zn] = rows
    return out


def ranking(res: dict) -> list[dict]:
    """Ranked table: every model, tape A pooled skill vs solv1_candle, tape B SOL + pooled paired diff vs mid (with t), SPA t, economics."""
    rows = []
    tb = res["tables"]["B_vs_mid"]; ta = res["tables"]["A_vs_solv1_candle"]; sp = res["spa"].get("B_SOL_vs_mid", {}); spp = res["spa"].get("B_pooled_vs_mid", {})
    for nm in res["models"]:
        r = {"model": nm, "family": res["model_cfg"].get(nm, {}).get("family", "baseline")}
        a = ta.get("pooled", {}).get("pooled", {}).get(nm, {}); r.update({"A_brier": a.get("brier"), "A_diff_vs_solv1_candle": a.get("diff_vs_bench"), "A_t_close": a.get("t_close"), "A_n": a.get("n")})
        for key, g in (("B_SOL", "SOL"), ("B_pooled", "pooled")):
            b = tb.get(g, {}).get("pooled", {}).get(nm, {})
            r.update({f"{key}_brier": b.get("brier"), f"{key}_diff_vs_mid": b.get("diff_vs_bench"), f"{key}_t_close": b.get("t_close"), f"{key}_t_hour": b.get("t_hour"), f"{key}_n": b.get("n_paired")})
            for zn in Z.ZONES:
                bz = tb.get(g, {}).get(zn, {}).get(nm, {}); r[f"{key}_{zn}_diff"] = bz.get("diff_vs_bench"); r[f"{key}_{zn}_t"] = bz.get("t_close")
        r["spa_t_B_SOL_pooled"] = (sp.get("t_stats") or {}).get(f"{nm}|pooled"); r["spa_t_B_pooled_pooled"] = (spp.get("t_stats") or {}).get(f"{nm}|pooled")
        ec = res["economics"].get(nm, {})
        for v in ("blend", "raw"):
            e = ec.get(v, {}); r[f"econ_{v}_trades"] = e.get("trades"); r[f"econ_{v}_c_per_ct"] = e.get("c_per_ct"); r[f"econ_{v}_se"] = e.get("se_c_per_ct_close"); r[f"econ_{v}_net"] = e.get("net_usd"); r[f"econ_{v}_sharpe"] = e.get("sharpe_per_15min"); r[f"econ_{v}_dd"] = e.get("max_drawdown_usd")
        rows.append(r)
    rows.sort(key=lambda r: -(r["B_SOL_diff_vs_mid"] if r["B_SOL_diff_vs_mid"] is not None else -9))
    for i, r in enumerate(rows): r["rank_B_SOL_diff"] = i + 1
    return rows


def forward_candidates(res: dict) -> list[str]:
    """Top-3 non-baseline, non-ensemble models by tape-B SOL pooled paired diff vs mid (pre-declared hook content), plus the
    tape-A pooled champion vs solv1_candle (the only comparison where anything survived the correction), de-duplicated."""
    out = [r["model"] for r in res["ranking"] if r["family"] not in ("baseline", "ensemble") and r["B_SOL_diff_vs_mid"] is not None][:3]
    ra = sorted([r for r in res["ranking"] if r["family"] not in ("baseline", "ensemble") and r.get("A_diff_vs_solv1_candle") is not None], key=lambda r: -r["A_diff_vs_solv1_candle"])
    for r in ra[:1]:
        if r["model"] not in out: out.append(r["model"])
    return out


def summary_md(res: dict) -> str:
    L = [f"# Tournament summary ({res['stamp_pt']}) — configurations tried: {res['n_configurations']} (+ mid); points A OOS {res['n_points_a_oos']}, B {res['n_points_b']} (decision-time subset {res['n_points_b_decision']})", ""]
    L.append("| rank | model | family | A Brier | A Δ vs solv1_candle (t) | B-SOL Brier | B-SOL Δ vs mid (t close / t hour) | B-SOL Δ final / mid / early | B-pooled Δ vs mid (t) | econ blend c/ct ± SE (n) | econ raw c/ct ± SE (n) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    f = lambda v, d=4: ("—" if v is None else (f"{v:.{d}f}" if isinstance(v, float) else str(v)))
    for r in res["ranking"]:
        L.append(f"| {r['rank_B_SOL_diff']} | {r['model']} | {r['family']} | {f(r['A_brier'])} | {f(r['A_diff_vs_solv1_candle'])} ({f(r['A_t_close'], 1)}) | {f(r['B_SOL_brier'])} | {f(r['B_SOL_diff_vs_mid'])} ({f(r['B_SOL_t_close'], 1)} / {f(r['B_SOL_t_hour'], 1)}) | "
                 f"{f(r['B_SOL_final_diff'])} / {f(r['B_SOL_mid_diff'])} / {f(r['B_SOL_early_diff'])} | {f(r['B_pooled_diff_vs_mid'])} ({f(r['B_pooled_t_close'], 1)}) | "
                 f"{f(r['econ_blend_c_per_ct'], 1)} ± {f(r['econ_blend_se'], 1)} ({f(r['econ_blend_trades'])}) | {f(r['econ_raw_c_per_ct'], 1)} ± {f(r['econ_raw_se'], 1)} ({f(r['econ_raw_trades'])}) |")
    L.append("")
    for label, s in res["spa"].items():
        if "error" in s: L.append(f"SPA {label}: {s['error']}"); continue
        L.append(f"SPA {label}: closes {s['n_closes']}, strategies {s['n_strategies']} (submitted {s.get('n_strategies_submitted')}), best `{s['best']}` mean Δ {s['best_mean_diff']:+.4f} t {s['best_t']:.2f} raw p {s['best_raw_p_one_sided']:.3f} | studentised: RC p {s['p_rc_white']:.3f}, SPA-c p {s['p_spa_consistent']:.3f} (lower {s['p_spa_lower']:.3f}, upper {s['p_spa_upper']:.3f}) | **non-studentised (calibrated): RC p {s.get('p_rc_white_nonstud', float('nan')):.3f}, SPA-c p {s.get('p_spa_consistent_nonstud', float('nan')):.3f}** (best by mean `{s.get('best_nonstud')}`) | #positive means {s['n_with_positive_mean']}")
        for zn, sz in s.get("per_zone", {}).items():
            if "error" in sz: continue
            L.append(f"    zone {zn}: closes {sz['n_closes']}, best `{sz['best']}` Δ {sz['best_mean_diff']:+.4f} t {sz['best_t']:.2f} | stud. RC p {sz['p_rc_white']:.3f} SPA p {sz['p_spa_consistent']:.3f} | non-stud. RC p {sz.get('p_rc_white_nonstud', float('nan')):.3f} SPA p {sz.get('p_spa_consistent_nonstud', float('nan')):.3f}")
    L.append(""); L.append(f"forward hook models: {res.get('forward_models')}; runtime {res.get('runtime_s')} s")
    return "\n".join(L)


# ------------------------------------------------------------------------------------------------------------- forward hook (scored-only)
FWD_OUT = ROOT / "data" / "tournament" / "forward.jsonl"


def forward_loop(poll_s: float = 2.0, once: bool = False, from_start: bool = False):
    """Tail data/sol-v1/snapshots.jsonl (SOL-v1's own entry evaluations) and log the forward candidates' probabilities next to
    SOL-v1's p_model / p_yes and the mid, for every live SOL evaluation. Scored-only: nothing here can trade."""
    from predhft.candles import fetch_1m
    with (OUT / "forward_models.pkl").open("rb") as fh: bundle = pickle.load(fh)
    models: dict[str, Z.Model] = bundle["models"]; log("forward hook models", list(models))
    snap_path = ROOT / "data" / "sol-v1" / "snapshots.jsonl"; off = 0 if from_start else (snap_path.stat().st_size if snap_path.exists() else 0)
    train_end = int(bundle["train_end_ts"]); n_rows = 0
    grids = None; last_grid = 0.0
    while True:
        now = time.time()
        if grids is None or now - last_grid > 300:
            try:
                for a in ASSETS: fetch_1m(a, now - 5 * 86400, now)
                grids = load_grids(); Z.set_grids(grids); last_grid = now
                for m in models.values(): m.extend(grids)
            except Exception as ex:
                log("grid refresh failed", type(ex).__name__, str(ex)[:100]); grids = grids or load_grids(); Z.set_grids(grids)
                for m in models.values(): m.extend(grids)
        if snap_path.exists() and snap_path.stat().st_size > off:
            with snap_path.open("rb") as fh:
                fh.seek(off); chunk = fh.read(); off = fh.tell()
            for l in chunk.decode("utf-8", "replace").splitlines():
                try: r = json.loads(l)
                except Exception: continue
                if r.get("kind") != "entry_eval" or r.get("x_now") is None or r.get("V") is None: continue
                g = grids["SOL"]; idx = g.idx_last_closed(r["t"])
                if idx < 400: continue
                pt = Z.Point("SOL", int(r["close_ts"]), float(r["t"]), float(r["secs_left"]), float(r["K"]), float(r["x_now"]), int(r["n_obs"]), r.get("partial"), int(r["m_rem"]), float(r["V"]), idx, None, None, "live",
                             mid=r.get("mid"), sigma_feed=r.get("sigma_1s"), tape="F", ref=ref_feats(grids, "SOL", idx, {}, int(r["close_ts"])))
                ctx = Z.FitContext(grids, r["t"], r["t"] + 1, [])
                row = {"t": r["t"], "ticker": r.get("ticker"), "close_ts": r["close_ts"], "secs_left": r["secs_left"], "K": r["K"], "x_now": r["x_now"], "mid": r.get("mid"), "yes_bid": r.get("yes_bid"), "yes_ask": r.get("yes_ask"),
                       "p_solv1_model": r.get("p_model"), "p_solv1_used": r.get("p_yes"), "solv1_decision": r.get("decision"), "sigma_1s_feed": r.get("sigma_1s"), "candle_idx_ts": int(g.ts[idx]),
                       "train_end_ts": train_end, "forward": bool(r["t"] >= train_end), "zoo": {}}
                for nm, m in models.items():
                    try: row["zoo"][nm] = float(m.predict([pt], ctx)[0])
                    except Exception as ex: row["zoo"][nm] = None; row.setdefault("errors", {})[nm] = f"{type(ex).__name__}: {str(ex)[:80]}"
                FWD_OUT.parent.mkdir(parents=True, exist_ok=True)
                with FWD_OUT.open("a", encoding="utf-8") as fh: fh.write(json.dumps(row) + "\n")
                n_rows += 1
        if once: log("forward hook once: rows written", n_rows); return
        time.sleep(poll_s)


def forward_score() -> dict:
    """Paired Brier of the logged forward probabilities vs mid and vs SOL-v1 on settled closes (official feed average).
    Rows are averaged per close first; mean / SE / t are across closes (close-clustered), so the thousands of per-second
    evaluations inside one window count once. Rows logged before the models' training end are reported separately as dev."""
    if not FWD_OUT.exists(): return {"n": 0}
    series, _ = load_cfb("SOL"); rows = [json.loads(l) for l in FWD_OUT.open(encoding="utf-8") if l.strip()]
    out = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(list))); n_settled = 0; closes = set(); n_dev = 0
    for r in rows:
        if not r.get("forward", True): n_dev += 1; continue            # rows before the models' training end are NOT forward evidence
        c = int(r["close_ts"]); avg = official_avg(series, c, 4)
        if avg is None: continue
        y = 1.0 if avg >= r["K"] else 0.0; n_settled += 1; closes.add(c); zn = Z.zone_of(r["secs_left"])
        for nm, p in r["zoo"].items():
            if p is None: continue
            for zz in ("pooled", zn):
                out[nm][f"{zz}|brier"][c].append((p - y) ** 2)
                if r.get("mid") is not None: out[nm][f"{zz}|diff_vs_mid"][c].append((r["mid"] - y) ** 2 - (p - y) ** 2)
                if r.get("p_solv1_model") is not None: out[nm][f"{zz}|diff_vs_solv1"][c].append((r["p_solv1_model"] - y) ** 2 - (p - y) ** 2)
    def agg(d):
        pc = np.array([np.mean(v) for v in d.values()]); se = float(pc.std(ddof=1) / math.sqrt(len(pc))) if len(pc) > 1 else None
        return {"rows": int(sum(len(v) for v in d.values())), "closes": len(pc), "mean": float(pc.mean()), "se_close": se, "t_close": (float(pc.mean() / se) if se else None)}
    return {"n_rows_settled": n_settled, "n_rows_dev_period_ignored": n_dev, "closes": len(closes), "by_model": {nm: {k: agg(d) for k, d in dd.items()} for nm, dd in out.items()}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); ap.add_argument("mode", choices=["run", "forward", "forward-score"]); ap.add_argument("--quick", action="store_true"); ap.add_argument("--once", action="store_true"); ap.add_argument("--force", action="store_true"); ap.add_argument("--from-start", action="store_true"); ap.add_argument("--stages", default="preds,metrics,economics,forward")
    a = ap.parse_args()
    if a.mode == "run": run_tournament(a.quick, a.force, tuple(a.stages.split(",")))
    elif a.mode == "forward": forward_loop(once=a.once, from_start=a.from_start)
    else: print(json.dumps(forward_score(), indent=1))
