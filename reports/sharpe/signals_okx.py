"""Fetch OKX BTC-USDT-SWAP 1-s candles over the replica span (cached) + Deribit DVOL 1-min; test perp->spot lead-lag and DVOL vs realized."""
import json, pathlib, math, statistics as st, time, urllib.request, sys
R = pathlib.Path("D:/prediction-hft/data"); S = pathlib.Path("D:/prediction-hft/reports/sharpe")
RESET = json.loads((R / "RESET.json").read_text())["reset_at"]
def g(u, to=10):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "predhft"}), timeout=to).read())
# replica BTC coinbase mid 1 Hz
T, V, CB = [], [], []
for l in (R / "replica/BTC.jsonl").read_text().splitlines():
    try: r = json.loads(l)
    except Exception: continue
    if r.get("replica") is None or r["t"] < RESET: continue
    T.append(r["t"]); V.append(r["replica"]); CB.append(r["mids"].get("coinbase"))
t_lo, t_hi = int(T[0]), int(T[-1])
okx_p = S / "okx_1s.json"
okx = json.loads(okx_p.read_text()) if okx_p.exists() else {}
if len(okx) < (t_hi - t_lo) * 0.8:
    after = (t_hi + 1) * 1000; calls = 0
    while after > t_lo * 1000 and calls < 800:
        try:
            d = g(f"https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT-SWAP&bar=1s&limit=100&after={after}")["data"]
        except Exception as e:
            print("err", e); time.sleep(2); continue
        if not d: break
        for c in d: okx[str(int(c[0]) // 1000)] = float(c[4])   # close of the 1-s bar
        after = int(d[-1][0]); calls += 1
        if calls % 50 == 0: okx_p.write_text(json.dumps(okx)); print(calls, len(okx), flush=True)
        time.sleep(0.11)
    okx_p.write_text(json.dumps(okx))
print("okx secs", len(okx))
# grid
gr = {}; gc = {}
for t, v, c in zip(T, V, CB): gr[int(t)] = v; gc[int(t)] = c
go = {int(k): v for k, v in okx.items()}
def rets(gd, k): return {s: math.log(gd[s + k] / gd[s]) for s in gd if (s + k) in gd and gd[s] and gd[s + k]}
def corr(xs, ys):
    n = len(xs); mx, my = st.mean(xs), st.mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys)); sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else float("nan")
out = {}
for k in (1, 2, 5, 10, 30, 60):
    ro = rets(go, k); rr = rets(gr, k); rc = rets({s: c for s, c in gc.items() if c}, k)
    for nm, rx in (("replica", rr), ("cb_mid", rc)):
        lead = [(ro[s - k], rx[s]) for s in rx if (s - k) in ro]; rev = [(rx[s - k], ro[s]) for s in ro if (s - k) in rx]; cont = [(ro[s], rx[s]) for s in rx if s in ro]
        if len(lead) < 100: continue
        xs, ys = zip(*lead); mx, my = st.mean(xs), st.mean(ys); beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        big = [(a, b) for a, b in lead if abs(a) > 2e-4]; agree = sum(1 for a, b in big if a * b > 0) / len(big) if big else float("nan")
        out[f"k={k}s OKXperp->{nm}"] = dict(corr_lead=round(corr(xs, ys), 3), corr_rev=round(corr(*zip(*rev)), 3), corr_contemp=round(corr(*zip(*cont)), 3), beta_lead=round(beta, 3), n=len(lead), se=round(1 / math.sqrt(len(lead) / k), 3), sign_agree_gt2bp=round(agree, 3), n_big=len(big))
        print(f"k={k}s OKXperp->{nm}", out[f"k={k}s OKXperp->{nm}"])
# final-60s only, k=5,10
for k in (5, 10):
    ro = rets(go, k); rr = rets(gr, k)
    lead = [(ro[s - k], rr[s]) for s in rr if (s - k) in ro and s % 900 >= 840]
    big = [(a, b) for a, b in lead if abs(a) > 2e-4]; agree = sum(1 for a, b in big if a * b > 0) / len(big) if big else float("nan")
    out[f"k={k}s OKXperp->replica final60"] = dict(corr_lead=round(corr(*zip(*lead)), 3), n=len(lead), se=round(1 / math.sqrt(len(lead) / k), 3), sign_agree_gt2bp=round(agree, 3), n_big=len(big))
    print(f"k={k}s final60", out[f"k={k}s OKXperp->replica final60"])
# perp-spot basis level and its 1-min change as predictor of next-1-min spot return
bas = {s: 1e4 * (go[s] / gr[s] - 1) for s in gr if s in go}
ss = sorted(bas); 
if ss:
    out["perp_basis_bp"] = dict(mean=round(st.mean(bas.values()), 2), sd=round(st.pstdev(bas.values()), 2), n=len(bas))
    pairs = [(bas[s] - bas[s - 60], math.log(gr[s + 60] / gr[s]) * 1e4) for s in gr if s in bas and (s - 60) in bas and (s + 60) in gr]
    xs, ys = zip(*pairs); mx, my = st.mean(xs), st.mean(ys); beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    out["basis_change_1m->spot_next_1m"] = dict(corr=round(corr(xs, ys), 3), beta_bp_per_bp=round(beta, 3), n=len(pairs), se=round(1 / math.sqrt(len(pairs) / 60), 3))
    print(out["perp_basis_bp"], out["basis_change_1m->spot_next_1m"])
# Deribit DVOL (1-min) vs realized per hour (replica 1-min log returns)
dv = g(f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=3600&start_timestamp={t_lo * 1000}&end_timestamp={t_hi * 1000}")["result"]["data"]
rows = []
for ts, o, h, l, c in dv:
    h0 = ts // 1000; m = [gr[s] for s in range(h0, h0 + 3600, 60) if s in gr]
    if len(m) < 55: continue
    r1 = [math.log(m[i + 1] / m[i]) for i in range(len(m) - 1)]; rz = 1e4 * math.sqrt(st.mean(x * x for x in r1))
    rows.append((time.gmtime(h0).tm_hour, round(o, 2), round(1e4 * o / 100 / math.sqrt(365 * 24 * 60), 2), round(rz, 2)))
out["dvol_vs_realized"] = dict(rows=rows, dvol_per_min_bp_mean=round(st.mean(r[2] for r in rows), 2), realized_bp_mean=round(st.mean(r[3] for r in rows), 2), ratio_realized_over_dvol=round(st.mean(r[3] / r[2] for r in rows), 2), corr_hourly=round(corr([r[2] for r in rows], [r[3] for r in rows]), 3), n=len(rows))
print("DVOL", {k: v for k, v in out["dvol_vs_realized"].items() if k != "rows"}); print(rows)
json.dump(out, open(S / "signals_okx_dvol.json", "w"), indent=1)
