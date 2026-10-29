"""P24 scorer — LLM DIRECT trade-decision arm (paper). docs/PREREG.md §P24.

Reads data/llm-direct/{decisions,fills,marks}.jsonl and the probability arm's data/llm-shadow/forecasts.jsonl plus SOL-v1's
data/sol-v1/fills.jsonl, and reports PER COHORT (prompt_hash + model):
  * decisions by action (and parse errors / risk_limited refusals), fills / partials / no-fills, EXITs;
  * realised economics after fees: net $, c/contract with close-clustered SE, hit rate, max drawdown, fees;
  * Sharpe per 15-min equity mark (mean / sd of 900-s equity differences from marks.jsonl, idle periods included, NOT annualised);
  * comparators on the SAME closes: the probability arm's gated simulated early entry (llm_decision in forecasts.jsonl, settled
    from the official feed) and SOL-v1's deterministic policy (its actual paper fills/exits/settles on those closes);
  * PREREG P24 status: primary endpoint and stop rule.
Positions still open when the runner was down are settled here from the official feed (flagged scorer_settled).
Exec models (PREREG P24 amendment): rows are reported per exec_model — 'ioc' (v1: immediate-or-cancel at the model's limit; rows
without the field) and 'rest_v2' (resting limit order, crossing-only fills, TTL / T-60 s cancel). Resting-order events
(rest_placed / rest_partial / rest_filled / rest_expired / rest_cancelled_*) are counted with time-to-fill. INFORMATIONAL only:
the historical IOC-unfilled intents are replayed with the v2 model over the ws book tape (data/sol-v1/book/<utc-date>.jsonl):
would-have-filled, fill price, time-to-fill, settlement outcome — no P&L, never pooled with realised rows.

    uv run python scripts/llm_direct_score.py [--root data/llm-direct] [--json] [--no-replay]
"""
from __future__ import annotations
import argparse, collections, json, math, pathlib, sys, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import numpy as np
from predhft import sol_v1 as S
from predhft import llm_direct as D
from llm_shadow_score import load_cfb, kalshi_results, settle, clustered, econ_llm, econ_summary

ROOT = pathlib.Path(__file__).resolve().parents[1]
PT = dt.timezone(dt.timedelta(hours=-7))   # prose only


def _rows(p: pathlib.Path) -> list[dict]:
    if not p.exists(): return []
    out = []
    for l in p.open(encoding="utf-8"):
        if l.strip():
            try: out.append(json.loads(l))
            except Exception: continue
    return out


def cohort_of(r: dict) -> str: return f"{r.get('prompt_hash') or '?'}|{r.get('model_name') or '?'}"
def exec_model_of(r: dict) -> str: return r.get("exec_model") or "ioc"       # rows before the v2 amendment carry no field: IOC


def book_tape(sol_root: pathlib.Path, ticker: str, t0: float, t1: float) -> list[dict]:
    """ws book rows for one ticker in [t0, t1] from data/sol-v1/book/<UTC date>.jsonl (both days if the window spans midnight)."""
    out = []; days = {dt.datetime.fromtimestamp(x, dt.timezone.utc).strftime("%Y%m%d") for x in (t0, t1)}
    for d in sorted(days):
        p = sol_root / "book" / f"{d}.jsonl"
        if not p.exists(): continue
        for l in p.open(encoding="utf-8"):
            if ticker not in l: continue
            try: r = json.loads(l)
            except Exception: continue
            if r.get("ticker") == ticker and t0 <= r["recv_t"] <= t1: out.append(r)
    return sorted(out, key=lambda r: r["recv_t"])


def replay_ioc_unfilled(fills: list[dict], sol_root: pathlib.Path, series, kres, cfg: dict | None = None, poll_s: float = 1.0) -> list[dict]:
    """INFORMATIONAL: every IOC entry that got no (or a partial) immediate fill, re-run as a v2 resting order over the recorded ws
    tape from its fill time: would it have filled, at what price, how long after placement, and how did the market settle.
    No P&L is computed (these were never positions and the decision would not have been the same under v2)."""
    cfg = {**D.CFG, **(cfg or {})}; out = []
    for r in fills:
        if r.get("kind") != "fill" or "note" in r or exec_model_of(r) != "ioc": continue
        rem = int(r.get("intended_qty", r.get("qty", 0))) - int(r.get("qty", 0))
        if rem <= 0: continue
        action = "BUY_YES" if r["side"] == "yes" else "BUY_NO"; t_placed = float(r["t"]); close_ts = int(r["close_ts"])
        o = D.new_order(r["intent_id"], r["ticker"], close_ts, float(r.get("K") or 0.0), action, rem, float(r["limit"]), t_placed, cfg, prompt_hash=r.get("prompt_hash"), model_name=r.get("model_name"), decision_secs_left=r.get("decision_secs_left"))
        tape = book_tape(sol_root, r["ticker"], t_placed - 30, min(o["expires_t"], o["cancel_at_t"]) + 5)
        rp = D.replay_resting(o, tape, poll_s=poll_s)
        st = settle(series, r["ticker"], close_ts, float(r["K"]), kres) if r.get("K") is not None else None
        res = (None if st is None else ("yes" if st["y"] == 1.0 else "no"))
        out.append({"intent_id": r["intent_id"], "ticker": r["ticker"], "t_placed_pt": dt.datetime.fromtimestamp(t_placed, PT).strftime("%Y-%m-%d %H:%M:%S PT"), "side": r["side"], "qty": rem, "limit": r["limit"], "ioc_best_offer": r.get("best_offer"),
                    "decision_secs_left": r.get("decision_secs_left"), "ttl_s": o["ttl_s"], "tape_rows": rp["n_tape_rows"], "would_have_filled": rp["status"], "qty_filled": rp["qty_filled"], "avg_px": rp["avg_px"], "fee": rp["fee"],
                    "time_to_first_fill_s": rp["time_to_first_fill_s"], "time_to_last_fill_s": rp["time_to_last_fill_s"], "n_fills": len(rp["fills"]), "fills": [{k: f[k] for k in ("t", "qty", "px", "best_offer")} for f in rp["fills"]],
                    "settlement": res, "side_would_have_won": (None if res is None else res == r["side"]), "note": "informational replay; not a position, no P&L"})
    return out


def resting_stats(fr: list[dict]) -> dict:
    """Counts of resting-order events for one cohort and time-to-fill among the orders that filled (fully or partially)."""
    ev = collections.Counter(f["kind"] for f in fr if str(f.get("kind", "")).startswith("rest_"))
    terminal = [f for f in fr if f.get("kind") in ("rest_filled", "rest_expired", "rest_cancelled_final_minute", "rest_cancelled_decision", "rest_cancelled_replaced", "rest_cancelled_exit")]
    ttf = [f["resting_for_s"] for f in fr if f.get("kind") == "rest_filled"]; first = [f["resting_for_s"] for f in fr if f.get("kind") in ("rest_partial", "rest_filled")]
    return {"events": dict(ev), "placed": ev.get("rest_placed", 0), "filled_fully": ev.get("rest_filled", 0), "ended_with_partial": sum(1 for f in terminal if f["kind"] != "rest_filled" and f.get("qty_filled", 0) > 0),
            "ended_unfilled": sum(1 for f in terminal if f["kind"] != "rest_filled" and f.get("qty_filled", 0) == 0), "time_to_full_fill_s": ({"n": len(ttf), "median": float(np.median(ttf)), "max": float(max(ttf))} if ttf else None),
            "time_to_first_fill_s": ({"n": len(first), "median": float(np.median(first))} if first else None), "still_resting": ev.get("rest_placed", 0) - len(terminal)}


def realised_trades(fills: list[dict], series, kres) -> list[dict]:
    """One realised record per closed position (settle or exit; a partial exit + later settle yields two records on the same
    close). Open positions whose close has settled on the feed are settled here (scorer_settled=True)."""
    out = []; open_by_intent: dict[str, dict] = {}
    for r in fills:
        k = r.get("kind")
        if k == "fill" and r.get("status") == "open": open_by_intent[r["intent_id"]] = r
        elif k in ("settle", "exit"):
            open_by_intent.pop(r["intent_id"], None)
            q = int(r["qty"]); out.append({"cohort": cohort_of(r), "exec_model": exec_model_of(r), "close_ts": int(r["close_ts"]), "ticker": r["ticker"], "kind": k, "side": r["side"], "qty": q, "pnl": float(r["pnl"]), "c_per_ct": 100 * float(r["pnl"]) / q,
                                            "win": (bool(r.get("win")) if k == "settle" else float(r["pnl"]) > 0), "fee": float(r.get("fee", 0.0)) + float(r.get("exit_fee", 0.0)), "scorer_settled": False, "t": r["t"]})
    for iid, p in open_by_intent.items():
        st = settle(series, p["ticker"], int(p["close_ts"]), float(p["K"]), kres)
        if st is None: continue
        win = (st["y"] == 1.0) if p["side"] == "yes" else (st["y"] == 0.0); q = int(p["qty"]); pnl = (1.0 if win else 0.0) * q - p["cost"] - p["fee"]
        out.append({"cohort": cohort_of(p), "exec_model": exec_model_of(p), "close_ts": int(p["close_ts"]), "ticker": p["ticker"], "kind": "settle", "side": p["side"], "qty": q, "pnl": pnl, "c_per_ct": 100 * pnl / q, "win": win, "fee": p["fee"], "scorer_settled": True, "t": p["t"]})
    return sorted(out, key=lambda r: r["t"])


def sharpe_15m(marks: list[dict], t0: float | None = None, t1: float | None = None) -> dict:
    """Equity sampled on the 900-s grid (last mark at or before each grid point; idle intervals carry the flat equity) ->
    per-interval $ differences -> mean/sd (not annualised). Needs >= 3 intervals."""
    ms = sorted((m for m in marks if (t0 is None or m["t"] >= t0) and (t1 is None or m["t"] <= t1)), key=lambda m: m["t"])
    if len(ms) < 2: return {"n_intervals": 0, "sharpe": None, "mean_usd": None, "sd_usd": None}
    g0 = int(ms[0]["t"]) // 900 * 900 + 900; g1 = int(ms[-1]["t"]) // 900 * 900
    eq = []; i = 0
    for g in range(g0, g1 + 1, 900):
        while i + 1 < len(ms) and ms[i + 1]["t"] <= g: i += 1
        eq.append(ms[i]["equity"])
    d = np.diff(np.array(eq, dtype=float))
    if len(d) < 3: return {"n_intervals": int(len(d)), "sharpe": None, "mean_usd": (float(d.mean()) if len(d) else None), "sd_usd": None}
    sd = float(d.std(ddof=1)); return {"n_intervals": int(len(d)), "sharpe": (float(d.mean() / sd) if sd > 0 else None), "mean_usd": float(d.mean()), "sd_usd": sd, "equity_first": eq[0], "equity_last": eq[-1]}


def summarize(trades: list[dict]) -> dict:
    if not trades: return {"trades": 0}
    pnl = [t["pnl"] for t in trades]; q = [t["qty"] for t in trades]; closes = [t["close_ts"] for t in trades]
    per = clustered([t["c_per_ct"] for t in trades], closes); cum = np.cumsum(pnl)
    return {"trades": len(trades), "closes": per["clusters"], "contracts": int(sum(q)), "net_usd": float(sum(pnl)), "fees_usd": float(sum(t["fee"] for t in trades)), "c_per_ct": 100 * float(sum(pnl)) / sum(q),
            "c_per_ct_close_mean": per["mean"], "se_c_per_ct_close": per["se"], "t": per["t"], "hit": float(np.mean([t["win"] for t in trades])), "max_drawdown_usd": float(np.max(np.maximum.accumulate(cum) - cum)),
            "scorer_settled": int(sum(t["scorer_settled"] for t in trades))}


def solv1_on_closes(sol_fills: list[dict], closes: set[int]) -> list[dict]:
    out = []
    for r in sol_fills:
        if r.get("kind") in ("settle", "exit") and int(r["close_ts"]) in closes:
            q = int(r["qty"]); out.append({"close_ts": int(r["close_ts"]), "qty": q, "pnl": float(r["pnl"]), "c_per_ct": 100 * float(r["pnl"]) / q, "win": float(r["pnl"]) > 0, "fee": float(r.get("fee", 0.0)) + float(r.get("exit_fee", 0.0)), "scorer_settled": False})
    return out


def score(root: pathlib.Path, shadow_root: pathlib.Path = ROOT / "data" / "llm-shadow", sol_root: pathlib.Path = ROOT / "data" / "sol-v1", replay: bool = True) -> dict:
    decisions = _rows(root / "decisions.jsonl"); fills = _rows(root / "fills.jsonl"); marks = _rows(root / "marks.jsonl")
    forecasts = _rows(shadow_root / "forecasts.jsonl"); sol_fills = _rows(sol_root / "fills.jsonl")
    if not decisions: return {"n_decisions": 0, "note": "no direct decisions yet"}
    series = load_cfb("SOL"); kres = kalshi_results()
    trades = realised_trades(fills, series, kres)
    open_orders = {}
    try: open_orders = json.loads((root / "open_orders.json").read_text(encoding="utf-8"))
    except Exception: pass
    out = {"n_decisions": len(decisions), "n_fill_rows": len(fills), "n_marks": len(marks), "cohorts": {}, "sharpe_15m_all": sharpe_15m(marks), "exec_models_seen": sorted({exec_model_of(f) for f in fills if f.get("kind") == "fill"}),
           "resting_now": [{k: o.get(k) for k in ("intent_id", "ticker", "side", "qty_remaining", "qty_filled", "limit", "t_placed", "expires_t", "cancel_at_t")} for o in (open_orders.get("orders") or [])],
           "ioc_unfilled_replay_informational": (replay_ioc_unfilled(fills, sol_root, series, kres) if replay else None)}
    for coh in sorted({cohort_of(d) for d in decisions}):
        ds = [d for d in decisions if cohort_of(d) == coh]; ph, model = coh.split("|")
        acts = collections.Counter(d["parsed"]["action"] for d in ds); errs = collections.Counter(d["parse_error"] for d in ds if d.get("parse_error"))
        results = collections.Counter(str((d.get("result") or {}).get("status")) for d in ds)
        by_time = collections.Counter((f"T-{int(round(d['decision_secs_left'] / 60))}min", d["parsed"]["action"]) for d in ds)
        fr = [f for f in fills if cohort_of(f) == coh]
        entries = [f for f in fr if f["kind"] == "fill" and "note" not in f]
        fills_full = sum(f["fill_status"] == "filled" for f in entries if "fill_status" in f); partial = sum(f["fill_status"] == "partial" for f in entries if "fill_status" in f); unfilled = sum(f["status"] == "unfilled" for f in entries)
        tr = [t for t in trades if t["cohort"] == coh]; closes = {t["close_ts"] for t in tr}
        by_exec = {}
        for em in sorted({exec_model_of(f) for f in entries} | {t["exec_model"] for t in tr}):
            en = [f for f in entries if exec_model_of(f) == em]
            by_exec[em] = {"entries_attempted": len(en), "immediate_full": sum(f.get("fill_status") == "filled" for f in en), "immediate_partial": sum(f.get("fill_status") == "partial" for f in en), "immediate_none": sum(f["status"] == "unfilled" for f in en),
                           "rested": sum(1 for f in en if f.get("resting_qty")), "resting": (resting_stats([f for f in fr if exec_model_of(f) == em]) if em != "ioc" else None), "econ_direct": summarize([t for t in tr if t["exec_model"] == em])}
        # comparator 1: probability arm's gated simulation on the same closes (one obs per close; the T-10 row if present else T-5)
        comp_prob = []
        best: dict[int, dict] = {}
        for r in forecasts:
            if r.get("prompt_hash") == ph and r.get("model_name") == model and int(r["close_ts"]) in closes and r.get("llm_decision"):
                c = int(r["close_ts"])
                if c not in best or r["decision_secs_left"] > best[c]["decision_secs_left"]: best[c] = r
        for c, r in best.items():
            st = settle(series, r["ticker"], c, float(r["K"]), kres)
            if st is None: continue
            e = econ_llm(r, st["y"])
            if e: comp_prob.append({**e, "close_ts": c, "scorer_settled": False})
        comp_sol = solv1_on_closes(sol_fills, closes)
        t0 = min((d["t"] for d in ds), default=None); t1 = max((d["t"] for d in ds), default=None)
        res = {"prompt_hash": ph, "model": model, "decisions": len(ds), "by_action": dict(acts), "by_time_action": {f"{k[0]} {k[1]}": v for k, v in sorted(by_time.items())}, "parse_errors": dict(errs), "result_status": dict(results),
               "entries_attempted": len(entries), "fills_full": fills_full, "fills_partial": partial, "no_fill": unfilled, "exits": sum(f["kind"] == "exit" for f in fr), "exit_unfilled": sum(f["kind"] == "exit_unfilled" for f in fr),
               "risk_limited": sum(str((d.get("result") or {}).get("status", "")).startswith("risk_limited") for d in ds), "open_now": sum(1 for f in fr if f["kind"] == "fill" and f["status"] == "open") - sum(1 for f in fr if f["kind"] in ("settle", "exit")),
               "by_exec_model": by_exec, "econ_direct": summarize(tr), "econ_probability_arm_same_closes": econ_summary(comp_prob, [e["close_ts"] for e in comp_prob]) if comp_prob else {"trades": 0},
               "econ_solv1_same_closes": summarize([{**t, "cohort": coh, "ticker": "", "kind": "", "side": "", "t": 0} for t in comp_sol]) if comp_sol else {"trades": 0},
               "sharpe_15m": sharpe_15m(marks, t0, (t1 + 1800) if t1 else None), "first_decision_pt": (dt.datetime.fromtimestamp(t0, PT).strftime("%Y-%m-%d %H:%M PT") if t0 else None)}
        # PREREG P24 status
        ed = res["econ_direct"]; ep = res["econ_probability_arm_same_closes"]; es = res["econ_solv1_same_closes"]
        enough = (ed.get("closes") or 0) >= 100
        pos_1se = bool(ed.get("se_c_per_ct_close") is not None and ed["c_per_ct_close_mean"] - ed["se_c_per_ct_close"] > 0)
        def _beats(e):  # direct c/ct mean > comparator mean (comparator with no trades counts as 0 c/ct on those closes)
            return bool(ed.get("c_per_ct_close_mean") is not None and ed["c_per_ct_close_mean"] > (e.get("c_per_ct_close_mean") if e.get("trades") else 0.0))
        def _worse_2se(e):
            if not ed.get("se_c_per_ct_close"): return False
            ref = e.get("c_per_ct_close_mean") if e.get("trades") else 0.0
            return ed["c_per_ct_close_mean"] + 2 * ed["se_c_per_ct_close"] < ref
        loss_stop = bool(ed.get("net_usd") is not None and ed["net_usd"] <= -25.0)
        res["prereg_p24_status"] = {"closes": ed.get("closes", 0), "n_required": 100, "promotion_candidate": bool(enough and pos_1se and _beats(ep) and _beats(es)),
                                    "stop_triggered": bool(loss_stop or (enough and (_worse_2se(ep) or _worse_2se(es)))),
                                    "components": {"net_positive_1se": pos_1se, "beats_probability_arm": _beats(ep), "beats_solv1": _beats(es), "realised_loss_ge_25": loss_stop, "worse_than_prob_2se": _worse_2se(ep), "worse_than_solv1_2se": _worse_2se(es)}}
        out["cohorts"][coh] = res
    return out


def md(res: dict) -> str:
    if not res.get("n_decisions"): return "P24: no direct decisions yet."
    f = lambda v, d=2: ("—" if v is None else (f"{v:.{d}f}" if isinstance(v, float) else str(v)))
    L = [f"# P24 LLM direct trade decision — score ({dt.datetime.now(PT).strftime('%Y-%m-%d %H:%M PT')})", "", f"decisions {res['n_decisions']}, fill rows {res['n_fill_rows']}, marks {res['n_marks']}; Sharpe/15-min mark (all marks, not annualised): {f(res['sharpe_15m_all'].get('sharpe'), 3)} over {res['sharpe_15m_all'].get('n_intervals')} intervals", ""]
    for coh, r in res["cohorts"].items():
        ed = r["econ_direct"]; ep = r["econ_probability_arm_same_closes"]; es = r["econ_solv1_same_closes"]; st = r["prereg_p24_status"]
        L += [f"## cohort prompt {r['prompt_hash']} · {r['model']} (first decision {r['first_decision_pt']})",
              f"- decisions {r['decisions']}: {r['by_action']}; by time {r['by_time_action']}; parse errors {r['parse_errors']}; results {r['result_status']}",
              f"- entries attempted {r['entries_attempted']}: filled {r['fills_full']}, partial {r['fills_partial']}, no fill {r['no_fill']}; exits {r['exits']} (unfilled {r['exit_unfilled']}); risk_limited {r['risk_limited']}; open now {r['open_now']}",
              f"- realised (direct): trades {ed.get('trades', 0)}, closes {ed.get('closes')}, net ${f(ed.get('net_usd'))} after fees ${f(ed.get('fees_usd'))}, {f(ed.get('c_per_ct_close_mean'), 1)} ± {f(ed.get('se_c_per_ct_close'), 1)} c/ct (close-clustered), hit {f(ed.get('hit'))}, max DD ${f(ed.get('max_drawdown_usd'))}, scorer-settled {ed.get('scorer_settled', 0)}",
              f"- probability arm (gated sim) same closes: trades {ep.get('trades', 0)}, {f(ep.get('c_per_ct_close_mean'), 1)} ± {f(ep.get('se_c_per_ct_close'), 1)} c/ct, net ${f(ep.get('net_usd'))} | SOL-v1 same closes: trades {es.get('trades', 0)}, {f(es.get('c_per_ct_close_mean'), 1)} ± {f(es.get('se_c_per_ct_close'), 1)} c/ct, net ${f(es.get('net_usd'))}",
              f"- Sharpe per 15-min mark: {f(r['sharpe_15m'].get('sharpe'), 3)} ({r['sharpe_15m'].get('n_intervals')} intervals, mean ${f(r['sharpe_15m'].get('mean_usd'), 3)} sd ${f(r['sharpe_15m'].get('sd_usd'), 3)})",
              f"- PREREG P24: {'PROMOTION-CANDIDATE (review)' if st['promotion_candidate'] else ('STOP -> scored-only' if st['stop_triggered'] else f'collecting ({st['closes']}/100)')} components {st['components']}"]
        for em, b in r["by_exec_model"].items():
            e = b["econ_direct"]; rs = b["resting"]
            L.append(f"- exec model `{em}`: entries {b['entries_attempted']} (immediate full {b['immediate_full']}, partial {b['immediate_partial']}, none {b['immediate_none']}; rested {b['rested']}); realised trades {e.get('trades', 0)}, net ${f(e.get('net_usd'))}, {f(e.get('c_per_ct_close_mean'), 1)} ± {f(e.get('se_c_per_ct_close'), 1)} c/ct"
                     + (f"; resting events {rs['events']}: filled fully {rs['filled_fully']}, ended partial {rs['ended_with_partial']}, ended unfilled {rs['ended_unfilled']}, still resting {rs['still_resting']}, time to full fill {rs['time_to_full_fill_s']}" if rs else ""))
        L.append("")
    if res.get("resting_now"): L += [f"resting now: {res['resting_now']}", ""]
    rp = res.get("ioc_unfilled_replay_informational")
    if rp is not None:
        L += [f"## INFORMATIONAL — historical IOC-unfilled intents replayed with exec model v2 over the ws tape (not positions, no P&L; ttl 60 s, cancel T-60 s, 1-s poll, crossing-only)"]
        if not rp: L.append("- none")
        for x in rp:
            L.append(f"- {x['t_placed_pt']} {x['ticker']} {x['side'].upper()} {x['qty']} @ {x['limit']} (T-{int(x['decision_secs_left'] or 0)}s; IOC best offer {x['ioc_best_offer']}): would-have-filled = **{x['would_have_filled']}** {x['qty_filled']}/{x['qty']}"
                     + (f" avg px {x['avg_px']} (fee ${x['fee']:.2f}) first fill +{x['time_to_first_fill_s']}s, last +{x['time_to_last_fill_s']}s in {x['n_fills']} fill(s)" if x['qty_filled'] else "")
                     + f"; settlement {x['settlement']} -> side would have {'WON' if x['side_would_have_won'] else ('LOST' if x['side_would_have_won'] is False else 'n/a')}; tape rows {x['tape_rows']}")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); ap.add_argument("--root", default="data/llm-direct"); ap.add_argument("--json", action="store_true"); ap.add_argument("--no-replay", action="store_true")
    a = ap.parse_args(); res = score(pathlib.Path(a.root), replay=not a.no_replay)
    dump = lambda o: json.dumps(o, indent=1, default=lambda x: (dict(x) if isinstance(x, collections.Counter) else str(x)))
    print(dump(res) if a.json else md(res))
    pathlib.Path(a.root).mkdir(parents=True, exist_ok=True); (pathlib.Path(a.root) / "score_latest.json").write_text(dump(res), encoding="utf-8")
