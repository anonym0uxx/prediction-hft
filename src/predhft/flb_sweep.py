"""S1 favorite-longshot sweep across Kalshi series at several horizons before close.
One hourly-candlestick call per settled market (25 h window), then mid at T-24h / T-6h / T-1h.
Usage: uv run python -m predhft.flb_sweep --n 300 --series KXHIGHNY,KXHIGHCHI,KXRAIN,KXNBAGAME,KXNFLGAME,KXBTCD,KXINXU,KXAAAGASD,KXTRUMPMENTION,KXRT
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, pathlib, statistics as st
from collections import defaultdict
from .kalshi_public import KalshiPublic, mid_from_candle
from .calibration import brier, log_loss, reliability
from .fees import kalshi_taker_fee

DEFAULT = "KXHIGHNY,KXHIGHCHI,KXHIGHMIA,KXRAIN,KXNBAGAME,KXNFLGAME,KXNCAAFGAME,KXBTCD,KXINXU,KXAAAGASD,KXTRUMPMENTION,KXRT,KXHOUSERACE"
BINS = [(0, .05), (.05, .15), (.15, .30), (.30, .50), (.50, .70), (.70, .85), (.85, .95), (.95, 1.0001)]

def ts(s): return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())

def collect(k: KalshiPublic, series: str, n: int, horizons: list[int]) -> dict[int, list]:
    pts = defaultdict(list); seen = 0
    for m in k.markets(series_ticker=series, status="settled", limit=200):
        if seen >= n: break
        seen += 1
        if m.get("result") not in ("yes", "no"): continue
        ct = ts(m["close_time"]); y = 1 if m["result"] == "yes" else 0
        try: cs = k.candlesticks(series, m["ticker"], ct - (max(horizons) + 1) * 3600, ct, 60)
        except Exception: continue
        bym = {int(c["end_period_ts"]): c for c in cs}
        for h in horizons:
            c = bym.get(ct - h * 3600)
            if not c: continue
            p = mid_from_candle(c)
            if p is not None and 0 < p < 1: pts[h].append((p, y))
    return pts

def report(all_pts: dict, rep_dir: pathlib.Path):
    lines = ["# S1 favorite-longshot sweep (Kalshi public history)", "", "Return = realized − implied − taker fee (cents per $1 contract bought at mid). Negative in low bins = longshots overpriced (sell side for a maker).", ""]
    agg = defaultdict(list)
    for series, byh in all_pts.items():
        lines.append(f"## {series}")
        for h, pts in sorted(byh.items(), reverse=True):
            if len(pts) < 20: continue
            lines.append(f"- T-{h}h: n={len(pts)} Brier={brier(pts):.4f} logloss={log_loss(pts):.3f}")
            for r in reliability(pts, BINS):
                fee = kalshi_taker_fee(10, min(max(r["implied"], 0.01), 0.99)) / 10 * 100
                ret = (r["realized"] - r["implied"]) * 100 - fee
                lines.append(f"    {r['lo']:.2f}-{min(r['hi'],1):.2f} n={r['n']:4d} implied={r['implied']:.3f} realized={r['realized']:.3f} ret={ret:+.1f}c")
            for p, y in pts: agg[h].append((p, y))
        lines.append("")
    lines.append("## ALL SERIES POOLED")
    for h, pts in sorted(agg.items(), reverse=True):
        lines.append(f"- T-{h}h: n={len(pts)} Brier={brier(pts):.4f}")
        for r in reliability(pts, BINS):
            fee = kalshi_taker_fee(10, min(max(r["implied"], 0.01), 0.99)) / 10 * 100
            ret = (r["realized"] - r["implied"]) * 100 - fee
            lines.append(f"    {r['lo']:.2f}-{min(r['hi'],1):.2f} n={r['n']:5d} implied={r['implied']:.3f} realized={r['realized']:.3f} ret={ret:+.1f}c")
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / f"s1-flb-sweep-{dt.date.today().isoformat()}.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--n", type=int, default=300); ap.add_argument("--series", default=DEFAULT); ap.add_argument("--horizons", default="24,6,1")
    a = ap.parse_args(); k = KalshiPublic(); hz = [int(x) for x in a.horizons.split(",")]
    out = {}
    for s in a.series.split(","):
        out[s] = collect(k, s, a.n, hz); print(s, {h: len(v) for h, v in out[s].items()}, flush=True)
    pathlib.Path("data/s1").mkdir(parents=True, exist_ok=True)
    (pathlib.Path("data/s1") / "points.json").write_text(json.dumps({s: {str(h): v for h, v in d.items()} for s, d in out.items()}))
    report(out, pathlib.Path("reports"))
