"""AUDIT ADDENDUM 2 (2026-10-28) — numbers for docs/AUDIT_ADDENDUM2_2026-10-28.md. Read-only over data/ (paper); no network.

Sections: P12 calibration diagnostic (fixed cohort + current), paired scoring model vs market, cluster bootstrap, conditional
calibration, ETH tie mass, latency / fee sensitivities (labelled, not applied), statistical spec (ratio estimator, cluster variance,
DEFF, Lan-DeMets OBF boundaries, power), hourly rule mapping, S1 maker record classification, cron-vs-ledger reconciliation.

Usage: uv run python scripts/addendum2_2026_10_28.py [--asof "2026-10-28 09:05:51"] [--out data/p22/addendum2_2026-10-28.json]
"""
from __future__ import annotations
import argparse, collections, datetime as dt, json, math, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft.fees import kalshi_taker_fee
from predhft.ledger_join import PT, REBASE_TS, FREEZE_TS, SUSPEND_TS, version_tag, pt
from predhft import stats_spec as SS

DATA = pathlib.Path("data")
BTCD_SUSPEND_TS = dt.datetime(2026, 10, 28, 9, 55, 23, tzinfo=PT).timestamp()
HOURLY_FAMS = ["s7live", "s7live-KXETHD", "s7live-KXSOLD", "s7final-KXBTCD", "s7p10-KXBTCD", "s7p10-KXETHD", "s7p10-KXSOLD", "s7p13-KXBTCD"]
CRON_ROOTS = ["s7live", "s7live-KXETHD", "s7live-KXSOLD", "s7final-KXBTCD", "s7p10-KXBTCD"]       # what the 09:05 cron summed as "n=20"


def load_jsonl(p: pathlib.Path) -> list[dict]:
    out = []
    if not p.exists(): return out
    for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if l.strip():
            try: out.append(json.loads(l))
            except Exception: pass
    return out


def load_trades() -> list[dict]:
    rows = []
    for f in sorted(DATA.glob("*/trades.jsonl")):
        for t in load_jsonl(f): t["fam"] = f.parent.name; rows.append(t)
    return rows


def mean(x):
    x = list(x); return sum(x) / len(x) if x else None


def logscore(p: float, w: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6); return -(w * math.log(p) + (1 - w) * math.log(1 - p))


# ------------------------------------------------------------------------------------------------------------ P12 cohort records
def p12_records(trades: list[dict], t_lo: float, t_hi: float) -> list[dict]:
    """One row per settled P12 fill in [t_lo, t_hi] with the model and market forecasts of the purchased side."""
    out = []
    for t in sorted(trades, key=lambda t: t["t"]):
        if not t["fam"].startswith("s7p15") or t.get("status") != "settled" or not (t_lo <= t["t"] <= t_hi): continue
        side = t["side"]; p_side = t["p_model"] if side == "yes" else 1 - t["p_model"]; m_side = t["p_market"] if side == "yes" else 1 - t["p_market"]
        win = 1.0 if t["result"] == side else 0.0; price = t["price"]; q = t["qty"]; fee = t["fee"]
        out.append({"ticker": t["ticker"], "asset": t["ticker"][2:5], "decision_pt": pt(t["t"]), "t": t["t"], "close_ts": t["close_ts"], "hour_block": int(t["close_ts"] // 3600), "secs_left": round(t["close_ts"] - t["t"], 1),
                    "side": side, "p_side_model": round(p_side, 4), "p_side_market_mid": round(m_side, 4), "price": round(price, 4), "quoted_touch": round(price - t.get("slippage", 0.01), 4), "qty": q, "fee": fee,
                    "outcome": "win" if win else "loss", "win": win, "model_implied_pnl": round(q * (p_side - price) - fee, 2), "market_implied_pnl": round(q * (m_side - price) - fee, 2), "realized_pnl": round(t["pnl"], 2),
                    "decision_latency_s": t.get("decision_latency_s"), "spot_src": t.get("spot_src"), "partial_n": t.get("partial_n"), "sigma1": t.get("sigma1"), "K": t.get("K"), "spot": t.get("spot"), "mins_left": t.get("mins_left"),
                    "fee_exact": round(0.07 * q * price * (1 - price), 4), "fam": t["fam"]})
    return out


def paired_scoring(R: list[dict]) -> dict:
    """Model vs market-mid on identical records: mean probability, hit, Brier, log score, realised $ under each forecast's implied decision.
    The model's implied decision IS the trade (p_side > price + fee by construction). The market's implied decision = buy only if the
    decision-time side-mid exceeds the BOOKED price + fee. Because the booked price comes from the re-check book (0.3-2.9 s later) this is
    true only when the book moved in the trade's favour between decision and fill (stale-quote effect); at an unmoved book it is never
    true (price = ask + 1 tick > mid)."""
    if not R: return {"n": 0}
    bm = [(r["p_side_model"] - r["win"]) ** 2 for r in R]; bk = [(r["p_side_market_mid"] - r["win"]) ** 2 for r in R]
    lm = [logscore(r["p_side_model"], r["win"]) for r in R]; lk = [logscore(r["p_side_market_mid"], r["win"]) for r in R]
    fee_pc = lambda r: r["fee"] / r["qty"]
    mkt_trade = [r for r in R if r["p_side_market_mid"] > r["price"] + fee_pc(r)]
    return {"n": len(R), "mean_p_side_model": round(mean(r["p_side_model"] for r in R), 4), "mean_p_side_market_mid": round(mean(r["p_side_market_mid"] for r in R), 4), "hit_rate": round(mean(r["win"] for r in R), 4),
            "brier_model": round(mean(bm), 4), "brier_market_mid": round(mean(bk), 4), "brier_diff_model_minus_market": round(mean(bm) - mean(bk), 4),
            "logscore_model": round(mean(lm), 4), "logscore_market_mid": round(mean(lk), 4), "logscore_diff_model_minus_market": round(mean(lm) - mean(lk), 4),
            "realized_usd_under_model_decision": round(sum(r["realized_pnl"] for r in R), 2), "realized_usd_under_market_decision": round(sum(r["realized_pnl"] for r in mkt_trade), 2), "n_trades_market_would_take": len(mkt_trade),
            "model_implied_usd": round(sum(r["model_implied_pnl"] for r in R), 2), "market_implied_usd_of_same_trades": round(sum(r["market_implied_pnl"] for r in R), 2)}


def bootstraps(R: list[dict], B: int = 4000) -> dict:
    stats = {"gap_mean_p_side_minus_hit": lambda S: mean(r["p_side_model"] for r in S) - mean(r["win"] for r in S),
             "brier_diff_model_minus_market": lambda S: mean((r["p_side_model"] - r["win"]) ** 2 for r in S) - mean((r["p_side_market_mid"] - r["win"]) ** 2 for r in S),
             "logscore_diff_model_minus_market": lambda S: mean(logscore(r["p_side_model"], r["win"]) for r in S) - mean(logscore(r["p_side_market_mid"], r["win"]) for r in S),
             "realized_usd_total": lambda S: sum(r["realized_pnl"] for r in S), "net_per_contract_c": lambda S: 100 * sum(r["realized_pnl"] for r in S) / sum(r["qty"] for r in S)}
    out = {}
    for name, fn in stats.items():
        out[name] = {"by_close": SS.cluster_bootstrap(R, fn, lambda r: r["close_ts"], B), "by_hour_block": SS.cluster_bootstrap(R, fn, lambda r: r["hour_block"], B)}
    return out


def conditional_calibration(R: list[dict]) -> dict:
    def cell(S):
        return {"n": len(S), "mean_p_side_model": (round(mean(r["p_side_model"] for r in S), 3) if S else None), "mean_p_side_market_mid": (round(mean(r["p_side_market_mid"] for r in S), 3) if S else None),
                "hit_rate": (round(mean(r["win"] for r in S), 3) if S else None), "net_usd": round(sum(r["realized_pnl"] for r in S), 2), "n_closes": len({r["close_ts"] for r in S})}
    price_b = {"<0.20": lambda p: p < 0.2, "0.20-0.50": lambda p: 0.2 <= p < 0.5, "0.50-0.80": lambda p: 0.5 <= p < 0.8, ">=0.80": lambda p: p >= 0.8}
    time_b = {"T-60..T-55 s (first eligible polls)": lambda s: s > 55, "T-55..T-45 s": lambda s: 45 < s <= 55, "T-45..T-30 s": lambda s: 30 < s <= 45, "T-30..T-6 s": lambda s: s <= 30}
    return {"by_entry_price": {k: cell([r for r in R if f(r["price"])]) for k, f in price_b.items()}, "by_time_remaining": {k: cell([r for r in R if f(r["secs_left"])]) for k, f in time_b.items()},
            "by_asset": {a: cell([r for r in R if r["asset"] == a]) for a in ("BTC", "ETH", "SOL")}, "by_side": {s: cell([r for r in R if r["side"] == s]) for s in ("yes", "no")}}


def eth_tie_mass(R: list[dict], markets: dict) -> dict:
    """ETH settles on a 2-dp index; the rules card books avg == K as YES ('>= K'). p_model is continuous (no tie mass). For every ETH fill:
    proj at decision, remaining sd (p_cal's 1.5 x sigma1 x r x sqrt(r/3)), tie mass P(|avg - K| < 0.005) and its sign effect on p_side."""
    snaps = [r for r in load_jsonl(DATA / "s7p15-KXETH15M" / "books.jsonl") if r.get("mins_left") is not None]
    rows = []
    for r in R:
        if r["asset"] != "ETH": continue
        near = [s for s in snaps if s["ticker"] == r["ticker"] and s["t"] <= r["t"] + 0.001 and r["t"] - s["t"] <= 6]
        s = max(near, key=lambda s: s["t"]) if near else None
        partial, n = (s.get("partial_twap"), s.get("partial_n") or 0) if s else (None, 0)
        rr = max(min(r["mins_left"] or 1.0, 1.0), 0.02); sig = r["sigma1"] or 0.0006
        proj = ((1 - rr) * partial + rr * r["spot"]) if (partial and n >= 3) else r["spot"]
        sd_bp = 1.5 * sig * 1e4 * rr * math.sqrt(rr / 3.0) if (partial and n >= 3) else sig * 1e4 * math.sqrt(max((r["mins_left"] or 1.0) - 2 / 3, 0.05))
        sdK = sd_bp * 1e-4 * r["K"]
        tie = SS.Phi((r["K"] + 0.005 - proj) / sdK) - SS.Phi((r["K"] - 0.005 - proj) / sdK) if sdK > 0 else None
        rows.append({"ticker": r["ticker"], "decision_pt": r["decision_pt"], "side": r["side"], "K": r["K"], "proj_at_decision": round(proj, 3), "remaining_sd_usd": round(sdK, 3), "tie_mass": (round(tie, 4) if tie is not None else None),
                     "p_side_model": r["p_side_model"], "p_side_effect_of_booking_tie_as_yes": (round(tie if r["side"] == "yes" else -tie, 4) if tie is not None else None), "outcome": r["outcome"], "realized_pnl": r["realized_pnl"],
                     "settled_on_tie": (abs(float(markets[r["ticker"]]["expiration_value"]) - float(markets[r["ticker"]]["floor_strike"])) < 0.005) if r["ticker"] in markets and markets[r["ticker"]].get("expiration_value") else None})
    eth_mk = [m for k, m in markets.items() if k.startswith("KXETH15M") and m.get("expiration_value") and m.get("floor_strike")]
    ties = sum(1 for m in eth_mk if abs(float(m["expiration_value"]) - float(m["floor_strike"])) < 0.005)
    return {"rows": rows, "mean_tie_mass": (round(mean(x["tie_mass"] for x in rows if x["tie_mass"] is not None), 4) if rows else None), "eth_markets_in_cache": len(eth_mk), "eth_markets_settled_on_tie": ties,
            "settlement_booking_rule": "result = yes iff 60-s average (2 dp for ETH) >= floor_strike; a tie counts as YES (verified on KXETH15M-26OCT271945-45, addendum §5)",
            "forecast_treatment": "p_model / p_cal are continuous normal / Student-t probabilities of avg > K: tie mass is not represented; booking it as YES means p_side(YES) is understated and p_side(NO) overstated by the tie mass"}


def sensitivities(R: list[dict]) -> dict:
    lat = [r for r in R if (r["decision_latency_s"] or 0) > 1.5]
    fee_gap = sum(r["fee"] - r["fee_exact"] for r in R)
    return {"latency_gt_1p5s": {"label": "SENSITIVITY ONLY — NOT APPLIED (P12 has no latency guard; frozen)", "n": len(lat), "fills": [{k: r[k] for k in ("ticker", "decision_pt", "side", "qty", "price", "decision_latency_s", "realized_pnl", "fam", "spot_src")} for r in lat],
                                "net_usd_of_these_fills": round(sum(r["realized_pnl"] for r in lat), 2), "cohort_net_without_them": round(sum(r["realized_pnl"] for r in R) - sum(r["realized_pnl"] for r in lat), 2),
                                "cohort_net_as_booked": round(sum(r["realized_pnl"] for r in R), 2)},
            "fee_rounding": {"label": "SENSITIVITY ONLY — NOT APPLIED (booked fees stay ceil-to-cent per order)", "fees_booked": round(sum(r["fee"] for r in R), 2), "fees_exact_6dp": round(sum(r["fee_exact"] for r in R), 4),
                             "overstatement_usd": round(fee_gap, 2), "cohort_net_with_exact_fees": round(sum(r["realized_pnl"] for r in R) + fee_gap, 2)}}


# ------------------------------------------------------------------------------------------------------------ statistical spec
def stat_spec(R: list[dict]) -> dict:
    pnl = [r["realized_pnl"] for r in R]; qty = [r["qty"] for r in R]
    by_close = SS.ratio_estimate(pnl, qty, [r["close_ts"] for r in R]); by_hour = SS.ratio_estimate(pnl, qty, [r["hour_block"] for r in R])
    per_ct = [r["realized_pnl"] / r["qty"] for r in R]; sd_pc = math.sqrt(sum((x - mean(per_ct)) ** 2 for x in per_ct) / (len(per_ct) - 1)) if len(per_ct) > 1 else None
    deff = max(by_close.get("deff") or 1.0, by_hour.get("deff") or 1.0, 1.0)
    delta, sd = 0.02, (sd_pc or 0.37)
    n_fixed = SS.fixed_sample_n(delta, sd)                                   # independent fills, one-sided 2.5 %, 80 % power
    # intended schedule: looks at 100 and 300 fills -> nominal fractions 1/3, 1 (treating 300 as final)
    b_nominal = SS.lan_demets_bounds([1 / 3, 1.0])
    I_300 = (300 / deff) / (sd * sd); drift_300 = delta * math.sqrt(I_300)
    power_300 = SS.gsd_power(b_nominal, drift_300, sims=100000)
    # information actually represented by 100 / 300 fills against the planned maximum (80 % power for +2 c)
    infl = 1.0
    # OBF inflation factor for the 2-look (1/3, 1) design: solve power(drift) = 0.8 by bisection on I_max
    lo, hi = 0.5 * n_fixed, 2.0 * n_fixed
    for _ in range(25):
        mid = (lo + hi) / 2; dr = delta * math.sqrt(mid) / sd
        if SS.gsd_power(b_nominal, dr, sims=40000) < 0.8: lo = mid
        else: hi = mid
    n_eff_max = (lo + hi) / 2; infl = n_eff_max / n_fixed
    fills_max = n_eff_max * deff
    frac_100 = (100 / deff) / n_eff_max; frac_300 = (300 / deff) / n_eff_max
    b_actual = SS.lan_demets_bounds(sorted({round(frac_100, 4), round(frac_300, 4), 1.0}))
    return {"estimand": "theta = sum(net P&L) / sum(filled contracts) over post-freeze P12 fills ($/contract; ratio estimator)",
            "H0": "theta <= 0 (one-sided alpha 0.025). Rejection establishes theta > 0 only; it does NOT establish theta >= 2 c. The 2 c minimum economically worthwhile edge is the power-planning alternative, nothing else.",
            "current_cohort": {"n_fills": len(R), "contracts": sum(qty), "theta_c_per_contract": round(100 * by_close["theta"], 3), "se_iid_fills_c": round(100 * by_close["se_iid"], 3),
                               "se_cluster_close_c": (round(100 * by_close["se_cluster"], 3) if by_close["se_cluster"] else None), "se_cluster_hour_c": (round(100 * by_hour["se_cluster"], 3) if by_hour["se_cluster"] else None),
                               "n_closes": by_close["n_clusters"], "n_hour_blocks": by_hour["n_clusters"], "deff_close": (round(by_close["deff"], 3) if by_close["deff"] else None), "deff_hour": (round(by_hour["deff"], 3) if by_hour["deff"] else None),
                               "icc_close": (round(by_close["icc_influence"], 3) if by_close["icc_influence"] is not None else None), "icc_hour": (round(by_hour["icc_influence"], 3) if by_hour["icc_influence"] is not None else None),
                               "n_effective_fills_close": (round(by_close["n_effective_fills"], 1) if by_close["n_effective_fills"] else None), "n_effective_fills_hour": (round(by_hour["n_effective_fills"], 1) if by_hour["n_effective_fills"] else None),
                               "z_vs_H0_cluster_close": (round(by_close["z_h0_theta_le_0"], 3) if by_close["z_h0_theta_le_0"] else None), "z_vs_H0_cluster_hour": (round(by_hour["z_h0_theta_le_0"], 3) if by_hour["z_h0_theta_le_0"] else None),
                               "sd_per_contract_pnl_c": (round(100 * sd_pc, 2) if sd_pc else None), "information_cluster_close": (round(by_close["information"], 1) if by_close["information"] else None)},
            "design_effect_used_for_planning": round(deff, 3),
            "power_planning": {"delta_c": 2.0, "sd_c_per_contract_fill_level": round(100 * sd, 2), "alpha_one_sided": 0.025, "power": 0.8, "n_fixed_independent_fills": round(n_fixed), "obf_2look_inflation": round(infl, 3),
                               "n_effective_fills_for_80pct_power": round(n_eff_max), "fills_needed_given_deff": round(fills_max), "closes_needed_given_mean_cluster_size": round(fills_max / by_close["mean_cluster_size"]),
                               "addendum_4500_figure": {"formula": "((z_crit + z_power) x sd / delta)^2 with z_crit = 2.80 (first OBF look, not the final boundary), z_power = 0.84, sd 37 c, delta 2 c", "value": round(((2.80 + 0.8416) * 0.37 / 0.02) ** 2),
                                                        "comment": "treats fills as independent and applies the interim boundary as if it were the final one; it is a precision target for one look, not the power of the sequential design"},
                               "precision_vs_power": "precision = SE of theta at a given n (SE_cluster = sd x sqrt(DEFF / n)); power = P(reject H0 | theta = 2 c) which also depends on the boundary set. 300 fills give SE ~ %.1f c: enough to resolve |theta| >~ %.0f c, not 2 c." % (100 * sd * math.sqrt(deff / 300), 100 * sd * math.sqrt(deff / 300) * 2)},
            "boundaries_nominal_schedule_100_300": {"schedule": "looks at 100 and 300 fills treated as information fractions 1/3 and 1 (as the addendum did)", "lan_demets_obf": b_nominal, "power_at_2c_if_300_is_final": round(power_300, 3),
                                                     "comment": "2.80 / 1.98 were the equal-spaced 2-look OBF constants (fractions 1/2, 1); the correct constants for 1/3, 1 are %.2f / %.2f" % (b_nominal[0]["z_boundary"], b_nominal[1]["z_boundary"])},
            "information_fractions_of_100_300_fills_vs_planned_max": {"frac_100": round(frac_100, 4), "frac_300": round(frac_300, 4), "lan_demets_obf_at_those_fractions_plus_final": b_actual,
                                                                        "comment": "against the information needed for 80 %% power at 2 c, 100 and 300 fills are %.1f %% and %.1f %% of the way; the OBF boundaries at such small fractions are > 4 SD, i.e. no efficacy claim is possible there" % (100 * frac_100, 100 * frac_300)},
            "milestone_status": "100 / 300 fills are DESCRIPTIVE REVIEW MILESTONES with NO automated pass until this spec is validated (owner directive 2026-10-28)"}


# ------------------------------------------------------------------------------------------------------------ hourly mapping
def hourly_mapping(trades: list[dict]) -> list[dict]:
    rules = {"s7live": ("P1 (PREREG) S7 crypto taker, model rule thr .05, windows T-8/T-5/T-3.5..T-2 (changed across restarts); P6 sigma gate later", "P1/P6"),
             "s7live-KXETHD": ("P1 ETH", "P1"), "s7live-KXSOLD": ("P1 SOL", "P1"), "s7final-KXBTCD": ("P2 final-minute T-1.0..T-0.15, thr .05, partial-TWAP", "P2"),
             "s7p10-KXBTCD": ("P10 redesign T-3.5..T-2.5, Student-t nu 8 scale .8, dyn thr, EWMA sigma; P11 hour-of-day tilt registered on this tape", "P10/P11"),
             "s7p10-KXETHD": ("P10 ETH (amendment 22:20 PT 10-26: paper lot 10)", "P10"), "s7p10-KXSOLD": ("P10 SOL (same amendment)", "P10"),
             "s7p13-KXBTCD": ("P13 hourly final-minute projection-side taker T-0.92..T-0.15, proj_gate, cap .95, lot 10; P16 edge >= 5 c after REBASE", "P13/P16")}
    out = []
    for fam in HOURLY_FAMS:
        T = [t for t in trades if t["fam"] == fam]; S = [t for t in T if t.get("status") == "settled"]
        log = DATA / fam / "runner.log"; starts = [l for l in log.read_text(encoding="utf-8", errors="replace").splitlines() if " start " in l] if log.exists() else []
        flags = sorted({" ".join(x for x in l.split(" start ", 1)[1].split() if x.split("=")[0] in ("thr", "window", "lot", "sigma", "dist", "thr_mode", "proj_gate", "scored_only")) for l in starts})
        out.append({"family_dir": f"data/{fam}", "rule": rules[fam][0], "prereg_id": rules[fam][1], "code_versions_flags_from_runner_log": flags, "n_start_lines": len(starts), "first_start_utc": (starts[0][:19] if starts else None), "last_start_utc": (starts[-1][:19] if starts else None),
                    "last_start_scored_only": ("scored_only=True" in starts[-1]) if starts else None, "fills": len(T), "settled": len(S), "wins": sum(1 for t in S if t["result"] == t["side"]), "contracts": sum(t["qty"] for t in S),
                    "net_usd": round(sum(t["pnl"] for t in S), 2), "first_fill_pt": (pt(min(t["t"] for t in T)) if T else None), "last_fill_pt": (pt(max(t["t"] for t in T)) if T else None),
                    "version_tag": (version_tag(fam, T[0]["t"]) if T else version_tag(fam, time.time())), "trades_jsonl_exists": (DATA / fam / "trades.jsonl").exists()})
    return out


# ------------------------------------------------------------------------------------------------------------ S1 + cron reconciliation
def s1_classification(now: float) -> dict:
    od = load_jsonl(DATA / "s1-maker" / "orders.jsonl"); ev = load_jsonl(DATA / "risk" / "events.jsonl")
    by = collections.Counter(o.get("status") for o in od)
    live = [o for o in od if o["status"] in ("resting", "filled") and o["close_ts"] > now]
    settled = [o for o in od if o["status"] == "settled"]; s1ev = [e for e in ev if e["family"] == "s1-maker"]
    return {"orders_total": len(od), "by_status": dict(by), "cron_open_resting_128_definition": "count of orders.jsonl rows with status in (open, resting, filled): 48 resting + 80 filled (unit = simulated ORDERS, not contracts, not $)",
            "records_with_market_still_open": len(live), "first_order_pt": pt(min(o["t"] for o in od)), "last_order_pt": pt(max(o["t"] for o in od)), "last_close_pt": pt(max(o["close_ts"] for o in od)),
            "classification": {"48 resting": "historical simulated resting orders on markets that have all closed (last close %s); never cancelled because the S1 runner stopped 10-27 02:11 PT -> STALE STATE, no exposure" % pt(max(o["close_ts"] for o in od)),
                               "80 filled": "simulated maker fills (strict/lenient print rule) on markets that have all closed; never settled by code (settle() ran only for 2) -> STALE STATE: historical paper fills with unknown booked outcome, not active exposure (qty %d contracts, cost $%.2f)" % (sum(o["qty"] for o in od if o["status"] == "filled"), sum(o["qty"] * o["price"] for o in od if o["status"] == "filled")),
                               "2 settled": "the only S1 outcomes ever booked: %s (ledger-only settlements, -$13.28 total, counted once in global accounting)" % ", ".join(f"{o['ticker']} {o.get('pnl'):+.2f}" for o in settled),
                               "3 cancelled": "cancelled before fill"},
            "risk_ledger_s1": {"open_events": sum(1 for e in s1ev if e["kind"] == "open"), "open_cost_usd": round(sum(e["cost"] for e in s1ev if e["kind"] == "open"), 2), "settle_events": [(e["ticker"], round(e["pnl"], 2)) for e in s1ev if e["kind"] == "settle"]},
            "global_open_0_definition": "trades.jsonl rows with status == open across S7 families (+ open-ledger events whose close_ts + 120 s is in the future) — 0; the 128 are in a different file, a different unit and a closed-market state"}


def cron_reconciliation(trades: list[dict], now: float) -> dict:
    cron = [t for t in trades if t["fam"] in CRON_ROOTS and t.get("status") == "settled"]
    led = load_jsonl(DATA / "ledger" / "decisions_fills.jsonl"); fills = [r for r in led if r["row_kind"] == "fill"]
    all_settled = [t for t in trades if t.get("status") == "settled"]
    return {"cron_n20_definition": "settled trades.jsonl rows in the five HOURLY roots the legacy script hard-coded (%s): %d fills, net $%.2f" % (", ".join(CRON_ROOTS), len(cron), sum(t["pnl"] for t in cron)),
            "cron_last24h_none_definition": "settled rows in those five roots with fill time >= now - 24 h: %d (all hourly families stopped 10-27; the P12 15M fills live in s7p15-* which the legacy script never read)" % sum(1 for t in cron if t["t"] >= now - 86400),
            "ledger_definition": "data/ledger/decisions_fills.jsonl rows with row_kind == fill (every family with a trades.jsonl, settled or open) — %d rows at the ledger's build time; rebuilt now: %d settled fills across %d families" % (len(fills), len(all_settled), len({t["fam"] for t in all_settled})),
            "fills_last_24h_all_families": sum(1 for t in all_settled if t["t"] >= now - 86400), "fills_last_24h_p12": sum(1 for t in all_settled if t["t"] >= now - 86400 and t["fam"].startswith("s7p15")),
            "excluded_from_both": "the two s1-maker ledger-only settlements (-$6.48, -$6.80) have no trades.jsonl row: in global accounting (ledger) once, in no strategy report (no decision record, no fill price/qty audit trail)"}


def occupancy(trades: list[dict], t0: float, t1: float, fam_prefix: str = "s7p15", bankroll: float = 250.0) -> dict:
    F = [t for t in trades if t["fam"].startswith(fam_prefix) and t0 <= t["t"] < t1 and t.get("status") == "settled"]
    occ = sum(t["cost"] * max(0.0, min(t["close_ts"] + 120, t1) - t["t"]) for t in F) / max(t1 - t0, 1)
    evs = sorted([(t["t"], t["cost"]) for t in F] + [(t["close_ts"] + 120, -t["cost"]) for t in F]); cur = mx = 0.0
    for _, c in evs: cur += c; mx = max(mx, cur)
    return {"window_pt": [pt(t0), pt(t1)], "fills": len(F), "time_average_open_cost_usd": round(occ, 3), "time_average_occupancy_of_bankroll": round(occ / bankroll, 5), "peak_open_cost_usd": round(mx, 2), "peak_occupancy_of_bankroll": round(mx / bankroll, 4),
            "definitions": {"opportunity_rate": "closes with >= 1 candidate snapshot / closes with >= 1 in-window snapshot (per asset)", "fill_rate": "fills / closes with >= 1 in-window snapshot (per asset)",
                            "capital_occupancy": "time-average of open cost over the window (positions held fill -> close + 120 s) / $250 bankroll; peak exposure reported separately as max simultaneous open cost"}}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--asof", default="2026-10-28 09:05:51"); ap.add_argument("--out", default="data/p22/addendum2_2026-10-28.json"); a = ap.parse_args()
    asof = dt.datetime.strptime(a.asof, "%Y-%m-%d %H:%M:%S").replace(tzinfo=PT).timestamp(); now = time.time()
    trades = load_trades()
    markets = {}
    for name in ("markets_cache.json", "audit_results_2026-10-28.json"):
        p = DATA / "p22" / name
        if p.exists():
            try: markets.update(json.loads(p.read_text(encoding="utf-8")))
            except Exception: pass
    R36 = p12_records(trades, FREEZE_TS, asof); Rnow = p12_records(trades, FREEZE_TS, now)
    out = {"generated_pt": pt(now), "boundaries": {"p12_freeze_pt": pt(FREEZE_TS), "p13_15m_suspension_pt": pt(SUSPEND_TS), "p13_btcd_suspension_pt": pt(BTCD_SUSPEND_TS), "calibration_cohort_asof_pt": pt(asof)},
           "p12_calibration": {"cohort_definition": "settled P12 fills (families s7p15-KX*15M) with fill time in [freeze %s, as-of %s]" % (pt(FREEZE_TS), pt(asof)), "n_fixed_cohort": len(R36), "n_current": len(Rnow), "current_asof_pt": pt(now),
                               "records_fixed_cohort": R36, "records_added_since": [r for r in Rnow if r["t"] > asof], "paired_scoring_fixed": paired_scoring(R36), "paired_scoring_current": paired_scoring(Rnow),
                               "bootstrap_fixed": bootstraps(R36), "bootstrap_current": bootstraps(Rnow), "conditional_fixed": conditional_calibration(R36), "conditional_current": conditional_calibration(Rnow),
                               "eth_tie": eth_tie_mass(Rnow, markets), "sensitivities_fixed": sensitivities(R36), "sensitivities_current": sensitivities(Rnow),
                               "note": "the gap between mean p_side and hit rate alone proves nothing about relative skill: a calibrated forecaster selected on p_side > price is expected to show hit < mean p_side only if the market is right and the model wrong; the paired Brier / log-score / realised-$ comparisons on identical records are the test"},
           "statistical_spec": stat_spec(Rnow), "hourly_mapping": hourly_mapping(trades), "s1_maker": s1_classification(now), "cron_reconciliation": cron_reconciliation(trades, now),
           "p12_occupancy_official_era": occupancy(trades, FREEZE_TS, now)}
    pathlib.Path(a.out).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k not in ("p12_calibration",)}, indent=1, default=str))
    pc = out["p12_calibration"]
    print(json.dumps({k: pc[k] for k in ("n_fixed_cohort", "n_current", "paired_scoring_fixed", "paired_scoring_current", "sensitivities_current", "conditional_fixed")}, indent=1, default=str))
    print(json.dumps({"eth_tie": {k: v for k, v in pc["eth_tie"].items() if k != "rows"}, "bootstrap_fixed": pc["bootstrap_fixed"]}, indent=1, default=str))
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()
