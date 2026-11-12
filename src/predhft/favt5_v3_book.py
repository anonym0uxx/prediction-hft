"""FavT5 v3 passive recorder: full Kalshi depth (snapshot + every delta) and every public trade print for the open
KXBTC15M and KXETH15M markets. READ-ONLY: it subscribes to market data and never sends an order.

Why: the v3 shadow must fill against sequence-validated displayed depth at measured arrival times, and the old live span
has no depth recording at all (see reports/favt5_v3/coverage_manifest.json). This closes that hole going forward.

Output  data/favt5-v3/book/<UTC day>/<ticker>.jsonl.gz   one row per websocket message, in arrival order:
          {"r": local receipt epoch (s), "s": frame sending_ts_ms, "q": subscription seq, "y": type, "m": message body}
        The body is stored as received (minus the ticker); nothing is aggregated, so any book state can be rebuilt.
        data/favt5-v3/book/feed.log.jsonl                 connects, subscriptions, sequence gaps, errors
Sequence rule: orderbook frames carry a per-subscription `seq`; a jump means lost deltas, so the gap is written into
every affected file ({"y": "gap"}) and the socket is reopened to get fresh snapshots. A replay must not use depth
between a gap row and the next snapshot. The system clock is not corrected; rows keep both local and exchange times.
"""
from __future__ import annotations
import asyncio, calendar, gzip, json, pathlib, time
import websockets
from .kalshi_auth import headers
from .kalshi_public import KalshiPublic

ROOT = pathlib.Path("data/favt5-v3/book"); WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"; PATH = "/trade-api/ws/v2"
SERIES = ("KXBTC15M", "KXETH15M"); CHANNELS = ["orderbook_delta", "trade"]
assert set(SERIES) == {"KXBTC15M", "KXETH15M"}, "v3 records BTC and ETH only"


def _log(row: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "feed.log.jsonl").open("a", encoding="utf-8") as f: f.write(json.dumps({"t": time.time(), **row}) + "\n")


def next_seq_ok(prev: int | None, seq: int | None) -> bool:
    """True when `seq` continues the subscription's sequence (or there is nothing to compare yet)."""
    return prev is None or seq is None or seq == prev + 1


class Tape:
    """One gzip stream per ticker per UTC day, flushed every few seconds so a crash loses at most that much."""
    def __init__(self) -> None: self.f: dict[str, gzip.GzipFile] = {}; self.last_flush = time.time()
    def write(self, ticker: str, row: dict) -> None:
        f = self.f.get(ticker)
        if f is None:
            d = ROOT / time.strftime("%Y%m%d", time.gmtime(row["r"])); d.mkdir(parents=True, exist_ok=True)
            f = self.f[ticker] = gzip.open(d / f"{ticker}.jsonl.gz", "ab", compresslevel=5)
        f.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
    def flush(self, now: float) -> None:
        if now - self.last_flush < 5: return
        self.last_flush = now
        for f in self.f.values(): f.flush()
    def close(self, tickers) -> None:
        for t in list(tickers):
            f = self.f.pop(t, None)
            if f: f.close()


def open_tickers(k: KalshiPublic) -> dict[str, float]:
    out = {}
    for ser in SERIES:
        for m in k.markets(series_ticker=ser, status="open", limit=50, max_pages=1):
            out[m["ticker"]] = calendar.timegm(time.strptime(m["close_time"], "%Y-%m-%dT%H:%M:%SZ"))
    return out


async def run() -> None:
    k = KalshiPublic(min_interval_s=0.5); tape = Tape(); backoff = 2
    while True:
        try:
            async with websockets.connect(WS, additional_headers=headers("GET", PATH), ping_interval=20, ping_timeout=20, max_size=2**24) as ws:
                _log({"event": "connected"}); backoff = 2
                subs: dict[str, float] = {}; sids: dict[int, str] = {}; seq: dict[int, int] = {}; next_id = 1; last_disc = 0.0

                async def ensure() -> None:
                    nonlocal next_id, last_disc
                    now = time.time()
                    if now - last_disc < 4: return
                    last_disc = now
                    try: live = await asyncio.to_thread(open_tickers, k)
                    except Exception as e: _log({"event": "discovery_error", "err": str(e)[:160]}); return
                    new = [t for t in live if t not in subs]
                    if new:
                        if not sids:
                            await ws.send(json.dumps({"id": next_id, "cmd": "subscribe", "params": {"channels": CHANNELS, "market_tickers": new}}))
                        else:
                            for sid in sids:                      # the exchange accepts exactly one subscription id per update
                                await ws.send(json.dumps({"id": next_id, "cmd": "update_subscription", "params": {"sids": [sid], "market_tickers": new, "action": "add_markets"}})); next_id += 1
                        next_id += 1; subs.update({t: live[t] for t in new}); _log({"event": "subscribe", "tickers": new})
                    old = [t for t, c in subs.items() if now > c + 45]
                    if old and sids:
                        for sid in sids:
                            await ws.send(json.dumps({"id": next_id, "cmd": "update_subscription", "params": {"sids": [sid], "market_tickers": old, "action": "delete_markets"}})); next_id += 1
                        for t in old: subs.pop(t, None)
                        tape.close(old)

                await ensure()
                while True:
                    try: raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError: await ensure(); tape.flush(time.time()); continue
                    r = time.time(); m = json.loads(raw); typ = m.get("type"); msg = m.get("msg") or {}
                    if typ == "subscribed": sids[msg.get("sid")] = msg.get("channel"); _log({"event": "subscribed", "msg": msg}); continue
                    if typ == "error": _log({"event": "error", "msg": m}); continue
                    if typ not in ("orderbook_snapshot", "orderbook_delta", "trade"): continue
                    tk = msg.pop("market_ticker", None); msg.pop("market_id", None)
                    if not tk: continue
                    sid, q = m.get("sid"), m.get("seq")
                    if typ != "trade":
                        if not next_seq_ok(seq.get(sid), q):
                            _log({"event": "seq_gap", "sid": sid, "expected": seq[sid] + 1, "got": q})
                            for t in subs: tape.write(t, {"r": r, "y": "gap", "expected": seq[sid] + 1, "got": q})
                            raise ConnectionError("orderbook sequence gap: reconnecting for fresh snapshots")
                        if q is not None: seq[sid] = q
                    tape.write(tk, {"r": r, "s": m.get("sending_ts_ms"), "q": q, "y": typ, "m": msg})
                    tape.flush(r)
                    if r - last_disc >= 4: await ensure()
        except Exception as e:
            _log({"event": "disconnect", "err": f"{type(e).__name__}: {str(e)[:200]}"})
            tape.close(list(tape.f)); await asyncio.sleep(backoff); backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    from .s7_live import single_instance
    _lock = single_instance(ROOT)
    asyncio.run(run())
