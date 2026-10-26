"""S1 maker paper engine — favorite-longshot harvesting as a MAKER (fees 0), honest fill simulation from the public trades feed.

Rule (pre-registered, from the FLB sweep): a day ahead of resolution,
  * SELL YES (post a YES ask = join best ask) on contracts priced 5-30c   — longshots are overpriced (realized 3-17% vs 9-22% implied)
  * BUY  YES (post a YES bid = join best bid) on contracts priced 50-95c  — favorites are underpriced (+10..15c)
A resting paper order fills only when, after it was posted, taker prints on OUR side at our price (or through it) accumulate more
contracts than the size that was ahead of us in the queue when we joined (queue_ahead = touch size at join). Hold to settlement.
Caps: one order per market, lot per market, max open per series. Scoreboard rows record bin-implied vs market at decision time.

Usage: uv run python -m predhft.s1_maker --loop
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, re, time
from .kalshi_public import KalshiPublic, f
from .s7_live import book_touch, single_instance
from .risk import series_multiplier, Ledger, halted
from .fusion import flow

SERIES_GLOBS = [r"^KXHIGH", r"^KXLOW(T|[A-Z]{2,4}$)", r"^KXRAIN", r"^KXINXU$", r"^KXNASDAQ100U$", r"^KXAAAGASD$", r"^KXBTCD$", r"^KXETHD$"]
SELL_LO, SELL_HI, BUY_LO, BUY_HI = 0.05, 0.30, 0.50, 0.95
MIN_H, MAX_H = 6.0, 36.0           # hours to close at decision time
def ts(s): return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

class Book:
    def __init__(self, root: pathlib.Path):
        self.root = root; root.mkdir(parents=True, exist_ok=True)
        self.orders = root / "orders.jsonl"; self.score = root / "scoreboard.jsonl"; self.log = root / "runner.log"
        self.items = [json.loads(l) for l in self.orders.read_text().splitlines() if l.strip()] if self.orders.exists() else []
    def save(self): self.orders.write_text("".join(json.dumps(o) + "\n" for o in self.items))
    def logline(self, m):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {m}"; print(line, flush=True); self.log.open("a").write(line + "\n")
    def by_ticker(self): return {o["ticker"]: o for o in self.items}

def eligible_series(k: KalshiPublic, db_path: pathlib.Path) -> list[str]:
    import sqlite3
    c = sqlite3.connect(db_path); rows = [r[0] for r in c.execute("SELECT DISTINCT series FROM markets WHERE series IS NOT NULL")]
    return [s for s in rows if any(re.search(g, s) for g in SERIES_GLOBS)]

def scan(k: KalshiPublic, book: Book, lot: int, max_open_per_series: int, db_path: pathlib.Path):
    now = time.time(); open_by_series: dict[str, int] = {}
    h = halted(Ledger(book.root, "orders.jsonl"))
    if h: book.logline(f"HALTED: {h}"); return
    for o in book.items:
        if o["status"] in ("resting", "filled"): open_by_series[o["series"]] = open_by_series.get(o["series"], 0) + 1
    have = book.by_ticker()
    for series in eligible_series(k, db_path):
        if open_by_series.get(series, 0) >= max_open_per_series: continue
        try: ms = list(k.markets(series_ticker=series, status="open", limit=200, max_pages=1))
        except Exception as e: book.logline(f"markets error {series}: {e}"); continue
        for m in ms:
            h = (ts(m["close_time"]) - now) / 3600
            if not (MIN_H <= h <= MAX_H) or m["ticker"] in have: continue
            yb, ya = f(m, "yes_bid_dollars") or 0.0, f(m, "yes_ask_dollars") or 1.0
            if ya - yb > 0.10 or (f(m, "volume_fp") or 0) < 50: continue
            mid = (yb + ya) / 2
            side = None
            if SELL_LO <= ya <= SELL_HI: side, price = "sell_yes", ya          # join the ask
            elif BUY_LO <= yb <= BUY_HI: side, price = "buy_yes", yb           # join the bid
            if not side: continue
            try: b = k.orderbook(m["ticker"]); byb, bya, bbs, bas = book_touch(b)
            except Exception: continue
            queue_ahead = bas if side == "sell_yes" else bbs
            fl = flow(k, m["ticker"]); imb = fl.get("imbalance")
            # sentiment/flow feature: do not sell YES into heavy YES-taker flow or buy YES into heavy NO-taker flow (fade only after it)
            if imb is not None and fl["n"] >= 5 and ((side == "sell_yes" and imb > 0.6) or (side == "buy_yes" and imb < -0.6)):
                book.logline(f"SKIP flow {m['ticker']} side={side} imbalance={imb:+.2f} n={fl['n']}"); continue
            lot_eff = max(1, int(lot * series_multiplier(series)))
            if side == "sell_yes" and abs(bya - price) > 1e-9: price = bya
            if side == "buy_yes" and abs(byb - price) > 1e-9: price = byb
            o = {"t": now, "ticker": m["ticker"], "series": series, "side": side, "price": price, "qty": lot_eff, "queue_ahead": queue_ahead, "filled": 0.0,
                 "close_ts": ts(m["close_time"]), "hours_to_close": round(h, 1), "mid_at_post": mid, "status": "resting", "fill_prints": 0, "flow_imbalance": imb, "flow_n": fl.get("n")}
            book.items.append(o); have[m["ticker"]] = o; open_by_series[series] = open_by_series.get(series, 0) + 1
            book.score.open("a").write(json.dumps({"t": now, "family": "s1_maker", "ticker": m["ticker"], "series": series, "side": side, "price": price, "p_market": mid, "hours": round(h, 1), "flow_imbalance": imb}) + "\n")
            book.logline(f"POST {side} {lot_eff}@{price:.2f} {m['ticker']} queue_ahead={queue_ahead:.0f} T-{h:.1f}h")
            if open_by_series[series] >= max_open_per_series: break
    book.save()

def match(k: KalshiPublic, book: Book):
    """Fill resting orders from prints after post time; cancel resting orders inside the final hour (we are not a last-hour maker)."""
    now = time.time(); changed = False
    for o in book.items:
        if o["status"] != "resting": continue
        if now > o["close_ts"] - 3600: o["status"] = "cancelled"; changed = True; book.logline(f"CANCEL unfilled {o['ticker']}"); continue
        try: tr = k.get("/markets/trades", ticker=o["ticker"], limit=200).get("trades", [])
        except Exception: continue
        vol = 0.0
        for t in tr:
            if ts(t["created_time"]) <= o["t"]: continue
            yp = float(t["yes_price_dollars"]); cnt = float(t["count_fp"])
            if o["side"] == "sell_yes" and t["taker_side"] == "yes" and yp >= o["price"] - 1e-9: vol += cnt
            if o["side"] == "buy_yes" and t["taker_side"] == "no" and yp <= o["price"] + 1e-9: vol += cnt
        o["fill_prints"] = vol
        fillable = max(0.0, vol - o["queue_ahead"])
        if fillable >= o["qty"]:
            o["filled"] = o["qty"]; o["status"] = "filled"; o["fill_t"] = now; changed = True
            book.logline(f"FILLED {o['side']} {o['qty']}@{o['price']:.2f} {o['ticker']} (prints after post {vol:.0f}, queue {o['queue_ahead']:.0f})")
    if changed: book.save()

def settle(k: KalshiPublic, book: Book):
    changed = False
    for o in book.items:
        if o["status"] != "filled" or time.time() < o["close_ts"] + 600: continue
        try: m = k.get(f"/markets/{o['ticker']}")["market"]
        except Exception: continue
        res = m.get("result")
        if res in ("yes", "no"):
            q, p = o["qty"], o["price"]
            pnl = (q * p - (q if res == "yes" else 0)) if o["side"] == "sell_yes" else ((q if res == "yes" else 0) - q * p)
            o["status"] = "settled"; o["result"] = res; o["pnl"] = pnl; o["fee"] = 0.0; changed = True
            book.logline(f"SETTLED {o['ticker']} {o['side']}@{p:.2f} result={res} pnl={pnl:+.2f}")
    if changed: book.save()

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--loop", action="store_true"); ap.add_argument("--lot", type=int, default=20)
    ap.add_argument("--max-open", type=int, default=6); ap.add_argument("--poll", type=int, default=120); a = ap.parse_args()
    root = pathlib.Path("data/s1-maker"); _lock = single_instance(root); book = Book(root); k = KalshiPublic(min_interval_s=0.35)
    book.logline(f"start S1 maker paper lot={a.lot} max_open/series={a.max_open}")
    last_scan = 0
    while True:
        try:
            if time.time() - last_scan > 1800: scan(k, book, a.lot, a.max_open, pathlib.Path("data/universe.db")); last_scan = time.time()
            match(k, book); settle(k, book)
        except Exception as e: book.logline(f"error {type(e).__name__}: {str(e)[:160]}")
        if not a.loop: break
        time.sleep(a.poll)

if __name__ == "__main__": main()
