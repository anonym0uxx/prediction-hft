"""AUDIT ADDENDUM 2026-10-28 — numbers for docs/AUDIT_ADDENDUM_2026-10-28.md. Read-only over data/ (paper); no network.
Reproduces the audited 97-fill cohort with --snapshot "2026-10-28 08:05" (PT; fills with t <= snapshot) and prints the live totals.

Usage: uv run python scripts/addendum_2026_10_28.py [--snapshot "2026-10-28 08:05"] [--out data/p22/addendum_2026-10-28.json]
"""
from __future__ import annotations
import argparse, collections, datetime as dt, glob, json, math, pathlib, re, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src")); sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from predhft.reject_taxonomy import classify, side_present
from predhft.fees import kalshi_taker_fee
from predhft.ledger_join import PT, REBASE_TS, FREEZE_TS, SUSPEND_TS, FOMC_WINDOW, version_tag, pt, tod_block
import audit_2026_10_28 as A

DATA = pathlib.Path("data"); OFFICIAL_TS = A.OFFICIAL_TS
FAM13 = {a: f"s7p13-KX{a}15M" for a in ("BTC", "ETH", "SOL")}; FAM12 = {a: f"s7p15-KX{a}15M" for a in ("BTC", "ETH", "SOL")}


def mean(x):
    x = list(x); return sum(x) / len(x) if x else float("nan")


def q(x, p):
    x = sorted(x); return x[min(len(x) - 1, int(p * len(x)))] if x else None


def cluster_se(vals, clusters):
    n = len(vals)
    if n < 2: return None
    m = sum(vals) / n; by = collections.defaultdict(float)
    for v, c in zip(vals, clusters): by[c] += v - m
    G = len(by)
    return math.sqrt(sum(s * s for s in by.values()) * G / (G - 1)) / n if G > 1 else None


# ------------------------------------------------------------------------------------------------------------ cohort table
def cohort(T: list[dict]) -> dict:
    """contracts, quoted cost (= booked cost - penalty), fees, penalty, net, max exposure (settled-only: open cost released at close+120),
    drawdown on cumulative realised P&L ordered by settlement time (settled-only; no marking exists in paper)."""
    S = sorted([t for t in T if t.get("status") == "settled"], key=lambda t: t["close_ts"])
    pen = sum(t["qty"] * t.get("slippage", 0.01) for t in S); cost = sum(t["cost"] for t in S); proceeds = sum(t["qty"] * (1.0 if t["result"] == t["side"] else 0.0) for t in S)
    cum = peak = mdd = 0.0
    for t in S:
        cum += t["pnl"]; peak = max(peak, cum); mdd = min(mdd, cum - peak)
    evs = sorted([(t["t"], t["cost"]) for t in S] + [(t["close_ts"] + 120, -t["cost"]) for t in S]); cur = mx = 0.0
    for _, c in evs: cur += c; mx = max(mx, cur)
    return {"fills": len(S), "wins": sum(1 for t in S if t["result"] == t["side"]), "contracts": sum(t["qty"] for t in S), "quoted_cost": round(cost - pen, 2), "fees": round(sum(t["fee"] for t in S), 2),
            "penalty": round(pen, 2), "proceeds": round(proceeds, 2), "gross_vs_quoted": round(proceeds - (cost - pen), 2), "net": round(sum(t["pnl"] for t in S), 2),
            "max_exposure_settled_only": round(mx, 2), "drawdown_settled_only": round(mdd, 2), "mean_price": (round(mean(t["price"] for t in S), 3) if S else None),
            "latency_gt_1p5s": sum(1 for t in S if (t.get("decision_latency_s") or 0) > 1.5)}


def cohort_table(trades: list[dict]) -> dict:
    out = {}
    for v in sorted({version_tag(t["fam"], t["t"]) for t in trades}):
        tv = [t for t in trades if version_tag(t["fam"], t["t"]) == v]; out[v] = {"ALL": cohort(tv)}
        for a in sorted({t["ticker"][2:5] for t in tv}): out[v][a] = cohort([t for t in tv if t["ticker"][2:5] == a])
    return out


def trace_95_vs_97(trades: list[dict]) -> dict:
    """The compact table's P12 pre-rebase row used the P12 funnel with a window starting 2026-10-26 23:00 PT; two fills at 22:44 PT fall outside."""
    pre = sorted([t for t in trades if t["fam"].startswith("s7p15") and t["t"] < REBASE_TS], key=lambda t: t["t"])
    cut = dt.datetime(2026, 10, 26, 23, 0, tzinfo=PT).timestamp()
    excluded = [{"fam": t["fam"], "ticker": t["ticker"], "t_pt": pt(t["t"]), "side": t["side"], "qty": t["qty"], "price": round(t["price"], 3), "fee": t["fee"], "pnl": round(t["pnl"], 2)} for t in pre if t["t"] < cut]
    return {"p12_pre_rebase_fills_by_version_tag": len(pre), "p12_pre_rebase_fills_window_from_23:00PT": len(pre) - len(excluded), "excluded_records": excluded,
            "p12_total_by_version_tag": sum(1 for t in trades if t["fam"].startswith("s7p15")), "p12_rows_in_compact_table": sum(1 for t in trades if t["fam"].startswith("s7p15")) - len(excluded)}


# ------------------------------------------------------------------------------------------------------------ halt replay
def halt_replay(trades: list[dict]) -> dict:
    ev = [json.loads(l) for l in open(DATA / "risk" / "events.jsonl", encoding="utf-8") if l.strip()]; ev.sort(key=lambda e: e["t"])
    pnl_of = {(t["fam"], t["ticker"]): t for t in trades}
    real = collections.defaultdict(float); crossed = None; during = []; non_trade_settles = []
    for e in ev:
        d = int(e["t"] // 86400)
        if e["kind"] == "settle":
            real[d] += e["pnl"]
            if (e["family"], e["ticker"]) not in pnl_of: non_trade_settles.append({"family": e["family"], "ticker": e["ticker"], "pnl": round(e["pnl"], 2), "t_pt": pt(e["t"])})
            if crossed is None and real[d] <= -25: crossed = {"t_pt": pt(e["t"]), "trigger": f"{e['family']} {e['ticker']} {e['pnl']:+.2f}", "realized_utc_day": round(real[d], 2),
                                                             "open_positions_at_crossing": [t["ticker"] for t in trades if t["t"] <= e["t"] < t["close_ts"] + 120]}
        if e["kind"] == "open" and real[d] <= -25:
            t = pnl_of.get((e["family"], e["ticker"])); during.append({"t_pt": pt(e["t"]), "family": e["family"], "ticker": e["ticker"], "cost": round(e["cost"], 2), "realized_at_open": round(real[d], 2), "pnl": (round(t["pnl"], 2) if t else None)})
    hs = [e for e in ev if e["kind"] == "halt_shadow"]
    return {"threshold": -25.0, "day_boundary": "00:00 UTC = 17:00 PT (risk.Ledger._day0): the daily counter resets at 17:00 PT", "crossed": crossed, "halt_shadow_events": len(hs),
            "halt_shadow_first_pt": (pt(hs[0]["t"]) if hs else None), "halt_shadow_last_pt": (pt(hs[-1]["t"]) if hs else None), "settle_events_not_in_any_trades_jsonl": non_trade_settles,
            "fills_taken_while_shadow_halted": during, "n_fills_while_halted": len(during), "net_of_fills_while_halted": round(sum(x["pnl"] for x in during if x["pnl"] is not None), 2),
            "wins_while_halted": sum(1 for x in during if (x["pnl"] or 0) > 0), "utc_day_10-27_realized_total": round(real[int(dt.datetime(2026, 10, 27, 12, tzinfo=dt.timezone.utc).timestamp() // 86400)], 2)}


# ------------------------------------------------------------------------------------------------------------ shared liquidity
def shared_liquidity(trades: list[dict]) -> dict:
    by = collections.defaultdict(list)
    for t in trades: by[t["ticker"]].append(t)
    pairs = []
    for tk, L in by.items():
        if len(L) < 2: continue
        L = sorted(L, key=lambda t: t["t"])
        for i in range(len(L)):
            for j in range(i + 1, len(L)):
                a, b = L[i], L[j]
                pairs.append({"ticker": tk, "fams": [a["fam"], b["fam"]], "dt_s": round(b["t"] - a["t"], 1), "same_side": a["side"] == b["side"], "sides": [a["side"], b["side"]], "qty": [a["qty"], b["qty"]],
                              "prices": [round(a["price"], 3), round(b["price"], 3)], "avail_at_touch": [a.get("avail_at_touch"), b.get("avail_at_touch")],
                              "combined_qty_exceeds_min_displayed": (a["side"] == b["side"] and (a["qty"] + b["qty"]) > min(a.get("avail_at_touch") or 1e9, b.get("avail_at_touch") or 1e9)), "era": ("pre-rebase" if b["t"] < REBASE_TS else "post-rebase")})
    # candidate coincidences on the tape since the rebase (symmetric same-ticker block in risk.size)
    def p12_cand(r, lot=10):
        secs = r["close_ts"] - r["t"]
        if not (6 <= secs <= 60): return None
        bid_ok, ask_ok = side_present(r)
        if not (bid_ok and ask_ok): return None
        yb, ya, ybs, yas, p, thr = r["yes_bid"], r["yes_ask"], r["bid_size"], r["ask_size"], r["p_model"], r["thr_used"]
        if r["t"] >= REBASE_TS and (r.get("partial_twap") is None or (r.get("partial_n") or 0) < 3): return None
        mid = (yb + ya) / 2
        if ya - yb > 0.06 or not (0.03 <= mid <= 0.97): return None
        if p - ya >= thr and yas >= lot: return "yes"
        if yb - p >= thr and ybs >= lot: return "no"
        return None
    coinc = {}
    for a in ("BTC", "ETH", "SOL"):
        r13 = [r for r in A.load_books(FAM13[a], REBASE_TS) if 9 <= r["close_ts"] - r["t"] <= 55.2]
        c13 = [(r, classify(r, f"KX{a}15M", official_era=(r["t"] >= OFFICIAL_TS))) for r in r13]; c13 = [(r, c) for r, c in c13 if c["cls"] == "candidate"]
        c15 = collections.defaultdict(list)
        for r in A.load_books(FAM12[a], REBASE_TS):
            s = p12_cand(r)
            if s: c15[r["ticker"]].append((r["t"], s))
        n = same = 0
        for r, c in c13:
            hit = [s for t15, s in c15.get(r["ticker"], []) if abs(t15 - r["t"]) <= 5]
            if hit: n += 1; same += int(c["side"] in hit)
        coinc[a] = {"p13_candidate_snapshots": len(c13), "p12_candidate_snapshots": sum(len(v) for v in c15.values()), "coincident_within_5s": n, "same_side": same}
    return {"same_ticker_fill_pairs": pairs, "n_pairs": len(pairs), "n_same_side_15M_pairs": sum(1 for p in pairs if p["same_side"] and "15M" in p["ticker"]),
            "n_pairs_combined_qty_exceeds_min_displayed": sum(1 for p in pairs if p["combined_qty_exceeds_min_displayed"]), "post_rebase_candidate_coincidences": coinc}


# ------------------------------------------------------------------------------------------------------------ fees
def fee_inputs(trades: list[dict]) -> dict:
    S = [t for t in trades if t.get("status") == "settled"]
    exact = sum(0.07 * t["qty"] * t["price"] * (1 - t["price"]) for t in S); booked = sum(t["fee"] for t in S)
    sub = sum(1 for t in S if abs(round((t["price"] - 0.01) * 100) - (t["price"] - 0.01) * 100) > 1e-6)
    return {"schedule": "Kalshi series fee_type=quadratic, fee_multiplier=1 for KXBTC15M/KXETH15M/KXSOL15M/KXBTCD (GET /series/<ticker>, 2026-10-28); taker 0.07 x C x P x (1-P)",
            "rounding_booked": "ceil to the cent per order (fees.kalshi_taker_fee)", "rounding_exchange": "trade fee ceil $0.000001 per fill; balance aligned to $0.01 (non-direct) or $0.0001 (direct member) via rounding fee + accumulator (docs.kalshi.com/getting_started/fee_rounding)",
            "fees_booked": round(booked, 2), "fees_exact_6dp": round(exact, 4), "overstatement_vs_direct_member": round(booked - exact, 2), "fills_with_subpenny_quoted_ask": sub, "n": len(S),
            "fee_recomputation_mismatches": [t["ticker"] for t in S if abs(kalshi_taker_fee(t["qty"], t["price"]) - t["fee"]) > 1e-6], "fractional_quantities_booked": sum(1 for t in S if t["qty"] != int(t["qty"]))}


# ------------------------------------------------------------------------------------------------------------ P22 audit
def p22_audit(path: pathlib.Path, lot: int = 10) -> dict:
    recs = [json.loads(l) for l in path.open(encoding="utf-8")]
    out = {"n_records": len(recs), "n_markets": len({r["ticker"] for r in recs})}
    # signed bias vs MAE/RMSE
    out["error_stats_bp"] = {}
    for h in (60, 45, 30, 20, 10):
        R = [r for r in recs if r["h"] == h]; eo = [r["err_off_bp"] for r in R]; er = [r["err_rep_bp"] for r in R]
        out["error_stats_bp"][h] = {"n": len(R), "bias_off": round(mean(eo), 3), "bias_rep": round(mean(er), 3), "mae_off": round(mean(map(abs, eo)), 3), "mae_rep": round(mean(map(abs, er)), 3),
                                    "rmse_off": round(math.sqrt(mean(x * x for x in eo)), 3), "rmse_rep": round(math.sqrt(mean(x * x for x in er)), 3), "mae_diff_off_minus_rep": round(mean(map(abs, eo)) - mean(map(abs, er)), 3),
                                    "bias_se_close_off": (round(cluster_se(eo, [r["close_ts"] for r in R]), 3) if len(R) > 1 else None)}
    # Brier vs mid on the two-sided subset: exact equality? clipping?
    M = [r for r in recs if r.get("two_sided") and r.get("mid") is not None]
    out["brier_vs_mid"] = {"two_sided_records": len(M), "exact_p_off_eq_mid": sum(1 for r in M if abs(r["p_off"] - r["mid"]) < 1e-9), "p_off_saturated_0_or_1": sum(1 for r in recs if r["p_off"] in (0.0, 1.0)),
                           "p_off_beyond_1e-4_of_bounds": sum(1 for r in recs if r["p_off"] > 0.9999 or r["p_off"] < 0.0001), "by_h": {}}
    for h in (60, 45, 30, 20, 10):
        Mh = [r for r in M if r["h"] == h]; d = [(r["p_off"] - r["y"]) ** 2 - (r["mid"] - r["y"]) ** 2 for r in Mh]
        out["brier_vs_mid"]["by_h"][h] = {"n": len(Mh), "brier_off": round(mean((r["p_off"] - r["y"]) ** 2 for r in Mh), 4), "brier_mid": round(mean((r["mid"] - r["y"]) ** 2 for r in Mh), 4), "diff_off_minus_mid": round(mean(d), 4),
                                          "se_close": (round(cluster_se(d, [r["close_ts"] for r in Mh]) or 0, 4) if len(Mh) > 1 else None), "se_hour": (round(cluster_se(d, [r["hour_block"] for r in Mh]) or 0, 4) if len(Mh) > 1 else None)}
    # edge definition: is it the mechanical -(a-b)/2 ?
    O = [r for r in recs if r.get("offer_off") is not None]
    ps = lambda r: r["p_off"] if r["side_off"] == "yes" else 1 - r["p_off"]
    mech = [100 * ((r["mid"] if r["side_off"] == "yes" else 1 - r["mid"]) - min(r["offer_off"] + 0.01, 0.99)) for r in O if r.get("mid") is not None]
    out["edge_definition"] = {"formula": "edge_c = 100 x (p_side - min(offer_for_lot + 0.01, 0.99) - fee(lot, price)/lot), side = sign(d_bp) of the forecast, offer = executable price for `lot` from top-3 depth (None if depth < lot)",
                              "mean_edge_all_offered": round(mean(r["edge_c_off"] for r in O), 2), "mean_if_p_side_replaced_by_market_mid_side": round(mean(mech), 2), "n_offered": len(O),
                              "offers_gt_0.98_where_cap_puts_paper_price_below_offer": sum(1 for r in O if r["offer_off"] > 0.98), "p_side_hist": dict(collections.Counter(round(min(max(ps(r), 0), 1), 1) for r in O))}
    # predefined rules (declared before looking): R1 side = argmax p_side with p_side >= .6 and intended-side depth >= lot; R2 same with p_side >= .9; R3 = R1 and offer <= .95 (P13 cap)
    def dist(R):
        e = [r["edge_c_off"] for r in R]; rl = [r["real_c_off"] for r in R]
        if not e: return {"n": 0}
        return {"n": len(e), "mean_edge_c": round(mean(e), 1), "p10": round(q(e, .1), 1), "p50": round(q(e, .5), 1), "p90": round(q(e, .9), 1), "share_gt_0": round(sum(1 for v in e if v > 0) / len(e), 2), "n_ge_5c": sum(1 for v in e if v >= 5),
                "realized_mean_c": round(mean(rl), 1), "realized_se_close": (round(cluster_se(rl, [r["close_ts"] for r in R]), 1) if cluster_se(rl, [r["close_ts"] for r in R]) is not None else None)}
    out["edge_by_rule"] = {}
    for h in (60, 45, 30, 20, 10):
        Rh = [r for r in O if r["h"] == h]
        with_book = [r for r in recs if r["h"] == h and r.get("book_src")]
        out["edge_by_rule"][h] = {"with_book": len(with_book), "two_sided": sum(1 for r in with_book if r.get("two_sided")), "intended_side_offered_depth_ge_lot": len(Rh), "no_offer_on_intended_side": sum(1 for r in with_book if r.get("offer_off") is None),
                                  "ALL_offered": dist(Rh), "R1_p_side_ge_.6": dist([r for r in Rh if ps(r) >= 0.6]), "R2_p_side_ge_.9": dist([r for r in Rh if ps(r) >= 0.9]), "R3_R1_and_offer_le_.95": dist([r for r in Rh if ps(r) >= 0.6 and r["offer_off"] <= 0.95]),
                                  "sensitivity_R2_excluding_offers_gt_.98": dist([r for r in Rh if ps(r) >= 0.9 and r["offer_off"] <= 0.98])}
    return out


# ------------------------------------------------------------------------------------------------------------ productivity
def productivity(trades: list[dict], t0: float, t1: float) -> dict:
    """Per family group in [t0, t1): windows (15M closes with >= 1 in-window snapshot), opportunity rate (closes with >= 1 candidate snapshot),
    fill rate (fills / windows), net $ per window, capital occupancy (mean open cost over the period / 250 bankroll), by asset and by regime."""
    out = {}
    for label, FAM, cand_fn in (("P13/P16 recorders (s7p13-*15M)", FAM13, None), ("P12 (s7p15-*15M)", FAM12, "p12")):
        res = {}
        for a, fam in FAM.items():
            rows = A.load_books(fam, t0, t1); closes = set(); cand_closes = set()
            for r in rows:
                secs = r["close_ts"] - r["t"]
                if cand_fn is None:
                    if not (9 <= secs <= 55.2): continue
                    closes.add(r["close_ts"])
                    if classify(r, f"KX{a}15M", official_era=(r["t"] >= OFFICIAL_TS))["cls"] == "candidate": cand_closes.add(r["close_ts"])
                else:
                    if not (6 <= secs <= 60): continue
                    closes.add(r["close_ts"])
                    bid_ok, ask_ok = side_present(r)
                    if bid_ok and ask_ok and r["yes_ask"] - r["yes_bid"] <= 0.06 and 0.03 <= (r["yes_bid"] + r["yes_ask"]) / 2 <= 0.97 and (r.get("partial_n") or 0) >= 3:
                        if (r["p_model"] - r["yes_ask"] >= r["thr_used"] and r["ask_size"] >= 10) or (r["yes_bid"] - r["p_model"] >= r["thr_used"] and r["bid_size"] >= 10): cand_closes.add(r["close_ts"])
            F = [t for t in trades if t["fam"] == fam and t0 <= t["t"] < t1 and t.get("status") == "settled"]
            occ = sum(t["cost"] * max(0.0, min(t["close_ts"] + 120, t1) - t["t"]) for t in F) / max(t1 - t0, 1) / 250.0
            by_tod = collections.defaultdict(lambda: [0, 0.0]); fomc = [0, 0.0]
            for t in F:
                by_tod[tod_block(t["t"])][0] += 1; by_tod[tod_block(t["t"])][1] += t["pnl"]
                if FOMC_WINDOW[0] <= t["t"] <= FOMC_WINDOW[1]: fomc[0] += 1; fomc[1] += t["pnl"]
            res[a] = {"windows": len(closes), "closes_with_candidate": len(cand_closes), "opportunity_rate": (round(len(cand_closes) / len(closes), 3) if closes else None), "fills": len(F), "fill_rate": (round(len(F) / len(closes), 3) if closes else None),
                      "net": round(sum(t["pnl"] for t in F), 2), "net_per_window": (round(sum(t["pnl"] for t in F) / len(closes), 3) if closes else None), "capital_occupancy_of_250": round(occ, 4),
                      "fills_by_tod_block": {k: {"n": v[0], "net": round(v[1], 2)} for k, v in sorted(by_tod.items())}, "fills_in_fomc_window": {"n": fomc[0], "net": round(fomc[1], 2)}}
        out[label] = res
    return out


def suspension_check() -> dict:
    out = {}
    for a, fam in FAM13.items():
        lines = (DATA / fam / "runner.log").read_text(encoding="utf-8", errors="replace").splitlines()
        starts = [l for l in lines if " start " in l]; last = starts[-1] if starts else ""
        after = [l for l in lines if l[:19] >= "2026-10-28T16:05:51"]
        buys_after = [l for l in after if " PAPER BUY " in l or " STALE " in l or " SKIP " in l or " GATE REJECT " in l]
        snaps_after = sum(1 for r in A.load_books(fam, SUSPEND_TS) if r["close_ts"] - r["t"] <= 60)
        tr = [json.loads(l) for l in (DATA / fam / "trades.jsonl").open(encoding="utf-8") if l.strip()]
        out[a] = {"last_start_line_utc": last[:19], "scored_only": "scored_only=True" in last, "ts_levers": "ts_levers=True" in last, "bet_gate": "bet_gate=True" in last, "proj_gate": "proj_gate=True" in last,
                  "submission_lines_after_switch": len(buys_after), "final_minute_snapshots_after_switch": snaps_after, "open_positions": [t["ticker"] for t in tr if t.get("status") == "open"], "fills_after_switch": sum(1 for t in tr if t["t"] >= SUSPEND_TS)}
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--snapshot", default="2026-10-28 08:05"); ap.add_argument("--out", default="data/p22/addendum_2026-10-28.json"); a = ap.parse_args()
    snap = dt.datetime.strptime(a.snapshot, "%Y-%m-%d %H:%M").replace(tzinfo=PT).timestamp()
    trades = A.load_trades(); T97 = [t for t in trades if t["t"] <= snap and t.get("status") == "settled"]; now = time.time()
    out = {"snapshot_pt": a.snapshot, "generated_pt": pt(now), "suspension": suspension_check(), "cohort_audited": cohort_table(T97), "cohort_audited_all": cohort(T97), "trace_95_vs_97": trace_95_vs_97(T97),
           "cohort_live_all": cohort([t for t in trades if t.get("status") == "settled"]), "cohort_live_p12_official": cohort([t for t in trades if t["fam"].startswith("s7p15") and t["t"] >= FREEZE_TS]),
           "fee_inputs": fee_inputs(T97), "halt_replay": halt_replay(trades), "shared_liquidity": shared_liquidity(trades), "p22": p22_audit(DATA / "p22" / "p22_paired_2026-10-28.jsonl"),
           "productivity_official_era": productivity(trades, OFFICIAL_TS, now), "productivity_post_gate_restart": productivity(trades, A.GATE_TS, now)}
    pathlib.Path(a.out).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8"); print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
