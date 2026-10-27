"""Order-flow + sentiment context feed (P15, scored-only). Free public endpoints, no accounts.
Every 60 s per asset: OKX spot taker buy/sell volume (5m), perp funding + premium, open interest (and 15-min OI change),
long/short account ratio, recent liquidations (long vs short notional, last 15 min), Coinbase trade-tape aggressor imbalance
(last 60 s and 300 s), and the daily Fear & Greed index. Rows -> data/context/<ASSET>.jsonl and <ASSET>.latest.json.
These features cannot move a 60-s settlement average; they are evaluated against HOURLY outcomes (P10 scored rows) and as
P13 pre-filters, never as live sizing inputs until a pre-registered test passes (docs/PREREG.md P15)."""
from __future__ import annotations
import json, pathlib, sys, time, urllib.request
ROOT = pathlib.Path("data/context"); UA = {"User-Agent": "predhft/0.1"}

def g(url: str, timeout: float = 6.0):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r: return json.loads(r.read())

def cb_imbalance(product: str) -> dict:
    tr = g(f"https://api.exchange.coinbase.com/products/{product}-USD/trades?limit=1000"); now = time.time(); out = {}
    for w in (60, 300):
        buy = sell = 0.0
        for t in tr:
            ts = time.mktime(time.strptime(t["time"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            if now - ts > w: continue
            v = float(t["size"]) * float(t["price"])
            # Coinbase 'side' is the MAKER side: side=sell means the taker bought
            if t["side"] == "sell": buy += v
            else: sell += v
        out[f"cb_imb_{w}s"] = round((buy - sell) / (buy + sell), 3) if buy + sell > 0 else None
        out[f"cb_notional_{w}s"] = round(buy + sell)
    return out

def okx(asset: str, oi_hist: list) -> dict:
    o = {}
    try:
        d = g(f"https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy={asset}&instType=SPOT&period=5m")["data"][0]
        o["okx_taker_buy"], o["okx_taker_sell"] = float(d[2]), float(d[1]); s = o["okx_taker_buy"] + o["okx_taker_sell"]
        o["okx_taker_imb_5m"] = round((o["okx_taker_buy"] - o["okx_taker_sell"]) / s, 3) if s else None
    except Exception: pass
    try:
        d = g(f"https://www.okx.com/api/v5/public/funding-rate?instId={asset}-USDT-SWAP")["data"][0]
        o["funding"] = float(d["fundingRate"]); o["premium"] = float(d["premium"]) if d.get("premium") else None
    except Exception: pass
    try:
        d = g(f"https://www.okx.com/api/v5/public/open-interest?instId={asset}-USDT-SWAP")["data"][0]; oi = float(d["oiUsd"]); o["oi_usd"] = round(oi)
        oi_hist.append((time.time(), oi)); oi_hist[:] = [x for x in oi_hist if time.time() - x[0] <= 900]
        o["oi_chg_15m_bp"] = round(1e4 * (oi / oi_hist[0][1] - 1), 2) if len(oi_hist) > 1 else None
    except Exception: pass
    try: o["ls_ratio"] = float(g(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={asset}&period=5m")["data"][0][1])
    except Exception: pass
    try:
        d = g(f"https://www.okx.com/api/v5/public/liquidation-orders?instType=SWAP&uly={asset}-USDT&state=filled&limit=100")["data"]
        lo = sh = 0.0; cut = (time.time() - 900) * 1000
        for row in d:
            for x in row.get("details", []):
                if float(x["ts"]) < cut: continue
                v = float(x["sz"]) * float(x["bkPx"])
                if x["posSide"] == "long": lo += v
                else: sh += v
        o["liq_long_15m"], o["liq_short_15m"] = round(lo), round(sh)
    except Exception: pass
    return o

def loop(assets: list[str], every: float = 60.0):
    ROOT.mkdir(parents=True, exist_ok=True); oi_hist = {a: [] for a in assets}; fng = {"t": 0}
    while True:
        t0 = time.time()
        if t0 - fng["t"] > 3600:
            try: d = g("https://api.alternative.me/fng/?limit=1")["data"][0]; fng = {"t": t0, "value": int(d["value"]), "label": d["value_classification"]}
            except Exception: fng["t"] = t0
        for a in assets:
            row = {"t": t0, "asset": a, "fng": fng.get("value")}
            try: row.update(cb_imbalance(a))
            except Exception as e: row["cb_err"] = str(e)[:60]
            row.update(okx(a, oi_hist[a]))
            (ROOT / f"{a}.jsonl").open("a").write(json.dumps(row) + "\n"); (ROOT / f"{a}.latest.json").write_text(json.dumps(row))
        time.sleep(max(1.0, every - (time.time() - t0)))

def latest(asset: str, root: pathlib.Path = ROOT, max_age: float = 180.0) -> dict | None:
    p = root / f"{asset}.latest.json"
    if not p.exists(): return None
    try: d = json.loads(p.read_text())
    except Exception: return None
    return d if time.time() - d["t"] <= max_age else None

if __name__ == "__main__":
    loop((sys.argv[1] if len(sys.argv) > 1 else "BTC,ETH,SOL").split(","))
