"""P19 quote-path scoring (pure functions; CLI in scripts/score_quote_path.py).
Joins settled Kalshi outcomes to the quote-path rows written by predhft.open_quotes and reports, per asset and per minutes-left
bucket: market-implied p vs realised frequency (calibration), Brier of the market mid vs a reference model (Brownian projection on
the official index: p_ref = Phi(d_bp / (sigma1_bp * sqrt(m - 2/3))), m = minutes left, the 60-s settlement average removes 2/3 of
the last minute's variance), and the fraction of windows where the eventually-winning side was offered at <= .95 / .90 / .80 at
that minutes-left, i.e. WHEN in the window an entry is possible and whether the market misprices early.
se is clustered by close_ts (the three assets share closes)."""
from __future__ import annotations
import json, math, pathlib
from collections import defaultdict

SIGMA_DEFAULT_BP = {"BTC": 3.5, "ETH": 5.0, "SOL": 5.5}     # REBASE §1.3 hour-vol medians, bp per minute (fallback only)
PRICE_LEVELS = (0.95, 0.90, 0.80)

def Phi(x: float) -> float: return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def load_rows(path: pathlib.Path) -> list[dict]:
    """v2 rows as written; v1 rows (single T-14.5 snapshot) mapped onto the same keys."""
    out = []
    if not pathlib.Path(path).exists(): return out
    for l in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if not l.strip(): continue
        try: r = json.loads(l)
        except Exception: continue
        if r.get("error") or not r.get("ticker"): continue
        if r.get("v", 1) == 1:
            r = {**r, "v": 1, "asset": r["series"][2:5], "secs_left": r["mins_left"] * 60.0, "yes_bid_size": r.get("yes_bid_qty"), "yes_ask_size": r.get("no_bid_qty"),
                 "cfb_partial_n": None, "replica": None, "sigma1": None}
        out.append(r)
    return out

def bucket_of(secs_left: float) -> int:
    """Minutes-left bucket b means secs_left in ((b-1)*60, b*60]; b=1 is the final minute (not recorded here), 15 = at open."""
    return max(1, int(math.ceil(secs_left / 60.0 - 1e-9)))

def p_ref(row: dict) -> float | None:
    """Reference model: Brownian projection of the official index (replica fallback) to the 60-s settlement average."""
    K = row.get("strike"); x = row.get("cfb_value") or row.get("replica")
    if not K or not x or K <= 0 or x <= 0: return None
    m = max(row["secs_left"] / 60.0, 1.0)
    s1 = row.get("sigma1"); s_bp = (s1 * 1e4) if s1 else SIGMA_DEFAULT_BP.get(row.get("asset", ""), 5.0)
    sd = s_bp * math.sqrt(max(m - 2.0 / 3.0, 1.0 / 3.0))
    return Phi(1e4 * math.log(x / K) / sd) if sd > 0 else None

def mid(row: dict) -> float | None:
    yb, ya = row.get("yes_bid"), row.get("yes_ask")
    if yb is None and ya is None: return None
    if yb is None: return ya
    if ya is None: return yb
    return (yb + ya) / 2.0

def win_side_ask(row: dict, result: str) -> float | None:
    """Ask of the side that eventually won, in that side's price space (NO ask = 1 - YES bid)."""
    if result == "yes": return row.get("yes_ask")
    yb = row.get("yes_bid"); return (1.0 - yb) if yb is not None else None

def cluster_mean_se(vals: list[float], clusters: list) -> tuple[float | None, float | None, int]:
    """Mean and cluster-robust se (cluster = close_ts): se = sd(cluster means) / sqrt(G) with cluster means weighted equally."""
    if not vals: return None, None, 0
    by: dict = defaultdict(list)
    for v, c in zip(vals, clusters): by[c].append(v)
    cm = [sum(v) / len(v) for v in by.values()]; G = len(cm); m = sum(cm) / G
    if G < 2: return m, None, G
    return m, math.sqrt(sum((x - m) ** 2 for x in cm) / (G - 1) / G), G

def score(rows: list[dict], results: dict[str, dict], p_bins: int = 10) -> dict:
    """results: ticker -> {"result": "yes"|"no", ...}. Returns {"buckets": {(asset,b): {...}}, "calibration": {asset: [...]}, "first_available": {asset: {level: [bucket,...]}}}."""
    joined = [r for r in rows if results.get(r["ticker"], {}).get("result") in ("yes", "no")]
    per_bucket: dict = defaultdict(lambda: {"n": 0, "brier_mkt": [], "brier_ref": [], "dbrier": [], "cl": [], "mkt_p_win": [], "avail": {lv: [] for lv in PRICE_LEVELS}, "windows": set()})
    cal: dict = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0]))      # asset -> bin -> [n, sum p, n_yes]
    first_av: dict = defaultdict(lambda: {lv: {} for lv in PRICE_LEVELS})    # asset -> level -> (ticker) -> earliest bucket (largest minutes-left) with win-side ask <= level
    for r in joined:
        res = results[r["ticker"]]["result"]; y = 1.0 if res == "yes" else 0.0; b = bucket_of(r["secs_left"]); a = r.get("asset") or r["series"][2:5]
        pm = mid(r)
        if pm is None: continue
        d = per_bucket[(a, b)]; d["n"] += 1; d["cl"].append(int(r["close_ts"])); d["windows"].add(r["ticker"])
        d["brier_mkt"].append((pm - y) ** 2); d["mkt_p_win"].append(pm if y else 1 - pm)
        pr = p_ref(r)
        if pr is not None: d["brier_ref"].append((pr - y) ** 2); d["dbrier"].append((pm - y) ** 2 - (pr - y) ** 2)
        wa = win_side_ask(r, res)
        for lv in PRICE_LEVELS:
            ok = wa is not None and 0 < wa <= lv; d["avail"][lv].append(1.0 if ok else 0.0)
            if ok: first_av[a][lv][r["ticker"]] = max(first_av[a][lv].get(r["ticker"], 0), b)
        k = min(p_bins - 1, int(pm * p_bins)); c = cal[a][k]; c[0] += 1; c[1] += pm; c[2] += int(y)
    out_b = {}
    for key, d in per_bucket.items():
        m_mkt, se_mkt, G = cluster_mean_se(d["brier_mkt"], d["cl"])
        m_db, se_db, _ = cluster_mean_se(d["dbrier"], d["cl"][:len(d["dbrier"])]) if d["dbrier"] else (None, None, 0)
        out_b[key] = {"n": d["n"], "closes": G, "windows": len(d["windows"]), "brier_mkt": m_mkt, "brier_ref": (sum(d["brier_ref"]) / len(d["brier_ref"]) if d["brier_ref"] else None),
                      "dbrier_mkt_minus_ref": m_db, "dbrier_se": se_db, "mkt_p_win_mean": sum(d["mkt_p_win"]) / len(d["mkt_p_win"]),
                      "avail": {lv: (sum(v) / len(v) if v else None) for lv, v in d["avail"].items()}}
    out_cal = {a: [{"bin_lo": k / p_bins, "n": v[0], "mean_p": v[1] / v[0], "freq": v[2] / v[0]} for k, v in sorted(bins.items())] for a, bins in cal.items()}
    out_first = {a: {lv: sorted(fb.values()) for lv, fb in lvls.items()} for a, lvls in first_av.items()}
    return {"buckets": out_b, "calibration": out_cal, "first_available": out_first, "n_rows": len(rows), "n_joined": len(joined)}

def fmt_report(sc: dict) -> str:
    L = [f"# Quote-path scoring: {sc['n_joined']} / {sc['n_rows']} rows joined to a settled outcome", ""]
    L += ["## Per asset x minutes-left bucket (b = minutes left, b=15 is the open; se clustered by close)", "",
          "| asset | b | n | closes | mkt p(win) | Brier mkt | Brier ref | dBrier mkt-ref (se) | win side <= .95 | <= .90 | <= .80 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for (a, b), d in sorted(sc["buckets"].items(), key=lambda kv: (kv[0][0], -kv[0][1])):
        f2 = lambda x: "—" if x is None else f"{x:.3f}"; pc = lambda x: "—" if x is None else f"{100 * x:.0f}%"
        se = "" if d["dbrier_se"] is None else f" ({d['dbrier_se']:.3f})"
        L.append(f"| {a} | {b} | {d['n']} | {d['closes']} | {f2(d['mkt_p_win_mean'])} | {f2(d['brier_mkt'])} | {f2(d['brier_ref'])} | {f2(d['dbrier_mkt_minus_ref'])}{se} | {pc(d['avail'][0.95])} | {pc(d['avail'][0.90])} | {pc(d['avail'][0.80])} |")
    L += ["", "## Calibration of the market mid (all buckets pooled)", "", "| asset | bin | n | mean p | realised YES freq |", "|---|---|---|---|---|"]
    for a, bins in sorted(sc["calibration"].items()):
        for c in bins: L.append(f"| {a} | {c['bin_lo']:.1f}-{c['bin_lo'] + 0.1:.1f} | {c['n']} | {c['mean_p']:.3f} | {c['freq']:.3f} |")
    L += ["", "## Earliest minutes-left at which the eventually-winning side was offered at <= level (per window; median [IQR])", "", "| asset | level | windows | median b | p25 | p75 |", "|---|---|---|---|---|---|"]
    for a, lv in sorted(sc["first_available"].items()):
        for level, xs in lv.items():
            if not xs: L.append(f"| {a} | {level} | 0 | — | — | — |"); continue
            q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
            L.append(f"| {a} | {level} | {len(xs)} | {q(0.5)} | {q(0.25)} | {q(0.75)} |")
    return "\n".join(L)
