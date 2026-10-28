"""P22 direct information test: does our calibrated p_cal (official-feed partial average) carry information the market mid
does not, at T-30 / T-20 / T-10 s?  Scored on the runners' own books.jsonl snapshots (official-feed era only), outcomes from
Kalshi settlement (cached in data/p22/results.json).  Reports per asset x bucket: n closes, Brier(mid), Brier(p_cal),
dBrier with close-clustered se, and the mean |p_cal - mid|.  Decision rule in docs/PREREG.md (P22).  No trading change.

Usage: uv run python scripts/p22_info_test.py [--no-fetch] [--since-days N]
"""
from __future__ import annotations
import argparse, json, math, pathlib, sys, time, collections
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft.kalshi_public import KalshiPublic

ROOT = pathlib.Path("data"); CACHE = ROOT / "p22" / "results.json"
FAMS = {"BTC": "s7p13-KXBTC15M", "ETH": "s7p13-KXETH15M", "SOL": "s7p13-KXSOL15M"}
BUCKETS = {"T-30": (0.42, 0.58), "T-20": (0.25, 0.42), "T-10": (0.12, 0.25)}   # mins_left ranges


def load_results(tickers, fetch=True):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    res = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    missing = [t for t in tickers if t not in res]
    if fetch and missing:
        k = KalshiPublic(min_interval_s=0.5)
        for t in missing:
            try:
                m = k.get(f"/markets/{t}").get("market", {})
                if m.get("result") in ("yes", "no"): res[t] = m["result"]
            except Exception: pass
        CACHE.write_text(json.dumps(res))
    return res


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--no-fetch", action="store_true"); ap.add_argument("--since-days", type=float, default=30); a = ap.parse_args()
    since = time.time() - a.since_days * 86400
    snaps = collections.defaultdict(list)                      # (asset, ticker) -> rows
    for asset, fam in FAMS.items():
        p = ROOT / fam / "books.jsonl"
        if not p.exists(): continue
        for l in p.read_text(encoding="utf-8").splitlines():
            try: r = json.loads(l)
            except Exception: continue
            if r.get("t", 0) < since or r.get("spot_src") != "cfb" or r.get("p_cal") is None or r.get("yes_bid") is None or r.get("yes_ask") is None: continue
            if r["yes_ask"] - r["yes_bid"] > 0.2: continue       # no real two-sided market
            snaps[(asset, r["ticker"])].append(r)
    results = load_results(sorted({t for _, t in snaps}), fetch=not a.no_fetch)
    out = {}
    for b, (lo, hi) in BUCKETS.items():
        for asset in FAMS:
            per_close = []                                       # one obs per close: last snapshot in bucket
            for (as_, tk), rows in snaps.items():
                if as_ != asset or tk not in results: continue
                rs = [r for r in rows if lo <= r["mins_left"] < hi]
                if not rs: continue
                r = rs[-1]; y = 1.0 if results[tk] == "yes" else 0.0; mid = (r["yes_bid"] + r["yes_ask"]) / 2; pc = min(max(r["p_cal"], 0.0), 1.0)
                per_close.append(((mid - y) ** 2, (pc - y) ** 2, abs(pc - mid), (pc > 0.5) == (y == 1.0), (mid > 0.5) == (y == 1.0)))
            n = len(per_close)
            if n == 0: out[(b, asset)] = {"n": 0}; continue
            bm = sum(x[0] for x in per_close) / n; bp = sum(x[1] for x in per_close) / n; d = [x[1] - x[0] for x in per_close]
            dm = sum(d) / n; se = math.sqrt(sum((v - dm) ** 2 for v in d) / max(n - 1, 1) / n)
            out[(b, asset)] = {"n": n, "brier_mid": round(bm, 4), "brier_pcal": round(bp, 4), "dBrier": round(dm, 4), "se": round(se, 4),
                               "mean_abs_gap": round(sum(x[2] for x in per_close) / n, 3), "hit_pcal": round(sum(x[3] for x in per_close) / n, 3), "hit_mid": round(sum(x[4] for x in per_close) / n, 3)}
    print(f"P22 info test ({time.strftime('%Y-%m-%d %H:%M PT')}), official-feed snapshots, two-sided books only. dBrier<0 => p_cal beats mid.")
    print(f"{'bucket':6s} {'asset':5s} {'n':>4s} {'Brier mid':>10s} {'Brier pcal':>11s} {'dBrier':>8s} {'se':>7s} {'|gap|':>6s} {'hit pcal':>9s} {'hit mid':>8s}")
    for (b, asset), v in out.items():
        if v["n"] == 0: print(f"{b:6s} {asset:5s} {0:4d}  (no data)"); continue
        print(f"{b:6s} {asset:5s} {v['n']:4d} {v['brier_mid']:10.4f} {v['brier_pcal']:11.4f} {v['dBrier']:+8.4f} {v['se']:7.4f} {v['mean_abs_gap']:6.3f} {v['hit_pcal']:9.3f} {v['hit_mid']:8.3f}")
    (ROOT / "p22" / "latest.json").write_text(json.dumps({f"{b}|{a_}": v for (b, a_), v in out.items()}, indent=1))


if __name__ == "__main__":
    main()
