"""S7 lag study: Kalshi hourly BTC strikes (KXBTCD) vs Coinbase spot in the final minutes before settlement.

No-lookahead: sigma is estimated from the trailing 120 one-minute log returns before each decision time.
Outputs: data/s7/rows.json (every decision point) and reports/s7-lag-<date>.md.
Usage: uv run python -m predhft.s7_lag --pages 60 --horizons 10,5,3,2,1
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, pathlib, statistics as st, time, urllib.request
from collections import defaultdict
from .kalshi_public import KalshiPublic, f
from .fees import kalshi_taker_fee

def ts(s: str) -> int: return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
def Phi(x: float) -> float: return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def coinbase_minutes(t0: int, t1: int, product: str = "BTC-USD") -> dict[int, float]:
    out = {}; cur = t0
    while cur < t1:
        end = min(cur + 300 * 60, t1)
        iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
        u = f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=60&start={iso(cur)}&end={iso(end)}"
        for attempt in range(4):
            try:
                d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "predhft"}), timeout=20).read()); break
            except Exception:
                time.sleep(1 + attempt)
        else: d = []
        for c in d: out[int(c[0])] = float(c[4])
        cur = end; time.sleep(0.25)
    return out

def trailing_sigma(spot: dict[int, float], t: int, n: int = 120) -> float | None:
    rets = []
    for i in range(1, n + 1):
        a, b = spot.get(t - i * 60), spot.get(t - (i - 1) * 60)
        if a and b: rets.append(math.log(b / a))
    return st.pstdev(rets) if len(rets) >= 60 else None

def run(pages: int, horizons: list[int], out_dir: pathlib.Path, rep_dir: pathlib.Path, series: str = "KXBTCD", product: str = "BTC-USD") -> dict:
    k = KalshiPublic(); ms = []
    for m in k.markets(series_ticker=series, status="settled", limit=200, max_pages=pages):
        if m.get("result") in ("yes", "no") and m.get("floor_strike") and (f(m, "volume_fp") or 0) > 0: ms.append(m)
    closes = sorted({m["close_time"] for m in ms})
    spot = coinbase_minutes(ts(closes[0]) - 4 * 3600, ts(closes[-1]) + 60, product)
    rows = []
    for m in ms:
        ct = ts(m["close_time"]); K = float(m["floor_strike"]); y = 1 if m["result"] == "yes" else 0
        try: cs = k.candlesticks(series, m["ticker"], ct - 15 * 60, ct, 1)
        except Exception: continue
        bym = {int(c["end_period_ts"]): c for c in cs}
        for h in horizons:
            t = ct - h * 60; c = bym.get(t); s = spot.get(t)
            if not c or s is None: continue
            try: yb = float(c["yes_bid"]["close_dollars"]); ya = float(c["yes_ask"]["close_dollars"])
            except (KeyError, TypeError, ValueError): continue
            sig = trailing_sigma(spot, t)
            if not sig: continue
            z = math.log(s / K) / (sig * math.sqrt(max(h - 2.0 / 3.0, 0.05))); p = Phi(z)  # TWAP settlement: mean over final minute
            rows.append({"ticker": m["ticker"], "close": m["close_time"], "h": h, "K": K, "spot": s, "sigma1": sig, "z": z, "p_model": p,
                         "yes_bid": yb, "yes_ask": ya, "y": y, "vol": f(m, "volume_fp") or 0, "vol_1m": float(c.get("volume_fp") or 0)})
    out_dir.mkdir(parents=True, exist_ok=True); (out_dir / "rows.json").write_text(json.dumps(rows))
    return summarize(rows, closes, rep_dir, series)

def summarize(rows, closes, rep_dir: pathlib.Path, series: str = "KXBTCD") -> dict:
    by = defaultdict(list)
    for r in rows: by[r["h"]].append(r)
    lines = [f"# S7 lag study — {series} vs Coinbase spot", f"hours={len(closes)} ({closes[0]} → {closes[-1]}), decision rows={len(rows)}", "",
             "## Calibration (Brier): model vs market mid", "| horizon | n | model | market mid |", "|---|---|---|---|"]
    for h in sorted(by, reverse=True):
        g = by[h]; mb = st.fmean((r["p_model"] - r["y"]) ** 2 for r in g); kb = st.fmean(((r["yes_bid"] + r["yes_ask"]) / 2 - r["y"]) ** 2 for r in g)
        lines.append(f"| T-{h}m | {len(g)} | {mb:.4f} | {kb:.4f} |")
    lines += ["", "## Taker rule at the touch, hold to expiry, 10-lot fee (net cents/contract)", "| horizon | thr | n | avg | 1se | hit | hours>0/hours |", "|---|---|---|---|---|---|---|"]
    res = {}
    for h in sorted(by, reverse=True):
        for thr in (0.03, 0.05, 0.08, 0.12):
            pnl = []; hrs = defaultdict(float)
            for r in by[h]:
                if r["yes_bid"] <= 0 or r["yes_ask"] >= 1 or r["yes_ask"] - r["yes_bid"] > 0.06: continue
                if r["p_model"] - r["yes_ask"] >= thr:
                    c = r["yes_ask"]; v = r["y"] - c - kalshi_taker_fee(10, c) / 10
                elif r["p_model"] - r["yes_bid"] <= -thr:
                    c = 1 - r["yes_bid"]; v = (1 - r["y"]) - c - kalshi_taker_fee(10, c) / 10
                else: continue
                pnl.append(v); hrs[r["close"]] += v
            n = len(pnl)
            if n:
                a = st.fmean(pnl); se = st.pstdev(pnl) / math.sqrt(n) if n > 1 else 0; hit = st.fmean(p > 0 for p in pnl)
                pos = sum(v > 0 for v in hrs.values())
                lines.append(f"| T-{h}m | {thr:.2f} | {n} | {a*100:+.1f}c | {se*100:.1f}c | {hit:.0%} | {pos}/{len(hrs)} |")
                res[(h, thr)] = {"n": n, "avg": a, "se": se, "hit": hit, "hours_pos": pos, "hours": len(hrs)}
    lines += ["", "Caveats: taker fills assumed at the 1-min candle close touch; settlement index is BRTI 60-s average (Coinbase used as proxy); sigma trailing 120 min, no lookahead."]
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / f"s7-lag-{series}-{dt.date.today().isoformat()}.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return res

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--pages", type=int, default=60); ap.add_argument("--horizons", default="10,5,3,2,1"); ap.add_argument("--series", default="KXBTCD"); ap.add_argument("--product", default="BTC-USD")
    a = ap.parse_args(); run(a.pages, [int(x) for x in a.horizons.split(",")], pathlib.Path(f"data/s7/{a.series}"), pathlib.Path("reports"), a.series, a.product)
