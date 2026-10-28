"""Forward operating summary for SOL-v1 from data/sol-v1/*.jsonl (no model changes; read-only).
Usage: uv run python scripts/sol_v1_forward_stats.py [--since UNIX_TS] [--root data/sol-v1]"""
from __future__ import annotations
import argparse, collections, json, pathlib, statistics as st, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

def rows(p: pathlib.Path):
    if not p.exists(): return []
    out = []
    for l in p.read_text(encoding="utf-8").splitlines():
        if l.strip():
            try: out.append(json.loads(l))
            except Exception: pass
    return out

def q(xs, k):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(k * len(xs)))], 3) if xs else None

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--since", type=float, default=0.0); ap.add_argument("--root", default="data/sol-v1"); a = ap.parse_args()
    root = pathlib.Path(a.root)
    snaps = [r for r in rows(root / "snapshots.jsonl") if r["t"] >= a.since]; dec = [r for r in rows(root / "decisions.jsonl") if r["t"] >= a.since]
    fills = [r for r in rows(root / "fills.jsonl") if r["t"] >= a.since]; marks = [r for r in rows(root / "marks.jsonl") if r["t"] >= a.since]
    closes = sorted({r["close_ts"] for r in snaps}); now = time.time()
    full = [c for c in closes if c + 90 <= now]
    out = {"since": a.since, "closes_seen": len(closes), "closes_completed": len(full), "first_close": (time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(closes[0])) if closes else None),
           "last_close": (time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(closes[-1])) if closes else None), "entry_evaluations": len(snaps),
           "eval_by_bucket": dict(sorted(collections.Counter(f"T-{int(min(r['secs_left'] // 60, 14))}m" for r in snaps).items(), key=lambda kv: int(kv[0][2:-1]))),
           "decisions": dict(collections.Counter(r["decision"] for r in snaps)), "abstain_reasons": dict(collections.Counter((r["reasons"][0] if r["reasons"] else "-") for r in snaps if r["decision"] != "enter")),
           "book_src": dict(collections.Counter(r.get("book_src") for r in snaps)),
           "ages_s": {"cfb_p50": q([r["cfb_age_s"] for r in snaps], .5), "cfb_p95": q([r["cfb_age_s"] for r in snaps], .95), "book_p50": q([r["book_age_s"] for r in snaps], .5), "book_p95": q([r["book_age_s"] for r in snaps], .95),
                      "decision_latency_p50": q([r["latency_s"] for r in snaps], .5), "decision_latency_p95": q([r["latency_s"] for r in snaps], .95)},
           "sigma_1s_bp": {"p50": q([1e4 * r["sigma_1s"] for r in snaps], .5), "p95": q([1e4 * r["sigma_1s"] for r in snaps], .95)},
           "p_model_minus_mid": {"p50": q([r["p_model"] - r["mid"] for r in snaps if r.get("p_model") is not None and r.get("mid") is not None], .5),
                                 "abs_p90": q([abs(r["p_model"] - r["mid"]) for r in snaps if r.get("p_model") is not None and r.get("mid") is not None], .9)}}
    evs = [100 * e["ev_pc"] for r in snaps if r.get("ev") for e in r["ev"].values() if e.get("ev_pc") is not None]
    reqs = [100 * e["req_pc"] for r in snaps if r.get("ev") for e in r["ev"].values() if e.get("req_pc") is not None]
    from predhft.fees import kalshi_taker_fee
    raw = []
    for r in snaps:
        for e in (r.get("ev") or {}).values():
            if e.get("best") is not None and 0 < e["best"] < 1:
                raw.append(100 * (e["p_side"] - e["best"] - e["adverse_pc"] - kalshi_taker_fee(10, e["best"]) / 10))
    out["raw_ev_at_best_offer_c"] = {"n": len(raw), "p5": q(raw, .05), "p25": q(raw, .25), "p50": q(raw, .5), "p75": q(raw, .75), "p95": q(raw, .95), "share_gt_3c": (round(sum(1 for x in raw if x > 3) / len(raw), 4) if raw else None)}
    out["ev_pc_c_all_sides_with_depth"] = {"n": len(evs), "p5": q(evs, .05), "p50": q(evs, .5), "p95": q(evs, .95), "max": (round(max(evs), 1) if evs else None), "share_ge_req": (round(sum(1 for r in snaps if r.get("ev") for e in r["ev"].values() if e.get("ev_pc") is not None and e["ev_pc"] >= e["req_pc"]) / len(evs), 4) if evs else None), "req_p50": q(reqs, .5)}
    out["fills"] = [{k: r.get(k) for k in ("kind", "ticker", "side", "qty", "intended_qty", "px", "limit", "px_dw_at_decision", "cost", "fee", "p_side", "p_model", "mid", "secs_left", "decision_latency_s", "book_src", "recheck_src", "result", "feed_result", "pnl", "exit_px", "proceeds")} for r in fills]
    he = [r for r in dec if r.get("kind") == "hold_exit"]
    out["hold_exit"] = {"evaluations": len(he), "exits": sum(1 for r in he if r["decision"] == "exit"), "exit_minus_hold_c_per_ct_p50": (q([100 * (r["exit_proceeds"] - r["hold_value"]) / max(1, r["exit_qty"] or 1) for r in he], .5) if he else None)}
    out["aborts"] = [r for r in dec if r.get("kind") == "entry_abort"]
    settles = [r for r in fills if r["kind"] in ("settle", "exit")]
    out["realized_usd"] = round(sum(r["pnl"] for r in settles), 2); out["fees_usd"] = round(sum(r.get("fee", 0) for r in fills if r["kind"] == "fill") + sum(r.get("exit_fee", 0) for r in fills if r["kind"] == "exit"), 2)
    if marks:
        eq = [m["equity"] for m in marks]; peak = eq[0]; dd = 0.0
        for e in eq: peak = max(peak, e); dd = min(dd, e - peak)
        # fixed 15-min returns incl. idle, from the marks closest to each boundary
        by = {}
        for m in marks: by[int(m["t"] // 900)] = m["equity"]
        ks = sorted(by); rets = [(by[b] - by[a_]) / 250.0 for a_, b in zip(ks, ks[1:])]
        out["marks"] = {"n": len(marks), "equity_first": eq[0], "equity_last": eq[-1], "max_drawdown_usd": round(dd, 2), "open_mark_last": marks[-1]["open_mark"], "ret_15min": [round(r, 5) for r in rets],
                        "sharpe_per_15min": (round(st.fmean(rets) / st.pstdev(rets), 3) if len(rets) > 1 and st.pstdev(rets) > 0 else None)}
    print(json.dumps(out, indent=1, default=str))

if __name__ == "__main__": main()
