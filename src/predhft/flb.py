"""Favorite-longshot / calibration probe for a Kalshi series using settled markets + candlesticks.
Usage: python -m predhft.flb KXHIGHNY [--n 300] [--hours 18]
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib
from .kalshi_public import KalshiPublic, mid_from_candle
from .calibration import reliability, brier, log_loss, flb_return_cents
from .fees import kalshi_taker_fee

def run(series: str, n: int, hours: float, out_dir: pathlib.Path) -> dict:
    k = KalshiPublic(); pts = []; seen = 0
    for m in k.markets(series_ticker=series, status="settled", limit=200):
        if seen >= n: break
        seen += 1
        if m.get("result") not in ("yes", "no"): continue
        ct = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        end = int((ct - dt.timedelta(hours=hours)).timestamp())
        try: cs = k.candlesticks(series, m["ticker"], end - 7200, end, 60)
        except Exception: continue
        if not cs: continue
        p = mid_from_candle(cs[-1])
        if p is not None and 0 < p < 1: pts.append((p, 1 if m["result"] == "yes" else 0))
    rep = {"series": series, "scanned": seen, "points": len(pts), "hours_before_close": hours,
           "brier": brier(pts) if pts else None, "log_loss": log_loss(pts) if pts else None,
           "bins": flb_return_cents(pts, taker_fee_cents=lambda p: kalshi_taker_fee(1, p) * 100) if pts else []}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"flb-{series}-{int(hours)}h.json").write_text(json.dumps(rep, indent=1))
    return rep

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("series"); ap.add_argument("--n", type=int, default=300); ap.add_argument("--hours", type=float, default=18)
    a = ap.parse_args(); rep = run(a.series, a.n, a.hours, pathlib.Path("reports"))
    print(json.dumps({k: v for k, v in rep.items() if k != "bins"}, indent=1))
    for b in rep["bins"]: print(f"  {b['lo']:.2f}-{min(b['hi'],1):.2f} n={b['n']:4d} implied={b['implied']:.3f} realized={b['realized']:.3f} ret_after_taker_fee={b['return_cents']:+.1f}c")
