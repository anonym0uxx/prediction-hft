"""SOL-v1 development replay over the recorded SOL tape, SAME code path as the live policy (predhft.sol_v1.decide / paper_fill /
hold_or_exit / settle_from_feed). docs/SOL_V1_2026-10-28.md.

Tape: data/open_quotes/quotes.jsonl (SOL rows, 30-s cadence open -> T-60 s), data/s7p15-KXSOL15M/books.jsonl and
data/s7p13-KXSOL15M/books.jsonl (final ~2 min, ~1 s), data/cfb/SOL.jsonl (official 1 Hz index, receipt-stamped). Results from
data/ts/results.json + the feed's own 60-s average + Kalshi REST for the rest (public, shared limiter; --no-fetch to skip).

Outputs: reports/sol_v1/replay_<stamp>.json (+ .md summary printed). Time-ordered split: first --dev-frac of closes = development
(KAPPA calibration), the rest = validation. The live forward boundary is the start timestamp of the deployed task (not here).

Usage: uv run python scripts/sol_v1_replay.py [--dev-frac 0.6] [--kappa auto|1.25] [--edge-base 3] [--no-fetch]
"""
from __future__ import annotations
import argparse, bisect, collections, json, math, pathlib, statistics as st, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft import sol_v1 as S
from predhft.stats_spec import Phi

OUT = pathlib.Path("reports/sol_v1"); RESULTS = pathlib.Path("data/ts/results.json")
TAPES = [pathlib.Path("data/s7p15-KXSOL15M/books.jsonl"), pathlib.Path("data/s7p13-KXSOL15M/books.jsonl")]
QUOTES = pathlib.Path("data/open_quotes/quotes.jsonl"); CFB = pathlib.Path("data/cfb/SOL.jsonl")

def load_cfb():
    rows = []
    for l in CFB.open(encoding="utf-8"):
        try: d = json.loads(l)
        except Exception: continue
        if d.get("value") and d.get("value_ts_ms"): rows.append((d["t"], int(d["value_ts_ms"]) // 1000, float(d["value"])))
    rows.sort(key=lambda r: r[1])
    series = []; recv = []; last = None
    for t, s, v in rows:
        if s == last: series[-1] = (s, v); recv[-1] = t; continue
        series.append((s, v)); recv.append(t); last = s
    return series, recv

def load_snapshots() -> dict[int, list[dict]]:
    """close_ts -> time-ordered book snapshots {t, K, yes_bid, yes_ask, bids, asks, recv_t, src}."""
    by: dict[int, list[dict]] = collections.defaultdict(list)
    for l in QUOTES.open(encoding="utf-8"):
        try: r = json.loads(l)
        except Exception: continue
        if r.get("asset") != "SOL" or r.get("orderbook_error") or not r.get("strike"): continue
        by[int(r["close_ts"])].append({"t": r["t"], "K": float(r["strike"]), "yes_bid": r.get("yes_bid"), "yes_ask": r.get("yes_ask"), "bids": r.get("bids") or [], "asks": r.get("asks") or [],
                                       "recv_t": r["t"], "src": "quotes", "ticker": r["ticker"]})
    for p in TAPES:
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
            by[int(r["close_ts"])].append({"t": r["t"], "K": float(r["K"]), "yes_bid": yb, "yes_ask": ya, "bids": bids, "asks": asks, "recv_t": r.get("book_recv_t") or r["t"], "src": p.parent.name, "ticker": r["ticker"]})
    out = {}
    for c, rows in by.items():
        rows.sort(key=lambda r: r["t"]); ded = []
        for r in rows:
            if ded and r["t"] - ded[-1]["t"] < 0.3: continue
            ded.append(r)
        out[c] = ded
    return out

def load_results(closes: dict[int, list[dict]], series, fetch: bool) -> dict[int, dict]:
    cache = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    res = {}
    missing = []
    for c, rows in closes.items():
        tk = rows[0]["ticker"]; K = rows[0]["K"]
        own = S.settle_from_feed(series, c, K)
        r = cache.get(tk)
        if r and r.get("result") in ("yes", "no"): res[c] = {"result": r["result"], "src": "kalshi", "expiration_value": r.get("expiration_value"), "feed": own}
        elif own: res[c] = {"result": own["result"], "src": "feed", "expiration_value": None, "feed": own}
        else: missing.append((c, tk, K))
    if missing and fetch:
        from predhft.kalshi_public import KalshiPublic
        k = KalshiPublic(min_interval_s=0.5)
        for c, tk, K in missing:
            try:
                m = k.get(f"/markets/{tk}")["market"]
                if m.get("result") in ("yes", "no"):
                    res[c] = {"result": m["result"], "src": "kalshi", "expiration_value": m.get("expiration_value"), "feed": None}
                    cache[tk] = {"result": m["result"], "expiration_value": m.get("expiration_value"), "floor_strike": K, "close_ts": c}
            except Exception as e: print("fetch fail", tk, e)
        RESULTS.write_text(json.dumps(cache), encoding="utf-8")
    return res

def cl_se(vals, clusters):
    n = len(vals)
    if n < 2: return None
    m = sum(vals) / n; by = collections.defaultdict(float)
    for v, c in zip(vals, clusters): by[c] += v - m
    G = len(by)
    return (math.sqrt(sum(s * s for s in by.values()) * G / (G - 1)) / n) if G > 1 else None

def bucket(secs_left): return int(min(max(secs_left, 0) // 60, 14))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--dev-frac", type=float, default=0.6); ap.add_argument("--edge-base", type=float, default=S.CFG["edge_base_c"])
    ap.add_argument("--kappa-final", type=float, default=S.CFG["kappa_final"]); ap.add_argument("--kappa-early", type=float, default=S.CFG["kappa_early"])
    ap.add_argument("--blend", default=None, help="w_final,w_mid,w_early (default: CFG)")
    ap.add_argument("--no-fetch", action="store_true"); ap.add_argument("--forward-after", type=float, default=None, help="unix ts: closes after this are excluded (live forward period)")
    a = ap.parse_args()
    series, recv = load_cfb(); closes = load_snapshots()
    closes = {c: r for c, r in closes.items() if series and c - 60 >= series[0][0] + 900}      # need feed history for vol
    if a.forward_after: closes = {c: r for c, r in closes.items() if c <= a.forward_after}
    results = load_results(closes, series, not a.no_fetch)
    closes = {c: r for c, r in closes.items() if c in results}
    order = sorted(closes); n_dev = int(round(len(order) * a.dev_frac)); dev = set(order[:n_dev]); val = set(order[n_dev:])
    print(f"closes {len(order)} dev {len(dev)} val {len(val)} snapshots {sum(len(v) for v in closes.values())} feed secs {len(series)}")
    recv_sorted = recv
    def now_sec_at(t):
        i = bisect.bisect_right(recv_sorted, t); return series[i - 1][0] if i else None, (recv_sorted[i - 1] if i else None)
    def settle_avg(c):
        f = results[c].get("feed"); ev = results[c].get("expiration_value")
        if ev: return float(ev)
        return f["avg"] if f else None
    # ---------- 1. calibration residuals (kappa=1) by horizon bucket, dev split
    resid = collections.defaultdict(list)
    for c in order:
        Sv = settle_avg(c)
        if Sv is None: continue
        for r in closes[c]:
            ns, _ = now_sec_at(r["t"])
            if ns is None: continue
            sig, n, src = S.realized_sigma_1s(series, ns)
            if src != "realized": continue
            terms = S.settlement_terms(series, c, ns); pr = S.p_settle_ge(terms, r["K"], sig, kappa=1.0)
            if pr["why"] != "model" or pr["sd_mean"] <= 0: continue
            zres = (Sv - pr["mu"]) / pr["sd_mean"]; resid[(c in dev, bucket(c - r["t"]))].append(zres)
    cal = {}
    for split in (True, False):
        zs_all = [z for (d, b), zs in resid.items() if d == split for z in zs]
        cal["dev" if split else "val"] = {"n": len(zs_all), "kappa_pooled": (math.sqrt(st.fmean(z * z for z in zs_all)) if zs_all else None),
                                           "by_bucket": {b: {"n": len(zs), "kappa": math.sqrt(st.fmean(z * z for z in zs)), "mean_z": st.fmean(zs), "share_abs_gt2": sum(1 for z in zs if abs(z) > 2) / len(zs)} for (d, b), zs in sorted(resid.items()) if d == split and zs}}
    cfg = json.loads(json.dumps(S.CFG)); cfg["kappa_final"] = a.kappa_final; cfg["kappa_early"] = a.kappa_early; cfg["edge_base_c"] = a.edge_base
    if a.blend: cfg["blend_w"] = dict(zip(("final", "mid", "early"), (float(x) for x in a.blend.split(","))))
    kappa = {"final": cfg["kappa_final"], "early": cfg["kappa_early"]}
    print(f"kappa dev pooled {cal['dev']['kappa_pooled']}; used {kappa}")
    # ---------- 1b. blend weight fit (log loss of w*p_model + (1-w)*mid) per horizon group, both splits, with the policy kappas
    bl = collections.defaultdict(list)
    for c in order:
        y = 1.0 if results[c]["result"] == "yes" else 0.0
        for r in closes[c]:
            if r["yes_bid"] is None or r["yes_ask"] is None or r["yes_ask"] - r["yes_bid"] > cfg["blend_max_spread"]: continue
            ns, _ = now_sec_at(r["t"])
            if ns is None: continue
            sl = c - r["t"]; sig, _, _ = S.realized_sigma_1s(series, ns); pr = S.p_settle_ge(S.settlement_terms(series, c, ns), r["K"], sig, S.kappa_for(sl, cfg))
            if pr["p"] is None: continue
            g = "final" if sl <= cfg["blend_split_s"][0] else ("mid" if sl <= cfg["blend_split_s"][1] else "early")
            bl[(g, "dev" if c in dev else "val")].append((pr["p"], (r["yes_bid"] + r["yes_ask"]) / 2, y, c))
    def ll(p, y): p = min(max(p, 1e-4), 1 - 1e-4); return -(y * math.log(p) + (1 - y) * math.log(1 - p))
    blend_fit = {}
    for (g, sp), rs in sorted(bl.items()):
        grid = {w / 10: {"logloss": round(st.fmean(ll(w / 10 * p + (1 - w / 10) * m, y) for p, m, y, _ in rs), 4), "brier": round(st.fmean((w / 10 * p + (1 - w / 10) * m - y) ** 2 for p, m, y, _ in rs), 4)} for w in range(11)}
        blend_fit[f"{g}|{sp}"] = {"n": len(rs), "closes": len({c for *_, c in rs}), "best_w_logloss": min(grid, key=lambda w: grid[w]["logloss"]), "grid": grid}
    print("blend fit (best w by log loss):", {k: (v["best_w_logloss"], v["n"]) for k, v in blend_fit.items()}, "used", cfg["blend_w"])
    # ---------- 2. paired Brier vs market mid + markouts, by minute bucket, both splits
    brier = collections.defaultdict(lambda: {"model": [], "mid": [], "cl": []}); mark = collections.defaultdict(lambda: {"v": [], "cl": []})
    size_fn = S.kelly_size_fn(lot_cap=cfg["lot"], min_lot=cfg["min_lot"])
    # ---------- 3. trading replay
    trades = []; evals = collections.Counter(); reasons = collections.Counter(); enter_by_bucket = collections.Counter(); eval_by_bucket = collections.Counter(); hold_exit_rows = 0; exits = 0
    ev_dist = []
    for c in order:
        rows = closes[c]; y = 1.0 if results[c]["result"] == "yes" else 0.0; split = "dev" if c in dev else "val"
        pos = None
        for i, r in enumerate(rows):
            ns, cfb_recv_t = now_sec_at(r["t"])
            if ns is None: continue
            book = {"yes_bid": r["yes_bid"], "yes_ask": r["yes_ask"], "bids": r["bids"], "asks": r["asks"], "src": r["src"]}
            rec = S.decide(r["t"], c, r["K"], series, cfb_recv_t, book, r["recv_t"], size_fn, cfg, latency_s=0.3, now_sec=ns)
            b = bucket(c - r["t"])
            if rec["p_model"] is not None and rec["mid"] is not None and r["yes_ask"] - r["yes_bid"] <= 0.10:   # raw model vs mid (the blend is scored in blend_fit)
                brier[(split, b)]["model"].append((rec["p_model"] - y) ** 2); brier[(split, b)]["mid"].append((rec["mid"] - y) ** 2); brier[(split, b)]["cl"].append(c)
                for dt_ in (30, 60, 120):
                    j = bisect.bisect_left([x["t"] for x in rows], r["t"] + dt_)
                    if j < len(rows) and rows[j]["t"] - (r["t"] + dt_) <= 10 and rows[j]["yes_bid"] is not None and rows[j]["yes_ask"] is not None:
                        mid2 = (rows[j]["yes_bid"] + rows[j]["yes_ask"]) / 2; sgn = 1.0 if rec["p_model"] > rec["mid"] else -1.0
                        mark[(split, b, dt_)]["v"].append(100 * sgn * (mid2 - rec["mid"])); mark[(split, b, dt_)]["cl"].append(c)
            if pos is None:
                evals[split] += 1; eval_by_bucket[(split, b)] += 1
                if rec["ev"]:
                    for s_, e in rec["ev"].items():
                        if e.get("ev_pc") is not None: ev_dist.append(round(100 * e["ev_pc"], 1))
                if rec["decision"] != "enter": reasons[(split, rec["reasons"][0] if rec["reasons"] else "?")] += 1; continue
                enter_by_bucket[(split, b)] += 1
                f = S.paper_fill(rec["side"], rec["qty"], rec["limit"], book)
                if f["qty"] <= 0: reasons[(split, "unfilled_at_limit")] += 1; continue
                surv = None
                if i + 1 < len(rows) and rows[i + 1]["t"] - r["t"] <= 5:
                    f2 = S.paper_fill(rec["side"], rec["qty"], rec["limit"], {"bids": rows[i + 1]["bids"], "asks": rows[i + 1]["asks"]}); surv = f2["qty"] >= f["qty"]
                pos = {"close_ts": c, "split": split, "t": r["t"], "secs_left": rec["secs_left"], "bucket": b, "side": rec["side"], "qty": f["qty"], "intended": rec["qty"], "px": f["px"], "cost": f["cost"], "fee": f["fee"],
                       "p_side": rec["p_side"], "p_model": rec["p_model"], "blend_w": rec["blend_w"], "mid": rec["mid"], "ev_pc": rec["ev_pc"], "survive_next": surv, "exit": None, "n_obs": rec["n_obs"], "x_now": rec["x_now"], "K": r["K"], "sigma_1s": rec["sigma_1s"]}
            else:
                if rec["p_yes"] is None: continue
                h = S.hold_or_exit(rec["p_yes"], pos["side"], book, pos["qty"], cfg["exit_margin_c"]); hold_exit_rows += 1
                if h["decision"] == "exit":
                    pos["exit"] = {"t": r["t"], "secs_left": round(c - r["t"], 1), "proceeds": h["exit_proceeds"], "px": h["exit_px"], "p_side": h["p_side"]}; exits += 1
                    pos["pnl"] = h["exit_proceeds"] - pos["cost"] - pos["fee"]; trades.append(pos); pos = None
        if pos is not None:
            win = 1.0 if ((results[c]["result"] == "yes") == (pos["side"] == "yes")) else 0.0
            pos["win"] = win; pos["pnl"] = win * pos["qty"] - pos["cost"] - pos["fee"]; trades.append(pos)
    # ---------- 4. portfolio accounting on fixed 15-min intervals incl. idle, conservative marks (settled-only here: no intra-window marks on tape)
    def port(split):
        tr = [t for t in trades if t["split"] == split]
        cl = sorted(c for c in order if (c in dev) == (split == "dev"))
        if not cl: return {}
        pnl_by_close = collections.defaultdict(float)
        for t in tr: pnl_by_close[t["close_ts"]] += t["pnl"]
        rets = [pnl_by_close.get(c, 0.0) / 250.0 for c in cl]       # one return per 15-min close incl. idle
        eq = 0.0; peak = 0.0; dd = 0.0; curve = []
        for c in cl: eq += pnl_by_close.get(c, 0.0); peak = max(peak, eq); dd = min(dd, eq - peak); curve.append(round(eq, 2))
        sd = st.pstdev(rets) if len(rets) > 1 else 0.0
        q = sum(t["qty"] for t in tr); net = sum(t["pnl"] for t in tr)
        return {"closes": len(cl), "trades": len(tr), "contracts": q, "net_usd": round(net, 2), "fees_usd": round(sum(t["fee"] for t in tr), 2), "c_per_ct": (round(100 * net / q, 2) if q else None),
                "se_c_per_ct_cluster_close": (round(100 * cl_se([t["pnl"] / t["qty"] for t in tr], [t["close_ts"] for t in tr]), 2) if len(tr) > 1 and cl_se([t["pnl"] / t["qty"] for t in tr], [t["close_ts"] for t in tr]) else None),
                "hit": (round(sum(1 for t in tr if t["pnl"] > 0) / len(tr), 3) if tr else None), "max_drawdown_usd": round(dd, 2), "sharpe_per_15min_interval": (round(st.fmean(rets) / sd, 3) if sd > 0 else None),
                "mean_ret_per_interval": (round(st.fmean(rets), 6) if rets else None), "exits": sum(1 for t in tr if t["exit"]), "survive_next_share": (round(sum(1 for t in tr if t["survive_next"]) / max(1, sum(1 for t in tr if t["survive_next"] is not None)), 2)),
                "n_survive_known": sum(1 for t in tr if t["survive_next"] is not None), "by_bucket": {b: {"n": sum(1 for t in tr if t["bucket"] == b), "net": round(sum(t["pnl"] for t in tr if t["bucket"] == b), 2)} for b in sorted({t["bucket"] for t in tr})}}
    out = {"stamp": time.time(), "closes": len(order), "dev_closes": len(dev), "val_closes": len(val), "dev_last_close": (max(dev) if dev else None), "val_first_close": (min(val) if val else None),
           "cfg": cfg, "calibration": cal, "kappa_used": kappa, "blend_fit": blend_fit, "results_src": collections.Counter(r["src"] for r in results.values()),
           "feed_vs_kalshi_result_mismatch": sum(1 for r in results.values() if r.get("feed") and r["src"] == "kalshi" and r["feed"]["result"] != r["result"]),
           "brier": {f"{s}|T-{b}m": {"n": len(v["model"]), "closes": len(set(v["cl"])), "brier_model": round(st.fmean(v["model"]), 4), "brier_mid": round(st.fmean(v["mid"]), 4),
                                     "diff_model_minus_mid": round(st.fmean(v["model"]) - st.fmean(v["mid"]), 4), "se_diff_close": (round(cl_se([a_ - b_ for a_, b_ in zip(v["model"], v["mid"])], v["cl"]), 4) if cl_se([a_ - b_ for a_, b_ in zip(v["model"], v["mid"])], v["cl"]) else None)}
                     for (s, b), v in sorted(brier.items()) if v["model"]},
           "markouts_c": {f"{s}|T-{b}m|+{d}s": {"n": len(v["v"]), "mean": round(st.fmean(v["v"]), 2), "se_close": (round(cl_se(v["v"], v["cl"]), 2) if cl_se(v["v"], v["cl"]) else None)} for (s, b, d), v in sorted(mark.items()) if v["v"]},
           "evals": dict(evals), "eval_by_bucket": {f"{s}|T-{b}m": n for (s, b), n in sorted(eval_by_bucket.items())}, "enter_by_bucket": {f"{s}|T-{b}m": n for (s, b), n in sorted(enter_by_bucket.items())},
           "abstain_reasons": {f"{s}|{r}": n for (s, r), n in sorted(reasons.items())}, "ev_pc_quantiles": ({q: round(st.quantiles(ev_dist, n=20)[i], 1) for q, i in (("p5", 0), ("p25", 4), ("p50", 9), ("p75", 14), ("p95", 18))} if len(ev_dist) > 20 else None),
           "hold_exit_evaluations": hold_exit_rows, "exits": exits, "portfolio": {"dev": port("dev"), "val": port("val")}, "trades": trades}
    OUT.mkdir(parents=True, exist_ok=True); stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    (OUT / f"replay_{stamp}.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8"); (OUT / "replay_latest.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k not in ("trades", "brier", "markouts_c", "cfg", "calibration", "blend_fit")}, indent=1, default=str))
    print("BRIER", json.dumps(out["brier"], indent=0)); print("MARKOUTS", json.dumps(out["markouts_c"], indent=0))
    for t in trades: print("TRADE", json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in t.items()}))

if __name__ == "__main__": main()
