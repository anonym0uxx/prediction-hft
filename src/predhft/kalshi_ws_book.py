"""Read-only Kalshi websocket orderbook subscriber for the live KXSOL15M market (SOL-v1 streaming book).

Authenticated like cfb_feed (Ed25519 headers; the only use of the key), channel `orderbook_delta`. Maintains the full book
from `orderbook_snapshot` + `orderbook_delta` messages and writes, per message, data/sol-v1/book/<UTC day>.jsonl
{t (local receipt), ticker, seq, type, yes_bid, yes_ask, bids[(yes_px,qty)x5 best first], asks[...x5], n_levels} and
data/sol-v1/book/latest.json (same, read by sol_v1._ws_book when fresh). Receipt timestamps are local wall-clock at message
arrival.

Market rollover: every loop iteration the next close boundary is computed; when a new KXSOL15M market appears (floor_strike
set), its ticker is added to the subscription (update_subscription add_markets) and the previous one removed after close.
Prices: snapshots carry `yes_dollars_fp`/`no_dollars_fp` = [[price_str, qty_str], ...] (both sides are BIDS in their own price
space); deltas carry `price_dollars`, `delta_fp`, `side`, `ts_ms` (exchange event time) and every frame has `sending_ts_ms`.
Both lags (local receipt - sending, local receipt - event) are recorded per row; the system clock offset is NOT corrected.
NO cents/fractional field is ever written as a trade; this module never sends an order.
"""
from __future__ import annotations
import asyncio, datetime as dt, json, pathlib, time
import websockets
from .kalshi_auth import headers
from .kalshi_public import KalshiPublic

ROOT = pathlib.Path("data/sol-v1/book"); WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"; PATH = "/trade-api/ws/v2"
SERIES = "KXSOL15M"; PERIOD_S = 900; DEPTH = 5

def _log(row: dict):
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "feed.log.jsonl").open("a", encoding="utf-8") as f: f.write(json.dumps(row) + "\n")

def _px(level, key_dollars: str, key_cents: str | None = None):
    """Level price in dollars from a dict level {price_dollars|price} or a list level [price, qty]."""
    if isinstance(level, dict):
        v = level.get(key_dollars)
        if v is not None: return float(v)
        c = level.get(key_cents or "price")
        return (float(c) / 100) if c is not None else None
    p = level[0]
    p = float(p); return p / 100 if p > 1 else p

def _qty(level):
    if isinstance(level, dict):
        for k in ("quantity_fp", "delta_fp", "quantity", "delta", "size"):
            if level.get(k) is not None: return float(level[k])
        return 0.0
    return float(level[1])

class Book:
    """Per-ticker book in Kalshi's native form: yes bids keyed by YES price, no bids keyed by NO price (both are BIDS).
    YES ask = 1 - best NO bid. Snapshot replaces; delta adds (negative delta removes)."""
    def __init__(self, ticker: str): self.ticker = ticker; self.yes: dict[float, float] = {}; self.no: dict[float, float] = {}; self.seq = None; self.last_top = None; self.last_write = 0.0
    def snapshot(self, msg: dict):
        self.yes = {}; self.no = {}
        for side, store in (("yes", self.yes), ("no", self.no)):
            lv = msg.get(f"{side}_dollars_fp") or msg.get(f"{side}_dollars") or msg.get(side) or []
            for l in lv:
                p = _px(l, "price_dollars"); q = _qty(l)
                if p is not None and q > 0: store[round(p, 4)] = q
    def delta(self, msg: dict):
        side = msg.get("side"); store = self.yes if side == "yes" else self.no
        p = msg.get("price_dollars"); p = float(p) if p is not None else (float(msg.get("price", 0)) / 100)
        d = msg.get("delta_fp"); d = float(d) if d is not None else float(msg.get("delta", 0))
        p = round(p, 4); q = store.get(p, 0.0) + d
        if q <= 1e-9: store.pop(p, None)
        else: store[p] = q
    def view(self, depth: int = DEPTH) -> dict:
        bids = sorted(self.yes.items(), reverse=True)[:depth]
        asks = sorted(((round(1 - p, 4), q) for p, q in self.no.items()))[:depth]
        return {"yes_bid": (bids[0][0] if bids else None), "yes_ask": (asks[0][0] if asks else None), "bids": [[p, q] for p, q in bids], "asks": [[p, q] for p, q in asks],
                "n_levels": len(self.yes) + len(self.no)}

def current_ticker(k: KalshiPublic, close_ts: int) -> str | None:
    iso = dt.datetime.fromtimestamp(close_ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for m in k.markets(series_ticker=SERIES, status="open", limit=200, max_pages=2):
        if m.get("close_time") == iso and m.get("floor_strike"): return m["ticker"]
    return None

async def run():
    k = KalshiPublic(min_interval_s=0.5); backoff = 2; books: dict[str, Book] = {}
    while True:
        try:
            async with websockets.connect(WS, additional_headers=headers("GET", PATH), ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                _log({"t": time.time(), "event": "connected"}); backoff = 2
                subscribed: set[str] = set(); sid = None; next_id = 1; last_disc = 0.0
                async def ensure_subscription():
                    nonlocal sid, next_id, last_disc
                    now = time.time()
                    if now - last_disc < 5: return
                    last_disc = now; close_ts = (int(now) // PERIOD_S + 1) * PERIOD_S
                    try: tk = await asyncio.to_thread(current_ticker, k, close_ts)
                    except Exception as e: _log({"t": now, "event": "discovery_error", "err": str(e)[:160]}); return
                    if not tk or tk in subscribed: return
                    if sid is None:
                        await ws.send(json.dumps({"id": next_id, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": [tk]}}))
                    else:
                        await ws.send(json.dumps({"id": next_id, "cmd": "update_subscription", "params": {"sids": [sid], "market_tickers": [tk], "action": "add_markets"}}))
                    next_id += 1; subscribed.add(tk); books[tk] = Book(tk); _log({"t": now, "event": "subscribe", "ticker": tk})
                    stale = [t for t in subscribed if t != tk and books.get(t) and _close_of(t) is not None and now > _close_of(t) + 30]
                    if stale and sid is not None:
                        await ws.send(json.dumps({"id": next_id, "cmd": "update_subscription", "params": {"sids": [sid], "market_tickers": stale, "action": "delete_markets"}})); next_id += 1
                        for t in stale: subscribed.discard(t); books.pop(t, None)
                await ensure_subscription()
                while True:
                    try: raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError: await ensure_subscription(); continue
                    t = time.time(); m = json.loads(raw); typ = m.get("type")
                    if typ == "subscribed": sid = (m.get("msg") or {}).get("sid", sid); _log({"t": t, "event": "subscribed", "msg": m}); continue
                    if typ == "error": _log({"t": t, "event": "error", "msg": m}); continue
                    if typ not in ("orderbook_snapshot", "orderbook_delta"): continue
                    msg = m.get("msg") or {}; tk = msg.get("market_ticker")
                    if tk not in books: books[tk] = Book(tk)
                    b = books[tk]
                    if typ == "orderbook_snapshot": b.snapshot(msg)
                    else: b.delta(msg)
                    b.seq = m.get("seq")
                    ex_ms = msg.get("ts_ms"); snd_ms = m.get("sending_ts_ms")
                    view = b.view()
                    row = {"t": t, "recv_t": t, "ticker": tk, "seq": b.seq, "type": typ, "exchange_ts_ms": ex_ms, "sending_ts_ms": snd_ms,
                           "lag_send_ms": (int(t * 1000) - int(snd_ms)) if snd_ms else None, "lag_event_ms": (int(t * 1000) - int(ex_ms)) if ex_ms else None, **view}
                    ROOT.mkdir(parents=True, exist_ok=True)
                    # tape: only when the displayed top-3 changes, at most every 0.2 s (the raw delta stream is ~10 MB / 2 min)
                    top = (tuple(map(tuple, view["bids"][:3])), tuple(map(tuple, view["asks"][:3])))
                    if top != b.last_top and t - b.last_write >= 0.2:
                        b.last_top = top; b.last_write = t
                        with (ROOT / f"{time.strftime('%Y%m%d', time.gmtime(t))}.jsonl").open("a", encoding="utf-8") as f: f.write(json.dumps(row) + "\n")
                    if _close_of(tk) == (int(t) // PERIOD_S + 1) * PERIOD_S:
                        tmp = ROOT / "latest.json.tmp"; tmp.write_text(json.dumps(row), encoding="utf-8")
                        try: tmp.replace(ROOT / "latest.json")
                        except OSError: pass
                    await ensure_subscription()
        except Exception as e:
            _log({"t": time.time(), "event": "disconnect", "err": f"{type(e).__name__}: {str(e)[:200]}"}); await asyncio.sleep(backoff); backoff = min(backoff * 2, 60)

def _close_of(ticker: str) -> int | None:
    """KXSOL15M-26OCT281630-30 -> close ts of 2026-10-28 16:30 America/New_York (Kalshi tickers are stamped in ET)."""
    try:
        from zoneinfo import ZoneInfo
        part = ticker.split("-")[1]; d = dt.datetime.strptime(part[:-4], "%y%b%d")
        hh, mm = int(part[-4:-2]), int(part[-2:]); return int(d.replace(hour=hh, minute=mm, tzinfo=ZoneInfo("America/New_York")).timestamp())
    except Exception: return None

if __name__ == "__main__":
    from .s7_live import single_instance
    _lock = single_instance(ROOT)          # one subscriber only (data/sol-v1/book/runner.lock)
    asyncio.run(run())
