"""AUDIT 2026-10-28 — numbers for docs/AUDIT_2026-10-28.md. Read-only over data/ (paper), network only to re-fetch Kalshi market
results (cached in data/p22/audit_results_2026-10-28.json / markets_cache.json). Prints markdown tables; writes data/p22/audit_2026-10-28.json.

Usage: uv run python scripts/audit_2026_10_28.py [--no-fetch] [--t0 "2026-10-27 21:51"] [--t1 "2026-10-28 07:05"]  (window in PT)
"""
from __future__ import annotations
import argparse, collections, datetime as dt, glob, json, math, pathlib, re, ast, statistics, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft.reject_taxonomy import classify, side_present, CLASSES
from predhft.fees import kalshi_taker_fee
from predhft.ts_levers import TIMING, ARM_BY_ID

PT = dt.timezone(dt.timedelta(hours=-7))
DATA = pathlib.Path("data")
REBASE_TS = dt.datetime(2026, 10, 27, 15, 20, tzinfo=PT).timestamp()       # P13 rebase restart
OFFICIAL_TS = dt.datetime(2026, 10, 27, 16, 0, tzinfo=PT).timestamp()      # cfb feed live
P15_RESTART_TS = dt.datetime(2026, 10, 27, 16, 5, 23, tzinfo=PT).timestamp()
GATE_TS = dt.datetime(2026, 10, 27, 21, 51, 36, tzinfo=PT).timestamp()     # s7p13 restart with --ts-levers --bet-gate
FAM15 = {a: f"s7p13-KX{a}15M" for a in ("BTC", "ETH", "SOL")}
FAM12 = {a: f"s7p15-KX{a}15M" for a in ("BTC", "ETH", "SOL")}

def pt(ts: float) -> str: return dt.datetime.fromtimestamp(ts, PT).strftime("%m-%d %H:%M PT")
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0: return (0.0, 1.0)
    p = k / n; den = 1 + z * z / n; c = (p + z * z / (2 * n)) / den; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))
def clopper(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Clopper-Pearson via the beta quantile (bisection on the regularised incomplete beta; no scipy)."""
    def ibeta(a, b, x):
        if x <= 0: return 0.0
        if x >= 1: return 1.0
        # continued fraction (Numerical Recipes betacf)
        def cf(a, b, x):
            MAXIT, EPS, FPMIN = 300, 3e-14, 1e-300; qab, qap, qam = a + b, a + 1, a - 1; c, d = 1.0, 1 - qab * x / qap
            d = 1 / (d if abs(d) > FPMIN else FPMIN); h = d
            for m in range(1, MAXIT + 1):
                m2 = 2 * m; aa = m * (b - m) * x / ((qam + m2) * (a + m2)); d = 1 + aa * d; d = 1 / (d if abs(d) > FPMIN else FPMIN); c = 1 + aa / c; c = c if abs(c) > FPMIN else FPMIN; h *= d * c
                aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2)); d = 1 + aa * d; d = 1 / (d if abs(d) > FPMIN else FPMIN); c = 1 + aa / c; c = c if abs(c) > FPMIN else FPMIN; de = d * c; h *= de
                if abs(de - 1) < EPS: break
            return h
        lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
        return math.exp(lb) * cf(a, b, x) / a if x < (a + 1) / (a + b + 2) else 1 - math.exp(lb) * cf(b, a, 1 - x) / b
    def q(a, b, p):
        lo, hi = 0.0, 1.0
        for _ in range(100):
            mid = (lo + hi) / 2
            if ibeta(a, b, mid) < p: lo = mid
            else: hi = mid
        return (lo + hi) / 2
    lo = 0.0 if k == 0 else q(k, n - k + 1, alpha / 2); hi = 1.0 if k == n else q(k + 1, n - k, 1 - alpha / 2)
    return lo, hi

# ------------------------------------------------------------------------------------------------------------------ trades
def load_trades() -> list[dict]:
    rows = []
    for f in sorted(glob.glob("data/*/trades.jsonl")):
        fam = pathlib.Path(f).parent.name
        for l in open(f, encoding="utf-8"):
            if l.strip(): t = json.loads(l); t["fam"] = fam; rows.append(t)
    return rows

def version_of(t: dict) -> str:
    fam = t["fam"]; ts = t["t"]
    if fam.startswith("s7live") or fam.startswith("s7final"): return "P1/P2 hourly model-rule (stopped)"
    if fam.startswith("s7p10"): return "P10 hourly redesign"
    if fam.startswith("s7p15"):
        if ts < REBASE_TS: return "P12 15M model-rule | replica, pre-rebase"
        if ts < P15_RESTART_TS: return "P12 15M model-rule | replica, post-rebase"
        return "P12 15M model-rule | official feed (FROZEN eval)"
    if fam.startswith("s7p13"):
        if ts < REBASE_TS: return "P13 15M projection | replica, pre-rebase (floor .97)"
        if ts < OFFICIAL_TS: return "P13/P16 | replica, post-rebase"
        if ts < GATE_TS: return "P16 | official feed, no gate"
        return "P16+P18+P20 | official feed, gate on"
    return fam

def pnl_stats(trades: list[dict]) -> dict:
    settled = sorted([t for t in trades if t.get("status") == "settled"], key=lambda t: t["close_ts"])
    net = sum(t["pnl"] for t in settled); fees = sum(t["fee"] for t in settled)
    # drawdown on cumulative realised P&L ordered by settlement (close_ts)
    cum = peak = 0.0; mdd = 0.0
    for t in settled:
        cum += t["pnl"]; peak = max(peak, cum); mdd = min(mdd, cum - peak)
    # capital at risk: max simultaneous open cost (open at t, released at close_ts + 120 s settle lag), all families together (correlated)
    ev = sorted([(t["t"], t["cost"]) for t in settled] + [(t["close_ts"] + 120, -t["cost"]) for t in settled])
    cur = mx = 0.0; when = None
    for ts, c in ev:
        cur += c
        if cur > mx: mx, when = cur, ts
    wins = sorted([t["pnl"] for t in settled if t["pnl"] > 0], reverse=True); gross_win = sum(wins)
    top3 = sum(wins[:3]) / gross_win if gross_win else None
    big = max(settled, key=lambda t: t["pnl"]) if settled else None
    by_asset = collections.defaultdict(lambda: [0, 0.0, 0]); by_bucket = collections.defaultdict(lambda: [0, 0, 0.0, 0.0])
    for t in settled:
        a = t["ticker"][2:5]; by_asset[a][0] += 1; by_asset[a][1] += t["pnl"]; by_asset[a][2] += int(t["result"] == t["side"])
        b = min(int(t["price"] * 10) / 10, 0.9); by_bucket[b][0] += 1; by_bucket[b][1] += int(t["result"] == t["side"]); by_bucket[b][2] += t["pnl"]; by_bucket[b][3] += t["qty"]
    return {"n": len(settled), "net": round(net, 2), "fees": round(fees, 2), "contracts": sum(t["qty"] for t in settled), "max_drawdown": round(mdd, 2), "capital_at_risk_max": round(mx, 2), "capital_at_risk_when": (pt(when) if when else None),
            "top3_share_of_gross_wins": (round(top3, 3) if top3 is not None else None), "gross_wins": round(gross_win, 2), "largest_winner": ({"ticker": big["ticker"], "pnl": round(big["pnl"], 2), "fam": big["fam"]} if big else None),
            "net_ex_largest_winner": (round(net - big["pnl"], 2) if big else None), "by_asset": {a: {"n": v[0], "net": round(v[1], 2), "wins": v[2]} for a, v in sorted(by_asset.items())},
            "by_price_bucket": {f"{b:.1f}": {"n": v[0], "wins": v[1], "net": round(v[2], 2), "contracts": v[3], "wilson95": [round(x, 3) for x in wilson(v[1], v[0])]} for b, v in sorted(by_bucket.items())}}

# ------------------------------------------------------------------------------------------------------------------ books
def load_books(fam: str, t0: float = 0, t1: float = 9e12) -> list[dict]:
    out = []
    p = DATA / fam / "books.jsonl"
    if not p.exists(): return out
    with p.open(encoding="utf-8") as f:
        for l in f:
            if not l.strip(): continue
            r = json.loads(l)
            if r.get("mins_left") is None or not (t0 <= r["t"] <= t1): continue
            out.append(r)
    return out

def arm_window_at(fam: str) -> dict[int, tuple[float, float] | None]:
    """close_ts -> live arm window from runner.log 'TS ARM <id> close=<iso>' lines."""
    out = {}
    p = DATA / fam / "runner.log"
    if not p.exists(): return out
    for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r"TS ARM (\S+) close=(\S+) ", l)
        if m and m.group(1) in ARM_BY_ID:
            ct = int(dt.datetime.fromisoformat(m.group(2).replace("Z", "+00:00")).timestamp()); out[ct] = ARM_BY_ID[m.group(1)].window_s
    return out

def taxonomy_recount(t0: float, t1: float) -> dict:
    res = {}
    for a, fam in FAM15.items():
        rows = load_books(fam, t0, t1); arms = arm_window_at(fam); series = f"KX{a}15M"
        prim = collections.Counter(); flags = collections.Counter(); closes = set(); checks = 0; cands = 0; feas = collections.Counter()
        per_close_feasible = collections.defaultdict(lambda: {"any_full": False, "any_but_arm": False, "n": 0})
        chain = {"cfb_age": [], "poll_gap": []}; last_t = {}
        legacy_sentinel = 0; one_sided = 0
        for r in sorted(rows, key=lambda r: r["t"]):
            secs = r["close_ts"] - r["t"]
            if not (9.0 <= secs <= 55.2): continue                      # a "check" = one poll of one market inside the runner's trading window
            checks += 1; closes.add(r["close_ts"])
            c = classify(r, series, arm_window_s=arms.get(r["close_ts"]), official_era=(r["t"] >= OFFICIAL_TS))
            prim[c["reason"]] += 1
            for f in c["flags"]: flags[f] += 1
            if c["cls"] == "candidate": cands += 1
            bid_ok, ask_ok = side_present(r)
            if (r.get("yes_ask") == 1.0 and (r.get("ask_size") or 0) == 0): legacy_sentinel += 1
            if bid_ok != ask_ok: one_sided += 1
            # joint feasibility of the four P13/P16 conditions (ignoring feed/data classes)
            fl = set(c["flags"]); tim = not any(f.startswith("timing_exclusion") for f in fl); arm_only = fl & {f for f in fl if f.startswith("timing_exclusion")} == {"timing_exclusion:ts_arm_window"}
            d_ok = "no_economic_opportunity:d_small" not in fl; fav_ok = not (fl & {"risk_constraint:fav_cap", "risk_constraint:depth_lt_lot", "no_economic_opportunity:no_offer", "no_economic_opportunity:no_offer_pinned"})
            edge_ok = c["edge_c"] is not None and c["edge_c"] >= 5.0
            key = (tim, d_ok, fav_ok, edge_ok); feas[key] += 1
            pc = per_close_feasible[r["close_ts"]]; pc["n"] += 1
            if tim and d_ok and fav_ok and edge_ok: pc["any_full"] = True
            if (tim or arm_only) and d_ok and fav_ok and edge_ok: pc["any_but_arm"] = True
            if r.get("cfb_age_s") is not None: chain["cfb_age"].append(r["cfb_age_s"])
            if r["ticker"] in last_t: chain["poll_gap"].append(r["t"] - last_t[r["ticker"]])
            last_t[r["ticker"]] = r["t"]
        def qs(x): return {"n": len(x), "p50": round(statistics.median(x), 2), "p90": round(sorted(x)[int(0.9 * len(x))], 2), "max": round(max(x), 2)} if x else None
        res[a] = {"unique_markets": len(closes), "checks": checks, "candidates": cands, "primary": dict(prim.most_common()), "flags_all": dict(flags.most_common()),
                  "legacy_ask_1_size_0_rows": legacy_sentinel, "one_sided_rows": one_sided,
                  "joint_feasibility": {"all_four": feas[(True, True, True, True)], "timing_d_fav_not_edge": feas[(True, True, True, False)], "timing_d_not_fav": sum(v for k, v in feas.items() if k[0] and k[1] and not k[2]),
                                        "timing_not_d": sum(v for k, v in feas.items() if k[0] and not k[1]), "not_timing": sum(v for k, v in feas.items() if not k[0])},
                  "closes_with_full_feasible_snapshot": sum(1 for v in per_close_feasible.values() if v["any_full"]), "closes_feasible_if_arm_window_ignored": sum(1 for v in per_close_feasible.values() if v["any_but_arm"]),
                  "cfb_age_s": qs(chain["cfb_age"]), "poll_gap_s": qs(chain["poll_gap"])}
    return res

def legacy_nofill(t0: float, t1: float) -> dict:
    out = {}
    for a, fam in FAM15.items():
        c = collections.Counter(); n = 0; stale = 0; buys = 0; skips = 0; gate = 0
        for l in (DATA / fam / "runner.log").read_text(encoding="utf-8", errors="replace").splitlines():
            try: ts = dt.datetime.fromisoformat(l[:25]).timestamp()
            except Exception: continue
            if not (t0 <= ts <= t1): continue
            m = re.search(r"NOFILL (\S+) reasons=(\{.*\}) min_fav", l)
            if m: n += 1; c.update(ast.literal_eval(m.group(2)))
            stale += " STALE " in l; buys += " PAPER BUY " in l; skips += " SKIP " in l; gate += " GATE REJECT " in l
        out[a] = {"nofill_closes": n, "reasons_sum": sum(c.values()), "reasons": dict(c), "stale": stale, "paper_buys": buys, "skips": skips, "gate_rejects": gate}
    return out

# ------------------------------------------------------------------------------------------------------------------ P12 table
def p12_counts(t0: float, t1: float, lot: int = 10) -> dict:
    """P12 (s7p15) funnel from the tape + log: checks (rows in T-1.0..T-0.1 with a two-sided book), candidates (dyn-threshold rule
    satisfied on the recorded row), submissions (PAPER BUY + STALE + SKIP lines), fills (PAPER BUY)."""
    out = {}
    for a, fam in FAM12.items():
        rows = load_books(fam, t0, t1); checks = cands = 0; closes = set(); prim = collections.Counter()
        for r in rows:
            secs = r["close_ts"] - r["t"]
            if not (6.0 <= secs <= 60.0): continue
            checks += 1; closes.add(r["close_ts"]); yb, ya, ybs, yas, p, thr = r["yes_bid"], r["yes_ask"], r["bid_size"], r["ask_size"], r["p_model"], r["thr_used"]
            bid_ok, ask_ok = side_present(r); mid = (yb + ya) / 2 if (bid_ok and ask_ok) else None
            if r["t"] >= REBASE_TS and (r.get("partial_twap") is None or (r.get("partial_n") or 0) < 3): prim["timing_exclusion:partial_warmup" if secs > 57 else "feed_health:partial_missing"] += 1; continue
            if not (bid_ok and ask_ok): prim["no_economic_opportunity:one_side_absent"] += 1; continue
            if ya - yb > 0.06: prim["risk_constraint:spread_gt_6c"] += 1; continue
            if not (0.03 <= mid <= 0.97): prim["risk_constraint:mid_outside_3_97"] += 1; continue
            if p - ya >= thr and yas >= lot: cands += 1; prim["candidate:yes"] += 1
            elif (yb - p) >= thr and ybs >= lot: cands += 1; prim["candidate:no"] += 1
            elif (p - ya >= thr) or (yb - p >= thr): prim["risk_constraint:depth_lt_lot"] += 1
            else: prim["no_economic_opportunity:edge_lt_dyn_thr"] += 1
        buys = stale = skips = 0
        for l in (DATA / fam / "runner.log").read_text(encoding="utf-8", errors="replace").splitlines():
            try: ts = dt.datetime.fromisoformat(l[:25]).timestamp()
            except Exception: continue
            if not (t0 <= ts <= t1): continue
            buys += " PAPER BUY " in l; stale += " STALE " in l; skips += " SKIP " in l
        out[a] = {"unique_markets": len(closes), "checks": checks, "candidates": cands, "submissions": buys + stale + skips, "fills": buys, "stale": stale, "skips": skips, "primary": dict(prim.most_common())}
    return out

# ------------------------------------------------------------------------------------------------------------------ P12 longshot
def p12_longshot(trades: list[dict]) -> list[dict]:
    rows = [t for t in trades if t["fam"].startswith("s7p15") and t.get("status") == "settled"]
    buckets = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 0.9), (0.9, 1.0)]
    out = []
    for lo, hi in buckets:
        b = [t for t in rows if lo <= t["price"] < hi]
        if not b: continue
        n = len(b); k = sum(1 for t in b if t["result"] == t["side"]); contracts = sum(t["qty"] for t in b)
        be = sum((t["price"] + t["fee"] / t["qty"]) * t["qty"] for t in b) / contracts        # contract-weighted breakeven win rate
        w = wilson(k, n); cp = clopper(k, n)
        out.append({"bucket": f"{lo:.1f}-{hi:.1f}", "n": n, "wins": k, "hit": round(k / n, 3), "breakeven_hit": round(be, 3), "wilson95": [round(x, 3) for x in w], "clopper95": [round(x, 3) for x in cp],
                    "net": round(sum(t["pnl"] for t in b), 2), "net_c_per_ct": round(100 * sum(t["pnl"] for t in b) / contracts, 1), "contracts": contracts, "justified": ("yes" if w[0] > be else "no" if w[1] < be else "undetermined")})
    return out

# ------------------------------------------------------------------------------------------------------------------ timestamp chain
def timestamp_chain(t0: float, t1: float) -> dict:
    """Feed publish (value_ts) -> local receipt (cfb t) -> snapshot/book fetch (books t) -> decision (trade t_dec) -> re-check (latency)."""
    out = {}
    for a, fam in FAM15.items():
        lag = []
        for l in open(DATA / "cfb" / f"{a}.jsonl", encoding="utf-8"):
            r = json.loads(l)
            if t0 <= r["t"] <= t1 and r.get("value_ts_ms"): lag.append(r["t"] - r["value_ts_ms"] / 1000.0)
        rows = load_books(fam, t0, t1); age = [r["cfb_age_s"] for r in rows if r.get("cfb_age_s") is not None and r["close_ts"] - r["t"] <= 60]
        lat = []
        for l in (DATA / fam / "runner.log").read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"\(latency ([\d.]+)s\)", l) or re.search(r"lat=([\d.]+)s", l)
            if m:
                try: ts = dt.datetime.fromisoformat(l[:25]).timestamp()
                except Exception: continue
                if t0 <= ts <= t1: lat.append(float(m.group(1)))
        def qs(x): return {"n": len(x), "p50": round(statistics.median(x), 3), "p90": round(sorted(x)[int(0.9 * len(x))], 3), "max": round(max(x), 3)} if x else None
        out[a] = {"feed_publish_to_local_receipt_s": qs(lag), "feed_receipt_to_book_snapshot_s (cfb_age_s)": qs(age), "decision_to_recheck_s (latency)": qs(lat)}
    return out

def candidate_survival(t0: float, t1: float) -> dict:
    """For every snapshot that passes all P16 checks (with and without the live arm window): does the NEXT snapshot (<= 4 s) still
    offer our side <= cap with size >= lot and edge >= 5c (would survive the live re-check), or did it die in execution?"""
    out = {}
    for a, fam in FAM15.items():
        rows = sorted(load_books(fam, t0, t1), key=lambda r: r["t"]); arms = arm_window_at(fam); series = f"KX{a}15M"
        by = collections.defaultdict(list)
        for r in rows: by[(r["close_ts"], r["ticker"])].append(r)
        c = collections.Counter()
        for key, S in by.items():
            for i, r in enumerate(S):
                secs = r["close_ts"] - r["t"]
                if not (9.0 <= secs <= 55.2): continue
                k = classify(r, series, arm_window_s=None, official_era=(r["t"] >= OFFICIAL_TS))
                if k["cls"] != "candidate": continue
                arm = arms.get(r["close_ts"]); in_arm = arm is None or (arm[0] <= secs <= arm[1])
                nxt = S[i + 1] if i + 1 < len(S) and S[i + 1]["t"] - r["t"] <= 4.0 else None
                if nxt is None: c[("no_next_snapshot", in_arm)] += 1; continue
                k2 = classify(nxt, series, arm_window_s=None, official_era=True)
                alive = k2["cls"] == "candidate" and k2["side"] == k["side"]
                c[("survives_recheck" if alive else "dies_in_execution:" + (k2["reason"] if k2["side"] == k["side"] else "side_flip"), in_arm)] += 1
        out[a] = {f"{k[0]} | {'in_arm_window' if k[1] else 'excluded_by_arm_window'}": v for k, v in sorted(c.items())}
    return out

# ------------------------------------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--t0", default="2026-10-27 21:51"); ap.add_argument("--t1", default="2026-10-28 07:05"); a = ap.parse_args()
    t0 = dt.datetime.strptime(a.t0, "%Y-%m-%d %H:%M").replace(tzinfo=PT).timestamp(); t1 = dt.datetime.strptime(a.t1, "%Y-%m-%d %H:%M").replace(tzinfo=PT).timestamp()
    trades = load_trades(); mk = json.loads((DATA / "p22" / "audit_results_2026-10-28.json").read_text())
    out = {"window_pt": [a.t0, a.t1]}
    # 1. reconciliation
    rec = {"n": len(trades), "result_mismatch": [], "pnl_mismatch": [], "fee_mismatch": [], "expval_missing_at_settle": 0, "expval_revised": [], "open": [t["ticker"] for t in trades if t.get("status") == "open"], "ties": []}
    for t in trades:
        m = mk.get(t["ticker"], {}); win = m.get("result") == t["side"]
        if m.get("result") != t.get("result"): rec["result_mismatch"].append(t["ticker"])
        if abs((1.0 if win else 0.0) * t["qty"] - t["cost"] - t["fee"] - t["pnl"]) > 0.005: rec["pnl_mismatch"].append(t["ticker"])
        if abs(kalshi_taker_fee(t["qty"], t["price"]) - t["fee"]) > 1e-6: rec["fee_mismatch"].append(t["ticker"])
        if t.get("expiration_value") is None: rec["expval_missing_at_settle"] += 1
        elif str(t["expiration_value"]) != str(m.get("expiration_value")): rec["expval_revised"].append((t["ticker"], t["expiration_value"], m.get("expiration_value")))
        try:
            if abs(float(m["expiration_value"]) - float(m["floor_strike"])) < 1e-9: rec["ties"].append((t["ticker"], t["fam"], t["side"], m["result"]))
        except Exception: pass
    out["reconciliation"] = rec
    # 2. P&L
    out["pnl_all"] = pnl_stats(trades)
    out["pnl_by_version"] = {}
    for v in sorted({version_of(t) for t in trades}):
        tv = [t for t in trades if version_of(t) == v]; s = pnl_stats(tv)
        s["unique_markets"] = len({t["ticker"] for t in tv}); s["hit"] = round(sum(1 for t in tv if t.get("result") == t["side"]) / len(tv), 3)
        s["first"] = pt(min(t["t"] for t in tv)); s["last"] = pt(max(t["t"] for t in tv)); out["pnl_by_version"][v] = s
    out["pnl_window"] = pnl_stats([t for t in trades if t0 <= t["t"] <= t1])
    # 3. taxonomy recount, legacy counters, P12 funnel, chain, survival
    out["legacy_nofill"] = legacy_nofill(t0, t1); out["taxonomy"] = taxonomy_recount(t0, t1); out["p12_funnel"] = p12_counts(t0, t1)
    out["p12_longshot"] = p12_longshot(trades); out["timestamp_chain"] = timestamp_chain(t0, t1); out["candidate_survival"] = candidate_survival(t0, t1)
    # whole official era for P13 as well
    out["taxonomy_official_era"] = taxonomy_recount(OFFICIAL_TS, time.time()); out["candidate_survival_official_era"] = candidate_survival(OFFICIAL_TS, time.time())
    (DATA / "p22" / "audit_2026-10-28.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))

if __name__ == "__main__":
    main()
