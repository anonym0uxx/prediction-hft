"""Gap 8: belief fusion fit, order-flow feature, cross-venue/range-book recorder.

fusion.fit()     — per family with >= 100 resolved scored rows: logistic fit  logit p* = a*logit(p_market) + b*logit(p_model) + c
                   (plain Newton iterations, no deps) -> data/fusion.json; fusion.blend(family, p_model, p_market) for runners.
flow(ticker)     — taker imbalance over the last 30 min from public prints: (yes_taker - no_taker)/(total), count, -> used by S1
                   to skip posting into a stampede on our side and recorded in its scoreboard rows.
recorder loop    — last 10 min of each hour: Kalshi KXBTC range-bucket books + Polymarket 'Bitcoin above ___ 5PM ET' ladder
                   (read-only, gamma + clob APIs) -> data/recorder/*.jsonl for gap statistics (arb-scan follow-up).
Usage: uv run python -m predhft.fusion fit | flow <ticker> | record --loop
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, pathlib, re, time, urllib.request
from .kalshi_public import KalshiPublic, f
from .s7_live import book_touch, single_instance
from .scoreboard import load_rows, results_for

ROOT = pathlib.Path("data"); FUS = ROOT / "fusion.json"
def logit(p): p = min(max(p, 1e-4), 1 - 1e-4); return math.log(p / (1 - p))
def sig(x): return 1 / (1 + math.exp(-x))

def fit_logistic(X, y, iters=50, l2=1e-3):
    n, d = len(X), len(X[0]); w = [0.0] * d
    for _ in range(iters):
        g = [0.0] * d; Hm = [[l2 if i == j else 0.0 for j in range(d)] for i in range(d)]
        for xi, yi in zip(X, y):
            p = sig(sum(wj * xj for wj, xj in zip(w, xi))); r = p - yi
            for i in range(d):
                g[i] += r * xi[i] + (l2 * w[i] if i < d - 1 else 0)
                for j in range(d): Hm[i][j] += p * (1 - p) * xi[i] * xi[j]
        # solve H dw = g (Gauss elimination)
        A = [row[:] + [g[i]] for i, row in enumerate(Hm)]
        for i in range(d):
            piv = max(range(i, d), key=lambda r: abs(A[r][i])); A[i], A[piv] = A[piv], A[i]
            if abs(A[i][i]) < 1e-12: return w
            for r in range(d):
                if r != i:
                    fct = A[r][i] / A[i][i]; A[r] = [a - fct * b for a, b in zip(A[r], A[i])]
        dw = [A[i][d] / A[i][i] for i in range(d)]; w = [wi - di for wi, di in zip(w, dw)]
        if max(abs(x) for x in dw) < 1e-6: break
    return w

def fit():
    rows = load_rows(); k = KalshiPublic(min_interval_s=0.2)
    res = results_for({r["ticker"] for r in rows if time.time() - r["t"] > 3600}, k); out = json.loads(FUS.read_text()) if FUS.exists() else {}
    for fam in sorted({r["family"] for r in rows}):
        rs = [r for r in rows if r["family"] == fam and r["ticker"] in res and r.get("p_model") is not None and r.get("p_market") is not None]
        if len(rs) < 100: out[fam] = {"n": len(rs), "status": "insufficient (<100 resolved)"}; continue
        X = [[logit(r["p_market"]), logit(r["p_model"]), 1.0] for r in rs]; y = [1.0 if res[r["ticker"]] == "yes" else 0.0 for r in rs]
        w = fit_logistic(X, y)
        bl = [(sig(sum(wi * xi for wi, xi in zip(w, x))) - yi) ** 2 for x, yi in zip(X, y)]
        bm = [(r["p_market"] - yi) ** 2 for r, yi in zip(rs, y)]
        out[fam] = {"n": len(rs), "a_market": w[0], "b_model": w[1], "c": w[2], "brier_blend": sum(bl) / len(bl), "brier_market": sum(bm) / len(bm), "fit_at": time.time(), "status": "fit"}
    FUS.write_text(json.dumps(out, indent=1)); return out

def blend(family: str, p_model: float, p_market: float) -> float:
    """Fused belief; falls back to shrinking the model toward the market when no fit exists (unproven skill)."""
    fus = json.loads(FUS.read_text()) if FUS.exists() else {}; w = fus.get(family)
    if not w or w.get("status") != "fit": return 0.5 * p_model + 0.5 * p_market
    return sig(w["a_market"] * logit(p_market) + w["b_model"] * logit(p_model) + w["c"])

def flow(k: KalshiPublic, ticker: str, minutes: int = 30) -> dict:
    cut = time.time() - minutes * 60; yes = no = 0.0; n = 0
    try: tr = k.get("/markets/trades", ticker=ticker, limit=200).get("trades", [])
    except Exception: return {"imbalance": None, "n": 0}
    for t in tr:
        if dt.datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp() < cut: continue
        c = float(t["count_fp"]); n += 1
        if t["taker_side"] == "yes": yes += c
        else: no += c
    tot = yes + no
    return {"imbalance": ((yes - no) / tot) if tot else None, "n": n, "yes": yes, "no": no}

def pg(u):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "predhft"}), timeout=20).read())

def record_once(k: KalshiPublic, out: pathlib.Path):
    now = time.time(); next_hour = (int(now) // 3600 + 1) * 3600; mins_left = (next_hour - now) / 60
    if mins_left > 10: return 0
    close_iso = dt.datetime.fromtimestamp(next_hour, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"); n = 0
    for series in ("KXBTC", "KXBTCD"):
        for m in k.markets(series_ticker=series, status="open", limit=1000, max_pages=2):
            if m["close_time"] != close_iso or (f(m, "volume_fp") or 0) <= 0: continue
            try: yb, ya, ybs, yas = book_touch(k.orderbook(m["ticker"]))
            except Exception: continue
            (out / "kalshi.jsonl").open("a").write(json.dumps({"t": now, "mins_left": round(mins_left, 2), "series": series, "ticker": m["ticker"], "floor": m.get("floor_strike"), "cap": m.get("cap_strike"),
                                                              "yes_bid": yb, "yes_ask": ya, "bid_size": ybs, "ask_size": yas}) + "\n"); n += 1
    # Polymarket hourly ladder for the same settlement (5PM ET only exists for some hours; search by slug pattern)
    from zoneinfo import ZoneInfo; et = dt.datetime.fromtimestamp(next_hour, dt.timezone.utc).astimezone(ZoneInfo("America/New_York"))
    slug = f"bitcoin-above-on-{et.strftime('%B').lower()}-{et.day}-{et.year}-{et.strftime('%I').lstrip('0')}{et.strftime('%p').lower()}-et"
    try:
        evs = pg(f"https://gamma-api.polymarket.com/events?slug={slug}")
        for m in (evs[0]["markets"] if evs else []):
            mm = re.search(r"\$?([\d,]{5,})", m["question"]); toks = json.loads(m["clobTokenIds"])
            b = pg(f"https://clob.polymarket.com/book?token_id={toks[0]}")
            bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]; asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
            (out / "polymarket.jsonl").open("a").write(json.dumps({"t": now, "mins_left": round(mins_left, 2), "slug": slug, "K": float(mm.group(1).replace(",", "")) if mm else None,
                                                                   "bid": max(bids)[0] if bids else 0, "ask": min(asks)[0] if asks else 1, "bid_size": max(bids)[1] if bids else 0, "ask_size": min(asks)[1] if asks else 0}) + "\n"); n += 1
    except Exception: pass
    return n

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True); sub.add_parser("fit")
    fl = sub.add_parser("flow"); fl.add_argument("ticker"); rc = sub.add_parser("record"); rc.add_argument("--loop", action="store_true"); a = ap.parse_args()
    k = KalshiPublic(min_interval_s=0.25)
    if a.cmd == "fit": print(json.dumps(fit(), indent=1))
    elif a.cmd == "flow": print(flow(k, a.ticker))
    else:
        out = ROOT / "recorder"; out.mkdir(parents=True, exist_ok=True); _lock = single_instance(out)
        while True:
            try: n = record_once(k, out)
            except Exception as e: print("error", e)
            if not a.loop: break
            time.sleep(30)
