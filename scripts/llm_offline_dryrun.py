"""P25 offline dry-run (RETROSPECTIVE, in-sample; docs/PREREG.md §P25 step 4). NOT a forward test.

For the last N closes that the live Opus cohorts forecast (data/llm-shadow/forecasts.jsonl), rebuild the SAME decision snapshot
from recorded data (data/sol-v1/snapshots.jsonl row at the live snapshot_t; official feed, candles, perp, context, ws book tape cut
causally at that instant; tail table built from candles strictly BEFORE the first dry-run close), assemble the v2a / v2b packets,
call the model (reasoning effort low), parse, simulate the decision with the P24 execution model (immediate crossing fill against
the snapshot book + resting replay on the recorded tape, TTL 60 s, hard limits lot 5..20 / $15), settle from the official feed and
compare Brier / decisions / paper P&L with the live cohort rows on the same closes.

Budget: data/llm-shadow/budget_offline.json (separate from the live sidecar's), cap --cap (default 160).
Key: Hermes .env at runtime (llm_shadow.load_credentials), never logged. Output: data/llm-shadow/offline/forecasts_offline.jsonl (+ raw text),
data/llm-shadow/offline/dryrun_summary.json, --md prints the comparison table.

    uv run python scripts/llm_offline_dryrun.py --closes 40 --variants v2a,v2b --cap 160
    uv run python scripts/llm_offline_dryrun.py --score-only --md
"""
from __future__ import annotations
import argparse, bisect, collections, datetime as dt, json, math, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft import llm_shadow as L, llm_packet_v2 as P2, llm_direct as D, sol_v1 as S
from predhft.fees import kalshi_taker_fee

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "llm-shadow" / "offline"
PT = dt.timezone(dt.timedelta(hours=-7))
MODEL = "anthropic/claude-opus-5.5"


def load_jsonl(p: pathlib.Path) -> list[dict]:
    out = []
    if not p.exists(): return out
    for l in p.open(encoding="utf-8"):
        try: out.append(json.loads(l))
        except Exception: pass
    return out

def load_cfb() -> list[tuple[int, float]]:
    rows = []
    for d in load_jsonl(ROOT / "data" / "cfb" / "SOL.jsonl"):
        if d.get("value") and d.get("value_ts_ms"): rows.append((int(d["value_ts_ms"]) // 1000, float(d["value"])))
    rows.sort(); out = []
    for s, v in rows:
        if out and out[-1][0] == s: out[-1] = (s, v)
        else: out.append((s, v))
    return out

def nearest_before(rows: list[dict], t: float, key: str = "t") -> dict | None:
    ts = [r[key] for r in rows]; i = bisect.bisect_right(ts, t)
    return rows[i - 1] if i > 0 else None

def snapshot_index(path: pathlib.Path) -> list[dict]:
    rows = [r for r in load_jsonl(path) if r.get("kind") == "entry_eval"]; rows.sort(key=lambda r: r["t"]); return rows

def find_snapshot(snaps: list[dict], close_ts: int, snapshot_t: float, tol: float = 20.0) -> dict | None:
    ts = [r["t"] for r in snaps]; i = bisect.bisect_right(ts, snapshot_t + 1e-6)
    for j in (i - 1, i):
        if 0 <= j < len(snaps) and int(snaps[j]["close_ts"]) == close_ts and abs(snaps[j]["t"] - snapshot_t) <= tol: return snaps[j]
    return None

def tape_for(ticker: str, t0: float, t1: float) -> list[dict]:
    days = {dt.datetime.fromtimestamp(x, dt.timezone.utc).strftime("%Y%m%d") for x in (t0, t1)}
    out = []
    for day in sorted(days):
        for r in load_jsonl(ROOT / "data" / "sol-v1" / "book" / f"{day}.jsonl"):
            if r.get("ticker") == ticker and r.get("recv_t") is not None and t0 <= r["recv_t"] <= t1: out.append(r)
    out.sort(key=lambda r: r["recv_t"]); return out

def candles_until(now: float) -> dict:
    import pandas as pd
    out = {}
    for a in ("SOL", "BTC", "ETH"):
        p = ROOT / "data" / "candles" / f"{a}_1m.parquet"
        if p.exists():
            d = pd.read_parquet(p); out[a] = d[(d.ts <= int(now)) & (d.ts > int(now) - 70 * 60)]
    return out

def simulate_decision(dec: dict, snap: dict, t_decision: float, tape: list[dict], K: float, close_ts: int) -> dict:
    """P24 execution model on recorded data: hard limits (ledger-free), immediate crossing fill vs the snapshot book, remainder
    rests on the tape (TTL 60 s, crossing-only, never in the final minute). Returns fill summary."""
    if dec["action"] not in ("BUY_YES", "BUY_NO"): return {"status": "no_trade", "qty": 0}
    hl = D.hard_limits(dec["action"], dec["qty"], dec["limit_price"], None, close_ts)
    if not hl["ok"]: return {"status": hl["reason"], "qty": 0}
    side = D.side_of(dec["action"]); book = {"bids": snap.get("bids") or [], "asks": snap.get("asks") or [], "recv_t": snap.get("book_recv_t") or snap["t"]}
    f = D.fill_entry(dec["action"], hl["qty"], hl["limit"], book)
    qty = int(f["qty"]); cost = float(f["cost"]); fee = float(f["fee"]); rest = None
    rem = hl["qty"] - qty
    if rem > 0 and tape:
        o = D.new_order("offline", snap.get("ticker"), close_ts, K, dec["action"], rem, hl["limit"], t_decision)
        rest = D.replay_resting(o, [r for r in tape if r["recv_t"] >= t_decision - 10])
        qty += rest["qty_filled"]; cost += rest["cost"]; fee += rest["fee"]
    return {"status": ("filled" if qty >= hl["qty"] else "partial" if qty > 0 else "unfilled"), "side": side, "qty": qty, "qty_wanted": hl["qty"], "limit": hl["limit"], "px": (round(cost / qty, 4) if qty else None), "cost": round(cost, 4), "fee": round(fee, 4),
            "immediate_qty": int(f["qty"]), "rest": ({k: rest[k] for k in ("event", "qty_filled", "time_to_first_fill_s")} if rest else None), "clamps": hl.get("clamps")}

def settle_pnl(fill: dict, y: float) -> float | None:
    if not fill or not fill.get("qty"): return None
    win = (y == 1.0) if fill["side"] == "yes" else (y == 0.0)
    return round((fill["qty"] * 1.0 - fill["cost"] if win else -fill["cost"]) - fill["fee"], 4)


def run(n_closes: int, variants: list[str], cap: int, model: str, decision_secs=(600.0, 300.0), dry: bool = False):
    OUT.mkdir(parents=True, exist_ok=True)
    live = [r for r in load_jsonl(ROOT / "data" / "llm-shadow" / "forecasts.jsonl") if r.get("model_name") == MODEL and r.get("p_llm") is not None and r.get("decision_secs_left") in decision_secs]
    closes = sorted({int(r["close_ts"]) for r in live})[-n_closes:]
    series = load_cfb(); snaps = snapshot_index(ROOT / "data" / "sol-v1" / "snapshots.jsonl")
    perps = sorted(load_jsonl(ROOT / "data" / "perp" / "SOL.jsonl"), key=lambda r: r["t"]); ctxs = sorted(load_jsonl(ROOT / "data" / "context" / "SOL.jsonl"), key=lambda r: r["t"])
    first_close = closes[0]; table = P2.load_or_build_tail_table(until_ts=first_close - 900)       # causal: history strictly before the first dry-run market opened
    brief = P2.load_brief()
    done = {(r["close_ts"], r["decision_secs_left"], r["variant"]) for r in load_jsonl(OUT / "forecasts_offline.jsonl") if r.get("p_llm") is not None}
    budget = L.Budget(ROOT / "data" / "llm-shadow" / "budget_offline.json", cap); client = None
    if not dry:
        key, _m, base = L.load_credentials(); client = L.LLMClient(key, model, base); del key
    red = client.red if client else L.Redactor(None)
    print(f"closes {len(closes)} ({dt.datetime.fromtimestamp(closes[0], PT):%m-%d %H:%M} .. {dt.datetime.fromtimestamp(closes[-1], PT):%m-%d %H:%M} PT), variants {variants}, table horizons {table and table['horizons_min']} built on {table and table['history']['days']} d, brief {'present' if brief else 'absent'}; budget {budget.snapshot()}")
    for c in closes:
        for dsl in decision_secs:
            ref = next((r for r in live if int(r["close_ts"]) == c and r["decision_secs_left"] == dsl), None)
            if ref is None: continue
            snap = find_snapshot(snaps, c, ref["snapshot_t"])
            if snap is None: print(f"  {ref['ticker']} T-{dsl:.0f}: no recorded snapshot (live was {ref.get('snapshot_src')}), skipped"); continue
            now = float(ref["t_sent"] or (snap["t"] + 2.0)); snap = {**snap, "snapshot_src": "recorded"}
            ser = [(s, v) for s, v in series if now - 720 <= s <= now]
            cand = candles_until(now); perp = nearest_before(perps, now); ctx = nearest_before(ctxs, now)
            tape = tape_for(ref["ticker"], now - 90, c)
            dblock = D.direct_block(dsl, None, {"yes_bid": snap.get("yes_bid"), "yes_ask": snap.get("yes_ask"), "bids": snap.get("bids"), "asks": snap.get("asks")})
            for v in variants:
                if (c, dsl, v) in done: continue
                pk = L.build_packet(snap, now, v, ser, cand, perp, ctx, dsl, direct=dblock, tape=[r for r in tape if r["recv_t"] <= now], table=table, brief=brief)
                msgs = L.messages_for(pk, v)
                row = {"kind": "forecast_offline", "retrospective": True, "variant": v, "prompt_hash": L.prompt_hash_for(v), "packet_hash": L.packet_hash(pk), "ticker": ref["ticker"], "close_ts": c, "decision_secs_left": dsl, "now": now, "snapshot_t": snap["t"],
                       "K": snap["K"], "x_now": snap.get("x_now"), "mid": snap.get("mid"), "yes_bid": snap.get("yes_bid"), "yes_ask": snap.get("yes_ask"), "p_model": snap.get("p_model"), "live_prompt_hash": ref["prompt_hash"], "live_p_llm": ref["p_llm"], "live_p_own": ref.get("p_own"),
                       "live_direct_action": (ref.get("direct_decision") or {}).get("action"), "model_name": model, "prompt_chars": sum(len(m["content"]) for m in msgs),
                       "v2_fields": {"breakeven_yes_10": pk["executable_now"]["yes"]["10"]["breakeven_p"], "breakeven_no_10": pk["executable_now"]["no"]["10"]["breakeven_p"], "move_bp_for_yes": pk["required_move"].get("move_bp_for_yes"), "sigma_units_for_yes": pk["required_move"].get("sigma_units_for_yes"),
                                     "p_yes_empirical_feed": ((pk.get("empirical_tails") or {}).get("feed_sigma") or {}).get("p_yes_empirical"), "p_yes_empirical_candle": ((pk.get("empirical_tails") or {}).get("candle_rv60") or {}).get("p_yes_empirical"), "p_gauss_kappa1": pk["required_move"].get("p_gauss_kappa1"),
                                     "ratio_realised_over_implied": pk["volatility"].get("ratio_realised_over_implied"), "last_60s": pk["book"].get("last_60s"), "conditions": pk.get("conditions_brief")},
                       "p_llm": None, "p_own": None, "edge_stated": None, "decision": None, "fill": None, "raw": None, "tokens": None, "error": None}
                if dry: row["error"] = "dry"; S._append(OUT / "packets_dry.jsonl", {**row, "packet": pk}); print(f"  {ref['ticker']} T-{dsl:.0f} {v}: packet {row['prompt_chars']} chars (~{row['prompt_chars'] // 4} tok)"); continue
                ok, why = budget.allow(time.time())
                if not ok: print("budget stop:", why); return
                res = client.complete(msgs, budget)
                if not res["ok"]: row["error"] = res["error"]; S._append(OUT / "forecasts_offline.jsonl", row); print("  error", res["error"]); continue
                budget.record(res.get("usage")); pr = L.parse_response(res["text"] or "")
                dec = D.parse_decision(pr.get("decision_raw"), False)
                fill = simulate_decision(dec, snap, now + 6.0, tape, float(snap["K"]), c)            # +6 s: typical LLM latency before the order exists
                row.update({"p_llm": pr["p_llm"], "p_own": pr.get("p_own"), "edge_stated": pr.get("edge"), "rationale": red.redact(pr.get("rationale") or ""), "beyond_market": red.redact(pr.get("beyond_market") or ""), "decision": dec, "fill": fill,
                            "raw": red.redact(res["text"] or "")[:1500], "tokens": res.get("usage"), "latency_s": res.get("latency_s"), "finish_reason": res.get("finish_reason")})
                if pr["p_llm"] is None: row["error"] = "unparsable"
                S._append(OUT / "forecasts_offline.jsonl", row)
                print(f"  {ref['ticker']} T-{dsl:.0f} {v}: p_own={row['p_own']} p_llm={row['p_llm']} (live {ref['p_llm']}, mid {snap.get('mid')}) -> {dec['action']} q={dec['qty']} L={dec['limit_price']} fill={fill.get('status')} tok={(res.get('usage') or {}).get('total_tokens')}")
    print("budget", budget.snapshot())


def score(decision_secs=(600.0, 300.0)) -> dict:
    series = load_cfb()
    try: kres = json.loads((ROOT / "data" / "ts" / "results.json").read_text(encoding="utf-8"))
    except Exception: kres = {}
    rows = [r for r in load_jsonl(OUT / "forecasts_offline.jsonl") if r.get("p_llm") is not None]
    live = [r for r in load_jsonl(ROOT / "data" / "llm-shadow" / "forecasts.jsonl") if r.get("model_name") == MODEL and r.get("p_llm") is not None]
    def y_of(ticker, c, K):
        kr = kres.get(ticker, {}).get("result")
        if kr in ("yes", "no"): return 1.0 if kr == "yes" else 0.0
        fs = S.settle_from_feed(series, int(c), float(K)); return None if fs is None else (1.0 if fs["result"] == "yes" else 0.0)
    def brier(p, y): return (p - y) ** 2
    out = {"retrospective": True, "note": "in-sample dry-run on recorded closes the live cohorts also saw; NOT a forward test", "variants": {}, "live_same_closes": {}}
    keys = {(r["close_ts"], r["decision_secs_left"]) for r in rows}
    for v in sorted({r["variant"] for r in rows}):
        vr = [r for r in rows if r["variant"] == v]; res = {"prompt_hash": vr[0]["prompt_hash"], "n": len(vr), "by_time": {}}
        for dsl in decision_secs:
            s = [r for r in vr if r["decision_secs_left"] == dsl]
            s = [(r, y_of(r["ticker"], r["close_ts"], r["K"])) for r in s]; s = [(r, y) for r, y in s if y is not None]
            if not s: continue
            d_mid = [brier(r["p_llm"], y) - brier(r["mid"], y) for r, y in s if r.get("mid") is not None]
            d_mod = [brier(r["p_llm"], y) - brier(r["p_model"], y) for r, y in s if r.get("p_model") is not None]
            own_vs_model = [abs((r["p_own"] if r.get("p_own") is not None else r["p_llm"]) - r["p_model"]) for r, y in s if r.get("p_model") is not None]
            acts = collections.Counter((r["decision"] or {}).get("action") for r, y in s)
            trades = [(r, y) for r, y in s if (r.get("fill") or {}).get("qty")]
            pnl = [settle_pnl(r["fill"], y) for r, y in trades]; qty = sum(r["fill"]["qty"] for r, y in trades)
            intended = sum(1 for r, y in s if (r["decision"] or {}).get("action") in ("BUY_YES", "BUY_NO"))
            res["by_time"][f"T-{int(dsl / 60)}min"] = {"n": len(s), "brier_llm": round(sum(brier(r["p_llm"], y) for r, y in s) / len(s), 4), "brier_mid": round(sum(brier(r["mid"], y) for r, y in s) / len(s), 4), "brier_model": round(sum(brier(r["p_model"], y) for r, y in s) / len(s), 4),
                                                        "llm_minus_mid": {"mean": round(sum(d_mid) / len(d_mid), 4), "se": round((sum((x - sum(d_mid) / len(d_mid)) ** 2 for x in d_mid) / (len(d_mid) - 1)) ** 0.5 / len(d_mid) ** 0.5, 4) if len(d_mid) > 1 else None},
                                                        "llm_minus_model": round(sum(d_mod) / len(d_mod), 4), "median_abs_p_own_minus_p_model": round(sorted(own_vs_model)[len(own_vs_model) // 2], 4), "frac_within_1c_of_model": round(sum(1 for x in own_vs_model if x <= 0.01) / len(own_vs_model), 3),
                                                        "frac_less_extreme_than_mid": round(sum(1 for r, y in s if abs(r["p_llm"] - 0.5) < abs(r["mid"] - 0.5)) / len(s), 3), "actions": dict(acts), "intended_trades": intended, "fills": len(trades), "contracts": qty,
                                                        "pnl_usd": round(sum(pnl), 2), "net_c_per_ct": (round(100 * sum(pnl) / qty, 2) if qty else None), "wins": sum(1 for x in pnl if x > 0),
                                                        "trades": [{"ticker": r["ticker"], "side": r["fill"]["side"], "qty": r["fill"]["qty"], "px": r["fill"]["px"], "p_own": r.get("p_own"), "mid": r["mid"], "y": y, "pnl": p} for (r, y), p in zip(trades, pnl)]}
        out["variants"][v] = res
    # live cohort(s) on the same (close, dsl) keys
    lv = [r for r in live if (int(r["close_ts"]), r["decision_secs_left"]) in keys]
    for coh in sorted({r["prompt_hash"] for r in lv}):
        cr = [r for r in lv if r["prompt_hash"] == coh]; res = {"n": len(cr), "by_time": {}}
        for dsl in decision_secs:
            s = [(r, y_of(r["ticker"], r["close_ts"], r["K"])) for r in cr if r["decision_secs_left"] == dsl]; s = [(r, y) for r, y in s if y is not None]
            if not s: continue
            d_mid = [brier(r["p_llm"], y) - brier(r["mid"], y) for r, y in s]
            acts = collections.Counter((r.get("direct_decision") or {}).get("action") for r, y in s)
            res["by_time"][f"T-{int(dsl / 60)}min"] = {"n": len(s), "brier_llm": round(sum(brier(r["p_llm"], y) for r, y in s) / len(s), 4), "brier_mid": round(sum(brier(r["mid"], y) for r, y in s) / len(s), 4), "brier_model": round(sum(brier(r["p_model"], y) for r, y in s) / len(s), 4),
                                                        "llm_minus_mid": round(sum(d_mid) / len(d_mid), 4), "median_abs_p_llm_minus_p_model": round(sorted(abs(r["p_llm"] - r["p_model"]) for r, y in s)[len(s) // 2], 4),
                                                        "frac_less_extreme_than_mid": round(sum(1 for r, y in s if abs(r["p_llm"] - 0.5) < abs(r["mid"] - 0.5)) / len(s), 3), "direct_actions": dict(acts)}
        out["live_same_closes"][coh] = res
    # realised P&L of the live direct arm on the same closes (fills.jsonl settle rows)
    fills = [f for f in load_jsonl(ROOT / "data" / "llm-direct" / "fills.jsonl") if f.get("kind") == "settle" and any(int(f["ticker"].split("-")[1][-4:] or 0) >= 0 for _ in [0])]
    tick_closes = {r["ticker"] for r in rows}
    lf = [f for f in fills if f["ticker"] in tick_closes]
    out["live_direct_same_closes"] = {"settled_positions": len(lf), "pnl_usd": round(sum(float(f["pnl"]) for f in lf), 2), "contracts": sum(int(f["qty"]) for f in lf), "wins": sum(1 for f in lf if f.get("win"))}
    out["n_rows"] = len(rows); out["closes"] = len({r["close_ts"] for r in rows}); out["budget_offline"] = json.loads((ROOT / "data" / "llm-shadow" / "budget_offline.json").read_text(encoding="utf-8")) if (ROOT / "data" / "llm-shadow" / "budget_offline.json").exists() else None
    (OUT / "dryrun_summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out

def md(res: dict) -> str:
    L_ = [f"# P25 offline dry-run (RETROSPECTIVE) — {res['n_rows']} rows over {res['closes']} closes", res["note"], "", "| cohort | time | n | Brier llm | mid | model | llm-mid | median |p-p_model| | less extreme than mid | actions | fills | P&L $ | net c/ct |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for v, r in res["variants"].items():
        for t, b in r["by_time"].items():
            L_.append(f"| {v} {r['prompt_hash'][:6]} | {t} | {b['n']} | {b['brier_llm']} | {b['brier_mid']} | {b['brier_model']} | {b['llm_minus_mid']['mean']}±{b['llm_minus_mid']['se']} | {b['median_abs_p_own_minus_p_model']} | {b['frac_less_extreme_than_mid']} | {b['actions']} | {b['fills']}/{b['intended_trades']} | {b['pnl_usd']} | {b['net_c_per_ct']} |")
    for coh, r in res["live_same_closes"].items():
        for t, b in r["by_time"].items():
            L_.append(f"| live {coh[:6]} | {t} | {b['n']} | {b['brier_llm']} | {b['brier_mid']} | {b['brier_model']} | {b['llm_minus_mid']} | {b['median_abs_p_llm_minus_p_model']} | {b['frac_less_extreme_than_mid']} | {b['direct_actions']} | - | - | - |")
    L_.append(f"\nlive direct arm on the same closes: {res['live_direct_same_closes']}")
    for v, r in res["variants"].items():
        for t, b in r["by_time"].items():
            for tr in b["trades"]: L_.append(f"- {v} {t} {tr['ticker']} {tr['side']} x{tr['qty']} @ {tr['px']} p_own={tr['p_own']} mid={tr['mid']} y={tr['y']} pnl={tr['pnl']}")
    return "\n".join(L_)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--closes", type=int, default=40); ap.add_argument("--variants", default="v2a,v2b"); ap.add_argument("--cap", type=int, default=160)
    ap.add_argument("--model", default=MODEL); ap.add_argument("--dry", action="store_true", help="assemble packets only (no calls)")
    ap.add_argument("--score-only", action="store_true"); ap.add_argument("--md", action="store_true")
    a = ap.parse_args()
    if not a.score_only: run(a.closes, [v.strip() for v in a.variants.split(",") if v.strip()], a.cap, a.model, dry=a.dry)
    res = score()
    print(md(res) if a.md else json.dumps({k: v for k, v in res.items() if k != "variants"} | {"variants": {v: {t: {kk: vv for kk, vv in b.items() if kk != "trades"} for t, b in r["by_time"].items()} for v, r in res["variants"].items()}}, indent=1))
