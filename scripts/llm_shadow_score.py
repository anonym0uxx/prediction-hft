"""P23 scorer — LLM early-entry shadow (scored-only). docs/PREREG.md §P23.

Reads data/llm-shadow/forecasts.jsonl, settles each close from the official feed (data/cfb/SOL.jsonl, exact 60-s average, 4 dp,
tie = YES; Kalshi result from data/ts/results.json as a cross-check when present) and reports, by decision time:
  * paired Brier / log-loss of p_llm vs mid and vs p_model, close-clustered SE (mean of per-close differentials; one obs per close
    per decision time) and hour-clustered SE as a secondary;
  * simulated economics of "LLM early entry" (the hypothetical SOL-v1 EV/fill pass of p_llm at the same snapshot) vs the
    deterministic policy's actual decision at that time (net c/contract, fills, drawdown, SE by close);
  * calibration slope (logit regression, needs >= 50 obs) and a reliability table;
  * budget / latency / token / cost summary and the PREREG promotion / stop rule status.

    uv run python scripts/llm_shadow_score.py [--root data/llm-shadow] [--json]
"""
from __future__ import annotations
import argparse, bisect, collections, json, math, pathlib, sys, time, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import numpy as np
from predhft import sol_v1 as S
from predhft.fees import kalshi_taker_fee

ROOT = pathlib.Path(__file__).resolve().parents[1]
EPS = 1e-4
PT = dt.timezone(dt.timedelta(hours=-7))   # prose only; computations are in unix seconds


def load_cfb(asset: str = "SOL") -> list[tuple[int, float]]:
    rows = []
    p = ROOT / "data" / "cfb" / f"{asset}.jsonl"
    if not p.exists(): return []
    for l in p.open(encoding="utf-8"):
        try: d = json.loads(l)
        except Exception: continue
        if d.get("value") and d.get("value_ts_ms"): rows.append((int(d["value_ts_ms"]) // 1000, float(d["value"])))
    rows.sort(); out = []
    for s, v in rows:
        if out and out[-1][0] == s: out[-1] = (s, v)
        else: out.append((s, v))
    return out


def kalshi_results() -> dict[str, dict]:
    p = ROOT / "data" / "ts" / "results.json"
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return {}


def settle(series, ticker: str, close_ts: int, K: float, kres: dict) -> dict | None:
    """Outcome from the official feed (exact rule); Kalshi result as cross-check. None if the close has not settled yet."""
    fs = S.settle_from_feed(series, close_ts, K) if series else None
    kr = kres.get(ticker, {}).get("result")
    y_feed = (1.0 if fs["result"] == "yes" else 0.0) if fs else None
    y_k = (1.0 if kr == "yes" else 0.0) if kr in ("yes", "no") else None
    if y_feed is None and y_k is None: return None
    return {"y": y_k if y_k is not None else y_feed, "y_feed": y_feed, "y_kalshi": y_k, "disagree": (y_feed is not None and y_k is not None and y_feed != y_k), "settle_avg": (fs or {}).get("avg")}


def brier(p, y): return (min(max(p, EPS), 1 - EPS) - y) ** 2
def logloss(p, y):
    p = min(max(p, EPS), 1 - EPS); return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def clustered(vals: list[float], clusters: list) -> dict:
    """Mean with SE from cluster means (one value per cluster); also returns n clusters."""
    by = collections.defaultdict(list)
    for v, c in zip(vals, clusters): by[c].append(v)
    pc = np.array([np.mean(v) for v in by.values()], dtype=float)
    if len(pc) == 0: return {"n": 0, "clusters": 0, "mean": None, "se": None, "t": None}
    se = float(pc.std(ddof=1) / math.sqrt(len(pc))) if len(pc) > 1 else None
    return {"n": len(vals), "clusters": int(len(pc)), "mean": float(pc.mean()), "se": se, "t": (float(pc.mean() / se) if se else None)}


def calibration_slope(p: list[float], y: list[float]) -> float | None:
    if len(p) < 50 or len(set(y)) < 2: return None
    try:
        from sklearn.linear_model import LogisticRegression
        X = np.array([math.log(min(max(v, EPS), 1 - EPS) / (1 - min(max(v, EPS), 1 - EPS))) for v in p]).reshape(-1, 1)
        return float(LogisticRegression(C=1e6, max_iter=1000).fit(X, np.array(y)).coef_[0, 0])
    except Exception: return None


def reliability(p: list[float], y: list[float], edges=(0, .2, .4, .6, .8, 1.0001)) -> list[dict]:
    out = []
    for a, b in zip(edges, edges[1:]):
        idx = [i for i, v in enumerate(p) if a <= v < b]
        if idx: out.append({"bin": f"[{a:.1f},{min(b, 1):.1f})", "n": len(idx), "mean_p": float(np.mean([p[i] for i in idx])), "freq_yes": float(np.mean([y[i] for i in idx]))})
    return out


def econ_llm(row: dict, y: float) -> dict | None:
    """Realised P&L of the hypothetical LLM early entry (simulated fill at the same snapshot), net of the exact taker fee."""
    d = row.get("llm_decision")
    if not d or d.get("decision") != "enter" or not d.get("qty"): return None
    q = int(d["qty"]); px = float(d["px"]); win = (y == 1.0) if d["side"] == "yes" else (y == 0.0)
    fee = kalshi_taker_fee(q, px); pnl = q * ((1.0 if win else 0.0) - px) - fee
    return {"side": d["side"], "qty": q, "px": px, "fee": fee, "pnl": pnl, "c_per_ct": 100 * pnl / q, "win": win, "ev_pc_at_decision": d.get("ev_pc")}


def econ_policy(row: dict, y: float) -> dict | None:
    """Deterministic policy's decision at the same time, priced at its depth-weighted price (+ fee). 'holding' = entered earlier (counted as no new entry here)."""
    d = row.get("policy_decision") or {}
    if d.get("decision") != "enter" or not d.get("qty") or d.get("px_dw") is None: return None
    q = int(d["qty"]); px = float(d["px_dw"]); win = (y == 1.0) if d["side"] == "yes" else (y == 0.0)
    fee = kalshi_taker_fee(q, px); pnl = q * ((1.0 if win else 0.0) - px) - fee
    return {"side": d["side"], "qty": q, "px": px, "fee": fee, "pnl": pnl, "c_per_ct": 100 * pnl / q, "win": win}


def econ_summary(entries: list[dict], closes: list) -> dict:
    if not entries: return {"trades": 0}
    pnl = [e["pnl"] for e in entries]; q = [e["qty"] for e in entries]
    per_ct = clustered([e["c_per_ct"] for e in entries], closes)
    cum = np.cumsum(pnl); dd = float(np.max(np.maximum.accumulate(cum) - cum)) if len(cum) else 0.0
    return {"trades": len(entries), "contracts": int(sum(q)), "net_usd": float(sum(pnl)), "c_per_ct": 100 * float(sum(pnl)) / sum(q), "c_per_ct_close_mean": per_ct["mean"], "se_c_per_ct_close": per_ct["se"], "t": per_ct["t"],
            "hit": float(np.mean([e["win"] for e in entries])), "max_drawdown_usd": dd, "fees_usd": float(sum(e["fee"] for e in entries)), "closes": per_ct["clusters"]}


def score(root: pathlib.Path) -> dict:
    fp = root / "forecasts.jsonl"
    if not fp.exists(): return {"n_rows": 0, "note": "no forecasts yet"}
    rows = [json.loads(l) for l in fp.open(encoding="utf-8") if l.strip()]
    series = load_cfb("SOL"); kres = kalshi_results()
    out = {"n_rows": len(rows), "n_with_p_llm": sum(r.get("p_llm") is not None for r in rows), "errors": collections.Counter(str(r.get("error")) for r in rows if r.get("error")),
           "by_decision_time": {}, "pooled": {}, "econ": {}, "budget": {}, "settled_rows": 0, "disagreements_feed_vs_kalshi": 0}
    lat = [r["latency_s"] for r in rows if r.get("latency_s") is not None]; tok = [r["tokens"] for r in rows if r.get("tokens")]
    out["budget"] = {"calls_logged": len(lat), "latency_s_median": (float(np.median(lat)) if lat else None), "latency_s_p90": (float(np.percentile(lat, 90)) if lat else None),
                     "prompt_tokens_mean": (float(np.mean([t.get("prompt_tokens", 0) for t in tok])) if tok else None), "completion_tokens_mean": (float(np.mean([t.get("completion_tokens", 0) for t in tok])) if tok else None),
                     "cost_usd_sum": float(sum((t.get("cost") or 0) for t in tok)), "cost_usd_per_call": (float(np.mean([(t.get("cost") or 0) for t in tok])) if tok else None),
                     "last_budget_state": next((r["budget_state"] for r in reversed(rows) if r.get("budget_state")), None), "prompt_hashes": sorted({r.get("prompt_hash") for r in rows if r.get("prompt_hash")}),
                     "models": sorted({r.get("model_name") for r in rows if r.get("model_name")}), "snapshot_src": collections.Counter(r.get("snapshot_src", "sol_v1") for r in rows)}
    # one obs per (close, decision time): the LAST logged row with a parsed p_llm
    best: dict[tuple[int, float], dict] = {}
    for r in rows:
        if r.get("p_llm") is None or r.get("mid") is None: continue
        best[(int(r["close_ts"]), float(r["decision_secs_left"]))] = r
    settled = []
    for (c, d), r in sorted(best.items()):
        st = settle(series, r["ticker"], c, float(r["K"]), kres)
        if st is None: continue
        if st["disagree"]: out["disagreements_feed_vs_kalshi"] += 1
        settled.append((c, d, r, st["y"]))
    out["settled_rows"] = len(settled); out["closes_settled"] = len({c for c, *_ in settled})
    groups = collections.defaultdict(list)
    for c, d, r, y in settled: groups[f"T-{int(round(d / 60))}min"].append((c, d, r, y)); groups["pooled"].append((c, d, r, y))
    for g, items in groups.items():
        closes = [c for c, *_ in items]; hours = [c // 3600 for c in closes]
        pl = [it[2]["p_llm"] for it in items]; pm = [it[2]["mid"] for it in items]; pmod = [it[2]["p_model"] for it in items]; ys = [it[3] for it in items]
        d_mid = [brier(m, y) - brier(p, y) for p, m, y in zip(pl, pm, ys)]            # positive = LLM better than mid
        d_mod = [brier(q, y) - brier(p, y) for p, q, y in zip(pl, pmod, ys) if q is not None]
        res = {"n": len(items), "closes": len(set(closes)), "brier_llm": float(np.mean([brier(p, y) for p, y in zip(pl, ys)])), "brier_mid": float(np.mean([brier(m, y) for m, y in zip(pm, ys)])),
               "brier_model": (float(np.mean([brier(q, y) for q, y in zip(pmod, ys) if q is not None])) if any(q is not None for q in pmod) else None),
               "logloss_llm": float(np.mean([logloss(p, y) for p, y in zip(pl, ys)])), "logloss_mid": float(np.mean([logloss(m, y) for m, y in zip(pm, ys)])),
               "diff_vs_mid": clustered(d_mid, closes), "diff_vs_mid_hour_se": clustered(d_mid, hours)["se"], "diff_vs_model": clustered(d_mod, [c for (c, _, r, _) in items if r["p_model"] is not None]),
               "cal_slope_llm": calibration_slope(pl, ys), "cal_slope_mid": calibration_slope(pm, ys), "reliability_llm": reliability(pl, ys),
               "mean_abs_llm_minus_mid": float(np.mean([abs(p - m) for p, m in zip(pl, pm)])), "frac_llm_within_2c_of_mid": float(np.mean([abs(p - m) <= 0.02 for p, m in zip(pl, pm)])),
               "beyond_market_none_frac": float(np.mean([str((it[2].get("beyond_market") or "")).strip().lower().startswith("none") for it in items]))}
        el = [(econ_llm(r, y), c) for c, d, r, y in items]; ep = [(econ_policy(r, y), c) for c, d, r, y in items]
        res["econ_llm_entry"] = econ_summary([e for e, _ in el if e], [c for e, c in el if e]); res["econ_policy_at_same_time"] = econ_summary([e for e, _ in ep if e], [c for e, c in ep if e])
        res["llm_decisions"] = collections.Counter((r.get("llm_decision") or {}).get("decision", "none") for _, _, r, _ in items)
        res["policy_decisions"] = collections.Counter((r.get("policy_decision") or {}).get("decision", "none") for _, _, r, _ in items)
        (out["pooled"] if g == "pooled" else out["by_decision_time"]).update({g: res} if g != "pooled" else res)
    # PREREG P23 status (primary endpoint: paired Brier vs mid at T-10 / T-5 over >= 100 closes)
    status = {}
    for g, res in out["by_decision_time"].items():
        dm = res["diff_vs_mid"]; dmod = res["diff_vs_model"]; ec = res["econ_llm_entry"]
        enough = dm["clusters"] >= 100
        beats_mid_1se = bool(dm["se"] and dm["mean"] - dm["se"] > 0); beats_model = bool(dmod.get("mean") is not None and dmod["mean"] > 0)
        econ_pos = bool(ec.get("trades") and ec.get("se_c_per_ct_close") is not None and ec["c_per_ct_close_mean"] - ec["se_c_per_ct_close"] > 0)
        worse_2se = bool(dm["se"] and dm["mean"] + 2 * dm["se"] < 0)
        status[g] = {"closes": dm["clusters"], "n_required": 100, "promotion_candidate": bool(enough and beats_mid_1se and beats_model and econ_pos), "stop_triggered": bool(enough and worse_2se),
                     "components": {"beats_mid_by_1se": beats_mid_1se, "beats_p_model": beats_model, "sim_econ_positive_1se": econ_pos, "worse_than_mid_2se": worse_2se}}
    out["prereg_p23_status"] = status
    return out


def md(res: dict) -> str:
    if not res.get("n_rows"): return "P23: no forecasts yet."
    L = [f"# P23 LLM early-entry shadow — score ({dt.datetime.now(PT).strftime('%Y-%m-%d %H:%M PT')})", "",
         f"rows {res['n_rows']} (parsed p_llm {res['n_with_p_llm']}), settled obs {res['settled_rows']} over {res.get('closes_settled', 0)} closes; errors {dict(res['errors'])}; feed/Kalshi disagreements {res['disagreements_feed_vs_kalshi']}",
         f"budget: calls {res['budget']['calls_logged']}, latency median {res['budget']['latency_s_median']} s (p90 {res['budget']['latency_s_p90']}), prompt {res['budget']['prompt_tokens_mean']} / completion {res['budget']['completion_tokens_mean']} tokens per call, "
         f"cost ${res['budget']['cost_usd_sum']:.4f} total (${res['budget']['cost_usd_per_call'] or 0:.5f}/call); models {res['budget']['models']}; prompt hashes {res['budget']['prompt_hashes']}; snapshot src {dict(res['budget']['snapshot_src'])}", ""]
    f = lambda v, d=4: ("—" if v is None else (f"{v:.{d}f}" if isinstance(v, float) else str(v)))
    L.append("| decision | n | closes | Brier LLM / mid / model | Δ vs mid ± SE (t) | Δ vs model ± SE | cal slope LLM | mean |p_llm−mid| | LLM entries: n, c/ct ± SE, hit, net $ | policy entries same time: n, c/ct, net $ | status |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for g, r in list(res["by_decision_time"].items()) + ([("pooled", res["pooled"])] if res["pooled"] else []):
        dm = r["diff_vs_mid"]; dd = r["diff_vs_model"]; e = r["econ_llm_entry"]; ep = r["econ_policy_at_same_time"]; st = res["prereg_p23_status"].get(g, {})
        L.append(f"| {g} | {r['n']} | {r['closes']} | {f(r['brier_llm'])} / {f(r['brier_mid'])} / {f(r['brier_model'])} | {f(dm['mean'])} ± {f(dm['se'])} ({f(dm['t'], 2)}) | {f(dd.get('mean'))} ± {f(dd.get('se'))} | {f(r['cal_slope_llm'], 2)} | {f(r['mean_abs_llm_minus_mid'], 3)} | "
                 f"{e.get('trades', 0)}, {f(e.get('c_per_ct_close_mean'), 1)} ± {f(e.get('se_c_per_ct_close'), 1)}, {f(e.get('hit'), 2)}, {f(e.get('net_usd'), 2)} | {ep.get('trades', 0)}, {f(ep.get('c_per_ct_close_mean'), 1)}, {f(ep.get('net_usd'), 2)} | "
                 f"{'PROMOTION-CANDIDATE (review)' if st.get('promotion_candidate') else ('STOP' if st.get('stop_triggered') else f'collecting ({st.get("closes", r["closes"])}/100)')} |")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); ap.add_argument("--root", default="data/llm-shadow"); ap.add_argument("--json", action="store_true")
    a = ap.parse_args(); res = score(pathlib.Path(a.root))
    if a.json: print(json.dumps(res, indent=1, default=lambda o: (dict(o) if isinstance(o, collections.Counter) else str(o))))
    else: print(md(res))
    pathlib.Path(a.root).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(a.root) / "score_latest.json").write_text(json.dumps(res, indent=1, default=lambda o: (dict(o) if isinstance(o, collections.Counter) else str(o))), encoding="utf-8")
