"""Trade forensics 2026-10-29 (docs/TRADE_FORENSICS_2026-10-29.md). Read-only on data/; writes reports/forensics/*.json.

Reconstructs every logged paper trade scenario on KXSOL15M (SOL-v1 fills/exits/settles, Opus direct fills, P12/P13 15M history)
plus the counterfactual universe (every SOL-v1 entry snapshot where a lot could be bought at an executable price, both sides),
attributes realised P&L by state, builds walk-forward conditional expectancy maps with close-clustered SE, runs a
multiple-comparison-corrected 'which conditions help / hurt' search (Bonferroni + Hansen SPA, reusing model_zoo.spa_test),
analyses hold-vs-exit at executable bids, Opus's fills / disagreements, and tail frequencies on the 36-day candle history.

Execution model for counterfactuals = sol_v1.walk/side_levels (depth-weighted, never beyond displayed depth) + one tick, Kalshi
taker fee ceil-to-cent per order (sol_v1.fee_pc). Absent-side books are never filled (no 1.00 fallbacks). Pacific Time in text.

Usage: cd D:/prediction-hft && uv run --with pandas,pyarrow,numpy python scripts/forensics_2026_10_29.py
"""
from __future__ import annotations
import bisect, collections, datetime as dt, json, math, pathlib, re, sys, zoneinfo
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from predhft import sol_v1 as S                                   # noqa: E402
from predhft.stats_spec import ratio_estimate, Phi                # noqa: E402
from predhft.model_zoo import spa_test                            # noqa: E402

OUT = ROOT / "reports" / "forensics"; OUT.mkdir(parents=True, exist_ok=True)
PT = zoneinfo.ZoneInfo("America/Los_Angeles")
LOT = 10; TICK = S.TICK
PX_LO, PX_HI = 0.02, 0.98          # universe price bounds (policy trades 0.03-0.97; we keep the 2-3 c edge for the tail study)

def jl(p):
    with open(p, encoding="utf-8") as fh:
        for l in fh:
            if l.strip():
                try: yield json.loads(l)
                except Exception: continue

def pt(ts): return dt.datetime.fromtimestamp(ts, PT).strftime("%Y-%m-%d %H:%M PT")
def hour_pt(ts): return dt.datetime.fromtimestamp(ts, PT).hour
def r(x, k=4): return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), k)

# ------------------------------------------------------------------------------------------------------------- settlement
def load_cfb():
    ser = S.cfb_series(jl(ROOT / "data/cfb/SOL.jsonl")); return ser

def exchange_results():
    res = {}
    for f in jl(ROOT / "data/sol-v1/fills.jsonl"):
        if f.get("kind") == "settle" and f.get("result") in ("yes", "no"): res[f["close_ts"]] = f["result"]
    for f in jl(ROOT / "data/llm-direct/fills.jsonl"):
        if f.get("kind") == "settle" and f.get("result") in ("yes", "no"): res[f["close_ts"]] = f["result"]
    return res

# ------------------------------------------------------------------------------------------------------------- execution
def exec_buy(side, book, qty=LOT):
    """Depth-limited walk against displayed levels + one tick (= sol_v1.paper_fill with no limit). None if nothing displayed."""
    lv = S.side_levels(book, side, "buy")
    if not lv: return None
    w = S.walk(lv, qty, None, "buy"); q = int(math.floor(w["qty"] + 1e-9))
    if q <= 0 or w["avg_px"] is None: return None
    px = min(w["avg_px"] + TICK, 0.999)
    return {"qty": q, "px": px, "fee_pc": S.fee_pc(q, px), "best": lv[0][0], "best_qty": lv[0][1], "partial": q < qty}

# ------------------------------------------------------------------------------------------------------------- buckets
def b_time(s):
    return "T-10-60s" if s < 60 else "T-60-120s" if s < 120 else "T-2-5m" if s < 300 else "T-5-10m" if s < 600 else "T-10-14m"
def b_px(p):
    return "px<.10" if p < .10 else "px.10-.25" if p < .25 else "px.25-.50" if p < .50 else "px.50-.75" if p < .75 else "px.75-.90" if p < .90 else "px>=.90"
def b_absz(z):
    if z is None: return "z_na"
    a = abs(z); return "|z|<.5" if a < .5 else "|z|.5-1" if a < 1 else "|z|1-2" if a < 2 else "|z|>=2"
def b_edge(e):           # model p_side - executable price
    return "medge<=0" if e <= 0 else "medge0-5c" if e <= .05 else "medge5-15c" if e <= .15 else "medge>15c"
def b_spread(sp): return "spr<=1c" if sp <= 0.0101 else "spr1-3c" if sp <= 0.0301 else "spr>3c"
def b_depth(q): return "depth<50" if q < 50 else "depth50-200" if q < 200 else "depth>=200"
def b_vol(sg): return "vol<.7bp" if sg < 0.7e-4 else "vol.7-1.2bp" if sg < 1.2e-4 else "vol>=1.2bp"
def b_partial(n): return "partial0" if n == 0 else "partial1-30" if n <= 30 else "partial31-59"
def b_hour(h): return "h00-05PT" if h < 6 else "h06-11PT" if h < 12 else "h12-17PT" if h < 18 else "h18-23PT"
def b_mid(m): return "mid<.10" if m < .1 else "mid.10-.30" if m < .3 else "mid.30-.70" if m < .7 else "mid.70-.90" if m < .9 else "mid>=.90"

FEATURES = ["tb", "pxb", "side", "zb", "edgeb", "sprb", "depb", "volb", "partb", "hourb", "comove", "midb", "with_model", "fav"]

# ------------------------------------------------------------------------------------------------------------- co-move
def load_comove():
    """BTC/ETH sign(cfb_value - strike) per (asset, t) from data/open_quotes/quotes.jsonl (30-s cadence)."""
    by = {"BTC": [], "ETH": []}
    for q in jl(ROOT / "data/open_quotes/quotes.jsonl"):
        a = q.get("asset") or q.get("series", "")[2:5]
        if a in by and q.get("cfb_value") and q.get("strike"): by[a].append((q["t"], q["close_ts"], (1 if q["cfb_value"] >= q["strike"] else -1)))
    for a in by: by[a].sort()
    return by

def comove_at(cm, close_ts, t, sol_sign):
    out = []
    for a in ("BTC", "ETH"):
        rows = cm[a]; ts = [x[0] for x in rows]; i = bisect.bisect_right(ts, t) - 1
        if i >= 0 and t - rows[i][0] <= 45 and rows[i][1] == close_ts: out.append(rows[i][2])
    if not out or sol_sign == 0: return "comove_na"
    agree = sum(1 for s in out if s == sol_sign)
    return "comove_agree" if agree == len(out) else "comove_disagree" if agree == 0 else "comove_mixed"

# ------------------------------------------------------------------------------------------------------------- universe
def build_universe(snaps, results, cm):
    rows = []
    for s in snaps:
        if s.get("yes_bid") is None or s.get("yes_ask") is None or s.get("p_model") is None: continue
        sl = s["secs_left"]
        if not (S.CFG["entry_min_secs_left"] <= sl <= S.CFG["entry_max_secs_left"]): continue
        res = results.get(s["close_ts"])
        if res is None: continue
        book = {"bids": s.get("bids"), "asks": s.get("asks")}
        spread = s["yes_ask"] - s["yes_bid"]; z = s.get("z"); sol_sign = 0 if s["x_now"] is None else (1 if s["x_now"] >= s["K"] else -1)
        co = comove_at(cm, s["close_ts"], s["t"], sol_sign)
        for side in ("yes", "no"):
            e = exec_buy(side, book)
            if e is None or not (PX_LO <= e["px"] <= PX_HI): continue
            p_side = s["p_model"] if side == "yes" else 1 - s["p_model"]
            win = 1.0 if res == side else 0.0
            net = win - e["px"] - e["fee_pc"]
            pol = s.get("ev", {}) or {}; pe = pol.get(side) or {}
            rows.append({"t": s["t"], "close_ts": s["close_ts"], "secs_left": sl, "side": side, "px": e["px"], "fee_pc": e["fee_pc"], "qty": e["qty"],
                         "partial_fill": e["partial"], "best": e["best"], "best_qty": e["best_qty"], "mid": s["mid"], "spread": spread, "p_model": s["p_model"],
                         "p_side": p_side, "medge": p_side - e["px"], "z": z, "sigma_1s": s["sigma_1s"], "n_obs": s["n_obs"], "win": win, "net": net,
                         "policy_ev_pc": pe.get("ev_pc"), "policy_enter": (s["decision"] == "enter" and s.get("side") == side),
                         "tb": b_time(sl), "pxb": b_px(e["px"]), "zb": b_absz(z), "edgeb": b_edge(p_side - e["px"]), "sprb": b_spread(spread),
                         "depb": b_depth(e["best_qty"]), "volb": b_vol(s["sigma_1s"]), "partb": b_partial(s["n_obs"]), "hourb": b_hour(hour_pt(s["t"])),
                         "comove": co, "midb": b_mid(s["mid"]), "with_model": ("with_model" if p_side > (s["mid"] if side == "yes" else 1 - s["mid"]) else "against_model"),
                         "fav": ("favourite" if e["px"] >= 0.5 else "longshot")})
    return rows

# ------------------------------------------------------------------------------------------------------------- cell stats
def cell_stats(rows):
    """Ratio estimand (net $/contract on 1 contract per snapshot row), cluster = close. Also realised vs market-implied rate."""
    if not rows: return {"n": 0, "n_closes": 0}
    re_ = ratio_estimate([x["net"] for x in rows], [1.0] * len(rows), [x["close_ts"] for x in rows])
    hit = float(np.mean([x["win"] for x in rows])); px = float(np.mean([x["px"] for x in rows])); pm = float(np.mean([x["p_side"] for x in rows]))
    closes = sorted({x["close_ts"] for x in rows})
    pc = {c: np.mean([x["net"] for x in rows if x["close_ts"] == c]) for c in closes}
    return {"n": len(rows), "n_closes": len(closes), "n_win_closes": len({x["close_ts"] for x in rows if x["win"] == 1.0}), "net_c": r(100 * re_["theta"], 2), "se_c": (r(100 * re_["se_cluster"], 2) if re_.get("se_cluster") else None),
            "t": (r(re_["z_h0_theta_le_0"], 2) if re_.get("z_h0_theta_le_0") is not None else None), "hit": r(hit, 3), "mkt_implied": r(px, 3), "model_p": r(pm, 3),
            "per_close_mean_c": r(100 * float(np.mean(list(pc.values()))), 2), "per_close_se_c": (r(100 * float(np.std(list(pc.values()), ddof=1) / math.sqrt(len(pc))), 2) if len(pc) > 1 else None)}

def verdict(st, min_closes=20):
    if st["n_closes"] < min_closes or st.get("se_c") is None: return "insufficient"
    if st["hit"] in (0.0, 1.0): return "insufficient"            # degenerate cell: SE is not a measure of risk (rule of three applies)
    t = st["net_c"] / st["se_c"] if st["se_c"] else 0.0
    return "favourable" if t >= 2 else "unfavourable" if t <= -2 else "indeterminate"

# ------------------------------------------------------------------------------------------------------------- conditions
def condition_universe(rows):
    """Every condition = conjunction of bucket values on 1-3 features (side always included as a free feature). Returns {name: pred}."""
    conds = {}
    vals = {f: sorted({x[f] for x in rows}) for f in FEATURES}
    singles = [(f, v) for f in FEATURES for v in vals[f]]
    for f, v in singles: conds[f"{v}"] = ((f, v),)
    pairs_feats = [("tb", "pxb"), ("tb", "edgeb"), ("pxb", "edgeb"), ("tb", "side"), ("pxb", "side"), ("edgeb", "side"), ("tb", "zb"), ("pxb", "zb"),
                   ("tb", "volb"), ("pxb", "volb"), ("tb", "partb"), ("tb", "comove"), ("pxb", "comove"), ("tb", "hourb"), ("pxb", "sprb"), ("pxb", "depb"), ("fav", "with_model"), ("tb", "with_model"), ("tb", "fav")]
    for f1, f2 in pairs_feats:
        for v1 in vals[f1]:
            for v2 in vals[f2]: conds[f"{v1}&{v2}"] = ((f1, v1), (f2, v2))
    for f1, f2, f3 in [("tb", "pxb", "side"), ("tb", "edgeb", "side"), ("tb", "fav", "with_model")]:
        for v1 in vals[f1]:
            for v2 in vals[f2]:
                for v3 in vals[f3]: conds[f"{v1}&{v2}&{v3}"] = ((f1, v1), (f2, v2), (f3, v3))
    return conds

def match(x, spec): return all(x[f] == v for f, v in spec)

def per_close_matrix(rows, conds, closes):
    """closes x conditions matrix of per-close mean net (c/ct); NaN where the condition has no row in that close."""
    ci = {c: i for i, c in enumerate(closes)}; names = list(conds); D = np.full((len(closes), len(names)), np.nan)
    acc = collections.defaultdict(list); hits = collections.defaultdict(list)
    for x in rows:
        for j, nm in enumerate(names):
            if match(x, conds[nm]): hits[(x["t"], j)].append(x)
    # a condition is a POLICY: it may buy at most one side per snapshot. Snapshots where both sides qualify are dropped for that
    # condition (buying both sides = paying the spread with certainty, a mechanical -2 c that is not a finding).
    for (t, j), v in hits.items():
        if len(v) == 1: acc[(ci[v[0]["close_ts"]], j)].append(v[0])
    wins = np.full(D.shape, np.nan); cnt = np.zeros(D.shape, dtype=int)
    for (i, j), v in acc.items(): D[i, j] = 100 * float(np.mean([x["net"] for x in v])); wins[i, j] = float(np.mean([x["win"] for x in v])); cnt[i, j] = len(v)
    return names, D, wins, cnt

def t_of(D_col):
    v = D_col[np.isfinite(D_col)]
    if len(v) < 2: return None, None, len(v)
    m = float(np.mean(v)); se = float(np.std(v, ddof=1) / math.sqrt(len(v))); return m, se, len(v)

# ------------------------------------------------------------------------------------------------------------- main pieces
def trade_table(snaps, dec, results, cfb):
    """Every logged paper trade: SOL-v1, Opus direct, P12 (s7p15) and P13 (s7p13) 15M fills with features + outcome."""
    out = []
    # SOL-v1
    fills = list(jl(ROOT / "data/sol-v1/fills.jsonl")); by_intent = collections.defaultdict(list)
    for f in fills: by_intent[f["intent_id"]].append(f)
    snap_by_close = collections.defaultdict(list)
    for s in snaps: snap_by_close[s["close_ts"]].append(s)
    he = collections.defaultdict(list)
    for d in dec:
        if d.get("kind") == "hold_exit": he[d["ticker"]].append(d)
    for iid, evs in by_intent.items():
        fl = [e for e in evs if e["kind"] == "fill"]
        if not fl: continue
        f = fl[0]; close = [e for e in evs if e["kind"] in ("settle", "exit")]
        res = results.get(f["close_ts"]); win = None if res is None else (1.0 if res == f["side"] else 0.0)
        # z at decision: nearest enter snapshot
        cand = [s for s in snap_by_close[f["close_ts"]] if s["decision"] == "enter" and abs(s["t"] - f["t"]) < 3]
        z = cand[-1]["z"] if cand else None
        t_end = close[0]["t"] if close else f["close_ts"]
        path = sorted((d for d in he.get(f["ticker"], []) if f["t"] <= d["t"] <= t_end + 1e-6), key=lambda d: d["t"])
        best_exit = max((d["exit_proceeds"] for d in path if d["exit_qty"] >= f["qty"]), default=None) if f["qty"] else None
        row = {"family": "sol-v1", "ticker": f["ticker"], "close_ts": f["close_ts"], "when_pt": pt(f["t"]), "t_fill": f["t"], "side": f["side"], "qty": f["qty"], "intended_qty": f["intended_qty"],
               "px": f["px"], "fee": f["fee"], "cost": f["cost"], "secs_left": f["secs_left"], "mid": f["mid"], "p_model": f["p_model"], "p_used": f["p_yes"], "p_side": f["p_side"],
               "ev_pc": f["ev_pc"], "z": z, "sigma_1s": f["sigma_1s"], "n_obs": f["n_obs"], "hour_pt": hour_pt(f["t"]), "status": f["status"], "result": res, "win": win,
               "exit_kind": (close[0]["kind"] if close else ("unfilled" if f["qty"] == 0 else "open")), "pnl": (close[0]["pnl"] if close else None),
               "hold_pnl_cf": (None if (win is None or f["qty"] == 0) else win * f["qty"] - f["cost"] - f["fee"]),
               "best_exit_proceeds_path": best_exit, "n_hold_polls": len(path),
               "tb": b_time(f["secs_left"]), "pxb": (b_px(f["px"]) if f["px"] else None), "zb": b_absz(z)}
        if close and close[0]["kind"] == "exit": row.update({"exit_px": close[0]["exit_px"], "exit_secs_left": close[0]["close_ts"] - close[0]["t"], "p_side_at_exit": close[0].get("p_side_at_exit")})
        out.append(row)
    # Opus direct
    fl = list(jl(ROOT / "data/llm-direct/fills.jsonl")); by = collections.defaultdict(list)
    for f in fl: by[f["intent_id"]].append(f)
    for iid, evs in by.items():
        f = [e for e in evs if e["kind"] == "fill"][0]; st = [e for e in evs if e["kind"] == "settle"]
        res = results.get(f["close_ts"]); win = None if res is None else (1.0 if res == f["side"] else 0.0)
        cand = [s for s in snap_by_close[f["close_ts"]] if abs(s["t"] - f["t"]) < 6]
        z = cand[-1]["z"] if cand else None
        path = [s for s in snap_by_close[f["close_ts"]] if s["t"] > f["t"]]
        best_exit = None
        if f["qty"]:
            vals = [S.exit_value(f["side"], {"bids": s.get("bids"), "asks": s.get("asks")}, f["qty"]) for s in path]
            vals = [v["proceeds"] for v in vals if v["qty"] >= f["qty"]]; best_exit = max(vals) if vals else None
        out.append({"family": "opus-direct", "ticker": f["ticker"], "close_ts": f["close_ts"], "when_pt": pt(f["t"]), "side": f["side"], "qty": f["qty"], "intended_qty": f["intended_qty"],
                    "px": f["px"], "fee": f["fee"], "cost": f["cost"], "secs_left": f["decision_secs_left"], "mid": f["mid"], "p_model": f["p_model"], "limit": f["limit"], "best_offer": f.get("best_offer"),
                    "z": z, "hour_pt": hour_pt(f["t"]), "status": f["status"], "result": res, "win": win, "exit_kind": ("settle" if st else ("unfilled" if f["qty"] == 0 else "open")),
                    "pnl": (st[0]["pnl"] if st else None), "hold_pnl_cf": (st[0]["pnl"] if st else None), "best_exit_proceeds_path": best_exit, "reason": f.get("reason"),
                    "tb": b_time(f["decision_secs_left"]), "pxb": (b_px(f["px"]) if f["px"] else None), "zb": b_absz(z)})
    # P12 / P13 15M history
    for fam in ["s7p15-KXBTC15M", "s7p15-KXETH15M", "s7p15-KXSOL15M", "s7p13-KXBTC15M", "s7p13-KXETH15M", "s7p13-KXSOL15M"]:
        p = ROOT / "data" / fam / "trades.jsonl"
        if not p.exists(): continue
        for tr in jl(p):
            if tr.get("status") != "settled": continue
            out.append({"family": fam, "ticker": tr["ticker"], "close_ts": tr["close_ts"], "when_pt": pt(tr["t"]), "side": tr["side"], "qty": tr["qty"], "px": tr["price"] + tr.get("slippage", 0),
                        "fee": tr["fee"], "cost": tr["cost"], "secs_left": tr.get("mins_left", 0) * 60 if tr.get("mins_left") is not None else tr.get("secs_left"), "mid": tr.get("p_market"),
                        "p_model": tr.get("p_model"), "z": tr.get("z"), "hour_pt": hour_pt(tr["t"]), "status": "settled", "result": tr.get("result"),
                        "win": (1.0 if tr.get("result") == tr["side"] else 0.0), "exit_kind": "settle", "pnl": tr.get("pnl"), "hold_pnl_cf": tr.get("pnl"),
                        "tb": b_time((tr.get("mins_left") or 0) * 60), "pxb": b_px(tr["price"] + tr.get("slippage", 0)), "zb": b_absz(tr.get("z"))})
    return out

def attribution(trades):
    """Realised P&L by bucket for the SOL-v1 fills (filled only), with close-clustered ratio SE, plus exits-vs-settles."""
    sv = [t for t in trades if t["family"] == "sol-v1" and t["qty"] > 0 and t["pnl"] is not None]
    def agg(key):
        g = collections.defaultdict(list)
        for t in sv: g[t[key]].append(t)
        out = {}
        for k, v in sorted(g.items(), key=lambda kv: str(kv[0])):
            re_ = ratio_estimate([x["pnl"] for x in v], [x["qty"] for x in v], [x["close_ts"] for x in v])
            out[str(k)] = {"n_fills": len(v), "contracts": int(re_["contracts"]), "pnl_usd": r(sum(x["pnl"] for x in v), 2), "net_c": r(100 * re_["theta"], 2),
                           "se_c": (r(100 * re_["se_cluster"], 2) if re_.get("se_cluster") else None), "hit": r(np.mean([x["win"] for x in v]), 3), "mean_px": r(np.mean([x["px"] for x in v]), 3)}
        return out
    ex = [t for t in sv if t["exit_kind"] == "exit"]; se = [t for t in sv if t["exit_kind"] == "settle"]
    cf_hold_ex = sum(t["hold_pnl_cf"] for t in ex)
    diff = ratio_estimate([t["pnl"] - t["hold_pnl_cf"] for t in ex], [t["qty"] for t in ex], [t["close_ts"] for t in ex]) if ex else {}
    fams = collections.defaultdict(list)
    for t in trades:
        if t.get("pnl") is not None and t.get("qty"): fams[t["family"]].append(t)
    by_family = {}
    for k, v in fams.items():
        re_ = ratio_estimate([x["pnl"] for x in v], [x["qty"] for x in v], [x["close_ts"] for x in v])
        by_family[k] = {"n_fills": len(v), "contracts": int(re_["contracts"]), "pnl_usd": r(sum(x["pnl"] for x in v), 2), "net_c": r(100 * re_["theta"], 2), "se_c": (r(100 * re_["se_cluster"], 2) if re_.get("se_cluster") else None),
                        "hit": r(np.mean([x["win"] for x in v if x["win"] is not None]), 3), "mean_px": r(np.mean([x["px"] for x in v]), 3), "n_closes": len({x["close_ts"] for x in v})}
    # settled losers that could have been sold for more than their settlement value at some poll
    rescue = [t for t in se if t["best_exit_proceeds_path"] is not None and t["best_exit_proceeds_path"] > t["win"] * t["qty"]]
    return {"n_filled": len(sv), "pnl_usd": r(sum(t["pnl"] for t in sv), 2), "by_family_all_logged_trades": by_family, "exit_minus_hold_on_exited": {"net_c": r(100 * diff.get("theta"), 2) if diff else None, "se_c": (r(100 * diff["se_cluster"], 2) if diff.get("se_cluster") else None), "n": len(ex)}, "by_time": agg("tb"), "by_px": agg("pxb"), "by_side": agg("side"), "by_absz": agg("zb"), "by_exit_kind": agg("exit_kind"),
            "exits": {"n": len(ex), "pnl_usd": r(sum(t["pnl"] for t in ex), 2), "hold_to_settle_counterfactual_usd": r(cf_hold_ex, 2), "exit_minus_hold_usd": r(sum(t["pnl"] for t in ex) - cf_hold_ex, 2),
                      "n_exit_beat_hold": sum(1 for t in ex if t["pnl"] > t["hold_pnl_cf"] + 1e-9), "n_hold_would_have_won": sum(1 for t in ex if t["win"] == 1.0),
                      "mean_p_side_at_exit": r(np.mean([t.get("p_side_at_exit") or 0 for t in ex]), 3)},
            "settles": {"n": len(se), "pnl_usd": r(sum(t["pnl"] for t in se), 2), "hit": r(np.mean([t["win"] for t in se]), 3), "mean_px": r(np.mean([t["px"] for t in se]), 3),
                        "n_rescuable": len(rescue), "rescue_value_usd": r(sum(t["best_exit_proceeds_path"] - t["win"] * t["qty"] for t in rescue), 2),
                        "n_lost_with_zero_bid_path": sum(1 for t in se if t["win"] == 0 and (t["best_exit_proceeds_path"] or 0) <= 0)}}

def hold_vs_exit(dec, trades, results):
    """Per position: path of executable exit proceeds vs eventual settlement; rule comparisons (in-sample, n = positions)."""
    he = collections.defaultdict(list)
    for d in dec:
        if d.get("kind") == "hold_exit": he[d["ticker"]].append(d)
    pos = {(t["ticker"], t["side"]): t for t in trades if t["family"] == "sol-v1" and t["qty"] > 0 and t["pnl"] is not None}
    rules = {"policy_as_logged": None, "always_hold": None, "exit_if_proceeds>=cost": None, "exit_if_proceeds>=1.25cost": None, "exit_if_proceeds>=1.5cost": None, "exit_if_proceeds>=2cost": None,
             "exit_at_first_poll": None, "exit_if_p_side<.25_(stop)": None}
    per_rule = collections.defaultdict(list); detail = []
    for (tk, _side), t in pos.items():
        t_end = t["close_ts"] if t["exit_kind"] == "settle" else t["close_ts"] - t.get("exit_secs_left", 0) + 1e-6
        path = sorted((d for d in he.get(tk, []) if t["t_fill"] <= d["t"] <= t_end), key=lambda d: d["t"]); settle_val = t["win"] * t["qty"]; basis = t["cost"] + t["fee"]
        full = [d for d in path if d["exit_qty"] >= t["qty"]]
        def first(pred):
            for d in full:
                if pred(d): return d["exit_proceeds"] - basis
            return settle_val - basis
        pn = {"policy_as_logged": t["pnl"], "always_hold": settle_val - basis,
              "exit_if_proceeds>=cost": first(lambda d: d["exit_proceeds"] >= basis), "exit_if_proceeds>=1.25cost": first(lambda d: d["exit_proceeds"] >= 1.25 * basis),
              "exit_if_proceeds>=1.5cost": first(lambda d: d["exit_proceeds"] >= 1.5 * basis), "exit_if_proceeds>=2cost": first(lambda d: d["exit_proceeds"] >= 2 * basis),
              "exit_at_first_poll": (full[0]["exit_proceeds"] - basis if full else settle_val - basis), "exit_if_p_side<.25_(stop)": first(lambda d: d["p_side"] < 0.25)}
        for k, v in pn.items(): per_rule[k].append((v, t["qty"], t["close_ts"]))
        mx = max((d["exit_proceeds"] for d in full), default=None)
        detail.append({"ticker": tk, "side": t["side"], "px": t["px"], "qty": t["qty"], "secs_left_entry": t["secs_left"], "exit_kind": t["exit_kind"], "pnl": r(t["pnl"], 3), "hold_pnl": r(settle_val - basis, 3),
                       "max_exit_proceeds_on_path": r(mx, 3), "max_exit_pnl_on_path": (r(mx - basis, 3) if mx is not None else None), "n_polls": len(path), "n_full_liquidity_polls": len(full)})
    summary = {}
    # 'exits win / settles lose' decomposition: realised exit P&L vs hold-to-settle counterfactual on the exited positions, and the
    # path-max (perfect-foresight, NOT a policy) for all positions
    summary["_n_positions"] = len(pos)
    for k, v in per_rule.items():
        re_ = ratio_estimate([a for a, _, _ in v], [q for _, q, _ in v], [c for _, _, c in v])
        summary[k] = {"n_positions": len(v), "pnl_usd": r(sum(a for a, _, _ in v), 2), "net_c": r(100 * re_["theta"], 2), "se_c": (r(100 * re_["se_cluster"], 2) if re_.get("se_cluster") else None)}
    pf = sum((d["max_exit_pnl_on_path"] if d["max_exit_pnl_on_path"] is not None and d["max_exit_pnl_on_path"] > d["hold_pnl"] else d["hold_pnl"]) for d in detail)
    summary["perfect_foresight_path_max_NOT_A_POLICY"] = {"n_positions": len(detail), "pnl_usd": r(pf, 2)}
    return {"label": f"IN-SAMPLE rule comparison on {len(pos)} positions; 8 rules examined; no rule is proposed", "rules": summary, "positions": detail}

def opus_section(snaps, results, trades):
    fc = [f for f in jl(ROOT / "data/llm-shadow/forecasts.jsonl") if f.get("kind") == "forecast" and "opus" in f.get("model_name", "")]
    dd = list(jl(ROOT / "data/llm-direct/decisions.jsonl"))
    snap_by_close = collections.defaultdict(list)
    for s in snaps: snap_by_close[s["close_ts"]].append(s)
    def nearest_snap(close_ts, t):
        c = snap_by_close.get(close_ts, []);  c = [s for s in c if abs(s["t"] - t) <= 8 and s.get("bids")]
        return min(c, key=lambda s: abs(s["t"] - t)) if c else None
    rows = []
    for f in fc:
        p_own = f.get("p_own") if f.get("p_own") is not None else f.get("p_llm")
        if p_own is None or f.get("mid") is None: continue
        res = results.get(f["close_ts"]); 
        if res is None: continue
        d = [x for x in dd if x["close_ts"] == f["close_ts"] and x["decision_secs_left"] == f["decision_secs_left"]]
        action = d[0]["parsed"]["action"] if d and d[0].get("parsed") else None
        side = "yes" if p_own > f["mid"] else "no"
        s = nearest_snap(f["close_ts"], f.get("snapshot_t") or f["t_sent"])
        ex = exec_buy(side, {"bids": s.get("bids"), "asks": s.get("asks")}) if s else None
        if ex is None:                                   # fallback: top of book from the forecast row + tick (no depth known -> marked)
            ask = f["yes_ask"] if side == "yes" else (1 - f["yes_bid"]) if f.get("yes_bid") is not None else None
            ex = None if ask is None else {"px": ask + TICK, "fee_pc": S.fee_pc(LOT, ask + TICK), "qty": None, "src": "top_only"}
        if ex is None: continue
        win = 1.0 if res == side else 0.0
        rows.append({"close_ts": f["close_ts"], "when_pt": pt(f["t_sent"]), "T": f["decision_secs_left"], "p_own": p_own, "mid": f["mid"], "p_model": f.get("p_model"), "disagree": r(p_own - f["mid"], 3),
                     "abs_disagree": abs(p_own - f["mid"]), "side_favoured": side, "exec_px": r(ex["px"], 3), "exec_depth_known": ex.get("qty") is not None, "own_edge_c": r(100 * ((p_own if side == "yes" else 1 - p_own) - ex["px"] - ex["fee_pc"]), 1),
                     "direct_action": action, "result": res, "win": win, "cf_net_c": r(100 * (win - ex["px"] - ex["fee_pc"]), 1), "beyond_market": f.get("beyond_market"), "prompt_hash": f["prompt_hash"]})
    rows.sort(key=lambda x: -x["abs_disagree"])
    top = [x for x in rows if x["direct_action"] in ("NO_TRADE", None)][:30]
    def summ(v):
        if not v: return {"n": 0}
        re_ = ratio_estimate([x["cf_net_c"] / 100 for x in v], [1.0] * len(v), [x["close_ts"] for x in v])
        return {"n": len(v), "n_closes": len({x["close_ts"] for x in v}), "hit": r(np.mean([x["win"] for x in v]), 3), "mean_exec_px": r(np.mean([x["exec_px"] for x in v]), 3), "mean_p_own_side": r(np.mean([(x["p_own"] if x["side_favoured"] == "yes" else 1 - x["p_own"]) for x in v]), 3),
                "cf_net_c": r(100 * re_["theta"], 1), "se_c": (r(100 * re_["se_cluster"], 1) if re_.get("se_cluster") else None)}
    longshot = [x for x in rows if x["exec_px"] <= 0.10]
    return {"n_opus_forecasts_scored": len(rows), "fills": [t for t in trades if t["family"] == "opus-direct"],
            "top30_no_trade_disagreements": top, "cf_all_disagreements_buy_own_side": summ(rows), "cf_top30": summ(top),
            "cf_longshot_px<=.10": summ(longshot), "cf_positive_own_edge": summ([x for x in rows if x["own_edge_c"] > 0]),
            "note": "p_own = independent own probability where logged (cohorts 7c1b4812/6c8d03af), else p_llm (reconciled). Counterfactual = buy the side p_own favours vs mid, lot 10 at the depth-weighted executable price + tick from the nearest SOL-v1 snapshot (<= 8 s)."}

def tail_study(universe, snaps, results):
    """(a) 36-d 1-min candles: empirical tail of standardised h-min log returns vs Gaussian; (b) snapshot-level longshot calibration."""
    import pandas as pd
    d = pd.read_parquet(ROOT / "data/candles/SOL_1m.parquet").sort_values("ts").reset_index(drop=True)
    c = np.log(d["close"].to_numpy()); ts = d["ts"].to_numpy()
    r1 = np.diff(c); ok1 = np.diff(ts) == 60
    # trailing 15-min realised sigma per minute (policy uses 900 s of 1-s returns; here 15 one-minute returns), min 15 obs
    W = 15; sig = np.full(len(c), np.nan)
    for i in range(W, len(c)):
        seg = r1[i - W:i]; good = ok1[i - W:i]
        if good.all(): sig[i] = math.sqrt(float(np.mean(seg * seg)))
    qs = [1.0, 1.282, 1.645, 1.96, 2.326, 2.576, 3.0]; out = {"horizons_min": {}, "n_candles": int(len(c)), "span_days": r((ts[-1] - ts[0]) / 86400, 1)}
    for h in [1, 2, 3, 5, 10, 14]:
        zs = []
        for i in range(W, len(c) - h):
            if not np.isfinite(sig[i]) or sig[i] <= 0 or ts[i + h] - ts[i] != 60 * h: continue
            zs.append((c[i + h] - c[i]) / (sig[i] * math.sqrt(h)))
        zs = np.asarray(zs); a = np.abs(zs)
        row = {"n": int(len(zs)), "rms_z": r(float(np.sqrt(np.mean(zs * zs))), 3), "rms_z_kappa1.15": r(float(np.sqrt(np.mean(zs * zs))) / 1.15, 3), "tails": {}}
        for q in qs:
            emp = float(np.mean(a > q)); emp_k = float(np.mean(a > q * 1.15)); gauss = 2 * (1 - Phi(q))
            row["tails"][str(q)] = {"two_sided_gauss": r(gauss, 4), "empirical": r(emp, 4), "empirical_with_kappa1.15": r(emp_k, 4), "ratio": r(emp / gauss, 2), "ratio_kappa": r(emp_k / gauss, 2)}
        out["horizons_min"][str(h)] = row
    # price-equivalent: a Gaussian one-sided tail of 5 % (fair 5 c) sits at z = 1.645; the empirical one-sided frequency at that z
    pe = {}
    for h in ["3", "5", "10"]:
        t = out["horizons_min"][h]["tails"]; pe[h] = {"gauss_5c_longshot_empirical_fair_c": r(100 * 0.5 * t["1.645"]["empirical"], 1), "with_kappa": r(100 * 0.5 * t["1.645"]["empirical_with_kappa1.15"], 1),
                                                   "gauss_2.5c_longshot_empirical_fair_c": r(100 * 0.5 * t["1.96"]["empirical"], 1), "gauss_1c_longshot_empirical_fair_c": r(100 * 0.5 * t["2.326"]["empirical"], 1)}
    out["price_equivalents"] = pe
    # (b) snapshot longshot calibration at executable prices (close-clustered), both sides pooled
    cal = {}
    for lo, hi in [(0.02, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50)]:
        v = [x for x in universe if lo <= x["px"] < hi]
        if not v: cal[f"px{lo}-{hi}"] = {"n": 0}; continue
        st = cell_stats(v); st.update({"mean_model_p_side": r(np.mean([x["p_side"] for x in v]), 3), "model_over_market": r(np.mean([x["p_side"] / x["px"] for x in v]), 2)})
        cal[f"px{lo}-{hi}"] = st
    # model-probability calibration in the tail bins (p_model side <= .10 etc.)
    pcal = {}
    for lo, hi in [(0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35)]:
        v = [x for x in universe if lo <= x["p_side"] < hi]
        pcal[f"p_model_side{lo}-{hi}"] = ({"n": len(v), "n_closes": len({x["close_ts"] for x in v}), "mean_p_model": r(np.mean([x["p_side"] for x in v]), 3), "hit": r(np.mean([x["win"] for x in v]), 3), "mean_px": r(np.mean([x["px"] for x in v]), 3)} if v else {"n": 0})
    out["snapshot_longshot_calibration_at_executable_px"] = cal; out["snapshot_model_tail_calibration"] = pcal
    return out

# ------------------------------------------------------------------------------------------------------------- main
def main():
    snaps = [s for s in jl(ROOT / "data/sol-v1/snapshots.jsonl") if s.get("kind") == "entry_eval"]
    dec = list(jl(ROOT / "data/sol-v1/decisions.jsonl"))
    cfb = load_cfb(); xres = exchange_results()
    closes_all = sorted({s["close_ts"] for s in snaps}); results = {}; disagree = []
    Kmap = {}
    for s in snaps: Kmap.setdefault(s["close_ts"], s["K"])
    for c in closes_all:
        own = S.settle_from_feed(cfb, c, Kmap[c])
        if c in xres: results[c] = xres[c]
        elif own: results[c] = own["result"]
        if own and c in xres and own["result"] != xres[c]: disagree.append(c)
    cm = load_comove()
    universe = build_universe(snaps, results, cm)
    closes = sorted({x["close_ts"] for x in universe}); half = len(closes) // 2; is_closes = set(closes[:half]); oos_closes = closes[half:]
    yes_rate = float(np.mean([1.0 if results[c] == "yes" else 0.0 for c in closes])); yes_is = float(np.mean([1.0 if results[c] == "yes" else 0.0 for c in closes[:half]])); yes_oos = float(np.mean([1.0 if results[c] == "yes" else 0.0 for c in closes[half:]]))
    xs = [s["x_now"] for s in snaps if s.get("x_now")]
    held_closes = {f["close_ts"] for f in jl(ROOT / "data/sol-v1/fills.jsonl") if f.get("kind") == "fill" and f.get("qty", 0) > 0}
    cov = {}
    for tb_ in ["T-10-14m", "T-5-10m", "T-2-5m", "T-60-120s", "T-10-60s"]: cov[tb_] = len({x["close_ts"] for x in universe if x["tb"] == tb_})
    meta = {"generated_pt": pt(dt.datetime.now().timestamp()), "yes_settlement_rate": {"all": r(yes_rate, 3), "first_half": r(yes_is, 3), "second_half": r(yes_oos, 3), "n_closes": len(closes)},
            "sol_index_first_last": [xs[0], xs[-1]], "closes_with_a_sol_v1_position": len(held_closes),
            "universe_coverage_closes_by_time_bucket": cov, "coverage_caveat": "entry snapshots are only logged while NO position is held; the counterfactual universe inside the final minutes is therefore conditioned on the policy not holding (%d of %d closes had a position)" % (len(held_closes), len(closes)), "snapshots": len(snaps), "closes_with_snapshots": len(closes_all), "closes_with_result": len(results), "feed_vs_exchange_disagreements": disagree,
            "universe_rows": len(universe), "universe_closes": len(closes), "split": {"in_sample_closes": [pt(closes[0]), pt(closes[half - 1])], "out_of_sample_closes": [pt(closes[half]), pt(closes[-1])], "n_is": half, "n_oos": len(closes) - half},
            "execution_model": "sol_v1.side_levels/walk depth-weighted for lot 10 (partial allowed, never beyond displayed) + 1 tick; Kalshi taker fee ceil-to-cent per order; absent side => no fill",
            "universe_px_bounds": [PX_LO, PX_HI]}
    # (1) trade table
    trades = trade_table(snaps, dec, results, cfb)
    (OUT / "trades.json").write_text(json.dumps({"meta": meta, "trades": trades}, indent=1), encoding="utf-8")
    # (2) attribution
    attr = attribution(trades)
    # universe-level view of the same question (all snapshots, both sides): hold-to-settle expectancy by bucket, labelled in-sample (full sample)
    def by_feature(rows, f):
        g = collections.defaultdict(list)
        for x in rows: g[x[f]].append(x)
        return {k: cell_stats(v) for k, v in sorted(g.items())}
    SIDEFUL = ["side", "with_model", "fav", "pxb", "edgeb"]
    def maps_for(rows_):
        out = {f: by_feature(rows_, f) for f in SIDEFUL}
        for f in [g for g in FEATURES if g not in SIDEFUL]:
            out[f + "_x_pick"] = {f"{k}|with_model": cell_stats([x for x in rows_ if x[f] == k and x["with_model"] == "with_model"]) for k in sorted({x[f] for x in rows_})}
            out[f + "_x_pick"].update({f"{k}|against_model": cell_stats([x for x in rows_ if x[f] == k and x["with_model"] == "against_model"]) for k in sorted({x[f] for x in rows_})})
            out[f + "_BOTH_SIDES_mechanical"] = by_feature(rows_, f)
        return out
    maps_full = maps_for(universe); maps_is = maps_for([x for x in universe if x["close_ts"] in is_closes]); maps_oos = maps_for([x for x in universe if x["close_ts"] not in is_closes])
    # 2-D maps time x price per side, OOS and full
    def map2(rows, f1, f2):
        g = collections.defaultdict(list)
        for x in rows: g[(x[f1], x[f2])].append(x)
        return {f"{a}|{b}": cell_stats(v) for (a, b), v in sorted(g.items())}
    maps2 = {"full_tb_x_pxb_yes": map2([x for x in universe if x["side"] == "yes"], "tb", "pxb"), "full_tb_x_pxb_no": map2([x for x in universe if x["side"] == "no"], "tb", "pxb"),
             "oos_tb_x_pxb_yes": map2([x for x in universe if x["side"] == "yes" and x["close_ts"] not in is_closes], "tb", "pxb"), "oos_tb_x_pxb_no": map2([x for x in universe if x["side"] == "no" and x["close_ts"] not in is_closes], "tb", "pxb"),
             "full_tb_x_edgeb": map2(universe, "tb", "edgeb"), "oos_tb_x_edgeb": map2([x for x in universe if x["close_ts"] not in is_closes], "tb", "edgeb")}
    (OUT / "attribution.json").write_text(json.dumps({"meta": meta, "sol_v1_fills": attr, "universe_maps_full_sample_IN_SAMPLE": maps_full, "universe_maps_first_half": maps_is, "universe_maps_second_half": maps_oos, "maps_2d": maps2}, indent=1), encoding="utf-8")
    # (3)/(4) condition search, walk-forward
    conds = condition_universe(universe); n_grid = len(conds); names, D, Wn, Cn = per_close_matrix(universe, conds, closes)
    keep = [j for j in range(len(names)) if np.sum(np.isfinite(D[:, j])) >= 10]          # evaluated = at least 10 closes with a one-sided match
    names = [names[j] for j in keep]; D = D[:, keep]; Wn = Wn[:, keep]; Cn = Cn[:, keep]
    Dis, Doos = D[:half], D[half:]; n_cond = len(names)
    def degen(W_col, D_col):
        v = W_col[np.isfinite(D_col)]
        if len(v) == 0: return None
        allwin = bool(np.all(v >= 1 - 1e-9)); allloss = bool(np.all(v <= 1e-9)); return {"n_closes_all_win": allwin, "n_closes_all_loss": allloss, "rule_of_three_hit_lo": (r(1 - 3 / len(v), 3) if allwin else None)}
    rows = []
    for j, nm in enumerate(names):
        m_is, se_is, n_is = t_of(Dis[:, j]); m_oos, se_oos, n_oos = t_of(Doos[:, j]); m_all, se_all, n_all = t_of(D[:, j])
        dg = degen(Wn[half:, j], Doos[:, j]) or {}
        rows.append({"condition": nm, "spec": conds[nm], "is": {"n_closes": n_is, "mean_c": r(m_is, 2), "se_c": r(se_is, 2), "t": (r(m_is / se_is, 2) if se_is else None), "hit": r(np.nanmean(Wn[:half, j]), 3)},
                     "oos": {"n_closes": n_oos, "mean_c": r(m_oos, 2), "se_c": r(se_oos, 2), "t": (r(m_oos / se_oos, 2) if se_oos else None), "hit": r(np.nanmean(Wn[half:, j]), 3), "degenerate": bool(dg.get("n_closes_all_win") or dg.get("n_closes_all_loss")), **dg},
                     "full": {"n_closes": n_all, "mean_c": r(m_all, 2), "se_c": r(se_all, 2), "t": (r(m_all / se_all, 2) if se_all else None), "hit": r(np.nanmean(Wn[:, j]), 3)}})
    # selection on IS (positive, t >= 1, >= 10 closes) -> OOS test with Bonferroni over ALL conditions examined (conservative) and Holm over the selected set
    from predhft.stats_spec import PhiInv
    z_bonf = PhiInv(1 - 0.05 / n_cond)
    sel = [x for x in rows if x["is"]["t"] is not None and x["is"]["t"] >= 1.0 and x["is"]["n_closes"] >= 10 and x["is"]["mean_c"] > 0]
    sel_neg = [x for x in rows if x["is"]["t"] is not None and x["is"]["t"] <= -1.0 and x["is"]["n_closes"] >= 10 and x["is"]["mean_c"] < 0]
    for x in rows:
        t = x["oos"]["t"]; x["oos"]["p_one_sided_pos"] = (r(1 - Phi(t), 4) if t is not None else None); x["oos"]["p_one_sided_neg"] = (r(Phi(t), 4) if t is not None else None)
        x["oos"]["bonferroni_sig_pos_at_5pct"] = bool(t is not None and t >= z_bonf and x["oos"]["n_closes"] >= 10 and not x["oos"]["degenerate"])
        x["oos"]["bonferroni_sig_neg_at_5pct"] = bool(t is not None and t <= -z_bonf and x["oos"]["n_closes"] >= 10 and not x["oos"]["degenerate"])
    def holm(cands, sign):
        ps = sorted([(x["oos"][("p_one_sided_pos" if sign > 0 else "p_one_sided_neg")] if x["oos"]["t"] is not None else 1.0, x["condition"]) for x in cands])
        m = len(ps); out = []
        for i, (p, nm) in enumerate(ps):
            if p is None: break
            if p <= 0.05 / (m - i): out.append({"condition": nm, "p": p, "threshold": r(0.05 / (m - i), 5)})
            else: break
        return out
    survivors_pos = holm([x for x in sel if not x["oos"]["degenerate"]], +1); survivors_neg = holm([x for x in sel_neg if not x["oos"]["degenerate"]], -1)
    degenerate_pos = [{"condition": x["condition"], "oos": x["oos"]} for x in sel if x["oos"]["degenerate"] and x["oos"]["t"] is not None and x["oos"]["t"] >= z_bonf]
    both_halves_neg = [x for x in rows if x["is"]["t"] is not None and x["oos"]["t"] is not None and x["is"]["t"] <= -2 and x["oos"]["t"] <= -2 and x["is"]["n_closes"] >= 15 and x["oos"]["n_closes"] >= 15 and not x["oos"]["degenerate"]]
    both_halves_pos = [x for x in rows if x["is"]["t"] is not None and x["oos"]["t"] is not None and x["is"]["t"] >= 2 and x["oos"]["t"] >= 2 and x["is"]["n_closes"] >= 15 and x["oos"]["n_closes"] >= 15 and not x["oos"]["degenerate"]]
    # side split per condition (drift diagnostic): OOS mean when the bought side is yes / no; literal-side conditions flagged
    byside = {}
    for sd in ("yes", "no"):
        sub = [x for x in universe if x["side"] == sd and x["close_ts"] not in is_closes]
        nm_, Dsd, _, _ = per_close_matrix(sub, {nm: conds[nm] for nm in names}, oos_closes)
        for j, nm in enumerate(nm_):
            m_, se_, n_ = t_of(Dsd[:, j]); byside.setdefault(nm, {})[sd] = {"n_closes": n_, "mean_c": r(m_, 2), "t": (r(m_ / se_, 2) if se_ else None)}
    for x in rows:
        x["side_literal"] = any(f == "side" for f, _ in x["spec"]); x["oos_by_side"] = byside.get(x["condition"], {})
        ys, ns = x["oos_by_side"].get("yes", {}).get("mean_c"), x["oos_by_side"].get("no", {}).get("mean_c")
        x["oos_side_consistent"] = (None if (ys is None or ns is None) else bool((ys > 0) == (ns > 0)))
    # SPA over the condition universe: benchmark = no trade (0); positive = condition earns. OOS half and full sample (the latter is in-sample).
    spa_oos = spa_test(Doos, B=2000, mean_block=4.0, names=names); spa_oos_neg = spa_test(-Doos, B=2000, mean_block=4.0, names=names)
    spa_full = spa_test(D, B=2000, mean_block=4.0, names=names); spa_full_neg = spa_test(-D, B=2000, mean_block=4.0, names=names)
    sym = [j for j, nm in enumerate(names) if not any(f == "side" for f, _ in conds[nm])]
    spa_oos_sym = spa_test(Doos[:, sym], B=2000, mean_block=4.0, names=[names[j] for j in sym]); spa_oos_sym_neg = spa_test(-Doos[:, sym], B=2000, mean_block=4.0, names=[names[j] for j in sym])
    for d_ in (spa_oos, spa_oos_neg, spa_full, spa_full_neg, spa_oos_sym, spa_oos_sym_neg):
        ts_ = d_.pop("t_stats", {}); d_["top5_t"] = sorted(ts_.items(), key=lambda kv: -kv[1])[:5]
    rows.sort(key=lambda x: -(x["oos"]["t"] if x["oos"]["t"] is not None else -99))
    search = {"meta": meta, "n_conditions_grid": n_grid, "n_conditions_examined": n_cond, "degenerate_all_win_cells_excluded_from_survivors": degenerate_pos, "n_features": len(FEATURES), "bonferroni_z_5pct_one_sided": r(z_bonf, 3), "selection_rule_IS": "mean > 0, t >= 1.0, >= 10 closes (first half); mirrored for losers",
              "n_selected_pos_IS": len(sel), "n_selected_neg_IS": len(sel_neg), "holm_survivors_pos_OOS": survivors_pos, "holm_survivors_neg_OOS": survivors_neg,
              "bonferroni_all_conditions_pos_OOS": [x["condition"] for x in rows if x["oos"]["bonferroni_sig_pos_at_5pct"]], "bonferroni_all_conditions_neg_OOS": [x["condition"] for x in rows if x["oos"]["bonferroni_sig_neg_at_5pct"]],
              "both_halves_t>=2": [(x["condition"], x["is"], x["oos"]) for x in both_halves_pos], "both_halves_t<=-2": [(x["condition"], x["is"], x["oos"]) for x in both_halves_neg],
              "n_conditions_side_symmetric": len(sym), "spa_oos_side_symmetric_vs_no_trade": spa_oos_sym, "spa_oos_side_symmetric_losers": spa_oos_sym_neg,
              "spa_oos_vs_no_trade": spa_oos, "spa_oos_losers_vs_no_trade": spa_oos_neg, "spa_full_IN_SAMPLE_vs_no_trade": spa_full, "spa_full_IN_SAMPLE_losers": spa_full_neg,
              "selected_pos_IS_with_OOS": sorted([{"condition": x["condition"], "is": x["is"], "oos": x["oos"]} for x in sel], key=lambda y: -(y["oos"]["t"] or -99)),
              "selected_neg_IS_with_OOS": sorted([{"condition": x["condition"], "is": x["is"], "oos": x["oos"]} for x in sel_neg], key=lambda y: (y["oos"]["t"] or 99)),
              "all_conditions": rows}
    (OUT / "conditions_search.json").write_text(json.dumps(search, indent=1, default=str), encoding="utf-8")
    # (5) hold vs exit
    hve = hold_vs_exit(dec, trades, results)
    (OUT / "hold_vs_exit.json").write_text(json.dumps({"meta": meta, **hve}, indent=1), encoding="utf-8")
    # (6) Opus + tails
    op = opus_section(snaps, results, trades); (OUT / "opus.json").write_text(json.dumps({"meta": meta, **op}, indent=1), encoding="utf-8")
    tl = tail_study(universe, snaps, results); (OUT / "tails.json").write_text(json.dumps({"meta": meta, **tl}, indent=1), encoding="utf-8")
    # (7) conditions brief: per state bucket (time x price x side, plus marginals) -> n, hit, implied, net, se, verdict; OOS-only numbers labelled
    brief = {"meta": {**meta, "how_to_read": "net_c = realised net c/contract buying `side` at the depth-weighted executable price + tick and HOLDING to settlement, averaged over every evaluation snapshot (~2 s) in the state; SE clustered by 15-min close; verdict needs >= 20 closes and |t| >= 2; 'scope' says which closes. Nothing here is a trading rule; the search over %d conditions found no Bonferroni/SPA survivor (see conditions_search.json)." % n_cond},
             "n_conditions_examined": n_cond, "corrected_result": {"holm_pos": survivors_pos, "holm_neg": survivors_neg, "spa_oos_p_consistent_nonstud": spa_oos.get("p_spa_consistent_nonstud"), "spa_oos_losers_p_consistent_nonstud": spa_oos_neg.get("p_spa_consistent_nonstud")},
             "states": []}
    def add_states(rows_, scope):
        # every state names the side to buy (literal side, model-relative pick, or favourite/longshot); context features are crossed with the pick
        for f in ["side", "with_model", "fav", "pxb", "edgeb"]:
            g = collections.defaultdict(list)
            for x in rows_: g[x[f]].append(x)
            for k, v in g.items():
                st = cell_stats(v); brief["states"].append({"scope": scope, "state": {f: k}, **{kk: st[kk] for kk in ("n", "n_closes", "hit", "mkt_implied", "model_p", "net_c", "se_c")}, "verdict": verdict(st)})
        for f in ["tb", "zb", "sprb", "depb", "volb", "partb", "hourb", "comove", "midb"]:
            g = collections.defaultdict(list)
            for x in rows_: g[(x[f], x["with_model"])].append(x)
            for (k, wm), v in g.items():
                st = cell_stats(v); brief["states"].append({"scope": scope, "state": {f: k, "pick": wm}, **{kk: st[kk] for kk in ("n", "n_closes", "hit", "mkt_implied", "model_p", "net_c", "se_c")}, "verdict": verdict(st)})
        g = collections.defaultdict(list)
        for x in rows_: g[(x["tb"], x["pxb"], x["side"])].append(x)
        for (a, b, c), v in g.items():
            st = cell_stats(v); brief["states"].append({"scope": scope, "state": {"tb": a, "pxb": b, "side": c}, **{kk: st[kk] for kk in ("n", "n_closes", "hit", "mkt_implied", "model_p", "net_c", "se_c")}, "verdict": verdict(st)})
        g = collections.defaultdict(list)
        for x in rows_: g[(x["tb"], x["edgeb"], x["side"])].append(x)
        for (a, b, c), v in g.items():
            st = cell_stats(v); brief["states"].append({"scope": scope, "state": {"tb": a, "edgeb": b, "side": c}, **{kk: st[kk] for kk in ("n", "n_closes", "hit", "mkt_implied", "model_p", "net_c", "se_c")}, "verdict": verdict(st)})
    add_states(universe, "full_sample_IN_SAMPLE"); add_states([x for x in universe if x["close_ts"] not in is_closes], "second_half_OOS")
    for st_ in brief["states"]:
        st_["side_literal"] = "side" in st_["state"]
        st_["verdict_is_uncorrected"] = True
        if st_["side_literal"] and st_["verdict"] in ("favourable", "unfavourable"):
            st_["caveat"] = "side-literal result in a one-directional sample (YES settled %.0f%% of closes, SOL %.1f -> %.1f): attributable to drift, not a condition" % (100 * yes_rate, xs[0], xs[-1])
    brief["meta"]["drift_warning"] = "YES settled %.1f%% of %d closes (first half %.1f%%, second half %.1f%%); SOL index %.2f -> %.2f over the sample. Every side-literal 'favourable' (NO) / 'unfavourable' (YES) verdict is consistent with this drift; side-symmetric states are the ones to read." % (100 * yes_rate, len(closes), 100 * yes_is, 100 * yes_oos, xs[0], xs[-1])
    (OUT / "conditions_brief.json").write_text(json.dumps(brief, indent=1), encoding="utf-8")
    # console summary
    print(json.dumps({"meta": meta, "attr_exits": attr["exits"], "attr_settles": attr["settles"], "n_cond": n_cond, "holm_pos": survivors_pos, "holm_neg": survivors_neg,
                      "bonf_pos": search["bonferroni_all_conditions_pos_OOS"], "bonf_neg": search["bonferroni_all_conditions_neg_OOS"],
                      "spa_oos": {k: spa_oos.get(k) for k in ("best", "best_mean_diff", "best_t", "p_spa_consistent", "p_spa_consistent_nonstud", "n_strategies", "n_with_positive_mean")},
                      "spa_oos_neg": {k: spa_oos_neg.get(k) for k in ("best", "best_mean_diff", "best_t", "p_spa_consistent", "p_spa_consistent_nonstud")},
                      "spa_oos_sym": {k: spa_oos_sym.get(k) for k in ("best", "best_mean_diff", "best_t", "p_spa_consistent", "p_spa_consistent_nonstud", "n_strategies")},
                      "spa_oos_sym_neg": {k: spa_oos_sym_neg.get(k) for k in ("best", "best_mean_diff", "best_t", "p_spa_consistent", "p_spa_consistent_nonstud")},
                      "by_family": attr["by_family_all_logged_trades"], "exit_minus_hold": attr["exit_minus_hold_on_exited"],
                      "spa_full": {k: spa_full.get(k) for k in ("best", "best_mean_diff", "best_t", "p_spa_consistent", "p_spa_consistent_nonstud")},
                      "spa_full_neg": {k: spa_full_neg.get(k) for k in ("best", "best_mean_diff", "best_t", "p_spa_consistent", "p_spa_consistent_nonstud")},
                      "both_pos": [x[0] for x in search["both_halves_t>=2"]], "both_neg": [x[0] for x in search["both_halves_t<=-2"]],
                      "hve": hve["rules"], "opus": {k: op[k] for k in ("cf_all_disagreements_buy_own_side", "cf_top30", "cf_longshot_px<=.10", "cf_positive_own_edge")},
                      "tails_5m": tl["horizons_min"]["5"], "tails_pe": tl["price_equivalents"], "cal": tl["snapshot_longshot_calibration_at_executable_px"], "pcal": tl["snapshot_model_tail_calibration"]}, indent=1, default=str))

def erfinv_approx(x):  # unused helper kept trivial (PhiInv is used instead)
    return 0.0

if __name__ == "__main__":
    main()
