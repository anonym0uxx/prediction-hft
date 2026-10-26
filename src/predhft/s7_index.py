"""S7-index: late-window paper runner for Kalshi S&P 500 / Nasdaq-100 end-of-day strike markets (KXINXU, KXNASDAQ100U).
Settlement = official index close (4:00 PM ET). Spot = Yahoo 1-min bars for ^GSPC / ^NDX (latency logged; if the last bar is
older than 90 s the runner stands down). Window T-8..T-2 min before the close; closing-auction noise added to variance.
Usage: uv run python -m predhft.s7_index --series KXINXU --symbol ^GSPC --lot 20
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, pathlib, statistics as st, time, urllib.request, urllib.parse
from zoneinfo import ZoneInfo
from .kalshi_public import KalshiPublic, f
from .fees import kalshi_taker_fee
from .s7_live import State, Phi, book_touch, settle, single_instance
from .risk import Ledger, size as risk_size

H = {"User-Agent": "Mozilla/5.0"}
ET = ZoneInfo("America/New_York")
AUCTION_BP = 0.0005   # extra std-dev (5 bp) for closing-auction repricing vs last pre-close print

def yahoo_bars(symbol: str):
    u = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1m&range=1d"
    r = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=15).read())["chart"]["result"][0]
    ts = r["timestamp"]; cl = r["indicators"]["quote"][0]["close"]
    bars = [(t, c) for t, c in zip(ts, cl) if c]
    return bars, r["meta"].get("regularMarketPrice"), r["meta"].get("regularMarketTime")

def run(series: str, symbol: str, thr: float, t_open: int, t_close: int, lot: int, poll_s: float, root: pathlib.Path):
    _lock = single_instance(root); k = KalshiPublic(min_interval_s=0.2); state = State(root)
    state.logline(f"start {series}/{symbol} thr={thr} window=T-{t_open}..T-{t_close}min lot={lot}")
    while True:
        try:
            now = dt.datetime.now(ET); close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
            if now.weekday() >= 5 or now > close_dt + dt.timedelta(minutes=1) or now < close_dt - dt.timedelta(minutes=t_open + 2):
                settle(k, state); time.sleep(60); continue
            mins_left = (close_dt - now).total_seconds() / 60
            bars, rmp, rmt = yahoo_bars(symbol)
            if not bars: time.sleep(poll_s); continue
            last_t, spot = bars[-1]; age = time.time() - last_t
            if rmp and rmt and rmt > last_t: spot, age = rmp, time.time() - rmt
            rets = [math.log(bars[i][1] / bars[i - 1][1]) for i in range(max(1, len(bars) - 120), len(bars))]
            sig = st.pstdev(rets) if len(rets) > 30 else 0.0004
            if age > 90: state.logline(f"feed stale {age:.0f}s — standing down this tick"); time.sleep(poll_s); continue
            close_iso = close_dt.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            cands = [m for m in k.markets(series_ticker=series, status="open", limit=200, max_pages=1) if m["close_time"] == close_iso and m.get("floor_strike") is not None]
            for m in cands:
                K = float(m["floor_strike"]); tk = m["ticker"]
                sd = math.sqrt(sig ** 2 * max(mins_left, 0.25) + AUCTION_BP ** 2)
                if abs(math.log(spot / K)) > 4 * sd: continue
                try: yb, ya, ybs, yas = book_touch(k.orderbook(tk))
                except Exception: continue
                p = Phi(math.log(spot / K) / sd)
                state.append(state.books, {"t": time.time(), "mins_left": round(mins_left, 2), "ticker": tk, "K": K, "spot": spot, "feed_age_s": round(age, 1), "sigma1": sig,
                                           "p_model": p, "yes_bid": yb, "yes_ask": ya, "bid_size": ybs, "ask_size": yas})
                if tk in state.open_positions or not (t_close <= mins_left <= t_open): continue
                side = None
                if p - ya >= thr and 0 < ya < 1 and yas > 0: side, price, avail = "yes", ya, yas
                elif yb - p >= thr and 0 < yb < 1 and ybs > 0: side, price, avail = "no", 1 - yb, ybs
                if side:
                    qty, why = risk_size(series, p if side == "yes" else 1 - p, price, avail, Ledger(root), lot_cap=lot)
                    if qty <= 0: state.logline(f"SKIP {tk} {side} {why}"); continue
                    cost = qty * price; fee = kalshi_taker_fee(qty, price)
                    t = {"t": time.time(), "ticker": tk, "series": series, "close_ts": close_dt.timestamp(), "side": side, "price": price, "qty": qty, "avail_at_touch": avail,
                         "cost": cost, "fee": fee, "p_model": p, "spot": spot, "K": K, "mins_left": round(mins_left, 2), "feed_age_s": round(age, 1), "status": "open", "risk": why}
                    state.append(state.trades, t); state.open_positions[tk] = t
                    state.logline(f"PAPER BUY {side.upper()} {qty}@{price:.2f} {tk} p={p:.2f} spot={spot:.2f} K={K:.2f} T-{mins_left:.1f}m age={age:.0f}s")
            time.sleep(poll_s)
        except KeyboardInterrupt: raise
        except Exception as e:
            state.logline(f"error {type(e).__name__}: {str(e)[:160]}"); Ledger(root).record_error(str(e)); time.sleep(10)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--series", default="KXINXU"); ap.add_argument("--symbol", default="^GSPC")
    ap.add_argument("--thr", type=float, default=0.05); ap.add_argument("--t-open", type=int, default=8); ap.add_argument("--t-close", type=int, default=2)
    ap.add_argument("--lot", type=int, default=20); ap.add_argument("--poll", type=float, default=10.0); a = ap.parse_args()
    run(a.series, a.symbol, a.thr, a.t_open, a.t_close, a.lot, a.poll, pathlib.Path(f"data/s7index-{a.series}"))
