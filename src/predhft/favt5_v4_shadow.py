"""FavT5 v4 SHADOW runner. Paper measurement only: this module has no order route and never signs a trading request.

Provenance (research/favt5_v4/RULE.md): mirror of a Polymarket profile ("blue.birds", 0xb958...f513,
verified live 2026-11-22: +$103.9K PnL on 39,928 markets, $13.2M volume) that buys the LESS-LIKELY side of
Bitcoin/Ethereum "Up or Down" windows at 20-35c and holds to settlement. Kalshi has no 5-minute series
(KXBTC5M/KXBTC5MIN/BTC5M all 404), so the analogue is the 15-minute series already on our stack.

Rule (frozen v4.0), evaluated once per market per timing cohort, at decision D = close - H:
  cohorts      early_t300 (H=300) and late_t090 (H=90) — the timing experiment is part of the design
  quote        YES bid/ask from the sequence-validated book; last frame for the SAME ticker, event <= D, age <= 3 s
  mid          (bid+ask)/2
  side         YES if mid < 0.5000 else NO  — the side the market considers less likely
  offer        best displayed ask for that side (resting opposite bids)
  band         offer in [0.20, 0.35] inclusive, else skip (not_in_band, the most common decision)
  limit        offer + 0.01 (one adverse cent), capped below 1.00
  size         paper budget $50.00: unit = limit + taker fee 0.07*limit*(1-limit); qty = floor(50/unit); never zero here
  execution    hold to settlement ($1 or $0); no stop, no exit, one decision per market per cohort
Shadow execution of an intent (same mechanics as v3):
  arrival      'measured' = intent ready + half the measured exchange round trip; replays of the same intent at D+1, +5, +30 s
  fill         walk displayed offers <= limit in the book as of that exchange time; partial fills kept; sequence gap = 'unknown'
  fees         exchange per-fill rounding up to $0.0001, applied to every simulated fill
Skip rows always carry the numbers behind them (offer, mid, quote ages) so 'not trading' is never silent.
Ledger data/favt5-v4/fills.jsonl: start | meta | decision | fill | settle | alert. Keys `<cohort>:<series>:<close>`
reloaded at start, so restart/reconnect cannot repeat one; a boundary passed while down is 'missed', never made up.
"""
from __future__ import annotations
import asyncio, calendar, collections, json, math, os, pathlib, socket, struct, time, urllib.request
from decimal import Decimal, ROUND_CEILING
import websockets
from .kalshi_auth import headers

ROOT = pathlib.Path("data/favt5-v4")
API = "https://api.elections.kalshi.com/trade-api/v2"; WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"; WS_PATH = "/trade-api/ws/v2"
COHORTS = {"early_t300": 300, "late_t090": 90}
SERIES = ("KXBTC15M", "KXETH15M"); DELAYS = (1, 5, 30); LATE_S = 1.0
BAND_LO, BAND_HI = Decimal("0.20"), Decimal("0.35")
SHADOW_BUDGET = Decimal("50.00")
RULE = {"version": "v4.0 frozen 2026-11-22", "band": [0.20, 0.35], "side": "less-likely by mid", "cohorts": COHORTS,
        "budget": 50.0, "limit": "offer + 0.01", "hold": "to settlement", "order_route": None}
OFFSET = 0.0
RTT: collections.deque = collections.deque(maxlen=9)


def now() -> float: return time.time() + OFFSET
def key(cohort: str, series: str, close: int) -> str: return f"{cohort}:{series}:{int(close)}"
def exch_fee(n: float, p: float) -> float: return math.ceil(0.07 * n * p * (1 - p) * 1e4 - 1e-9) / 1e4

def clean(x):
    """NaN/inf are not JSON; a missing number is written as null."""
    if isinstance(x, float): return x if math.isfinite(x) else None
    if isinstance(x, dict): return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)): return [clean(v) for v in x]
    return x

def _write(row: dict, name: str = "fills.jsonl") -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / name).open("a", encoding="utf-8") as f: f.write(json.dumps(clean({"t": time.time(), **row}), allow_nan=False) + "\n")


def less_likely_side(bid: float, ask: float) -> str:
    """The side the market prices below 0.50; a dead tie goes NO (arbitrary, recorded as mid exactly 0.5)."""
    return "YES" if (bid + ask) / 2 < 0.5 else "NO"

def intent_for(bid: float, ask: float, offers: list[tuple[float, float]]) -> dict | None:
    """Mechanical intent: less-likely side in the 20-35c band, one adverse cent, sized to the $50 paper budget."""
    side = less_likely_side(bid, ask)
    if not offers: return None
    offer = Decimal(str(offers[0][0])); why = None
    if not (BAND_LO <= offer <= BAND_HI): return None
    limit = min(float(offer) + 0.01, 0.999)
    unit = limit + 0.07 * limit * (1 - limit)
    qty = math.floor(float(SHADOW_BUDGET) / unit)
    if qty < 1: return None
    cost = math.ceil(unit * qty * 100 - 1e-8) / 100
    return {"side": side, "offer": float(offer), "limit": round(limit, 4), "qty": qty, "max_total_cost": cost,
            "modeled_fee": round(cost - qty * limit, 4)}


class Book:
    """Displayed depth for one ticker, rebuilt from a snapshot plus every delta. `valid` is False until a snapshot
    arrives and after any lost sequence number."""
    def __init__(self) -> None:
        self.side = {"yes": {}, "no": {}}; self.valid = False; self.event = float("nan"); self.recv = float("nan"); self.trades = collections.deque(maxlen=4000)
    def snapshot(self, msg: dict, event: float, recv: float) -> None:
        for s in ("yes", "no"): self.side[s] = {round(float(p), 4): float(q) for p, q in msg.get(f"{s}_dollars_fp") or []}
        self.valid = True; self.event, self.recv = event, recv
    def delta(self, msg: dict, event: float, recv: float) -> None:
        d = self.side[msg["side"]]; p = round(float(msg["price_dollars"]), 4); q = d.get(p, 0.0) + float(msg["delta_fp"])
        if q > 1e-9: d[p] = q
        else: d.pop(p, None)
        self.event, self.recv = event, recv
    def top(self) -> tuple[float, float] | None:
        """YES bid and YES ask (the ask is one minus the best NO bid)."""
        if not self.valid or not self.side["yes"] or not self.side["no"]: return None
        return max(self.side["yes"]), round(1 - max(self.side["no"]), 4)
    def offers(self, buy: str) -> list[tuple[float, float]]:
        """Displayed offers for buying `buy` ('YES'/'NO'), cheapest first: resting bids on the opposite side."""
        opp = self.side["no" if buy == "YES" else "yes"]
        return sorted((round(1 - p, 4), q) for p, q in opp.items())
    def imbalance(self) -> float | None:
        if not self.side["yes"] or not self.side["no"]: return None
        y = self.side["yes"][max(self.side["yes"])]; n = self.side["no"][max(self.side["no"])]
        return round((y - n) / (y + n), 4) if y + n > 0 else None
    def flow(self, t: float, secs: float) -> float:
        return round(sum(c if s == "yes" else -c for ts, s, c in self.trades if t - secs < ts <= t), 2)


def fill_against(offers: list[tuple[float, float]], qty: int, limit: float) -> dict:
    """Buy up to `qty` whole contracts at displayed prices <= limit. No size is invented beyond what is shown."""
    left = qty; cost = fee = 0.0; got = 0; shown = 0.0
    for p, q in offers:
        if p > limit + 1e-9: break
        shown += q; n = min(left, int(q))
        if n > 0: got += n; left -= n; cost += n * p; fee += exch_fee(n, p)
    return {"filled": got, "shown_at_limit": round(shown, 2), "cost": round(cost, 4), "fee": round(fee, 4), "avg_px": round(cost / got, 4) if got else None}


def _get(path: str, timeout: float = 8.0) -> dict:
    return json.load(urllib.request.urlopen(urllib.request.Request(API + path, headers={"User-Agent": "predhft-v4-shadow"}), timeout=timeout))


def sntp(host: str = "time.cloudflare.com") -> float | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2); t0 = time.time(); s.sendto(b"\x1b" + 47 * b"\0", (host, 123)); d, _ = s.recvfrom(64); t3 = time.time()
        f = lambda o: struct.unpack("!II", d[o:o + 8]); ts = lambda o: f(o)[0] - 2208988800 + f(o)[1] / 2**32
        return ((ts(32) - t0) + (ts(40) - t3)) / 2
    except Exception: return None


class Runner:
    def __init__(self) -> None:
        self.books: dict[str, Book] = {}; self.mk: dict[str, dict] = {}; self.keys: set[str] = set(); self.open: dict[str, dict] = {}
        self.targets: list[dict] = []; self.seq: dict[int, int] = {}; self.sids: dict[int, str] = {}
        p = ROOT / "fills.jsonl"
        if p.exists():
            for line in p.open(encoding="utf-8"):
                try: r = json.loads(line)
                except Exception: continue
                if r.get("kind") == "decision": self.keys.add(r["key"]); self.open[r["key"]] = r
                elif r.get("kind") == "settle": self.open.pop(r["key"], None)
            self.open = {k: r for k, r in self.open.items() if r.get("intent")}

    # ---- decisions -------------------------------------------------------------------------------------------------
    def due(self, t: float) -> None:
        for tk, m in list(self.mk.items()):
            for cohort, h in COHORTS.items():
                k = key(cohort, m["series"], m["close"]); D = m["close"] - h
                if k in self.keys or t < D: continue
                self.keys.add(k)
                if t - D > LATE_S: _write({"kind": "decision", "key": k, "cohort": cohort, "ticker": tk, "series": m["series"], "close": m["close"], "decision_ts": D, "intent": None, "skip": "missed boundary (not running or late at the decision time)", "late_s": round(t - D, 3)}); continue
                try: self.decide(cohort, tk, m, D, t)
                except Exception as e: _write({"kind": "decision", "key": k, "cohort": cohort, "ticker": tk, "series": m["series"], "close": m["close"], "decision_ts": D, "intent": None, "skip": f"error: {type(e).__name__}: {str(e)[:160]}"})

    def decide(self, cohort: str, tk: str, m: dict, D: float, t: float) -> None:
        b = self.books.get(tk) or Book(); top = b.top()
        quote = {"event": b.event, "received": b.recv, "bid": top[0] if top else float("nan"), "ask": top[1] if top else float("nan")}
        quote_ok = top is not None and b.valid and all(math.isfinite(x) and x <= D for x in (b.event, b.recv)) and D - b.event <= 3
        row = {"kind": "decision", "key": key(cohort, m["series"], m["close"]), "cohort": cohort, "ticker": tk, "series": m["series"], "close": m["close"], "decision_ts": D,
               "clock_offset_s": round(OFFSET, 4), "quote": quote, "quote_age_s": round(D - b.event, 3) if math.isfinite(b.event) else None, "book_valid": b.valid,
               "spread": round(quote["ask"] - quote["bid"], 4) if top else None, "imbalance_top": b.imbalance(), "flow_60s": b.flow(D, 60), "flow_10s": b.flow(D, 10)}
        intent = None
        if not quote_ok:
            row["skip"] = "no fresh two-sided quote" if top else "no quote / book invalid"
        else:
            mid = round((top[0] + top[1]) / 2, 4); row["mid"] = mid
            side = less_likely_side(*top)
            offers = b.offers(side); row["less_likely_side"] = side
            row["less_likely_offer"] = offers[0][0] if offers else None
            if not offers: row["skip"] = "no resting offer on the less-likely side"
            else:
                intent = intent_for(*top, offers)
                if intent is None: row["skip"] = f"less-likely offer {offers[0][0]} outside band [0.20, 0.35]"
        row["intent"] = intent; row["t_ready"] = now(); row["compute_s"] = round(row["t_ready"] - t, 4)
        if intent:
            offers = b.offers(intent["side"]); rtt = sorted(RTT)[len(RTT) // 2] if RTT else 0.3
            row.update(offer=intent["offer"], available_at_limit=fill_against(offers, 10**9, intent["limit"])["shown_at_limit"], rtt_s=round(rtt, 4))
            for label, at in [("measured", row["t_ready"] + rtt / 2)] + [(f"+{d}s", D + d) for d in DELAYS]:
                self.targets.append({"key": row["key"], "ticker": tk, "label": label, "at": at, "intent": intent, "close": m["close"]})
            self.open[row["key"]] = row
        _write(row)

    # ---- shadow fills ----------------------------------------------------------------------------------------------
    def fills(self, tk: str | None, event: float, idle: bool = False) -> None:
        keep = []
        for g in self.targets:
            hit = (g["ticker"] == tk and event > g["at"]) or (idle and event > g["at"] + 3)
            if not hit: keep.append(g); continue
            b = self.books.get(g["ticker"]); i = g["intent"]; row = {"kind": "fill", "key": g["key"], "ticker": g["ticker"], "replay": g["label"], "arrival_ts": g["at"], "book_event_ts": b.event if b else None}
            if g["at"] >= g["close"]: row.update(status="unavailable: arrival after close", filled=0)
            elif b is None or not b.valid: row.update(status="unknown: book lost sequence, no fill assumed", filled=0)
            else:
                f = fill_against(b.offers(i["side"]), i["qty"], i["limit"]); best = b.offers(i["side"])[0][0] if b.offers(i["side"]) else None
                row.update(f, status="full" if f["filled"] == i["qty"] else ("partial" if f["filled"] else "none"), best_offer=best, quiet_book=idle,
                           slippage_vs_limit=round(f["avg_px"] - i["limit"], 4) if f["filled"] else None)
            _write(row)
        self.targets = keep

    # ---- feed -------------------------------------------------------------------------------------------------------
    def frame(self, m: dict, r: float) -> None:
        typ = m.get("type"); msg = m.get("msg") or {}; sid, q = m.get("sid"), m.get("seq")
        if typ == "subscribed": self.sids[msg.get("sid")] = msg.get("channel"); return
        if self.sids.get(sid) == "orderbook_delta" and q is not None:
            if self.seq.get(sid) is not None and q != self.seq[sid] + 1:
                for b in self.books.values(): b.valid = False
                _write({"kind": "alert", "msg": f"book sequence gap: expected {self.seq[sid] + 1}, got {q}"}); raise ConnectionError("sequence gap")
            self.seq[sid] = q
        tk = msg.get("market_ticker")
        if not tk or tk not in self.mk: return
        b = self.books.setdefault(tk, Book())
        if typ == "trade": b.trades.append((msg.get("ts_ms", 0) / 1000, msg.get("taker_side"), float(msg.get("count_fp") or 0))); return
        event = (msg.get("ts_ms") or m.get("sending_ts_ms") or r * 1000) / 1000
        self.fills(tk, event)
        if typ == "orderbook_snapshot": b.snapshot(msg, event, r)
        elif typ == "orderbook_delta" and b.valid: b.delta(msg, event, r)

    async def ws_loop(self) -> None:
        backoff = 2
        while True:
            try:
                async with websockets.connect(WS, additional_headers=headers("GET", WS_PATH), ping_interval=20, ping_timeout=20, max_size=2**24) as ws:
                    self.sids.clear(); self.seq.clear(); subs: set[str] = set(); nid = 1; backoff = 2; idle_at = 0.0
                    for b in self.books.values(): b.valid = False
                    _write({"event": "connected"}, "log.jsonl")
                    while True:
                        new = [t for t in self.mk if t not in subs]
                        if new:
                            if not self.sids and not subs: await ws.send(json.dumps({"id": nid, "cmd": "subscribe", "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": new}})); nid += 1
                            elif self.sids:
                                for sid in list(self.sids): await ws.send(json.dumps({"id": nid, "cmd": "update_subscription", "params": {"sids": [sid], "market_tickers": new, "action": "add_markets"}})); nid += 1
                            else: new = []
                            subs.update(new)
                        try: raw = await asyncio.wait_for(ws.recv(), timeout=0.02)
                        except asyncio.TimeoutError: t = now(); self.due(t); self.fills(None, t, idle=True); continue
                        r = now(); self.due(r); self.frame(json.loads(raw), r)
                        if self.targets and r > idle_at: self.fills(None, r, idle=True); idle_at = r + 0.5
            except Exception as e:
                for b in self.books.values(): b.valid = False
                _write({"event": "disconnect", "err": f"{type(e).__name__}: {str(e)[:200]}"}, "log.jsonl"); await asyncio.sleep(backoff); backoff = min(backoff * 2, 30)

    async def discover_loop(self) -> None:
        while True:
            try:
                for ser in SERIES:
                    d = await asyncio.to_thread(_get, f"/markets?series_ticker={ser}&status=open&limit=20"); r = now()
                    for m in d.get("markets") or []:
                        close = calendar.timegm(time.strptime(m["close_time"], "%Y-%m-%dT%H:%M:%SZ"))
                        if m["ticker"] not in self.mk: self.mk[m["ticker"]] = {"series": ser, "close": close}
                for tk in [t for t, m in self.mk.items() if now() > m["close"] + 60]: self.mk.pop(tk, None); self.books.pop(tk, None)
            except Exception as e: _write({"event": "discovery_error", "err": str(e)[:160]}, "log.jsonl")
            await asyncio.sleep(5)

    async def clock_loop(self) -> None:
        global OFFSET
        n = 0
        while True:
            if n % 30 == 0:
                o = await asyncio.to_thread(sntp)
                if o is not None and abs(o) < 5: OFFSET = o; _write({"event": "clock", "ntp_minus_local_s": round(o, 4)}, "log.jsonl")
            try:
                t0 = time.time(); await asyncio.to_thread(_get, "/exchange/status", 4.0); RTT.append(time.time() - t0)
            except Exception: pass
            n += 1; await asyncio.sleep(60)

    async def settle_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            for k, d in list(self.open.items()):
                if now() < d["close"] + 90 or any(g["key"] == k for g in self.targets): continue
                try: m = (await asyncio.to_thread(_get, f"/markets/{d['ticker']}"))["market"]
                except Exception: continue
                if m.get("result") not in ("yes", "no"): continue
                i = d["intent"]; won = (m["result"] == "yes") == (i["side"] == "YES"); res = {}
                p = ROOT / "fills.jsonl"; rows = [json.loads(l) for l in p.open(encoding="utf-8") if f'"{k}"' in l]
                for f in (r for r in rows if r.get("kind") == "fill"):
                    n = f.get("filled") or 0
                    res[f["replay"]] = {"status": f["status"], "filled": n, "net_exchange_fees": round(n * won - f.get("cost", 0) - f.get("fee", 0), 4) if n else 0.0}
                _write({"kind": "settle", "key": k, "cohort": d["cohort"], "ticker": d["ticker"], "side": i["side"], "result": m["result"], "won": won, "expiration_value": m.get("expiration_value"),
                        "offer": i["offer"], "replays": res}); self.open.pop(k, None)

    async def run(self) -> None:
        global OFFSET
        o = sntp(); OFFSET = o if o is not None and abs(o) < 5 else 0.0
        _write({"kind": "start", "mode": "shadow", "order_route": None, "campaigns": list(COHORTS), "rule": RULE, "clock_offset_s": round(OFFSET, 4), "keys_loaded": len(self.keys)})
        _write({"kind": "meta", "rule": RULE, "note": "mechanical mirror of Polymarket 'blue.birds' profile (research/favt5_v4/RULE.md); Kalshi analogue = 15-minute series because no 5-minute series exists"})
        await asyncio.gather(self.ws_loop(), self.discover_loop(), self.clock_loop(), self.settle_loop())


if __name__ == "__main__":
    if os.environ.get("PREDHFT_MODE", "paper") == "live": raise SystemExit("favt5_v4_shadow is shadow-only; it has no live mode")
    from .s7_live import single_instance
    _lock = single_instance(ROOT)
    asyncio.run(Runner().run())
