"""Historical test: are favourites underpriced at T-5 in Kalshi 15-minute crypto markets? Read-only public API.
For each settled market: 1-min candles around close-300 s; take the candle ending at T-5 (yes_bid/yes_ask close).
Favourite = side with mid >= .5. Reports win rate vs price by bucket, taker P&L (cross at ask + fee) and the
maker-at-bid upper bound (no fee, assumes fill; real fills are adverse-selected), chronological halves, per-week."""
import json, math, pathlib, sys, time, urllib.request, statistics as S
from datetime import datetime
sys.path.insert(0, "src")
from predhft.fees import kalshi_taker_fee
API = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = sys.argv[1:] or ["KXSOL15M", "KXBTC15M", "KXETH15M"]
CAP = 3000
cache_p = pathlib.Path("reports/quant/fav_t5_cache.json"); cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}
def get(u):
    for i in range(5):
        try: return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "x"}), timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 404: return {}
            time.sleep(2 + 3 * i)
        except Exception: time.sleep(1 + 2 * i)
    return {}
ts = lambda s: int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
for ser in SERIES:
    cur, n = None, 0
    while n < CAP:
        d = get(f"{API}/markets?series_ticker={ser}&status=settled&limit=200" + (f"&cursor={cur}" if cur else ""))
        ms = d.get("markets", []); cur = d.get("cursor")
        for m in ms:
            n += 1; tk = m["ticker"]
            if tk in cache or m.get("result") not in ("yes", "no"): continue
            c = ts(m["close_time"])
            cs = get(f"{API}/series/{ser}/markets/{tk}/candlesticks?start_ts={c - 420}&end_ts={c - 240}&period_interval=1").get("candlesticks", [])
            pick = [x for x in cs if x["end_period_ts"] <= c - 300]
            if not pick: cache[tk] = None; continue
            x = pick[-1]
            try: b, a = float(x["yes_bid"]["close_dollars"]), float(x["yes_ask"]["close_dollars"])
            except Exception: cache[tk] = None; continue
            cache[tk] = {"ser": ser, "close": c, "bid": b, "ask": a, "y": 1 if m["result"] == "yes" else 0}
            time.sleep(0.34)
            if len(cache) % 200 == 0: cache_p.write_text(json.dumps(cache)); print(ser, n, len(cache), flush=True)
        if not cur or not ms: break
    cache_p.write_text(json.dumps(cache))
rows = []
for tk, v in cache.items():
    if not v or not (0 < v["bid"] < v["ask"] < 1): continue
    mid = (v["bid"] + v["ask"]) / 2; fy = mid >= 0.5
    p_bid = v["bid"] if fy else 1 - v["ask"]; p_ask = v["ask"] if fy else 1 - v["bid"]
    win = v["y"] == (1 if fy else 0)
    rows.append(dict(ser=v["ser"], t=v["close"], fav=max(mid, 1 - mid), win=win, bid=p_bid, ask=p_ask,
                     taker=(win - p_ask) - kalshi_taker_fee(1, p_ask), maker=(win - p_bid)))
rows.sort(key=lambda r: r["t"])
def agg(rs):
    if not rs: return None
    w = S.mean(r["win"] for r in rs)
    tk = [r["taker"] for r in rs]; mk = [r["maker"] for r in rs]
    se = lambda v: S.stdev(v) / math.sqrt(len(v)) if len(v) > 1 else None
    return {"n": len(rs), "win%": round(100 * w, 1), "avg_ask": round(S.mean(r["ask"] for r in rs), 3),
            "taker_c": round(100 * S.mean(tk), 2), "taker_se_c": round(100 * se(tk), 2) if se(tk) else None,
            "maker_bound_c": round(100 * S.mean(mk), 2)}
B = ((.5, .6), (.6, .7), (.7, .85), (.85, .93), (.93, .97), (.97, 1.0))
out = {"n_total": len(rows), "range": [datetime.fromtimestamp(rows[0]["t"]).isoformat(), datetime.fromtimestamp(rows[-1]["t"]).isoformat()] if rows else None}
for ser in sorted({r["ser"] for r in rows}) + ["ALL"]:
    rs = [r for r in rows if ser == "ALL" or r["ser"] == ser]; h = len(rs) // 2
    out[ser] = {"buckets": {f"{lo:.2f}-{hi:.2f}": agg([r for r in rs if lo <= r["fav"] < hi]) for lo, hi in B},
                "fav_50_85_first_half": agg([r for r in rs[:h] if .5 <= r["fav"] < .85]),
                "fav_50_85_second_half": agg([r for r in rs[h:] if .5 <= r["fav"] < .85])}
wk = {}
for r in rows:
    if .5 <= r["fav"] < .85: wk.setdefault(datetime.fromtimestamp(r["t"]).strftime("%G-W%V"), []).append(r)
out["weekly_fav_50_85"] = {k: agg(v) for k, v in sorted(wk.items())}
pathlib.Path("reports/quant/fav_t5_history.json").write_text(json.dumps(out, indent=1)); print(json.dumps(out, indent=1))
