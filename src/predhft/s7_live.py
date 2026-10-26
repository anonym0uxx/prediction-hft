"""S7 live PAPER runner (no account, no orders). Polls Kalshi public order books for near-the-money KXBTCD strikes in the final
minutes of each hour, computes the spot-distance model, and paper-fills a taker buy against the size actually resting at the touch.
Logs every book snapshot and paper trade; settles from the official market result. CPU only.

Usage: uv run python -m predhft.s7_live --thr 0.05 --t-open 8 --t-close 2 --lot 20
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, pathlib, statistics as st, time, urllib.request
from .kalshi_public import KalshiPublic, f
from .fees import kalshi_taker_fee

CB_BASE = "https://api.exchange.coinbase.com/products"
def utcnow() -> float: return time.time()
def ts(s: str) -> int: return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
def Phi(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
def twap_horizon(mins_left: float) -> float:
    """Settlement is the mean of the index over the final minute (rules card): Var(mean_[T-1,T] - S_t) = sigma^2 (tau - 1 + 1/3)."""
    return max(mins_left - 2.0 / 3.0, 0.05)
def jget(u):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "predhft"}), timeout=10).read())

class State:
    def __init__(self, root: pathlib.Path):
        self.root = root; root.mkdir(parents=True, exist_ok=True)
        self.trades = root / "trades.jsonl"; self.books = root / "books.jsonl"; self.log = root / "runner.log"
        self.open_positions: dict[str, dict] = {}
        if self.trades.exists():
            for l in self.trades.read_text().splitlines():
                t = json.loads(l)
                if t.get("status") == "open": self.open_positions[t["ticker"]] = t
    def logline(self, m):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {m}"
        print(line, flush=True); self.log.open("a").write(line + "\n")
    def append(self, path, obj): path.open("a").write(json.dumps(obj) + "\n")
    def rewrite_trades(self, trades): self.trades.write_text("".join(json.dumps(t) + "\n" for t in trades))

def sigma_1m(product: str = "BTC-USD") -> float | None:
    end = dt.datetime.now(dt.timezone.utc); start = end - dt.timedelta(minutes=180)
    d = jget(f"{CB_BASE}/{product}/candles?granularity=60&start={start.isoformat()}&end={end.isoformat()}")
    px = [float(c[4]) for c in sorted(d)]
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
    return st.pstdev(rets[-120:]) if len(rets) >= 60 else None

def book_touch(ob: dict):
    """Return (yes_bid, yes_ask, yes_bid_size, yes_ask_size) in dollars/contracts from orderbook_fp."""
    o = ob.get("orderbook_fp") or {}
    yes = [(float(p), float(q)) for p, q in (o.get("yes_dollars") or [])]
    no = [(float(p), float(q)) for p, q in (o.get("no_dollars") or [])]
    yb, ybs = (max(yes)[0], max(yes)[1]) if yes else (0.0, 0.0)
    if no:
        nb = max(no); ya, yas = 1 - nb[0], nb[1]
    else: ya, yas = 1.0, 0.0
    return yb, ya, ybs, yas

def settle(k: KalshiPublic, state: State):
    if not state.open_positions: return
    trades = [json.loads(l) for l in state.trades.read_text().splitlines()]
    changed = False
    for t in trades:
        if t.get("status") != "open" or utcnow() < t["close_ts"] + 120: continue
        try: m = k.get(f"/markets/{t['ticker']}")["market"]
        except Exception: continue
        res = m.get("result")
        if res in ("yes", "no"):
            win = (res == t["side"])
            t["status"] = "settled"; t["result"] = res; t["pnl"] = (1.0 if win else 0.0) * t["qty"] - t["cost"] - t["fee"]
            state.open_positions.pop(t["ticker"], None); changed = True
            state.logline(f"SETTLED {t['ticker']} side={t['side']} result={res} qty={t['qty']} pnl={t['pnl']:+.2f}")
    if changed: state.rewrite_trades(trades)

def single_instance(root: pathlib.Path):
    """Windows-safe exclusive lock: a file opened with O_EXCL-style semantics via msvcrt locking; held for process lifetime."""
    import os, msvcrt
    root.mkdir(parents=True, exist_ok=True)
    fh = open(root / "runner.lock", "a+")
    try: msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("another runner holds the lock; exiting", flush=True); raise SystemExit(0)
    return fh

def run(thr: float, t_open: int, t_close: int, lot: int, poll_s: float, root: pathlib.Path, series: str = "KXBTCD", product: str = "BTC-USD"):
    _lock = single_instance(root)
    k = KalshiPublic(min_interval_s=0.12); state = State(root); sig = None; sig_ts = 0
    state.logline(f"start {series}/{product} thr={thr} window=T-{t_open}..T-{t_close}min lot={lot}")
    while True:
        try:
            now = utcnow(); next_hour = (int(now) // 3600 + 1) * 3600; mins_left = (next_hour - now) / 60
            if mins_left > t_open + 1 or mins_left < t_close:
                settle(k, state); time.sleep(min(20, max(2, (mins_left - t_open) * 60) if mins_left > t_open else 5)); continue
            if now - sig_ts > 900:
                s = sigma_1m(product); sig = s or sig; sig_ts = now
            close_iso = dt.datetime.fromtimestamp(next_hour, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            spot = float(jget(f"{CB_BASE}/{product}/ticker")["price"])
            cands = [m for m in k.markets(series_ticker=series, status="open", limit=1000, max_pages=2) if m["close_time"] == close_iso and m.get("floor_strike")]
            # near the money: within 4 sigma*sqrt(t) in log space
            band = 4 * (sig or 0.0006) * math.sqrt(max(mins_left, 0.5))
            near = [m for m in cands if abs(math.log(spot / float(m["floor_strike"]))) <= band]
            for m in near:
                K = float(m["floor_strike"]); tk = m["ticker"]
                try: ob = k.orderbook(tk)
                except Exception: continue
                yb, ya, ybs, yas = book_touch(ob)
                p = Phi(math.log(spot / K) / ((sig or 0.0006) * math.sqrt(twap_horizon(mins_left))))
                snap = {"t": now, "mins_left": round(mins_left, 2), "ticker": tk, "K": K, "spot": spot, "sigma1": sig, "p_model": p,
                        "yes_bid": yb, "yes_ask": ya, "bid_size": ybs, "ask_size": yas}
                state.append(state.books, snap)
                if tk in state.open_positions or not (t_close <= mins_left <= t_open): continue
                side = None
                if p - ya >= thr and 0 < ya < 1 and yas > 0: side, price, avail = "yes", ya, yas
                elif (yb - p) >= thr and 0 < yb < 1 and ybs > 0: side, price, avail = "no", 1 - yb, ybs
                if side:
                    qty = int(min(lot, avail)); cost = qty * price; fee = kalshi_taker_fee(qty, price)
                    t = {"t": now, "ticker": tk, "close_ts": next_hour, "side": side, "price": price, "qty": qty, "avail_at_touch": avail,
                         "cost": cost, "fee": fee, "p_model": p, "spot": spot, "K": K, "mins_left": round(mins_left, 2), "status": "open"}
                    state.append(state.trades, t); state.open_positions[tk] = t
                    state.logline(f"PAPER BUY {side.upper()} {qty}@{price:.2f} {tk} p_model={p:.2f} avail={avail:.0f} T-{mins_left:.1f}m")
            time.sleep(poll_s)
        except KeyboardInterrupt: raise
        except Exception as e:
            state.logline(f"error {type(e).__name__}: {str(e)[:160]}"); time.sleep(5)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--thr", type=float, default=0.05); ap.add_argument("--t-open", type=int, default=8)
    ap.add_argument("--t-close", type=int, default=2); ap.add_argument("--lot", type=int, default=20); ap.add_argument("--poll", type=float, default=4.0); ap.add_argument("--series", default="KXBTCD"); ap.add_argument("--product", default="BTC-USD")
    a = ap.parse_args(); root = pathlib.Path("data/s7live") if a.series == "KXBTCD" else pathlib.Path(f"data/s7live-{a.series}")
    run(a.thr, a.t_open, a.t_close, a.lot, a.poll, root, a.series, a.product)
