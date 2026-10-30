"""P25 step 1 — reasoning audit of every Opus answer in data/llm-shadow/forecasts.jsonl (docs/OPUS_EQUIP_2026-10-29.md §1).

Per failure mode: a deterministic detector (regex on rationale/beyond_market/decision.reason, or arithmetic on the logged numbers),
the count over all parsed Opus answers, and up to 3 examples (ticker, time, the sentence). Plus p_llm / p_own calibration vs the
mid and vs p_model on settled forecasts (settled from the official feed, data/cfb/SOL.jsonl, sol_v1.settle_from_feed).

    uv run python scripts/opus_audit_2026_10_29.py            -> reports/opus_audit_2026-10-29.json + markdown table on stdout
"""
from __future__ import annotations
import collections, datetime as dt, json, math, pathlib, re, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft import sol_v1 as S
from predhft.fees import kalshi_taker_fee

ROOT = pathlib.Path(__file__).resolve().parents[1]
PT = dt.timezone(dt.timedelta(hours=-7))
MODEL = "anthropic/claude-opus-5.5"
OUT = ROOT / "reports" / "opus_audit_2026-10-29.json"


def jl(p):
    out = []
    if not p.exists(): return out
    for l in p.open(encoding="utf-8"):
        try: out.append(json.loads(l))
        except Exception: pass
    return out

def load_cfb():
    rows = []
    for d in jl(ROOT / "data" / "cfb" / "SOL.jsonl"):
        if d.get("value") and d.get("value_ts_ms"): rows.append((int(d["value_ts_ms"]) // 1000, float(d["value"])))
    rows.sort(); out = []
    for s, v in rows:
        if out and out[-1][0] == s: out[-1] = (s, v)
        else: out.append((s, v))
    return out

def text_of(r):
    d = r.get("direct_decision") or {}
    return " ".join(str(x) for x in (r.get("rationale"), r.get("beyond_market"), d.get("reason")) if x)

def fee_pc(px, q=10): return kalshi_taker_fee(q, px) / q if 0 < px < 1 else 0.0

def pt(ts): return dt.datetime.fromtimestamp(ts, PT).strftime("%m-%d %H:%M")

def ex(r, s=None):
    return {"ticker": r["ticker"], "t": f"T-{int(r['decision_secs_left'] // 60)}", "pt": pt(r["close_ts"]), "cohort": r["prompt_hash"][:4], "mid": r.get("mid"), "p_model": (round(r["p_model"], 3) if r.get("p_model") is not None else None), "p_llm": r.get("p_llm"), "p_own": r.get("p_own"), "y": r.get("_y"), "text": (s or text_of(r))[:260]}


def audit(rows):
    series = load_cfb()
    op = [r for r in rows if r.get("model_name") == MODEL and r.get("p_llm") is not None]
    for r in op:
        st = S.settle_from_feed(series, int(r["close_ts"]), float(r["K"]))
        r["_y"] = None if st is None else (1.0 if st["result"] == "yes" else 0.0)
    settled = [r for r in op if r["_y"] is not None]
    n = len(op); modes = {}
    def add(key, title, hits, note=""):
        modes[key] = {"title": title, "n": len(hits), "of": n, "frac": round(len(hits) / n, 3) if n else None, "note": note, "examples": [ex(*h) if isinstance(h, tuple) else ex(h) for h in hits[:3]]}
    rx = lambda pat, r: re.search(pat, text_of(r), re.I)
    # 1. anchoring to p_model
    within1 = [r for r in op if r.get("p_model") is not None and abs(r["p_llm"] - r["p_model"]) <= 0.01]
    between = [r for r in op if r.get("p_model") is not None and r.get("mid") is not None and min(r["mid"], r["p_model"]) - 1e-9 <= r["p_llm"] <= max(r["mid"], r["p_model"]) + 1e-9]
    avg_words = [r for r in op if rx(r"land(s|ing)? between|between the (two|model|mid)|blend|shad(e|ing) (toward|to)|split the difference|midway|average of|halfway", r)]
    cites_model = [r for r in op if rx(r"\b(gaussian|the model|model gives|model's|p_model|model probability|model says|z\s*[≈=~]|z about|z of)\b", r)]
    pown_rows = [r for r in op if r.get("p_own") is not None and r.get("p_model") is not None]
    pown_within1 = [r for r in pown_rows if abs(r["p_own"] - r["p_model"]) <= 0.01]
    add("anchor_cites_model", "rationale cites the Gaussian/model probability or z", cites_model, "the packet handed it p_model, z, sd: it re-derived the policy's number")
    add("anchor_averaging_words", "explicit averaging language ('land between', 'blend', 'shade toward', 'midway')", avg_words)
    add("anchor_between_mid_and_model", "p_llm lies between the mid and p_model (inclusive)", between, "a forecaster with independent information would fall outside that interval about half the time")
    add("anchor_within_1c_of_model", "|p_llm - p_model| <= 1 c", within1)
    add("anchor_p_own_within_1c_of_model", "|p_own - p_model| <= 1 c (A3 cohort only, p_own logged)", pown_within1, f"of {len(pown_rows)} rows with p_own; A3 asked for p_own BEFORE reconciliation")
    # 2. tails / favourites
    fat = [r for r in op if rx(r"fat[- ]tail|tail risk|tails? (are|is) fat|heavy[- ]tail|kurtosis|jump risk", r)]
    less_extreme = [r for r in op if r.get("mid") is not None and abs(r["p_llm"] - 0.5) < abs(r["mid"] - 0.5) - 1e-9]
    tails_lift = [r for r in settled if r["mid"] <= 0.20 and r["p_llm"] > r["mid"] + 0.005]
    fav_fade = [r for r in settled if r["mid"] >= 0.80 and r["p_llm"] < r["mid"] - 0.005]
    add("tail_words", "'fat tails'/'tail risk'/'jump risk' invoked as a word, no number", fat)
    add("less_extreme_than_mid", "p_llm closer to 0.5 than the mid (inherited Gaussian variance inflation)", less_extreme)
    modes["tails_lifted_outcomes"] = {"title": "settled: mid <= 0.20 and p_llm > mid (tail lifted)", "n": len(tails_lift), "won": sum(1 for r in tails_lift if r["_y"] == 1.0), "mean_mid": round(sum(r["mid"] for r in tails_lift) / len(tails_lift), 3) if tails_lift else None, "mean_p_llm": round(sum(r["p_llm"] for r in tails_lift) / len(tails_lift), 3) if tails_lift else None, "examples": [ex(r) for r in tails_lift[:3]]}
    modes["favourites_faded_outcomes"] = {"title": "settled: mid >= 0.80 and p_llm < mid (favourite faded)", "n": len(fav_fade), "won": sum(1 for r in fav_fade if r["_y"] == 1.0), "mean_mid": round(sum(r["mid"] for r in fav_fade) / len(fav_fade), 3) if fav_fade else None, "mean_p_llm": round(sum(r["p_llm"] for r in fav_fade) / len(fav_fade), 3) if fav_fade else None, "examples": [ex(r) for r in fav_fade[:3]]}
    # 3. fee / spread arithmetic
    spread_fee = [r for r in op if rx(r"spread (and|plus|\+) (the )?fee|fees? (and|plus|\+) (the )?spread|spread, fees|after (the )?spread|spread cost|crossing the spread", r)]
    thin = [r for r in op if rx(r"too thin|thin edge|marginal edge|not enough edge|insufficient edge|no edge", r)]
    # stated-edge check: rationale quotes an edge in cents while NO_TRADE; compare with p_llm - ask - fee
    thin_but_edge = []
    for r in op:
        d = r.get("direct_decision") or {}
        if d.get("action") != "NO_TRADE" or r.get("yes_ask") is None or r.get("yes_bid") is None: continue
        p = r.get("p_own") if r.get("p_own") is not None else r["p_llm"]
        e_yes = p - r["yes_ask"] - fee_pc(r["yes_ask"]); e_no = (1 - p) - (1 - r["yes_bid"]) - fee_pc(1 - r["yes_bid"])
        if max(e_yes, e_no) >= 0.04: thin_but_edge.append((r, f"NO_TRADE with own-p edge yes={100*e_yes:.1f}c no={100*e_no:.1f}c (p={p}, ask={r['yes_ask']}, bid={r['yes_bid']}) :: " + text_of(r)))
    traded = [r for r in op if (r.get("direct_decision") or {}).get("action") in ("BUY_YES", "BUY_NO")]
    traded_edges = []
    for r in traded:
        d = r["direct_decision"]; p = r.get("p_own") if r.get("p_own") is not None else r["p_llm"]
        if d["action"] == "BUY_YES" and r.get("yes_ask") is not None: e = p - r["yes_ask"] - fee_pc(r["yes_ask"])
        elif d["action"] == "BUY_NO" and r.get("yes_bid") is not None: e = (1 - p) - (1 - r["yes_bid"]) - fee_pc(1 - r["yes_bid"])
        else: e = None
        traded_edges.append({**ex(r, d.get("reason")), "action": d["action"], "qty": d.get("qty"), "limit": d.get("limit_price"), "own_edge_c": (round(100 * e, 1) if e is not None else None), "y": r["_y"]})
    add("spread_plus_fees_double_count", "'spread and/plus fees' counted as costs on top of the ask (the ask already is the spread)", spread_fee)
    add("thin_edge_words", "'too thin'/'no edge'/'marginal edge' as the stated reason", thin)
    modes["no_trade_with_own_edge_ge_4c"] = {"title": "NO_TRADE while own p vs executable price shows >= 4 c edge after fee (inconsistency)", "n": len(thin_but_edge), "of": sum(1 for r in op if (r.get('direct_decision') or {}).get('action') == 'NO_TRADE'), "examples": [ex(r, s) for r, s in thin_but_edge[:3]]}
    modes["trades_taken"] = {"title": "BUY decisions taken: own edge at decision and outcome", "n": len(traded), "rows": traded_edges}
    # 4. partial window / settlement rule
    spot_terminal = [r for r in op if rx(r"spot (needs|must|has to|would need)|needs? (to|a) (move|rise|fall|drop|climb)|needs \+?-?\d+(\.\d+)?\s?bp|must (move|rise|fall)", r) and not rx(r"(average|mean|settle-mean|settlement mean|60-s|60s|window)", r)]
    mentions_avg = [r for r in op if rx(r"(settle(ment)?[- ]mean|average|mean of|60-s(econd)? (average|mean|window)|window)", r)]
    add("required_move_as_terminal_spot", "frames the requirement as 'spot needs +X bp' with no mention of the 60-s averaging", spot_terminal, "settlement is the mean of 60 future seconds, so the effective horizon is shorter than the time to close")
    add("mentions_settlement_averaging", "mentions the settlement average / window", mentions_avg)
    # 5. side conversion / NO price
    no_conv = []
    for r in op:
        d = r.get("direct_decision") or {}
        if d.get("action") == "BUY_NO" and d.get("limit_price") is not None and r.get("yes_bid") is not None:
            if d["limit_price"] > (1 - r["yes_bid"]) + 0.03 or d["limit_price"] < (1 - r["yes_ask"]) - 0.03: no_conv.append((r, f"BUY_NO limit {d['limit_price']} vs NO ask {round(1 - r['yes_bid'], 3)} :: " + str(d.get("reason"))))
    no_words = [r for r in op if rx(r"\bNO (side|at|price|contract)|buy(ing)? no\b|1 ?- ?(yes|bid|ask|mid)", r)]
    add("no_side_price_error", "BUY_NO limit inconsistent with 1 - yes_bid (> 3 c off)", no_conv)
    add("no_side_mentioned", "mentions the NO side / 1 - price at all", no_words, "the v1 prompt asked it to convert; the v2 packet converts for it")
    # 6. BTC/ETH
    btc = [r for r in op if rx(r"\b(BTC|ETH|bitcoin|ether|co-?move|cross-asset|beta)\b", r)]
    btc_dir = [r for r in op if re.search(r"\b(BTC|ETH|bitcoin|ether)\b[^.]{0,60}\b(up|down|weak|strong|rally|sell|bear|bull|drift|lead|confirm|diverg)", text_of(r), re.I)]
    add("btc_eth_mentioned", "BTC/ETH mentioned at all", btc)
    add("btc_eth_used_directionally", "BTC/ETH direction used as a reason (not just listed)", btc_dir)
    # 7. p_own vs action, p_own vs p_llm
    moved = [r for r in pown_rows if abs(r["p_own"] - r["p_llm"]) > 0.005]
    add("p_own_differs_from_p_yes", "p_own != final p_yes (moved after 'reconciliation')", moved, f"of {len(pown_rows)} rows with p_own")
    uncertainty = [r for r in op if rx(r"model uncertainty|uncertain(ty)? (in|about|of) (the )?(model|sigma|vol)|conflicting signals|mixed signals", r)]
    add("uncertainty_as_reason_to_pass", "'model uncertainty' / 'conflicting signals' as the reason not to trade", uncertainty, "uncertainty is already in the probability; it is not a second cost")
    candle_vol = [r for r in op if rx(r"1-?min(ute)? (candle )?(vol|realised|realized|rv)|candle vol|candle rv", r)]
    add("cites_1m_candle_vol_as_feed_vol", "cites the 1-min candle vol where the 1-s feed sigma drives settlement", candle_vol)
    beyond_none = [r for r in op if str(r.get("beyond_market") or "").strip().lower().startswith("none")]
    add("beyond_market_none", "beyond_market = 'none' (used nothing the price does not reflect)", beyond_none)
    # calibration tables
    def brier(p, y): return (p - y) ** 2
    cal = {}
    for coh in sorted({r["prompt_hash"] for r in settled}) + ["all"]:
        for d in (600.0, 300.0):
            s = [r for r in settled if (coh == "all" or r["prompt_hash"] == coh) and r["decision_secs_left"] == d]
            if not s: continue
            dm = [brier(r["p_llm"], r["_y"]) - brier(r["mid"], r["_y"]) for r in s]
            m = sum(dm) / len(dm); se = (sum((x - m) ** 2 for x in dm) / (len(dm) - 1)) ** 0.5 / len(dm) ** 0.5 if len(dm) > 1 else None
            so = [r for r in s if r.get("p_own") is not None]
            cal[f"{coh}|T-{int(d // 60)}"] = {"n": len(s), "brier_llm": round(sum(brier(r["p_llm"], r["_y"]) for r in s) / len(s), 4), "brier_mid": round(sum(brier(r["mid"], r["_y"]) for r in s) / len(s), 4), "brier_model": round(sum(brier(r["p_model"], r["_y"]) for r in s) / len(s), 4),
                                               "llm_minus_mid": round(m, 4), "se": (round(se, 4) if se else None), "yes_rate": round(sum(r["_y"] for r in s) / len(s), 3), "mean_p_llm": round(sum(r["p_llm"] for r in s) / len(s), 3), "mean_mid": round(sum(r["mid"] for r in s) / len(s), 3),
                                               "p_own": ({"n": len(so), "brier_p_own": round(sum(brier(r["p_own"], r["_y"]) for r in so) / len(so), 4), "brier_mid": round(sum(brier(r["mid"], r["_y"]) for r in so) / len(so), 4), "brier_p_llm": round(sum(brier(r["p_llm"], r["_y"]) for r in so) / len(so), 4)} if so else None)}
    # reliability of p_llm vs mid in bins
    bins = [(0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)]
    rel = []
    for lo, hi in bins:
        s = [r for r in settled if lo <= r["p_llm"] < hi]
        sm = [r for r in settled if lo <= r["mid"] < hi]
        rel.append({"bin": f"[{lo},{hi if hi <= 1 else 1})", "llm_n": len(s), "llm_mean_p": (round(sum(r["p_llm"] for r in s) / len(s), 3) if s else None), "llm_hit": (round(sum(r["_y"] for r in s) / len(s), 3) if s else None),
                    "mid_n": len(sm), "mid_mean_p": (round(sum(r["mid"] for r in sm) / len(sm), 3) if sm else None), "mid_hit": (round(sum(r["_y"] for r in sm) / len(sm), 3) if sm else None)})
    # disagreement classes: |p_llm - mid| >= 5c, who was right
    big = [r for r in settled if abs(r["p_llm"] - r["mid"]) >= 0.05]
    llm_right = sum(1 for r in big if brier(r["p_llm"], r["_y"]) < brier(r["mid"], r["_y"]))
    out = {"generated_pt": dt.datetime.now(PT).strftime("%Y-%m-%d %H:%M PT"), "model": MODEL, "n_answers": n, "n_settled": len(settled), "cohorts": dict(collections.Counter(r["prompt_hash"] for r in op)), "decision_times": dict(collections.Counter(r["decision_secs_left"] for r in op)),
           "modes": modes, "calibration": cal, "reliability": rel, "big_disagreements_ge_5c": {"n": len(big), "llm_closer_than_mid": llm_right, "examples": [ex(r) for r in big[:5]]},
           "actions": dict(collections.Counter((r.get("direct_decision") or {}).get("action") for r in op))}
    return out


def md_table(a):
    L = [f"| # | failure mode | detector | n / {a['n_answers']} | example |", "|---|---|---|---|---|"]
    order = ["anchor_cites_model", "anchor_averaging_words", "anchor_between_mid_and_model", "anchor_within_1c_of_model", "anchor_p_own_within_1c_of_model", "less_extreme_than_mid", "tail_words", "spread_plus_fees_double_count", "thin_edge_words", "no_trade_with_own_edge_ge_4c",
             "required_move_as_terminal_spot", "mentions_settlement_averaging", "no_side_price_error", "no_side_mentioned", "btc_eth_mentioned", "btc_eth_used_directionally", "p_own_differs_from_p_yes", "uncertainty_as_reason_to_pass", "cites_1m_candle_vol_as_feed_vol", "beyond_market_none"]
    for i, k in enumerate(order, 1):
        m = a["modes"][k]; e = m["examples"][0] if m["examples"] else None
        exs = f"{e['ticker'][-7:]} {e['t']} {e['cohort']}: \"{e['text'][:150]}\"" if e else "-"
        L.append(f"| {i} | {m['title']} | {k} | {m['n']}{(' (of ' + str(m['of']) + ')') if m.get('of') not in (None, a['n_answers']) else ''} | {exs.replace('|', '/')} |")
    for k in ("tails_lifted_outcomes", "favourites_faded_outcomes"):
        m = a["modes"][k]; L.append(f"| | {m['title']} | {k} | {m['n']} settled, won {m['won']} (mean mid {m['mean_mid']}, mean p_llm {m['mean_p_llm']}) | {(m['examples'][0]['ticker'][-7:] + ' ' + m['examples'][0]['text'][:120]).replace('|', '/') if m['examples'] else '-'} |")
    m = a["modes"]["trades_taken"]; L.append(f"| | {m['title']} | trades_taken | {m['n']} | " + "; ".join(f"{t['ticker'][-7:]} {t['action']} q{t['qty']} L{t['limit']} edge {t['own_edge_c']}c y={t['y']}" for t in m["rows"]) + " |")
    return "\n".join(L)


if __name__ == "__main__":
    rows = jl(ROOT / "data" / "llm-shadow" / "forecasts.jsonl")
    a = audit(rows)
    OUT.write_text(json.dumps(a, indent=1, default=str), encoding="utf-8")
    print(md_table(a)); print()
    print("| cohort | T | n | Brier llm | mid | model | llm-mid ± se | yes rate | mean p_llm | mean mid | p_own (n, Brier own / mid / p_yes) |"); print("|---|---|---|---|---|---|---|---|---|---|---|")
    for k, c in a["calibration"].items():
        po = c["p_own"]; L = f"{po['n']}: {po['brier_p_own']} / {po['brier_mid']} / {po['brier_p_llm']}" if po else "-"
        print(f"| {k.split('|')[0][:6]} | {k.split('|')[1]} | {c['n']} | {c['brier_llm']} | {c['brier_mid']} | {c['brier_model']} | {c['llm_minus_mid']:+} ± {c['se']} | {c['yes_rate']} | {c['mean_p_llm']} | {c['mean_mid']} | {L} |")
    print(); print("reliability", json.dumps(a["reliability"])); print("big disagreements", {k: v for k, v in a["big_disagreements_ge_5c"].items() if k != "examples"}); print("actions", a["actions"]); print("wrote", OUT)
