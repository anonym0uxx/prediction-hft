"""BRTI/ETH/SOL real-time index REPLICA (review C2/C3/N5): 1 Hz median of L1 mids across public constituent books
(Coinbase, Kraken, Bitstamp, Gemini, Crypto.com — LMAX/Bullish have no free L1). CF Benchmarks RTI is a depth-weighted
mid across these books; the median-of-mids replica tracks it to a few bp. Writes:
  data/replica/<PRODUCT>.jsonl        1 Hz rows {t, mids{venue}, replica, cb_last, basis_bp}
  data/replica/<PRODUCT>.latest.json  {t, replica, partial_twap, partial_n, window_start} for consumers (s7_live)
partial_twap = running mean of replica samples since the top-of-hour-minus-60 s (the settlement averaging window).
Usage: uv run python -m predhft.index_replica --products BTC,ETH,SOL
"""
from __future__ import annotations
import argparse, json, pathlib, statistics, threading, time, urllib.request
from .s7_live import single_instance

H = {"User-Agent": "predhft-replica"}
def jget(u, timeout=3):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=timeout).read())

VENUES = {
    "coinbase": lambda s: (lambda d: (float(d["bid"]), float(d["ask"]), float(d["price"])))(jget(f"https://api.exchange.coinbase.com/products/{s}-USD/ticker")),
    "kraken": lambda s: (lambda d: (float(d["b"][0]), float(d["a"][0]), None))(next(iter(jget(f"https://api.kraken.com/0/public/Ticker?pair={'XBT' if s == 'BTC' else s}USD")["result"].values()))),
    "bitstamp": lambda s: (lambda d: (float(d["bid"]), float(d["ask"]), None))(jget(f"https://www.bitstamp.net/api/v2/ticker/{s.lower()}usd/")),
    "gemini": lambda s: (lambda d: (float(d["bid"]), float(d["ask"]), None))(jget(f"https://api.gemini.com/v1/pubticker/{s.lower()}usd")),
    "cryptocom": lambda s: (lambda d: (float(d["b"]), float(d["k"]), None))(jget(f"https://api.crypto.com/exchange/v1/public/get-ticker?instrument_name={s}_USD")["result"]["data"][0]),
}

def sample(product: str) -> dict:
    out: dict[str, tuple] = {}; t_s = time.time()
    def one(v, fn):
        try: out[v] = fn(product)
        except Exception: pass
    th = [threading.Thread(target=one, args=(v, fn), daemon=True) for v, fn in VENUES.items()]
    [t.start() for t in th]; [t.join(timeout=1.2) for t in th]
    mids = {v: (b + a) / 2 for v, (b, a, _) in out.items() if b and a and a >= b and (a - b) / a < 0.005}
    rep = statistics.median(mids.values()) if len(mids) >= 2 else None
    cb_last = out.get("coinbase", (None, None, None))[2]
    return {"t": time.time(), "mids": {v: round(m, 4) for v, m in mids.items()}, "replica": rep, "cb_last": cb_last, "n_venues": len(mids), "sample_ms": int((time.time() - t_s) * 1000),
            "basis_bp": (round((cb_last / rep - 1) * 1e4, 2) if rep and cb_last else None)}

def loop(products: list[str], root: pathlib.Path):
    partial: dict[str, list] = {p: [] for p in products}; win: dict[str, int] = {p: 0 for p in products}
    while True:
        t0 = time.time(); next_hour = (int(t0) // 900 + 1) * 900; wstart = next_hour - 60   # next 15-min boundary (hourly closes are a subset)
        res: dict[str, dict] = {}
        ths = [threading.Thread(target=lambda p=p: res.__setitem__(p, sample(p)), daemon=True) for p in products]   # products in parallel: cadence 1.22 -> 1.0 s
        [t.start() for t in ths]; [t.join(timeout=4.0) for t in ths]
        for p in products:
            r = res.get(p)
            if not r or r["replica"] is None: continue
            (root / f"{p}.jsonl").open("a").write(json.dumps(r) + "\n")
            if win[p] != wstart: win[p] = wstart; partial[p] = []
            if r["t"] >= wstart: partial[p].append(r["replica"])
            (root / f"{p}.latest.json").write_text(json.dumps({"t": r["t"], "replica": r["replica"], "cb_last": r["cb_last"], "n_venues": len(r["mids"]),
                                                               "window_start": wstart, "partial_n": len(partial[p]), "partial_twap": (sum(partial[p]) / len(partial[p]) if partial[p] else None)}))
        time.sleep(max(0.0, 1.0 - (time.time() - t0)))

def latest(product: str, root: pathlib.Path = pathlib.Path("data/replica"), max_age: float = 2.5) -> dict | None:
    p = root / f"{product}.latest.json"
    if not p.exists(): return None
    try: d = json.loads(p.read_text())
    except Exception: return None
    return d if time.time() - d["t"] <= max_age else None

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--products", default="BTC,ETH,SOL"); ap.add_argument("--once", action="store_true"); a = ap.parse_args()
    root = pathlib.Path("data/replica"); root.mkdir(parents=True, exist_ok=True)
    if a.once:
        for p in a.products.split(","): print(p, sample(p))
    else:
        _lock = single_instance(root); loop(a.products.split(","), root)
