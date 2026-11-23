"""Signal tests on our books since reset: imbalance, projection, market-vs-projection, time-of-day."""
import json, pathlib, math, collections, statistics as st, time
R = pathlib.Path("D:/prediction-hft/data"); S = pathlib.Path("D:/prediction-hft/reports/sharpe")
RESET = json.loads((R / "RESET.json").read_text())["reset_at"]
res = json.loads((S / "results.json").read_text())
Y = {t: (1.0 if v["result"] == "yes" else 0.0) for t, v in res.items() if v.get("result") in ("yes", "no")}
BASIS = {"KXBTC": 0.44, "KXETH": 0.7, "KXSOL": 1.2}
def basis(tk): return next((b for k, b in BASIS.items() if tk.startswith(k)), 0.0)
def cl_se(vals_by_cluster):
    # mean and cluster-robust se of a mean over all observations
    allv = [v for vs in vals_by_cluster.values() for v in vs]; n = len(allv); G = len(vals_by_cluster)
    if n == 0: return float("nan"), float("nan"), 0, 0
    mu = sum(allv) / n
    if G < 2: return mu, float("nan"), n, G
    se = math.sqrt(sum((sum(vs) - mu * len(vs)) ** 2 for vs in vals_by_cluster.values()) * G / (G - 1)) / n
    return mu, se, n, G
def load_books(glob):
    out = []
    for p in R.glob(glob):
        for l in p.read_text().splitlines():
            if not l.strip(): continue
            try: r = json.loads(l)
            except Exception: continue
            if r.get("t", 0) < RESET or "ticker" not in r or r.get("stale_fill"): continue
            if r["ticker"] not in Y: continue
            r["y"] = Y[r["ticker"]]; r["family"] = p.parent.name; out.append(r)
    return out
b15 = load_books("s7p1[35]-KX*15M/books.jsonl"); bH = load_books("s7p10-KXBTCD/books.jsonl") + load_books("s7p13-KXBTCD/books.jsonl") + load_books("s7final-KXBTCD/books.jsonl")
print("15M snaps", len(b15), "markets", len({r["ticker"] for r in b15}), "| hourly snaps", len(bH), "markets", len({r["ticker"] for r in bH}))
out = {}
# ---- Test 1: book imbalance residual predictor. contested rows only
def imb_test(rows, lo, hi, label):
    rr = [r for r in rows if lo <= r["mins_left"] <= hi and r.get("imb") is not None and 0 < r["yes_ask"] < 1 and r["yes_bid"] > 0 and 0.05 <= (r["yes_bid"] + r["yes_ask"]) / 2 <= 0.95]
    if not rr: print(label, "no rows"); return
    terc = sorted(rr, key=lambda r: r["imb"]); k = len(terc) // 3
    res_ = {}
    for name, part in (("lo", terc[:k]), ("mid", terc[k:2 * k]), ("hi", terc[2 * k:])):
        cl = collections.defaultdict(list)
        for r in part: cl[r["close_ts"]].append(r["y"] - (r["yes_bid"] + r["yes_ask"]) / 2)
        mu, se, n, G = cl_se(cl); res_[name] = dict(mean_resid=round(mu, 3), se=round(se, 3), n=n, G=G, imb_range=(round(part[0]["imb"], 2), round(part[-1]["imb"], 2)))
    # slope: regress resid on imb, cluster se via cluster-level jackknife approx (G clusters)
    xs = [r["imb"] for r in rr]; ys = [r["y"] - (r["yes_bid"] + r["yes_ask"]) / 2 for r in rr]
    mx, my = st.mean(xs), st.mean(ys); sxx = sum((x - mx) ** 2 for x in xs); beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else float("nan")
    cl = collections.defaultdict(list)
    for r, x, y in zip(rr, xs, ys): cl[r["close_ts"]].append((x - mx) * (y - my - beta * (x - mx)))
    G = len(cl); se_b = math.sqrt(sum(sum(v) ** 2 for v in cl.values()) * G / max(G - 1, 1)) / sxx if sxx else float("nan")
    res_["slope"] = dict(beta=round(beta, 3), se=round(se_b, 3), n=len(rr), G=G)
    print(label, json.dumps(res_)); out[label] = res_
imb_test(b15, 0.17, 1.5, "imb_15M_T-90..T-10s"); imb_test(b15, 0.17, 0.75, "imb_15M_T-45..T-10s")
imb_test(bH, 2.5, 3.5, "imb_hourly_T-3.5..T-2.5"); imb_test(bH, 0.15, 1.0, "imb_hourly_final_minute")
# ---- Test 2/3: projection sign vs outcome and vs market favourite, 15M, T-55..T-10 s
def proj_rows(rows, lo=10 / 60, hi=55 / 60):
    rr = []
    for r in rows:
        if not (lo <= r["mins_left"] <= hi) or not r.get("partial_twap") or r.get("partial_n", 0) < 3: continue
        bb = basis(r["ticker"]); sp = r["spot"] * (1 + bb * 1e-4); pt = r["partial_twap"] * (1 + bb * 1e-4)
        if r["family"].startswith("s7p13"): sp, pt = r["spot"], r["partial_twap"]   # p13 runner already applied basis? no: books snap stores corrected spot but partial? check: snap uses spot (corrected) and partial (corrected)
        rr_ = max(min(r["mins_left"], 1.0), 0.02); proj = (1 - rr_) * pt + rr_ * sp; d = 1e4 * (proj / r["K"] - 1)
        fav = r["yes_ask"] if (r["yes_bid"] + r["yes_ask"]) / 2 >= 0.5 else (1 - r["yes_bid"] if r["yes_bid"] > 0 else 1.0)
        mid = (r["yes_bid"] + r["yes_ask"]) / 2 if 0 < r["yes_ask"] < 1 else None
        rr.append(dict(r, d_bp=d, proj_side=1.0 if d >= 0 else 0.0, fav=fav, mid=mid))
    return rr
pr = proj_rows(b15)
print("proj rows", len(pr), "markets", len({r["ticker"] for r in pr}))
tab = {}
for lo_, hi_ in ((0, 0.5), (0.5, 1), (1, 2), (2, 4), (4, 1e9)):
    part = [r for r in pr if lo_ <= abs(r["d_bp"]) < hi_]
    cl = collections.defaultdict(list)
    for r in part: cl[r["close_ts"]].append(1.0 if r["proj_side"] == r["y"] else 0.0)
    mu, se, n, G = cl_se(cl)
    contested = [r for r in part if r["fav"] <= 0.95]
    cl2 = collections.defaultdict(list)
    for r in contested: cl2[r["close_ts"]].append(1.0 if r["proj_side"] == r["y"] else 0.0)
    mu2, se2, n2, G2 = cl_se(cl2)
    # market-disagree rows: mid on the other side of .5 from proj
    dis = [r for r in part if r["mid"] is not None and ((r["mid"] >= 0.5) != (r["proj_side"] == 1.0))]
    cl3 = collections.defaultdict(list)
    for r in dis: cl3[r["close_ts"]].append(1.0 if r["proj_side"] == r["y"] else 0.0)
    mu3, se3, n3, G3 = cl_se(cl3)
    tab[f"|d| {lo_}-{hi_}bp"] = dict(acc=round(mu, 3), se=round(se, 3), n=n, markets=G, contested_acc=round(mu2, 3), contested_n=n2, contested_markets=G2, disagree_acc=round(mu3, 3), disagree_n=n3, disagree_markets=G3)
print(json.dumps(tab, indent=0)); out["proj_sign_15M"] = tab
# per-market: what share of markets ever offered fav<=.95 with |d|>=1 in window (opportunity rate) and the P13 paper P&L at the FIRST such snap
first = {}
for r in sorted(pr, key=lambda r: r["t"]):
    if abs(r["d_bp"]) >= (2 if r["ticker"].startswith("KXSOL") else 1) and r["fav"] <= 0.95 and r["ticker"] not in first:
        side_yes = r["proj_side"] == 1.0; px = min((r["yes_ask"] if side_yes else 1 - r["yes_bid"]) + 0.01, 0.99)
        win = (r["y"] == 1.0) == side_yes; pnl = (1 - px) if win else -px
        first[r["ticker"]] = dict(px=px, win=win, pnl=pnl, mins_left=r["mins_left"], d=r["d_bp"], asset=r["ticker"][:5])
mk = len({r["ticker"] for r in pr})
pn = [v["pnl"] for v in first.values()]
print("P13 synthetic from books: markets with opportunity", len(first), "of", mk, "hit", round(sum(v["win"] for v in first.values()) / max(len(first), 1), 3), "mean c/ct", round(100 * st.mean(pn), 1) if pn else None, "se", round(100 * st.pstdev(pn) / math.sqrt(len(pn)), 1) if len(pn) > 1 else None, "avg px", round(st.mean(v["px"] for v in first.values()), 3) if first else None)
out["p13_synth"] = dict(opp=len(first), markets=mk, hit=sum(v["win"] for v in first.values()) / max(len(first), 1), mean_c=100 * st.mean(pn) if pn else None, se_c=100 * st.pstdev(pn) / math.sqrt(len(pn)) if len(pn) > 1 else None, by_asset={a: [round(v["pnl"], 2) for v in first.values() if v["asset"] == a] for a in ("KXBTC", "KXETH", "KXSOL")}, rows=first)
# ---- Test 6: time of day: at T-55..T-45 s the fav price distribution by UTC hour, 15M
tod = collections.defaultdict(list)
for r in pr:
    if 45 / 60 <= r["mins_left"] <= 55 / 60: tod[time.gmtime(r["t"]).tm_hour].append(r["fav"])
print("ToD fav at T-50s (UTC hour: n, share<=.95, median fav):")
tod_out = {}
for h in sorted(tod):
    v = tod[h]; tod_out[h] = dict(n=len(v), share_le95=round(sum(1 for x in v if x <= 0.95) / len(v), 2), med_fav=round(st.median(v), 3)); print(h, tod_out[h])
out["tod"] = tod_out
# volumes by hour from results (15M)
vol = collections.defaultdict(list)
for t, v in res.items():
    if "15M" in t and v.get("volume") is not None: vol[time.gmtime(v["close_ts"]).tm_hour].append(float(v["volume"]))
out["vol_by_hour_15M"] = {h: dict(n=len(v), med=round(st.median(v))) for h, v in sorted(vol.items())}
print("15M volume by UTC hour", out["vol_by_hour_15M"])
# market volume by asset
va = collections.defaultdict(list)
for t, v in res.items():
    if v.get("volume") is not None: va[t.split("-")[0]].append(float(v["volume"]))
print("median volume by series", {k: (len(v), round(st.median(v))) for k, v in va.items()})
json.dump(out, open(S / "signals_books.json", "w"), indent=1, default=str)
