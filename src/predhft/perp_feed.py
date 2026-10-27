"""OKX perp feed (SHARPE_PLAN §5.2, P14 scored-only): 1 Hz last price of {BTC,ETH,SOL}-USDT-SWAP and the 300-s median basis vs the BRTI replica.
Writes data/perp/<P>.jsonl and data/perp/<P>.latest.json {t, px, basis_med_bp}. No account; public endpoint."""
from __future__ import annotations
import json, pathlib, statistics, sys, threading, time, urllib.request
from collections import deque
ROOT = pathlib.Path("data/perp"); OKX = "https://www.okx.com/api/v5/market/ticker?instId={p}-USDT-SWAP"

def _get(url: str, timeout: float = 1.5):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "predhft/0.1"}), timeout=timeout) as r: return json.loads(r.read())

def loop(products: list[str]):
    from predhft.index_replica import latest as replica_latest
    ROOT.mkdir(parents=True, exist_ok=True); hist = {p: deque(maxlen=300) for p in products}
    while True:
        t0 = time.time(); res: dict[str, float] = {}
        def one(p):
            try: res[p] = float(_get(OKX.format(p=p))["data"][0]["last"])
            except Exception: pass
        th = [threading.Thread(target=one, args=(p,), daemon=True) for p in products]; [t.start() for t in th]; [t.join(timeout=1.8) for t in th]
        for p, px in res.items():
            rep = replica_latest(p)
            if rep and rep.get("replica"): hist[p].append(1e4 * (px / rep["replica"] - 1))
            basis = statistics.median(hist[p]) if len(hist[p]) >= 30 else None
            row = {"t": t0, "px": px, "basis_med_bp": (round(basis, 3) if basis is not None else None), "n_basis": len(hist[p])}
            (ROOT / f"{p}.jsonl").open("a").write(json.dumps(row) + "\n"); (ROOT / f"{p}.latest.json").write_text(json.dumps(row))
        time.sleep(max(0.0, 1.0 - (time.time() - t0)))

def latest(product: str, root: pathlib.Path = ROOT, max_age: float = 3.0) -> dict | None:
    p = root / f"{product}.latest.json"
    if not p.exists(): return None
    try: d = json.loads(p.read_text())
    except Exception: return None
    return d if time.time() - d["t"] <= max_age else None

if __name__ == "__main__":
    loop((sys.argv[1] if len(sys.argv) > 1 else "BTC,ETH,SOL").split(","))
