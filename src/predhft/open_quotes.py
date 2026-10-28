"""P19 quote-PATH recorder (scored-only, NO orders). Supersedes the single T-14.5 min snapshot (review in
docs/ROBUST_BETTING_2026-10-27.md §1). For every open KX*15M market it records, every SAMPLE_S (30 s) from market open until
T-STOP_S (60 s; the final minute is covered by the runners' books.jsonl at ~1 s), one row with:
  top-of-book (YES bid/ask in probability space), top-3 depth on both sides, strike, official index value / settlement partial /
  feed age, replica value / age, trailing 1-min sigma from the official feed, market last price / volume.
Outcomes are joined later by scripts/score_quote_path.py (Kalshi market result). Touches no ledger, risk state or runner.
Output: data/open_quotes/quotes.jsonl (append; rows carry "v": 2; the two v1 rows from the first version are still readable).

Rate budget: one /markets listing per series per 15-min window (cached by close_ts, retried while empty) + one /orderbook per
market per sample = 3 requests / 30 s through the SHARED cross-process limiter (kalshi_public._shared_slot, 0.5 s slot, no
priority lane) -> ~0.1 req/s, and nothing inside the runners' final minute.
Usage: uv run python -m predhft.open_quotes [--interval 30] [--stop-s 60] [--once]
"""
from __future__ import annotations
import argparse, json, pathlib, time, datetime as dt
from .kalshi_public import KalshiPublic, f
from .feed_tail import cfb_recent, sigma_1m_from_rows

OUT = pathlib.Path("data/open_quotes/quotes.jsonl")
SERIES = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL"}
PERIOD_S = 900
SAMPLE_S = 30.0          # sample cadence
STOP_S = 60.0            # last sample at >= T-60 s: the final minute belongs to the runners (books.jsonl), never compete with them
DEPTH = 3

# ------------------------------------------------------------------------------------------------------------- orderbook parsing
def _levels(raw, in_dollars: bool) -> list[tuple[float, float]]:
    out = []
    for lv in raw or []:
        try: p, q = float(lv[0]), float(lv[1])
        except (TypeError, ValueError, IndexError): continue
        if not in_dollars: p = p / 100.0
        if 0.0 < p < 1.0 and q > 0: out.append((p, q))
    return out

def parse_book(ob: dict, depth: int = DEPTH) -> dict:
    """YES-space top-of-book and depth from a Kalshi orderbook payload. Never relies on level ORDER (Kalshi returns ascending
    price today, but that is not a contract): best bid = max price. YES asks are derived from NO bids: ask = 1 - no_bid.
    Returns {yes_bid, yes_ask, yes_bid_size, yes_ask_size, bids[(p,q) desc], asks[(p,q) asc], bid_depth, ask_depth, ok}."""
    book = ob.get("orderbook_fp") if isinstance(ob.get("orderbook_fp"), dict) else None
    if book is not None: yes, no = _levels(book.get("yes_dollars"), True), _levels(book.get("no_dollars"), True)
    else:
        book = ob.get("orderbook") if isinstance(ob.get("orderbook"), dict) else {}
        yes, no = _levels(book.get("yes"), False), _levels(book.get("no"), False)
    bids = sorted(yes, key=lambda x: -x[0])[:depth]                        # best YES bid first
    asks = sorted(((round(1.0 - p, 4), q) for p, q in no), key=lambda x: x[0])[:depth]   # best YES ask first
    yb, ybs = (bids[0] if bids else (None, 0.0)); ya, yas = (asks[0] if asks else (None, 0.0))
    return {"yes_bid": yb, "yes_ask": ya, "yes_bid_size": ybs, "yes_ask_size": yas, "bids": bids, "asks": asks,
            "bid_depth": round(sum(q for _, q in bids), 2), "ask_depth": round(sum(q for _, q in asks), 2), "ok": bool(yes or no)}

def close_ts_of(m: dict) -> float | None:
    try: return dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
    except Exception: return None

# ------------------------------------------------------------------------------------------------------------------- recorder
class Recorder:
    def __init__(self, k: KalshiPublic | None = None, out: pathlib.Path = OUT, stop_s: float = STOP_S, depth: int = DEPTH):
        self.k = k or KalshiPublic(min_interval_s=0.5); self.out = out; self.stop_s = stop_s; self.depth = depth
        self.markets: dict[tuple[str, int], dict | None] = {}     # (series, close_ts) -> market dict (None = not listed yet)
        self.sigma: dict[str, tuple[float, float | None]] = {}      # asset -> (t_computed, sigma1)

    def market_for(self, series: str, close_ts: int) -> dict | None:
        key = (series, close_ts)
        if self.markets.get(key): return self.markets[key]
        try: ms = list(self.k.markets(series_ticker=series, status="open", max_pages=1))
        except Exception as e:
            self.markets.setdefault(key, None); self._err(series, close_ts, f"markets: {str(e)[:120]}"); return None
        for m in ms:
            c = close_ts_of(m)
            if c is not None and m.get("floor_strike") is not None: self.markets[(series, int(c))] = m
        self.markets.setdefault(key, None)
        for old in [kk for kk in self.markets if kk[1] < close_ts - 2 * PERIOD_S]: self.markets.pop(old, None)
        return self.markets.get(key)

    def sigma_1m(self, asset: str, now: float) -> float | None:
        t, s = self.sigma.get(asset, (0.0, None))
        if now - t < 300: return s
        try: s = sigma_1m_from_rows(cfb_recent(asset, 150 * 60, now))
        except Exception: s = None
        self.sigma[asset] = (now, s); return s

    def _err(self, series: str, close_ts: int, msg: str):
        self._write({"v": 2, "t": time.time(), "series": series, "close_ts": close_ts, "error": msg})

    def _write(self, row: dict):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        with self.out.open("a", encoding="utf-8") as fh: fh.write(json.dumps(row) + "\n")

    def sample(self, now: float | None = None) -> int:
        """One pass over the three series; returns rows written. Skips the final `stop_s` seconds of each window."""
        now = now or time.time(); close_ts = (int(now) // PERIOD_S + 1) * PERIOD_S; secs_left = close_ts - now
        if secs_left < self.stop_s: return 0
        n = 0
        for series, asset in SERIES.items():
            m = self.market_for(series, close_ts)
            if not m: continue
            tk = m["ticker"]
            try: ob = self.k.orderbook(tk); book = parse_book(ob, self.depth); ob_err = None
            except Exception as e: book = parse_book({}, self.depth); ob_err = str(e)[:120]
            t_ob = time.time()
            cfb = rep = None
            try:
                from .cfb_feed import latest as cfb_latest; cfb = cfb_latest(asset, max_age=10.0)
            except Exception: cfb = None
            try:
                from .index_replica import latest as rep_latest; rep = rep_latest(asset, max_age=10.0)
            except Exception: rep = None
            row = {"v": 2, "t": t_ob, "series": series, "asset": asset, "ticker": tk, "close_ts": close_ts, "secs_left": round(close_ts - t_ob, 1),
                   "mins_left": round((close_ts - t_ob) / 60.0, 3), "strike": f(m, "floor_strike"),
                   "yes_bid": book["yes_bid"], "yes_ask": book["yes_ask"], "yes_bid_size": book["yes_bid_size"], "yes_ask_size": book["yes_ask_size"],
                   "bids": book["bids"], "asks": book["asks"], "bid_depth": book["bid_depth"], "ask_depth": book["ask_depth"],
                   "last_price": f(m, "last_price_dollars") or f(m, "last_price"), "volume": f(m, "volume_fp") or f(m, "volume"),
                   "cfb_value": (cfb or {}).get("value"), "cfb_partial": (cfb or {}).get("partial_twap"), "cfb_partial_n": (cfb or {}).get("partial_n"),
                   "cfb_age_s": (round(t_ob - cfb["t"], 2) if cfb else None), "replica": (rep or {}).get("replica"), "replica_age_s": (round(t_ob - rep["t"], 2) if rep else None),
                   "sigma1": self.sigma_1m(asset, t_ob), "orderbook_error": ob_err}
            self._write(row); n += 1
        return n

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--interval", type=float, default=SAMPLE_S); ap.add_argument("--stop-s", type=float, default=STOP_S)
    ap.add_argument("--once", action="store_true"); a = ap.parse_args()
    rec = Recorder(stop_s=a.stop_s)
    if a.once: print("rows", rec.sample()); return
    while True:
        now = time.time(); nxt = (int(now // a.interval) + 1) * a.interval      # align to the 30-s grid so samples line up across days
        time.sleep(max(0.0, nxt - now))
        try: n = rec.sample(time.time()); print(time.strftime("%H:%M:%S"), "rows", n, flush=True)
        except Exception as e: print(time.strftime("%H:%M:%S"), "sample failed", type(e).__name__, str(e)[:200], flush=True); time.sleep(5)

if __name__ == "__main__":
    main()
