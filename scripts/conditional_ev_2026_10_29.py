"""Conditional net-expectancy audit for KXSOL15M paper trading (SOL-v1 snapshots), 2026-10-29.

Question: under which observable conditions did taken AND counterfactual trades earn positive net expectancy at executable prices?
Design (binding guards):
  * counterfactual = buy 1 contract of a side at the recorded book via sol_v1.paper_fill (depth walk + 1 tick), hold to settlement;
    settlement from the official 1 Hz feed (settle_from_feed); absent-side books (no ask) are NOT filled.
  * one evaluation per (close, side, 30-s time bucket) to limit serial duplication; SE clustered by close.
  * walk-forward: conditions screened on the first 50% of closes (time-ordered), reported on the last 50%.
  * multiple comparisons: Bonferroni over the number of conditions examined (reported).
Outputs docs/CONDITIONAL_EV_2026-10-29.md and data/sol-v1/conditional_ev_2026-10-29.json
"""
from __future__ import annotations
import json, math, sys, bisect, collections, datetime as dt
from zoneinfo import ZoneInfo
sys.path.insert(0, "src")
from predhft import sol_v1
from predhft.fees import kalshi_taker_fee

PT = ZoneInfo("America/Los_Angeles")
ROOT = "."
# ------------------------------------------------------------------ official index series
series = []
with open(f"{ROOT}/data/cfb/SOL.jsonl") as f:
    for l in f:
        try: r = json.loads(l)
        except Exception: continue
        series.append((int(r["value_ts_ms"] // 1000), float(r["value"])))
series.sort(); dedup = {}
for s, v in series: dedup[s] = v
series = sorted(dedup.items())

snaps = []
with open(f"{ROOT}/data/sol-v1/snapshots.jsonl") as f:
    for l in f:
        try: snaps.append(json.loads(l))
        except Exception: pass
closes = sorted({(s["close_ts"], s["K"]) for s in snaps})
settle = {}
for c, K in closes:
    r = sol_v1.settle_from_feed(series, c, K)
    if r: settle[c] = r["result"]
print("snapshots", len(snaps), "closes", len(closes), "settled-from-feed", len(settle))

# ------------------------------------------------------------------ counterfactual rows
def book_of(s): return {"bids": s.get("bids") or [], "asks": s.get("asks") or [], "yes_bid": s.get("yes_bid"), "yes_ask": s.get("yes_ask")}
rows = []; seen = set()
for s in snaps:
    c = s["close_ts"]
    if c not in settle or s.get("mid") is None or s.get("p_model") is None: continue
    tb = int(s["secs_left"] // 30)
    for side in ("yes", "no"):
        key = (c, side, tb)
        if key in seen: continue
        seen.add(key)
        b = book_of(s)
        lv = sol_v1.side_levels(b, side, "buy")
        if not lv: continue
        best = lv[0][0]
        if best >= 0.995: continue                      # absent-side 1.00 fallback: never fill
        fl = sol_v1.paper_fill(side, 1, best, b)
        if fl["qty"] < 1: continue
        px = fl["px"]; fee = fl["fee"]
        win = 1.0 if settle[c] == side else 0.0
        pnl = win - px - fee
        p_side = s["p_model"] if side == "yes" else 1 - s["p_model"]
        rows.append({"close": c, "side": side, "secs_left": s["secs_left"], "px": px, "pnl": pnl, "z": s.get("z"), "p_side": p_side,
                     "edge": p_side - px - fee, "spread": (s["yes_ask"] - s["yes_bid"]) if s.get("yes_bid") is not None else None,
                     "sigma": s.get("sigma_1s"), "n_obs": s.get("n_obs") or 0, "hour": dt.datetime.fromtimestamp(c, PT).hour,
                     "depth": lv[0][1]})
print("cf rows", len(rows))

# ------------------------------------------------------------------ conditions
def cond_defs(rs):
    sig_med = sorted(r["sigma"] for r in rs if r["sigma"])[len(rs) // 2]
    C = {}
    for lo, hi in [(0, 60), (60, 180), (180, 300), (300, 480), (480, 900)]:
        C[f"secs_left[{lo},{hi})"] = lambda r, lo=lo, hi=hi: lo <= r["secs_left"] < hi
    for lo, hi in [(0, 0.5), (0.5, 1), (1, 2), (2, 99)]:
        C[f"|z|[{lo},{hi})"] = lambda r, lo=lo, hi=hi: r["z"] is not None and lo <= abs(r["z"]) < hi
    for lo, hi in [(0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1)]:
        C[f"px[{lo},{hi})"] = lambda r, lo=lo, hi=hi: lo <= r["px"] < hi
    for sd in ("yes", "no"): C[f"side={sd}"] = lambda r, sd=sd: r["side"] == sd
    C["spread<=0.02"] = lambda r: r["spread"] is not None and r["spread"] <= 0.02
    C["spread>0.02"] = lambda r: r["spread"] is not None and r["spread"] > 0.02
    C["depth>=100"] = lambda r: r["depth"] >= 100
    C["depth<100"] = lambda r: r["depth"] < 100
    C["sigma<=median"] = lambda r: r["sigma"] is not None and r["sigma"] <= sig_med
    C["sigma>median"] = lambda r: r["sigma"] is not None and r["sigma"] > sig_med
    C["partial_started"] = lambda r: r["n_obs"] > 0
    C["partial_not_started"] = lambda r: r["n_obs"] == 0
    for h0, h1 in [(0, 6), (6, 12), (12, 18), (18, 24)]:
        C[f"hourPT[{h0},{h1})"] = lambda r, h0=h0, h1=h1: h0 <= r["hour"] < h1
    for e in (0.0, 0.02, 0.05, 0.10):
        C[f"model_edge>={e}"] = lambda r, e=e: r["edge"] >= e
    C["model_edge<0"] = lambda r: r["edge"] < 0
    # two-way interactions used by the live rule family: model-favoured side x price bucket, and x time
    for lo, hi in [(0, 0.1), (0.1, 0.3), (0.3, 0.7), (0.7, 1)]:
        C[f"model_edge>=0 & px[{lo},{hi})"] = lambda r, lo=lo, hi=hi: r["edge"] >= 0 and lo <= r["px"] < hi
    for lo, hi in [(0, 60), (60, 180), (180, 480), (480, 900)]:
        C[f"model_edge>=0 & secs_left[{lo},{hi})"] = lambda r, lo=lo, hi=hi: r["edge"] >= 0 and lo <= r["secs_left"] < hi
    return C

def clustered(rs):
    """Mean pnl per contract and close-clustered SE (cluster = close; equal weight per row)."""
    if not rs: return None
    by = collections.defaultdict(list)
    for r in rs: by[r["close"]].append(r["pnl"])
    n = len(rs); mean = sum(r["pnl"] for r in rs) / n
    G = len(by)
    if G < 2: return {"n": n, "closes": G, "mean": mean, "se": float("nan"), "t": float("nan")}
    # cluster-robust variance of the mean: sum over clusters of (sum of residuals)^2 / n^2, small-sample G/(G-1)
    v = sum((sum(p - mean for p in ps)) ** 2 for ps in by.values()) / n ** 2 * G / (G - 1)
    se = math.sqrt(v)
    return {"n": n, "closes": G, "mean": mean, "se": se, "t": (mean / se if se > 0 else float("nan"))}

close_order = sorted(settle)
split = close_order[len(close_order) // 2]
train = [r for r in rows if r["close"] < split]; test = [r for r in rows if r["close"] >= split]
C = cond_defs(rows)
K_tests = len(C)
from statistics import NormalDist
N = NormalDist()
bonf_z = N.inv_cdf(1 - 0.05 / K_tests)      # one-sided, positive expectancy
res = []
for name, fn in C.items():
    a = clustered([r for r in train if fn(r)]); b = clustered([r for r in test if fn(r)]); f = clustered([r for r in rows if fn(r)])
    res.append({"condition": name, "train": a, "test": b, "full": f})
all_cf = clustered(rows)
# screening: significant positive in-sample at raw 5%, then test out-of-sample with Bonferroni over K_tests
screened = [r for r in res if r["train"] and r["train"]["n"] >= 30 and r["train"]["t"] > 1.645]
survivors = [r for r in screened if r["test"] and r["test"]["n"] >= 30 and r["test"]["t"] > bonf_z]
full_bonf = [r for r in res if r["full"] and r["full"]["n"] >= 30 and r["full"]["t"] > bonf_z]

# ------------------------------------------------------------------ taken trades (SOL-v1 deterministic paper)
fills = [json.loads(l) for l in open(f"{ROOT}/data/sol-v1/fills.jsonl")]
pos = {}
for r in fills:
    if r["kind"] == "fill":
        pos.setdefault(r["intent_id"], {"ticker": r["ticker"], "close": r["close_ts"], "side": r["side"], "px": r["px"], "qty": r["qty"], "secs_left": r["secs_left"], "cost": r["cost"] + r["fee"], "pnl": None, "end": None, "z": None})
        p = pos[r["intent_id"]]; p["cost"] = r["cost"] + r["fee"]
    elif r["kind"] in ("exit", "settle"):
        p = pos.get(r["intent_id"])
        if p is None: continue
        if r["kind"] == "exit": p["pnl"] = r.get("pnl", (r.get("proceeds", 0) - p["cost"])); p["end"] = "exit"
        else: p["pnl"] = r["pnl"]; p["end"] = "settle"
taken = [p for p in pos.values() if p["pnl"] is not None]
def tk_stats(ps):
    if not ps: return None
    rs = [{"close": p["close"], "pnl": p["pnl"] / p["qty"]} for p in ps]
    return clustered(rs)
taken_conds = {
    "all": taken, "end=exit": [p for p in taken if p["end"] == "exit"], "end=settle": [p for p in taken if p["end"] == "settle"],
    "px<0.10": [p for p in taken if p["px"] < 0.10], "px[0.10,0.30)": [p for p in taken if 0.10 <= p["px"] < 0.30], "px>=0.30": [p for p in taken if p["px"] >= 0.30],
    "side=yes": [p for p in taken if p["side"] == "yes"], "side=no": [p for p in taken if p["side"] == "no"],
    "secs_left>=300": [p for p in taken if p["secs_left"] >= 300], "secs_left<300": [p for p in taken if p["secs_left"] < 300],
}
taken_res = {k: {"n_positions": len(v), "pnl_usd": sum(p["pnl"] for p in v), "per_contract": tk_stats(v)} for k, v in taken_conds.items()}
K_total = K_tests + len(taken_conds)

out = {"snapshots": len(snaps), "closes_with_feed_settlement": len(settle), "cf_rows": len(rows), "split_close_ts": split,
       "conditions_examined_counterfactual": K_tests, "conditions_examined_taken": len(taken_conds), "conditions_examined_total": K_total,
       "bonferroni_z_one_sided": bonf_z, "all_counterfactual": all_cf, "results": res, "screened_in_sample": [r["condition"] for r in screened],
       "walk_forward_survivors": [r["condition"] for r in survivors], "full_sample_bonferroni_positive": [r["condition"] for r in full_bonf],
       "taken": taken_res}
json.dump(out, open(f"{ROOT}/data/sol-v1/conditional_ev_2026-10-29.json", "w"), indent=1, default=str)

def fmt(s): return "—" if not s else f"n={s['n']} G={s['closes']} mean={100*s['mean']:+.2f}c se={100*s['se']:.2f}c t={s['t']:+.2f}"
L = ["# Conditional net expectancy, KXSOL15M paper (SOL-v1 tape), 2026-10-29", "",
     f"Snapshots {len(snaps)}; closes with official-feed settlement {len(settle)} (of {len(closes)} seen); counterfactual rows {len(rows)} "
     "(1 contract per close x side x 30-s bucket, filled via sol_v1.paper_fill at best ask + 1 tick, held to settlement; no absent-side fills).",
     f"Walk-forward split at close_ts {split} ({dt.datetime.fromtimestamp(split, PT):%Y-%m-%d %H:%M PT}): first half = screening (IN-SAMPLE), second half = test.",
     f"Conditions examined: {K_tests} counterfactual + {len(taken_conds)} taken-trade cuts = {K_total}. Bonferroni one-sided z at 5%/{K_tests} = {bonf_z:.2f}. SE clustered by close.", "",
     f"All counterfactual rows: {fmt(all_cf)}", "",
     "## Counterfactual conditions (train = IN-SAMPLE | test = out-of-sample)", "", "| condition | train (in-sample) | test | full (in-sample) |", "|---|---|---|---|"]
for r in res: L.append(f"| {r['condition']} | {fmt(r['train'])} | {fmt(r['test'])} | {fmt(r['full'])} |")
L += ["", f"Screened in-sample (t>1.645, n>=30): {', '.join(out['screened_in_sample']) or 'none'}",
      f"Walk-forward survivors (test t > {bonf_z:.2f}, n>=30): {', '.join(out['walk_forward_survivors']) or 'none'}",
      f"Full-sample Bonferroni-positive (IN-SAMPLE, for reference only): {', '.join(out['full_sample_bonferroni_positive']) or 'none'}", "",
      "## Taken trades (SOL-v1 deterministic paper; per-contract, close-clustered)", "", "| cut | positions | pnl $ | per contract |", "|---|---|---|---|"]
for k, v in taken_res.items(): L.append(f"| {k} | {v['n_positions']} | {v['pnl_usd']:+.2f} | {fmt(v['per_contract'])} |")
L += ["", "All taken-trade cuts have <30 positions except possibly 'all'; nothing here is an 'optimal' rule. See JSON for full numbers."]
open(f"{ROOT}/docs/CONDITIONAL_EV_2026-10-29.md", "w").write("\n".join(L))
print("\n".join(L[:8])); print("screened", out["screened_in_sample"]); print("survivors", out["walk_forward_survivors"]); print("full_bonf", out["full_sample_bonferroni_positive"])
for k, v in taken_res.items(): print(k, v["n_positions"], round(v["pnl_usd"], 2), fmt(v["per_contract"]))
