"""P26 backfill — does an external signal add information beyond the Kalshi mid at T-10 / T-5? (docs/EXTERNAL_SIGNALS_2026-10-29.md §5)

Closes: every KXSOL15M close with a two-sided Kalshi quote within 20 s of the decision time in data/open_quotes/quotes.jsonl
(30-s cadence since 2026-10-28 04:30Z). Outcome: official feed (exact rule) with the Kalshi result as cross-check (llm_shadow_score.settle).
Signals with obtainable history:
  * Polymarket 'SOL Up or Down 15m' Up price at the decision time (CLOB prices-history, ~1 point / min; the last point at or before
    the decision time, age recorded) - same window, Chainlink TWAP settlement;
  * Binance spot SOLUSDT aggressor imbalance over the 300 s / 60 s before the decision time (data-api.binance.vision aggTrades).
Tests (paired, one obs per close per decision time, close-clustered SE = sd/sqrt(n) since each close is one cluster):
  * Brier(mid) - Brier(poly) and Brier(mid) - Brier(mean(mid, poly));
  * in-sample logistic y ~ a + b*logit(mid) + c*logit(poly) and y ~ a + b*logit(mid) + c*imb (Newton, no sklearn): t of c;
  * disagreement cells: when |poly - mid| >= 5 c, how often the Polymarket side won vs the mid's implied rate;
  * Polymarket's own resolution vs ours (Chainlink TWAP vs CF Benchmarks 60-s average): fraction of closes where the two indices
    settle differently (index-basis flip rate) - the ceiling on how much one market can say about the other.
Everything here is IN-SAMPLE for any fitted coefficient and is labelled so. Writes reports/ext_signals/backfill_latest.json.

    uv run python scripts/ext_signals_backfill.py [--max-closes N] [--no-binance] [--decisions 600,300]
"""
from __future__ import annotations
import argparse, collections, json, math, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src")); sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import numpy as np
from predhft.ext_feeds import polymarket as PM, binance_flow as BN
import llm_shadow_score as SC

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "ext_signals"
CACHE = ROOT / "data" / "ext" / "backfill_cache.json"
EPS = 1e-4


def logit(p): p = min(max(p, EPS), 1 - EPS); return math.log(p / (1 - p))


def load_quotes(series="KXSOL15M") -> dict[int, list[dict]]:
    by = collections.defaultdict(list)
    for l in (ROOT / "data/open_quotes/quotes.jsonl").open(encoding="utf-8"):
        try: d = json.loads(l)
        except Exception: continue
        if d.get("series") != series or d.get("yes_bid") is None or d.get("yes_ask") is None: continue
        by[int(d["close_ts"])].append(d)
    return by


def quote_at(rows: list[dict], t: float, tol: float = 20.0) -> dict | None:
    best = min(rows, key=lambda r: abs(r["t"] - t))
    return best if abs(best["t"] - t) <= tol else None


def fit_logit(X: np.ndarray, y: np.ndarray, iters: int = 50) -> tuple[np.ndarray, np.ndarray]:
    """Newton-Raphson logistic regression; returns (beta, se). X includes the intercept column."""
    b = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ b)); W = p * (1 - p)
        H = X.T @ (X * W[:, None]) + 1e-8 * np.eye(X.shape[1]); g = X.T @ (y - p)
        step = np.linalg.solve(H, g); b = b + step
        if np.max(np.abs(step)) < 1e-8: break
    p = 1 / (1 + np.exp(-X @ b)); W = p * (1 - p); H = X.T @ (X * W[:, None]) + 1e-8 * np.eye(X.shape[1])
    return b, np.sqrt(np.diag(np.linalg.inv(H)))


def paired(diffs: list[float]) -> dict:
    a = np.array(diffs, dtype=float); n = len(a)
    if n < 2: return {"n": n, "mean": (float(a.mean()) if n else None), "se": None, "t": None}
    se = float(a.std(ddof=1) / math.sqrt(n)); return {"n": n, "mean": float(a.mean()), "se": se, "t": (float(a.mean() / se) if se > 0 else None)}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--max-closes", type=int, default=None); ap.add_argument("--no-binance", action="store_true"); ap.add_argument("--decisions", default="600,300"); ap.add_argument("--sleep", type=float, default=0.15)
    a = ap.parse_args(); decisions = [int(x) for x in a.decisions.split(",")]
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    quotes = load_quotes(); kres = SC.kalshi_results(); series = SC.load_cfb("SOL"); now = time.time()
    closes = sorted(c for c in quotes if c + 120 < now)
    if a.max_closes: closes = closes[-a.max_closes:]
    rows = []; n_poly_missing = 0; n_unsettled = 0
    for c in closes:
        q0 = quotes[c][0]; K = float(q0["strike"]); ticker = q0["ticker"]
        st = SC.settle(series, ticker, c, K, kres)
        if st is None: n_unsettled += 1; continue
        key = str(c)
        if key not in cache:
            ent = {"poly": None, "hist": None, "bn": {}}
            try:
                mk = PM.fetch_market(PM.slug_for(c)); time.sleep(a.sleep)
                if mk:
                    ent["poly"] = {k: mk.get(k) for k in ("slug", "volume_usd", "closed", "outcome_prices")}
                    ent["hist"] = PM.price_history(mk["up_token"], c - 960, c + 60); time.sleep(a.sleep)
            except Exception as e: ent["poly_err"] = f"{type(e).__name__}:{str(e)[:60]}"
            cache[key] = ent
        ent = cache[key]
        for d in decisions:
            t = c - d; q = quote_at(quotes[c], t)
            if q is None: continue
            mid = (q["yes_bid"] + q["yes_ask"]) / 2
            r = {"close_ts": c, "d": d, "t": t, "mid": mid, "spread": round(q["yes_ask"] - q["yes_bid"], 3), "y": st["y"], "y_feed": st.get("y_feed"), "y_kalshi": st.get("y_kalshi"), "K": K, "cfb_value": q.get("cfb_value")}
            if ent.get("hist"):
                p, age = PM.price_at(ent["hist"], q["t"]); r["poly"] = p; r["poly_age_s"] = age
                op = ent["poly"].get("outcome_prices")
                if op:
                    try: pr = json.loads(op); r["poly_up_won"] = (1.0 if float(pr[0]) > 0.5 else 0.0) if float(pr[0]) != float(pr[1]) else None
                    except Exception: pass
            else: n_poly_missing += 1
            if not a.no_binance:
                bk = f"{d}"
                if bk not in ent["bn"]:
                    try:
                        tr = BN.agg_trades_between(int((t - 300) * 1000), int(t * 1000)); ent["bn"][bk] = BN.flow_stats(tr, t * 1000); time.sleep(a.sleep)
                    except Exception as e: ent["bn"][bk] = {"err": f"{type(e).__name__}:{str(e)[:60]}"}
                r.update({"bn_imb_300s": ent["bn"][bk].get("imb_300s"), "bn_imb_60s": ent["bn"][bk].get("imb_60s"), "bn_notional_300s": ent["bn"][bk].get("notional_300s")})
            rows.append(r)
        if len(cache) % 20 == 0: CACHE.parent.mkdir(parents=True, exist_ok=True); CACHE.write_text(json.dumps(cache), encoding="utf-8")
    CACHE.parent.mkdir(parents=True, exist_ok=True); CACHE.write_text(json.dumps(cache), encoding="utf-8")

    rep = {"generated_pt": time.strftime("%Y-%m-%d %H:%M PT", time.localtime()), "scope": "IN_SAMPLE (any fitted coefficient); paired Brier differences are descriptive", "n_closes_with_quotes": len(closes), "n_unsettled": n_unsettled, "n_decision_rows": len(rows), "by_decision": {}}
    # index-basis flip rate (Polymarket Chainlink resolution vs our CF resolution), one per close
    flips = {}
    for r in rows:
        if r.get("poly_up_won") is not None: flips[r["close_ts"]] = (r["poly_up_won"] != r["y"])
    rep["index_basis"] = {"n_closes_both_resolved": len(flips), "n_disagree": int(sum(flips.values())), "flip_rate": (round(sum(flips.values()) / len(flips), 4) if flips else None),
                          "note": "closes where Polymarket (Chainlink 60-s TWAP) and Kalshi/our feed (CF Benchmarks 60-s average) settled on different sides: the ceiling on what either market can know about the other's outcome"}
    for d in decisions:
        R = [r for r in rows if r["d"] == d]; out = {"n": len(R)}
        P = [r for r in R if r.get("poly") is not None]
        out["polymarket"] = {"n": len(P), "median_poly_age_s": (float(np.median([r["poly_age_s"] for r in P])) if P else None)}
        if len(P) >= 10:
            bm = [SC.brier(r["mid"], r["y"]) for r in P]; bp = [SC.brier(r["poly"], r["y"]) for r in P]; ba = [SC.brier((r["mid"] + r["poly"]) / 2, r["y"]) for r in P]
            out["polymarket"].update({"brier_mid": float(np.mean(bm)), "brier_poly": float(np.mean(bp)), "brier_avg": float(np.mean(ba)),
                                      "paired_mid_minus_poly": paired([m - p for m, p in zip(bm, bp)]), "paired_mid_minus_avg": paired([m - p for m, p in zip(bm, ba)]),
                                      "mean_abs_diff_c": float(np.mean([abs(r["poly"] - r["mid"]) * 100 for r in P])), "corr_logit": float(np.corrcoef([logit(r["mid"]) for r in P], [logit(r["poly"]) for r in P])[0, 1])})
            X = np.array([[1.0, logit(r["mid"]), logit(r["poly"])] for r in P]); y = np.array([r["y"] for r in P])
            try: b, se = fit_logit(X, y); out["polymarket"]["logit_in_sample"] = {"b_mid": float(b[1]), "se_mid": float(se[1]), "b_poly": float(b[2]), "se_poly": float(se[2]), "t_poly": float(b[2] / se[2])}
            except Exception as e: out["polymarket"]["logit_in_sample"] = {"err": str(e)[:60]}
            dis = [r for r in P if abs(r["poly"] - r["mid"]) >= 0.05]
            if dis:
                side_won = [(r["y"] if r["poly"] > r["mid"] else 1 - r["y"]) for r in dis]; implied = [(r["mid"] if r["poly"] > r["mid"] else 1 - r["mid"]) for r in dis]; poly_said = [(r["poly"] if r["poly"] > r["mid"] else 1 - r["poly"]) for r in dis]
                out["polymarket"]["disagree_ge5c"] = {"n": len(dis), "poly_side_hit": float(np.mean(side_won)), "mid_implied": float(np.mean(implied)), "poly_said": float(np.mean(poly_said)), "se_hit": float(np.std(side_won, ddof=1) / math.sqrt(len(dis))) if len(dis) > 1 else None}
            # mid-shift test: does the sign of (poly - mid) carry? hit of the poly-favoured side minus mid-implied, all rows with any disagreement
            dd = [r for r in P if abs(r["poly"] - r["mid"]) >= 0.01]
            if dd: out["polymarket"]["sign_test_ge1c"] = {"n": len(dd), "excess_hit_of_poly_side": float(np.mean([(r["y"] if r["poly"] > r["mid"] else 1 - r["y"]) - (r["mid"] if r["poly"] > r["mid"] else 1 - r["mid"]) for r in dd])), "se": float(np.std([(r["y"] if r["poly"] > r["mid"] else 1 - r["y"]) - (r["mid"] if r["poly"] > r["mid"] else 1 - r["mid"]) for r in dd], ddof=1) / math.sqrt(len(dd))) if len(dd) > 1 else None}
        B = [r for r in R if r.get("bn_imb_300s") is not None]
        out["binance_imbalance"] = {"n": len(B)}
        if len(B) >= 20:
            y = np.array([r["y"] for r in B])
            for f in ("bn_imb_300s", "bn_imb_60s"):
                Bf = [r for r in B if r.get(f) is not None]
                if len(Bf) < 20: continue
                X = np.array([[1.0, logit(r["mid"]), r[f]] for r in Bf]); yy = np.array([r["y"] for r in Bf])
                try:
                    b, se = fit_logit(X, yy); pf = 1 / (1 + np.exp(-X @ b)); bm = [SC.brier(r["mid"], r["y"]) for r in Bf]; bf = [SC.brier(float(p), r["y"]) for p, r in zip(pf, Bf)]
                    out["binance_imbalance"][f] = {"n": len(Bf), "b_imb": float(b[2]), "se_imb": float(se[2]), "t_imb": float(b[2] / se[2]), "b_mid": float(b[1]), "paired_brier_mid_minus_fitted_IN_SAMPLE": paired([m - x for m, x in zip(bm, bf)]),
                                                   "corr_imb_vs_residual": float(np.corrcoef([r[f] for r in Bf], [r["y"] - r["mid"] for r in Bf])[0, 1])}
                except Exception as e: out["binance_imbalance"][f] = {"err": str(e)[:60]}
        rep["by_decision"][str(d)] = out
    OUT.mkdir(parents=True, exist_ok=True); (OUT / "backfill_latest.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    (OUT / "backfill_rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
