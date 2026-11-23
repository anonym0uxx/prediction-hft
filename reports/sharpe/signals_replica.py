"""Replica-based tests: cross-asset lead-lag (1 Hz), first-minute drift vs 15-min outcome, replica-TWAP basis vs official expiration_value, EWMA sigma vs realized."""
import json, pathlib, math, collections, statistics as st, time, bisect
R = pathlib.Path("D:/prediction-hft/data"); S = pathlib.Path("D:/prediction-hft/reports/sharpe")
RESET = json.loads((R / "RESET.json").read_text())["reset_at"]
res = json.loads((S / "results.json").read_text())
def load(p):
    T, V, CB = [], [], []
    for l in (R / "replica" / f"{p}.jsonl").read_text().splitlines():
        if not l.strip(): continue
        try: r = json.loads(l)
        except Exception: continue
        if r.get("replica") is None or r["t"] < RESET: continue
        T.append(r["t"]); V.append(r["replica"]); CB.append(r["mids"].get("coinbase"))
    return T, V, CB
D = {a: load(a) for a in ("BTC", "ETH", "SOL")}
out = {}
def grid(T, V, step=1.0):
    """resample to a 1-s grid (last value carried); returns dict sec->value"""
    g = {}
    for t, v in zip(T, V): g[int(t)] = v
    return g
G = {a: grid(*D[a][:2]) for a in D}
print({a: (len(D[a][0]), round((D[a][0][-1] - D[a][0][0]) / 3600, 1)) for a in D})
# ---- cross-asset lead-lag: corr(ret_BTC[t-k,t], ret_X[t,t+k]) for k in 1,2,5,10,30 s, and reverse
def rets(g, k):
    out = {}
    for s in g:
        if s + k in g and g[s] and g[s + k]: out[s] = math.log(g[s + k] / g[s])
    return out
def corr(xs, ys):
    n = len(xs); mx, my = st.mean(xs), st.mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys)); sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else float("nan")
ll = {}
for k in (1, 2, 5, 10, 30):
    rb = rets(G["BTC"], k)
    for X in ("ETH", "SOL"):
        rx = rets(G[X], k)
        # lead: BTC past k vs X next k (non-overlapping: BTC[t-k,t] vs X[t,t+k])
        pairs = [(rb[s - k], rx[s]) for s in rx if (s - k) in rb]
        pairs_rev = [(rx[s - k], rb[s]) for s in rb if (s - k) in rx]
        pairs_cont = [(rb[s], rx[s]) for s in rx if s in rb]
        c1 = corr(*zip(*pairs)); c2 = corr(*zip(*pairs_rev)); c0 = corr(*zip(*pairs_cont))
        # beta of X next on BTC past, and R2 (effect size); se of corr ~ 1/sqrt(n_indep), n_indep = n/k
        n = len(pairs); se = 1 / math.sqrt(n / k)
        ll[f"k={k}s BTC->{X}"] = dict(corr_lead=round(c1, 3), corr_rev=round(c2, 3), corr_contemp=round(c0, 3), n=n, se=round(se, 3))
for k, v in ll.items(): print(k, v)
out["crossasset"] = ll
# same but restricted to final 60 s of each 15-min window
ll2 = {}
for k in (2, 5, 10):
    rb = rets(G["BTC"], k)
    for X in ("ETH", "SOL"):
        rx = rets(G[X], k)
        pairs = [(rb[s - k], rx[s]) for s in rx if (s - k) in rb and (s % 900) >= 840]
        n = len(pairs); c1 = corr(*zip(*pairs)) if n > 10 else float("nan")
        # sign agreement: P(sign X_next == sign BTC_past | |BTC_past| > 1 bp)
        big = [(a, b) for a, b in pairs if abs(a) > 1e-4]
        agree = sum(1 for a, b in big if a * b > 0) / len(big) if big else float("nan")
        ll2[f"k={k}s BTC->{X} final60s"] = dict(corr_lead=round(c1, 3), n=n, se=round(1 / math.sqrt(max(n / k, 1)), 3), sign_agree_big=round(agree, 3), n_big=len(big))
for k, v in ll2.items(): print(k, v)
out["crossasset_final60"] = ll2
# ---- replica TWAP vs official expiration_value (basis, bp) for 15M and hourly markets
bas = collections.defaultdict(list)
for tk, v in res.items():
    if not v.get("expiration_value") or not v.get("close_ts"): continue
    a = "BTC" if "BTC" in tk else "ETH" if "ETH" in tk else "SOL"
    g = G[a]; ct = int(v["close_ts"]); vals = [g[s] for s in range(ct - 60, ct) if s in g]
    if len(vals) < 50: continue
    tw = st.mean(vals); ev = float(v["expiration_value"])
    if "15M" in tk: bas[a + "15M"].append(1e4 * (ev / tw - 1))
    elif tk.startswith(("KXBTCD", "KXETHD", "KXSOLD")): bas[a + "D"].append(1e4 * (ev / tw - 1))
out["basis_bp"] = {k: dict(n=len(v), mean=round(st.mean(v), 2), sd=round(st.pstdev(v), 2), p10=round(sorted(v)[len(v) // 10], 2), p90=round(sorted(v)[9 * len(v) // 10], 2)) for k, v in bas.items() if len(v) > 2}
print("basis official/replica TWAP (bp)", out["basis_bp"])
# ---- first-minute drift (15-min windows): K_hat = mean replica over [open-60, open); d1 = mean replica[open, open+60) - K_hat; y = mean[close-60, close) >= K_hat
fm = {}
for a in D:
    g = G[a]; rows = []
    t0 = int(min(g)) // 900 * 900 + 900; t1 = int(max(g)) // 900 * 900
    for op in range(t0, t1, 900):
        cl = op + 900
        k_ = [g[s] for s in range(op - 60, op) if s in g]; m1 = [g[s] for s in range(op, op + 60) if s in g]; m7 = [g[s] for s in range(op + 420, op + 480) if s in g]; fin = [g[s] for s in range(cl - 60, cl) if s in g]
        if min(len(k_), len(m1), len(m7), len(fin)) < 40: continue
        K = st.mean(k_); d1 = 1e4 * (st.mean(m1) / K - 1); d7 = 1e4 * (st.mean(m7) / K - 1); dy = 1e4 * (st.mean(fin) / K - 1)
        rows.append((d1, d7, dy))
    n = len(rows)
    if n < 5: continue
    def agree(i):
        b = [r for r in rows if abs(r[i]) > 0.5]; return (sum(1 for r in b if r[i] * r[2] > 0) / len(b) if b else float("nan"), len(b))
    a1, n1 = agree(0); a7, n7 = agree(1)
    c1 = corr([r[0] for r in rows], [r[2] for r in rows]); c7 = corr([r[1] for r in rows], [r[2] for r in rows])
    # beta of final-vs-first: does the first minute's move persist (beta>0), mean-revert (beta<0 vs 1 means full persistence = random walk gives beta=1 since d_y includes d1)
    xs = [r[0] for r in rows]; ys = [r[2] for r in rows]; mx, my = st.mean(xs), st.mean(ys)
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    resid = [y - my - beta * (x - mx) for x, y in zip(xs, ys)]; se_b = math.sqrt(sum(r * r for r in resid) / (n - 2) / sum((x - mx) ** 2 for x in xs))
    fm[a] = dict(n=n, sign_agree_min1=round(a1, 3), n_big1=n1, corr_min1=round(c1, 3), beta_min1=round(beta, 2), se_beta=round(se_b, 2), sign_agree_min7=round(a7, 3), n_big7=n7, corr_min7=round(c7, 3), sd_dy_bp=round(st.pstdev(ys), 1), sd_d1_bp=round(st.pstdev(xs), 1))
    print(a, fm[a])
out["first_minute_drift"] = fm
# ---- EWMA sigma vs realized: per hour, realized sd of 1-min log returns (replica) vs sigma1 logged in books at that hour (median)
sig_books = collections.defaultdict(list)
for p in R.glob("s7p1*/books.jsonl"):
    a = "BTC" if "BTC" in p.parent.name else "ETH" if "ETH" in p.parent.name else "SOL"
    for l in p.read_text().splitlines():
        try: r = json.loads(l)
        except Exception: continue
        if r.get("sigma1") and r.get("t", 0) >= RESET: sig_books[(a, int(r["t"]) // 3600)].append(r["sigma1"])
rv = {}
for a in D:
    g = G[a]; rows = []
    for h in sorted({k[1] for k in sig_books if k[0] == a}):
        m = [g[s] for s in range(h * 3600, h * 3600 + 3600, 60) if s in g]
        if len(m) < 50: continue
        r1 = [math.log(m[i + 1] / m[i]) for i in range(len(m) - 1)]; rz = math.sqrt(st.mean([x * x for x in r1]))
        rows.append((st.median(sig_books[(a, h)]), rz, h))
    if rows:
        ratio = [r[1] / r[0] for r in rows]
        rv[a] = dict(n_hours=len(rows), mean_sigma_ewma_bp=round(1e4 * st.mean(r[0] for r in rows), 2), mean_realized_bp=round(1e4 * st.mean(r[1] for r in rows), 2), ratio_mean=round(st.mean(ratio), 2), ratio_sd=round(st.pstdev(ratio), 2), hourly=[(r[2] % 24, round(1e4 * r[0], 2), round(1e4 * r[1], 2)) for r in rows])
        print(a, {k: v for k, v in rv[a].items() if k != "hourly"})
out["sigma_vs_realized"] = rv
json.dump(out, open(S / "signals_replica.json", "w"), indent=1, default=str)
