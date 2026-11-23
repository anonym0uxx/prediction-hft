"""(a) Kalshi tape taker-flow in the final 90 s vs outcome on contested 15M markets; (b) OKX-perp-adjusted projection vs plain projection on BTC 15M snaps."""
import json, pathlib, math, collections, statistics as st, time, sys, datetime as dt
sys.path.insert(0, "D:/prediction-hft/src")
from predhft.kalshi_public import KalshiPublic
R = pathlib.Path("D:/prediction-hft/data"); S = pathlib.Path("D:/prediction-hft/reports/sharpe")
RESET = json.loads((R / "RESET.json").read_text())["reset_at"]
res = json.loads((S / "results.json").read_text()); Y = {t: (1.0 if v["result"] == "yes" else 0.0) for t, v in res.items() if v.get("result") in ("yes", "no")}
books = []
for p in R.glob("s7p1[35]-KX*15M/books.jsonl"):
    for l in p.read_text().splitlines():
        try: r = json.loads(l)
        except Exception: continue
        if r.get("t", 0) >= RESET and r.get("ticker") in Y and not r.get("stale_fill"): r["family"] = p.parent.name; r["y"] = Y[r["ticker"]]; books.append(r)
def fav(r): return r["yes_ask"] if (r["yes_bid"] + r["yes_ask"]) / 2 >= 0.5 else (1 - r["yes_bid"] if r["yes_bid"] > 0 else 1.0)
contested = sorted({r["ticker"] for r in books if 10 / 60 <= r["mins_left"] <= 90 / 60 and 0 < r["yes_ask"] < 1 and fav(r) <= 0.95})
print("contested markets", len(contested))
tape_p = S / "tape.json"; tape = json.loads(tape_p.read_text()) if tape_p.exists() else {}
k = KalshiPublic(min_interval_s=0.4)
for tk in contested:
    if tk in tape: continue
    ct = res[tk]["close_ts"]; cutoff = ct - 100; rows = []; cur = None
    for page in range(12):
        d = k.get("/markets/trades", ticker=tk, limit=1000, cursor=cur)
        for t in d["trades"]:
            ts = dt.datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()
            rows.append((ts, float(t["yes_price_dollars"]), float(t["count_fp"]), t["taker_side"]))
        cur = d.get("cursor")
        if not cur or (rows and rows[-1][0] < cutoff): break
    tape[tk] = rows; tape_p.write_text(json.dumps(tape)); print(tk, len(rows), flush=True)
out = {}
# (a) flow imbalance in [T-90, T-45] (signed taker volume, yes=+) vs outcome, conditional on mid at T-45 from the tape (last price before T-45)
def flow_test(w0, w1, label):
    cl = collections.defaultdict(list); rows = []
    for tk, rws in tape.items():
        ct = res[tk]["close_ts"]; y = Y[tk]
        seg = [r for r in rws if ct - w0 <= r[0] < ct - w1]
        if len(seg) < 5: continue
        vol = sum(r[2] for r in seg); fl = sum(r[2] * (1 if r[3] == "yes" else -1) for r in seg) / vol
        last_px = max(seg, key=lambda r: r[0])[1]   # last yes price before T-w1
        rows.append((tk, fl, last_px, y, vol))
    if len(rows) < 4: print(label, "n", len(rows)); return
    # residual y - last_px regressed on flow
    xs = [r[1] for r in rows]; ys = [r[3] - r[2] for r in rows]; mx, my = st.mean(xs), st.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs); beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    resid = [y - my - beta * (x - mx) for x, y in zip(xs, ys)]; se = math.sqrt(sum(r * r for r in resid) / max(len(rows) - 2, 1) / sxx)
    # sign test: does flow sign agree with outcome more than the last price's side does?
    agree_flow = sum(1 for r in rows if (r[1] > 0) == (r[3] == 1.0)) / len(rows); agree_px = sum(1 for r in rows if (r[2] >= 0.5) == (r[3] == 1.0)) / len(rows)
    # among markets where flow sign disagrees with last-price side
    dis = [r for r in rows if (r[1] > 0) != (r[2] >= 0.5)]; dis_flow_right = sum(1 for r in dis if (r[1] > 0) == (r[3] == 1.0))
    out[label] = dict(n=len(rows), beta_resid_on_flow=round(beta, 3), se=round(se, 3), agree_flow=round(agree_flow, 3), agree_lastpx=round(agree_px, 3), n_disagree=len(dis), flow_right_when_disagree=dis_flow_right, med_vol=round(st.median(r[4] for r in rows)))
    print(label, out[label])
flow_test(90, 45, "tape_flow_T-90..T-45"); flow_test(60, 30, "tape_flow_T-60..T-30"); flow_test(45, 15, "tape_flow_T-45..T-15")
# (b) perp-adjusted projection on BTC 15M snaps T-55..T-10 s
okx = {int(kk): v for kk, v in json.loads((S / "okx_1s.json").read_text()).items()}
rep = {}
for l in (R / "replica/BTC.jsonl").read_text().splitlines():
    try: r = json.loads(l)
    except Exception: continue
    if r.get("replica") and r["t"] >= RESET: rep[int(r["t"])] = r["replica"]
BAS = 0.44
def perp_spot(t):
    """perp-implied replica spot at time t: okx[t] / (1 + median basis over the prior 300 s), basis from matched seconds"""
    s = int(t); b = [okx[u] / rep[u] for u in range(s - 300, s - 2) if u in okx and u in rep]
    if len(b) < 60 or s not in okx: return None
    return okx[s] / st.median(b)
rows = []
for r in books:
    if not r["ticker"].startswith("KXBTC") or not (10 / 60 <= r["mins_left"] <= 55 / 60) or not r.get("partial_twap") or r.get("partial_n", 0) < 3: continue
    bb = 0.0 if r["family"].startswith("s7p13") else BAS
    sp = r["spot"] * (1 + bb * 1e-4); pt = r["partial_twap"] * (1 + bb * 1e-4); rr_ = max(min(r["mins_left"], 1.0), 0.02)
    ps = perp_spot(r["t"])
    if ps is None: continue
    ps *= (1 + BAS * 1e-4)
    proj = (1 - rr_) * pt + rr_ * sp; proj2 = (1 - rr_) * pt + rr_ * ps
    d1 = 1e4 * (proj / r["K"] - 1); d2 = 1e4 * (proj2 / r["K"] - 1)
    rows.append((r["ticker"], r["close_ts"], d1, d2, r["y"], fav(r), r["mins_left"], 1e4 * (ps / sp - 1)))
print("BTC snaps with perp", len(rows), "markets", len({r[0] for r in rows}))
def acc(sel, i):
    cl = collections.defaultdict(list)
    for r in sel: cl[r[1]].append(1.0 if ((r[i] >= 0) == (r[4] == 1.0)) else 0.0)
    allv = [v for vs in cl.values() for v in vs]; n = len(allv); G = len(cl)
    if n == 0: return (None, None, 0, 0)
    mu = st.mean(allv); se = math.sqrt(sum((sum(vs) - mu * len(vs)) ** 2 for vs in cl.values()) * G / max(G - 1, 1)) / n if G > 1 else None
    return (round(mu, 3), round(se, 3) if se else None, n, G)
tab = {}
for lo, hi in ((0, 0.5), (0.5, 1), (1, 2), (2, 1e9)):
    sel = [r for r in rows if lo <= abs(r[2]) < hi]
    tab[f"|d| {lo}-{hi}"] = dict(acc_proj=acc(sel, 2), acc_perp_proj=acc(sel, 3), perp_minus_spot_bp_sd=round(st.pstdev([r[7] for r in sel]), 2) if len(sel) > 1 else None)
    print(f"|d| {lo}-{hi}", tab[f"|d| {lo}-{hi}"])
sel = [r for r in rows if r[5] <= 0.95]
tab["contested fav<=.95"] = dict(acc_proj=acc(sel, 2), acc_perp_proj=acc(sel, 3))
print("contested", tab["contested fav<=.95"])
# how often does the perp adjustment flip the sign, and who is right when it does
flip = [r for r in rows if (r[2] >= 0) != (r[3] >= 0)]
tab["flips"] = dict(n=len(flip), markets=len({r[0] for r in flip}), perp_right=sum(1 for r in flip if (r[3] >= 0) == (r[4] == 1.0)), spot_right=sum(1 for r in flip if (r[2] >= 0) == (r[4] == 1.0)))
print("flips", tab["flips"])
out["perp_proj_BTC15M"] = tab
json.dump(out, open(S / "signals_tape_perp.json", "w"), indent=1, default=str)
