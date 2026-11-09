"""P32 forward paper arm: buy the T-5 near-certain favourite in Kalshi 15-minute crypto markets (taker, real depth).

Hypothesis (pre-registered 2026-11-02, from scripts/fav_t5_history.py on 9,000 settled markets 10-01..11-02):
at T-5 (300 s before close) the side with mid >= 0.97 wins more often than its ask implies:
+0.72 c/ct after taker fee (lot 100), both chronological halves +0.72/+0.73, 11 losses in 1,795 events.
Plausible mechanism: settlement is a 60-s average of the index, which shrinks late variance vs a point price.

Rule: at T-5 for each open market in SERIES, read the live order book; favourite side = mid >= 0.97.
Walk the favourite's asks at prices <= MAX_PX, up to LOT contracts (depth-weighted, no touch-fill assumption).
Fee = Kalshi taker fee on the walked qty. Hold to settlement. Paper only: no order endpoint exists in this module.
Output: data/fav-t5/fills.jsonl (entries) + settles appended after result. Read-only public endpoints.
"""
from __future__ import annotations
import json, math, pathlib, time, urllib.request
from datetime import datetime, timezone
from .fees import kalshi_taker_fee

API = "https://api.elections.kalshi.com/trade-api/v2"
ROOT = pathlib.Path("data/fav-t5")
# 2026-11-08 (Alon): BTC + ETH only, paper and any future live arm. SOL dropped: its one loss (11-03, -$296.75)
# erased ~all SOL profit (+$72.77 over 79 trades vs BTC +$323.90 / ETH +$411.22 with zero losses). History stays in fills.jsonl.
# BINDING for live: any live FavT5 arm must import this SERIES (never its own list) so SOL cannot slip back in.
SERIES = ["KXBTC15M", "KXETH15M"]
assert "KXSOL15M" not in SERIES, "FavT5 is BTC/ETH-only by owner decision 2026-11-08"
CFG = {"secs_before_close": 300, "window_s": 20, "fav_min_mid": 0.97, "max_px": 0.99, "lot": 300, "poll_s": 5, "settle_after_s": 120}
UA = {"User-Agent": "predhft-fav-t5/1 (paper)"}


def _get(path: str) -> dict:
    for i in range(4):
        try: return json.load(urllib.request.urlopen(urllib.request.Request(API + path, headers=UA), timeout=20))
        except urllib.error.HTTPError as e:
            if e.code == 404: return {}
            time.sleep(1 + 2 * i)
        except Exception: time.sleep(1 + 2 * i)
    return {}


def _ts(s: str) -> int: return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
def _append(p: pathlib.Path, rec: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f: f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def book_sides(ob: dict) -> tuple[list, list]:
    """Return (yes_asks, no_asks) as [(price, qty)] ascending. Kalshi books list resting BIDS per side; a YES ask at p
    is a NO bid at 1-p."""
    b = ob.get("orderbook_fp") or {}
    yes_bids = [(float(p), float(q)) for p, q in (b.get("yes_dollars") or [])]
    no_bids = [(float(p), float(q)) for p, q in (b.get("no_dollars") or [])]
    yes_asks = sorted((round(1 - p, 4), q) for p, q in no_bids)
    no_asks = sorted((round(1 - p, 4), q) for p, q in yes_bids)
    return yes_asks, no_asks


def walk(asks: list, lot: int, max_px: float) -> tuple[float, float]:
    qty = cost = 0.0
    for p, q in asks:
        if p > max_px or qty >= lot: break
        take = min(q, lot - qty); qty += take; cost += take * p
    return qty, cost


def decide(m: dict, ob: dict, cfg: dict = CFG) -> dict | None:
    yes_asks, no_asks = book_sides(ob)
    if not yes_asks or not no_asks: return None
    best_yes_ask, best_no_ask = yes_asks[0][0], no_asks[0][0]
    yes_bid = 1 - best_no_ask; mid = (yes_bid + best_yes_ask) / 2
    if mid >= cfg["fav_min_mid"]: side, asks = "yes", yes_asks
    elif 1 - mid >= cfg["fav_min_mid"]: side, asks = "no", no_asks
    else: return {"side": None, "qty": 0, "mid": round(mid, 4), "reason": "not near-certain"}
    qty, cost = walk(asks, cfg["lot"], cfg["max_px"])
    q = int(qty)
    if q <= 0: return {"side": side, "qty": 0, "mid": mid, "reason": "no depth <= max_px"}
    avg = cost / qty; fee = kalshi_taker_fee(q, avg)
    return {"side": side, "qty": q, "avg_px": round(avg, 5), "fee": fee, "cost": round(q * avg + fee, 4), "mid": round(mid, 4), "best_ask": asks[0][0]}


def run(cfg: dict = CFG) -> None:
    done = set(); open_pos: dict[str, dict] = {}
    fills = ROOT / "fills.jsonl"
    if fills.exists():
        for l in open(fills, encoding="utf-8"):
            r = json.loads(l); done.add(r["ticker"])
            if r["kind"] == "entry" and r.get("qty", 0) > 0: open_pos[r["ticker"]] = r
            if r["kind"] == "settle": open_pos.pop(r["ticker"], None)
    _append(ROOT / "log.jsonl", {"t": time.time(), "msg": f"start cfg={json.dumps(cfg)} series={SERIES}"})
    while True:
        now = time.time()
        try:
            for ser in SERIES:
                ms = _get(f"/markets?series_ticker={ser}&status=open&limit=20").get("markets", [])
                for m in ms:
                    tk = m["ticker"]; c = _ts(m["close_time"]); left = c - now
                    if tk in done or not (cfg["secs_before_close"] - cfg["window_s"] < left <= cfg["secs_before_close"]): continue
                    ob = _get(f"/markets/{tk}/orderbook"); d = decide(m, ob, cfg); done.add(tk)
                    rec = {"kind": "entry", "t": time.time(), "ticker": tk, "series": ser, "close_ts": c, "secs_left": round(left, 1), **(d or {"qty": 0, "reason": "not near-certain"})}
                    _append(fills, rec)
                    if d and d.get("qty", 0) > 0: open_pos[tk] = rec
            for tk, r in list(open_pos.items()):
                if now < r["close_ts"] + cfg["settle_after_s"]: continue
                mk = _get(f"/markets/{tk}").get("market", {}); res = mk.get("result")
                if res not in ("yes", "no"): continue
                win = res == r["side"]; pnl = round((r["qty"] if win else 0) - r["cost"], 4)
                _append(fills, {"kind": "settle", "t": time.time(), "ticker": tk, "side": r["side"], "qty": r["qty"], "result": res, "win": win, "pnl": pnl})
                open_pos.pop(tk, None)
        except Exception as e:
            _append(ROOT / "log.jsonl", {"t": time.time(), "msg": f"error {type(e).__name__}: {e}"})
        time.sleep(cfg["poll_s"])


if __name__ == "__main__":
    run()
