"""P19 open-quote recorder (scored-only, NO orders). At T-14.5 min before each 15M close (i.e. just after the window opens)
record the Kalshi top-of-book for every open KX*15M market, its strike, the official index value and partial average, and the
replica spot. Outcomes are joined later from the Kalshi settlement results (see scripts/score_open_quotes.py).
Output: data/open_quotes/quotes.jsonl (one row per market per snapshot). Pure observation: no trade, no ledger, no risk state."""
from __future__ import annotations
import json, pathlib, time, datetime as dt
from .kalshi_public import KalshiPublic, f
from .cfb_feed import latest as cfb_latest

OUT = pathlib.Path("data/open_quotes/quotes.jsonl")
SERIES = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL"}
OFFSET_S = 14.5 * 60          # snapshot at T-14.5 min (window open is T-15 min)


def _touch(ob: dict):
    """Best bid/ask on YES side from the orderbook payload (fp/dollars or cents; returns probabilities)."""
    book = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    def top(side):
        levels = book.get(side) or []
        if not levels: return None, None
        p, q = levels[-1][0], levels[-1][1]
        p = float(p); p = p / 100.0 if p > 1.0 else p
        return p, float(q)
    yb, ybq = top("yes_dollars") if "yes_dollars" in book else top("yes")
    no, noq = top("no_dollars") if "no_dollars" in book else top("no")
    ya = (1.0 - no) if no is not None else None
    return yb, ya, ybq, noq


def snapshot(k: KalshiPublic, now: float | None = None) -> int:
    now = now or time.time(); n = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for series, asset in SERIES.items():
        try:
            mkts = list(k.markets(series_ticker=series, status="open", max_pages=1))
        except Exception as e:
            rows.append({"t": now, "series": series, "error": f"markets: {str(e)[:120]}"}); continue
        cfb = cfb_latest(asset)
        for m in mkts:
            tk = m.get("ticker"); close = m.get("close_time")
            try: close_ts = dt.datetime.fromisoformat(close.replace("Z", "+00:00")).timestamp()
            except Exception: continue
            mins_left = (close_ts - now) / 60.0
            if not (13.5 <= mins_left <= 15.5):           # only the snapshot window of this close
                continue
            try:
                ob = k.orderbook(tk)
                yb, ya, ybq, noq = _touch(ob)
            except Exception as e:
                yb = ya = ybq = noq = None; ob = {"error": str(e)[:120]}
            rows.append({"t": now, "series": series, "ticker": tk, "close_ts": close_ts, "mins_left": round(mins_left, 3),
                         "strike": f(m, "floor_strike"), "yes_bid": yb, "yes_ask": ya, "yes_bid_qty": ybq, "no_bid_qty": noq,
                         "last_price": f(m, "last_price_dollars") or f(m, "last_price"), "volume": f(m, "volume"),
                         "cfb_value": (cfb or {}).get("value"), "cfb_partial": (cfb or {}).get("partial_twap"),
                         "cfb_age_s": round(now - cfb["t"], 2) if cfb else None, "orderbook_error": ob.get("error")})
            n += 1
    with OUT.open("a", encoding="utf-8") as fh:
        for r in rows: fh.write(json.dumps(r) + "\n")
    return n


def main():
    k = KalshiPublic(min_interval_s=0.12)
    while True:
        now = time.time()
        # next close is the next quarter hour; snapshot at close - 14.5 min
        nxt = (int(now // 900) + 1) * 900
        target = nxt - OFFSET_S
        if target < now: target += 900
        time.sleep(max(0.0, target - now))
        try:
            n = snapshot(k, time.time())
            print(time.strftime("%H:%M:%S"), "snapshot rows", n, flush=True)
        except Exception as e:
            print(time.strftime("%H:%M:%S"), "snapshot failed", str(e)[:200], flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
