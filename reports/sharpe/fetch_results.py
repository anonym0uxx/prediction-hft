"""Fetch Kalshi results/expiration_value for every ticker seen in books since reset (cached)."""
import json, pathlib, sys, time
sys.path.insert(0, "D:/prediction-hft/src")
from predhft.kalshi_public import KalshiPublic
R = pathlib.Path("D:/prediction-hft/data"); OUT = pathlib.Path("D:/prediction-hft/reports/sharpe/results.json")
RESET = json.loads((R / "RESET.json").read_text())["reset_at"]
cache = json.loads(OUT.read_text()) if OUT.exists() else {}
tks = {}
for p in list(R.glob("s7*/books.jsonl")) + [R / "recorder/kalshi.jsonl"]:
    for l in p.read_text().splitlines():
        if not l.strip(): continue
        try: r = json.loads(l)
        except Exception: continue
        if r.get("t", 0) < RESET or "ticker" not in r or "close_ts" not in r: continue
        if r["close_ts"] > time.time() - 120: continue
        tks[r["ticker"]] = r["close_ts"]
need = [t for t in tks if t not in cache or cache[t].get("result") in (None, "")]
print("tickers", len(tks), "need", len(need))
k = KalshiPublic(min_interval_s=0.4)
for i, t in enumerate(need):
    try:
        m = k.get(f"/markets/{t}")["market"]
        cache[t] = {"result": m.get("result"), "expiration_value": m.get("expiration_value"), "floor_strike": m.get("floor_strike"), "close_time": m.get("close_time"), "close_ts": tks[t], "volume": m.get("volume")}
    except Exception as e:
        print("err", t, e)
    if i % 25 == 0: OUT.write_text(json.dumps(cache)); print(i, flush=True)
OUT.write_text(json.dumps(cache))
print("done", sum(1 for v in cache.values() if v.get("result")))
