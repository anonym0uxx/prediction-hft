"""Per-family realized stats from data/<family>/trades.jsonl (settled, since RESET)."""
import json, glob, math, pathlib, statistics as st, collections, sys, time
R = pathlib.Path("D:/prediction-hft/data")
RESET = json.loads((R / "RESET.json").read_text())["reset_at"]
NOW = time.time()
fams = sorted(p.parent.name for p in R.glob("*/trades.jsonl"))
rows = []
all_tr = {}
for f in fams:
    tr = [json.loads(l) for l in (R / f / "trades.jsonl").read_text().splitlines() if l.strip()]
    tr = [t for t in tr if t.get("t", 0) >= RESET]
    all_tr[f] = tr
    s = [t for t in tr if t.get("status") == "settled" and "pnl" in t]
    if not tr: continue
    # per contract net cents
    pc = [100 * t["pnl"] / t["qty"] for t in s]
    pnl = [t["pnl"] for t in s]
    cost = [t["cost"] for t in s]
    n = len(s)
    t0 = min(t["t"] for t in tr); span_d = max((NOW - t0) / 86400, 1e-6)
    # family runtime: use first/last runner.log line if exists
    lg = R / f / "runner.log"
    if lg.exists():
        ls = [l for l in lg.read_text(errors="ignore").splitlines() if l.strip()]
        # runner.log lines start with ISO ts? sample
    mean = st.mean(pc) if n else float("nan"); sd = st.pstdev(pc) if n > 1 else float("nan")
    hit = sum(1 for t in s if t["pnl"] > 0) / n if n else float("nan")
    # clustered se by close_ts
    cl = collections.defaultdict(list)
    for t in s: cl[t["close_ts"]].append(100 * t["pnl"] / t["qty"])
    G = len(cl)
    if G > 1:
        gm = [sum(v) for v in cl.values()]; mu = sum(gm) / n
        se_cl = math.sqrt(sum((sum(v) - mu * len(v)) ** 2 for v in cl.values()) * G / (G - 1)) / n
    else: se_cl = float("nan")
    tpd = n / span_d
    sr_trade = mean / sd if n > 1 and sd > 0 else float("nan")
    # daily P&L series (UTC day) in $
    day = collections.defaultdict(float)
    for t in s: day[int(t["close_ts"] // 86400)] += t["pnl"]
    dvals = list(day.values())
    # drawdown on cumulative $ P&L in trade order
    cum = 0; peak = 0; mdd = 0
    for t in sorted(s, key=lambda x: x["close_ts"]):
        cum += t["pnl"]; peak = max(peak, cum); mdd = min(mdd, cum - peak)
    ev_per_dollar = sum(pnl) / sum(cost) if cost else float("nan")
    sr_day_proj = sr_trade * math.sqrt(tpd) if n > 1 and sd > 0 else float("nan")
    rows.append(dict(family=f, n=n, open=len(tr) - n, pnl=round(sum(pnl), 2), mean_c=round(mean, 1), sd_c=round(sd, 1) if n > 1 else None, se_cl=round(se_cl, 1) if G > 1 else None, G=G, hit=round(hit, 2), span_d=round(span_d, 2), tpd=round(tpd, 1),
                     sr_trade=round(sr_trade, 2) if n > 1 else None, sr_day=round(sr_day_proj, 2) if n > 1 else None, mdd=round(mdd, 2), ev_per_dollar=round(ev_per_dollar, 3), avg_cost=round(st.mean(cost), 2) if cost else None,
                     avg_px=round(st.mean(t["price"] for t in s), 2) if s else None, days=len(dvals), day_pnls=[round(v, 2) for v in dvals]))
for r in rows: print(json.dumps(r))
json.dump(rows, open("D:/prediction-hft/reports/sharpe/fam_stats.json", "w"), indent=1)
# all trades flat
flat = [dict(t, family=f) for f, tr in all_tr.items() for t in tr if t.get("status") == "settled"]
json.dump(flat, open("D:/prediction-hft/reports/sharpe/all_trades.json", "w"))
print("settled total", len(flat), round(sum(t["pnl"] for t in flat), 2))
