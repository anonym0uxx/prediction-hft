"""FavT5 v3 SHADOW runner. Paper measurement only: this module has no order route and never signs a trading request.

Rule (frozen, owner order 2026-11-12): KXBTC15M, one evaluation per expiry at exactly close-480 s, scored by the
reference `favt5_v3_index.signal` (byte-identical to the archive's deep/favt5_v3_index.py). This file only gathers the
inputs the scorer asks for, records what it returned, and measures what a real order of that intent would have met.

Inputs at the decision instant D = close-480 (all times on the NTP-corrected local clock, raw stamps kept too):
  quote            YES bid/ask from the sequence-validated book; event = exchange time of the last book frame for the
                   SAME ticker, received = local receipt of that frame; both must be <= D and the event within 3 s
  quoted_strike    the exchange's floor_strike for the SAME ticker, fetched before D (model input)
  official_strike  the strike rebuilt from the locally received index: mean of the 60 one-second values in
                   [open-60, open). Missing seconds -> not confirmable -> the scorer rejects
  index            one-second official ticks received by the passive recorder (data/cfb/<ASSET>.jsonl), event+receipt
Shadow execution of an intent (limit, qty, whole-dollar cap from the scorer; never changed here):
  arrival          'measured' = time the intent was ready + half the measured exchange round trip; plus independent
                   replays of the identical intent at D+1, +2, +5, +15, +30 s
  fill             walk displayed offers <= limit in the book as of that exchange time; partial fills kept; a book that
                   lost a sequence number is 'unknown', never assumed filled
  fees             exchange rounding (per fill, up to $0.0001; within $0.00012 of our 137 real fills) AND the frozen
                   replay assumption (full size at the limit, cash rounded up to a whole cent), kept side by side
Other arms logged for the owner's comparisons, each with its own key and never mixed into the BTC rule:
  btc_fav_t5       same-budget favourite at close-300, offer 0.965-0.990 (archive id tick|300|favorite|0.965|0.99|None)
  eth_challenge    the same scorer fed the ETH contract's own index (ETHUSD_RTI)
Ledger data/favt5-v3/shadow/fills.jsonl: start | meta | decision | fill | settle | alert. One decision per key
`<arm>:<series>:<close epoch>`; keys are reloaded at start, so a restart or reconnect cannot repeat one, and a boundary
that passed while the process was down is recorded as skipped, never made up."""
from __future__ import annotations
import asyncio, calendar, collections, json, math, os, pathlib, socket, struct, time, urllib.request
from decimal import Decimal, ROUND_CEILING
import websockets
from . import favt5_v3_index as ref
from .kalshi_auth import headers

ROOT = pathlib.Path("data/favt5-v3/shadow")
CFB = pathlib.Path(os.environ.get("PREDHFT_CFB_DIR", "D:/prediction-hft/data/cfb"))
API = "https://api.elections.kalshi.com/trade-api/v2"; WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"; WS_PATH = "/trade-api/ws/v2"
ARMS = {"btc_v3": ("KXBTC15M", "BTC", 480), "btc_fav_t5": ("KXBTC15M", "BTC", 300), "eth_challenge": ("KXETH15M", "ETH", 480)}
SERIES = ("KXBTC15M", "KXETH15M"); DELAYS = (1, 2, 5, 15, 30); LATE_S = 1.0
VOL_SPLIT_VBPS = 0.4411494766632067          # archive: median main_vbps on the development days (inference.py); frozen
SHADOW_BUDGET = Decimal("50.00")                # owner changed per-window cap $10 -> $50 (2026-11-15); paper only, never frozen back
FROZEN = {"taker": 0.07, "tick_in_band": 0.01, "cash_rounding": "whole cent, upward, per order", "budget": 50.0, "budget_history": {"10.0": "2026-11-12 -> 2026-11-15", "50.0": "owner change 2026-11-15"}}
OFFSET = 0.0                                  # seconds to ADD to the local clock to get network time
RTT: collections.deque = collections.deque(maxlen=9)


def now() -> float: return time.time() + OFFSET
def key(arm: str, series: str, close: int) -> str: return f"{arm}:{series}:{int(close)}"
def exch_fee(n: float, p: float) -> float: return math.ceil(0.07 * n * p * (1 - p) * 1e4 - 1e-9) / 1e4
def bump(p: float) -> float: return round(p + (0.001 if p < 0.1 or p >= 0.9 else 0.01), 4)   # one legal adverse tick
def band(offer: float) -> str: return "15-29.9c" if offer < 0.29999 else ("30-44.9c" if offer < 0.44999 else "45-60c")


def _clean(x):
    """NaN/inf are not JSON; a missing number is written as null."""
    if isinstance(x, float): return x if math.isfinite(x) else None
    if isinstance(x, dict): return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)): return [_clean(v) for v in x]
    return x


def _write(row: dict, name: str = "fills.jsonl") -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / name).open("a", encoding="utf-8") as f: f.write(json.dumps(_clean({"t": time.time(), **row}), allow_nan=False) + "\n")


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
        """Signed taker volume in contracts over the last `secs` (bought YES positive, bought NO negative)."""
        return round(sum(c if s == "yes" else -c for ts, s, c in self.trades if t - secs < ts <= t), 2)


def fill_against(offers: list[tuple[float, float]], qty: int, limit: float) -> dict:
    """Buy up to `qty` whole contracts at displayed prices <= limit. No size is invented beyond what is shown."""
    left = qty; cost = fee = 0.0; got = 0; shown = 0.0
    for p, q in offers:
        if p > limit + 1e-9: break
        shown += q; n = min(left, int(q))
        if n > 0: got += n; left -= n; cost += n * p; fee += exch_fee(n, p)
    return {"filled": got, "shown_at_limit": round(shown, 2), "cost": round(cost, 4), "fee": round(fee, 4), "avg_px": round(cost / got, 4) if got else None}


def side_table(fair_yes: float, bid: float, ask: float) -> list[dict]:
    """Both sides as the reference `select` prices them (logging only; the intent itself always comes from ref.signal)."""
    out = []
    for side, prob, offer in (("YES", fair_yes, ask), ("NO", 1 - fair_yes, 1 - bid)):
        offer = Decimal(str(round(offer, 4))); limit = offer + ref.CENT; unit = limit + ref.TAKER * limit * (1 - limit)
        qty = int(ref.BUDGET / unit); cost = (qty * unit).quantize(ref.CENT, rounding=ROUND_CEILING); edge = prob * qty - float(cost)
        out.append({"side": side, "probability": prob, "offer": float(offer), "limit": float(limit), "qty": qty, "max_total_cost": float(cost),
                    "modeled_fee": round(float(cost) - qty * float(limit), 4), "modeled_net_dollars": edge, "edge_per_contract": edge / qty if qty else None,
                    "in_band": bool(Decimal(".15") <= offer <= Decimal(".60")), "qualifies": bool(Decimal(".15") <= offer <= Decimal(".60") and qty and cost <= ref.BUDGET and edge >= .05 * qty)})
    return out


def favourite(bid: float, ask: float, budget: Decimal = None) -> dict | None:
    """Same-budget favourite control: the dearer side priced 0.965-0.990, one adverse tick, sized to the same budget as the scorer (archive stress.py)."""
    cap = float(budget) if budget is not None else float(SHADOW_BUDGET)
    if not (0 < bid <= ask < 1 and ask - bid <= .06 + 1e-12): return None
    best = None
    for side, price in (("YES", round(ask, 4)), ("NO", round(1 - bid, 4))):
        limit = bump(price); unit = limit + .07 * limit * (1 - limit); qty = math.floor(cap / unit + 1e-10); cost = math.ceil(unit * qty * 100 - 1e-8) / 100
        if .965 <= price <= .99 and limit < 1 and qty > 0 and cost <= cap and (best is None or price > best["offer"]):
            best = {"side": side, "offer": price, "limit": limit, "qty": int(qty), "max_total_cost": cost}
    return best


def diagnose(snap: dict) -> tuple[str | None, dict]:
    """Why the reference returned nothing, in its own order of checks, plus the numbers behind the decision."""
    D = snap["close"] - 480; q = snap["quote"]; info: dict = {}
    if any(not math.isfinite(t) or t > D for t in (q["event"], q["received"])): return "no quote received by the decision time", info
    if not math.isfinite(snap["strike_received"]) or snap["strike_received"] > D: return "strike not published by the decision time", info
    if D - q["event"] > 3: return "quote older than 3 s", info
    s, qs = snap["official_strike"], snap["quoted_strike"]
    if not (math.isfinite(qs) and qs > 0): return "strike not published by the decision time", info
    if not (math.isfinite(s) and s > 0): return "strike could not be confirmed from the received index", info
    if abs(qs - s) > .02: return "strike difference over $0.02", info
    slow = ref.index_features(snap["brti"], D, 3); fast = ref.index_features(snap["brti"], D, 1)
    if slow is None: return "index or volatility history missing (3 s lag)", info
    if fast is None: return "index not available within 1 s", info
    if not (0 < q["bid"] <= q["ask"] < 1): return "no two-sided quote", info
    last, value, rv = slow; tv = max(0, snap["close"] - 60 - last) + 59 * 119 / (6 * 60)
    sigma = math.hypot(max(rv, value * .000012) * math.sqrt(tv), qs * .000035); mid = (q["bid"] + q["ask"]) / 2
    fair = .75 * ref.t5_cdf((value - qs) / sigma) + .25 * mid; vb = fast[2] / fast[1] * 1e4
    info.update(index_last=last, index_value=value, index_age=D - last, rv=rv, sigma=sigma, fair_yes=fair, vol_bps=vb,
                vol_regime="higher" if vb >= VOL_SPLIT_VBPS else "lower", sides=side_table(fair, q["bid"], q["ask"]))
    if q["ask"] - q["bid"] > .06: return "spread above 6 cents", info
    if not any(x["in_band"] for x in info["sides"]): return "no offer between $0.15 and $0.60", info
    if not any(x["qualifies"] for x in info["sides"]): return "modeled net edge below $0.05 per contract", info
    return None, info


def index_ticks(asset: str, tail_bytes: int = 700_000) -> list[dict]:
    """The last ~20 minutes of one-second official index values as the passive recorder received them."""
    p = CFB / f"{asset}.jsonl"; out = []
    with p.open("rb") as f:
        f.seek(0, 2); f.seek(max(0, f.tell() - tail_bytes)); f.readline()
        for line in f:
            try: r = json.loads(line)
            except Exception: continue
            if r.get("value") and r.get("value_ts_ms"): out.append({"event": r["value_ts_ms"] / 1000, "received": r["t"] + OFFSET, "value": float(r["value"])})
    return out


def rebuilt_strike(ticks: list[dict], open_ts: int, by: float) -> float:
    """Mean of the 60 exact-second index values in [open-60, open) received by `by`; NaN if any second is missing."""
    v = {int(t["event"]): t["value"] for t in ticks if t["event"] == int(t["event"]) and open_ts - 60 <= t["event"] < open_ts and t["received"] <= by}
    return sum(v.values()) / 60 if len(v) == 60 else float("nan")


def lead_lag(a: list[dict], b: list[dict], D: float, span: int = 300) -> dict:
    """BTC vs ETH one-second returns over the last `span` s: correlation at shifts of -3..+3 s (positive = first leads)."""
    def rets(t):
        v = {int(x["event"]): x["value"] for x in t if x["received"] <= D}
        return {s: math.log(v[s] / v[s - 1]) for s in range(int(D) - span, int(D)) if s in v and s - 1 in v}
    ra, rb = rets(a), rets(b); out = {}
    for k in range(-3, 4):
        xs = [(ra[s], rb[s + k]) for s in ra if s + k in rb]
        if len(xs) < 60: continue
        ma = sum(x for x, _ in xs) / len(xs); mb = sum(y for _, y in xs) / len(xs)
        va = sum((x - ma) ** 2 for x, _ in xs); vb = sum((y - mb) ** 2 for _, y in xs)
        out[k] = round(sum((x - ma) * (y - mb) for x, y in xs) / math.sqrt(va * vb), 4) if va > 0 and vb > 0 else None
    return out


def sntp(host: str = "time.cloudflare.com") -> float | None:
    """Network time minus local time, in seconds (one SNTP exchange). None on failure."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2); t0 = time.time(); s.sendto(b"\x1b" + 47 * b"\0", (host, 123)); d, _ = s.recvfrom(64); t3 = time.time()
        f = lambda o: struct.unpack("!II", d[o:o + 8]); ts = lambda o: f(o)[0] - 2208988800 + f(o)[1] / 2**32
        return ((ts(32) - t0) + (ts(40) - t3)) / 2
    except Exception: return None


def _get(path: str, timeout: float = 8.0) -> dict:
    return json.load(urllib.request.urlopen(urllib.request.Request(API + path, headers={"User-Agent": "predhft-v3-shadow"}), timeout=timeout))


def exchange_meta() -> dict:
    """Fee and price-grid metadata as the exchange publishes it now, with every difference from the frozen replay."""
    out = {"kind": "meta", "frozen": FROZEN, "series": {}, "divergences": []}
    for ser in SERIES:
        s = _get(f"/series/{ser}")["series"]; m = (_get(f"/markets?series_ticker={ser}&status=open&limit=1")["markets"] or [{}])[0]
        steps = {(r["start"], r["end"]): float(r["step"]) for r in m.get("price_ranges") or []}
        out["series"][ser] = {"fee_type": s.get("fee_type"), "fee_multiplier": s.get("fee_multiplier"), "price_level_structure": m.get("price_level_structure"), "price_ranges": m.get("price_ranges")}
        if s.get("fee_type") != "quadratic" or float(s.get("fee_multiplier") or 0) != 1: out["divergences"].append(f"{ser}: fee is {s.get('fee_type')} x{s.get('fee_multiplier')}, frozen replay assumes quadratic x1 (7%)")
        if any(abs(st - .01) > 1e-9 for (a, b), st in steps.items() if float(a) < .60 and float(b) > .15): out["divergences"].append(f"{ser}: tick inside $0.15-$0.60 is not $0.01")
    out["divergences"].append("fee rounding: the exchange charges each fill rounded up to $0.0001; the frozen replay rounds the whole order's cash up to a cent (replay is the more expensive of the two)")
    out["divergences"].append("size: the exchange shows fractional contracts; shadow fills count whole contracts only")
    return out


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
            for arm, (ser, asset, h) in ARMS.items():
                if m["series"] != ser: continue
                k = key(arm, ser, m["close"]); D = m["close"] - h
                if k in self.keys or t < D: continue
                self.keys.add(k)
                if t - D > LATE_S: _write({"kind": "decision", "key": k, "arm": arm, "ticker": tk, "close": m["close"], "decision_ts": D, "intent": None, "skip": "missed boundary (not running or late at the decision time)", "late_s": round(t - D, 3)}); continue
                try: self.decide(arm, tk, m, asset, D, t)
                except Exception as e: _write({"kind": "decision", "key": k, "arm": arm, "ticker": tk, "close": m["close"], "decision_ts": D, "intent": None, "skip": f"error: {type(e).__name__}: {str(e)[:160]}"})

    def decide(self, arm: str, tk: str, m: dict, asset: str, D: float, t: float) -> None:
        b = self.books.get(tk) or Book(); top = b.top(); ticks = index_ticks(asset)
        quote = {"event": b.event, "received": b.recv, "bid": top[0] if top else float("nan"), "ask": top[1] if top else float("nan")}
        snap = {"series": "KXBTC15M", "close": m["close"], "quote": quote, "strike_received": m["strike_recv"], "quoted_strike": m["strike"] or float("nan"),
                "official_strike": rebuilt_strike(ticks, m["close"] - 900, D), "brti": ticks}
        row = {"kind": "decision", "key": key(arm, m["series"], m["close"]), "arm": arm, "ticker": tk, "series": m["series"], "close": m["close"], "decision_ts": D,
               "clock_offset_s": round(OFFSET, 4), "quote": quote, "quote_age_s": round(D - b.event, 3) if math.isfinite(b.event) else None, "book_valid": b.valid,
               "spread": round(quote["ask"] - quote["bid"], 4) if top else None, "quoted_strike": snap["quoted_strike"], "official_strike": snap["official_strike"], "strike_received": m["strike_recv"],
               "imbalance_top": b.imbalance(), "flow_60s": b.flow(D, 60), "flow_10s": b.flow(D, 10)}
        if arm == "btc_fav_t5":
            ok = top is not None and all(math.isfinite(x) and x <= D for x in (b.event, b.recv)) and D - b.event <= 3 and ref.index_features(ticks, D, 1) is not None
            intent = favourite(*top) if ok else None
            row["skip"] = None if intent else ("no fresh quote or index" if not ok else "no side offered at $0.965-$0.990")
        else:
            snap480 = dict(snap)                       # the scorer hard-codes close-480; both scored arms decide there
            intent = ref.signal(snap480); why, info = diagnose(snap480); row.update(info); row["skip"] = None if intent else (why or "reference returned no intent")
            if (intent is None) != (why is not None): row["alert"] = "diagnosis disagrees with the reference scorer"; _write({"kind": "alert", "key": row["key"], "msg": row["alert"]})
            if arm == "btc_v3":
                try: row["lead_lag_btc_vs_eth"] = lead_lag(ticks, index_ticks("ETH"), D)
                except Exception: pass
        row["intent"] = intent; row["t_ready"] = now(); row["compute_s"] = round(row["t_ready"] - t, 4)
        if intent:
            offers = b.offers(intent["side"]); rtt = sorted(RTT)[len(RTT) // 2] if RTT else 0.3
            offer = intent.get("offer", round(intent["limit"] - 0.01, 4))           # the scorer bids one adverse cent above the offer
            row.update(offer=offer, entry_band="favourite" if arm == "btc_fav_t5" else band(offer),
                       available_at_limit=fill_against(offers, 10**9, intent["limit"])["shown_at_limit"], rtt_s=round(rtt, 4))
            for label, at in [("measured", row["t_ready"] + rtt / 2)] + [(f"+{d}s", D + d) for d in DELAYS]:
                self.targets.append({"key": row["key"], "ticker": tk, "label": label, "at": at, "intent": intent, "close": m["close"]})
            self.open[row["key"]] = row
        _write(row)

    # ---- shadow fills ----------------------------------------------------------------------------------------------
    def fills(self, tk: str | None, event: float, idle: bool = False) -> None:
        """Before a frame stamped `event` is applied to `tk`'s book, settle every replay whose arrival time has passed."""
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
                           frozen_replay_filled=bool(best is not None and best <= i["limit"] + 1e-9), slippage_vs_limit=round(f["avg_px"] - i["limit"], 4) if f["filled"] else None)
            _write(row)
        self.targets = keep

    # ---- feed ------------------------------------------------------------------------------------------------------
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
                        if self.targets and r > idle_at: self.fills(None, r, idle=True); idle_at = r + 0.5   # a quiet ticker must not wait on its own next frame
            except Exception as e:
                for b in self.books.values(): b.valid = False
                _write({"event": "disconnect", "err": f"{type(e).__name__}: {str(e)[:200]}"}, "log.jsonl"); await asyncio.sleep(backoff); backoff = min(backoff * 2, 30)

    async def discover_loop(self) -> None:
        while True:
            try:
                for ser in SERIES:
                    d = await asyncio.to_thread(_get, f"/markets?series_ticker={ser}&status=open&limit=20"); r = now()
                    for m in d.get("markets") or []:
                        close = calendar.timegm(time.strptime(m["close_time"], "%Y-%m-%dT%H:%M:%SZ")); e = self.mk.get(m["ticker"])
                        if e is None: e = self.mk[m["ticker"]] = {"series": ser, "close": close, "strike": None, "strike_recv": float("nan")}
                        if e["strike"] is None and m.get("floor_strike"): e["strike"] = float(m["floor_strike"]); e["strike_recv"] = r
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
                    res[f["replay"]] = {"status": f["status"], "filled": n, "net_exchange_fees": round(n * won - f.get("cost", 0) - f.get("fee", 0), 4) if n else 0.0,
                                        "net_frozen_replay": round(i["qty"] * won - i["max_total_cost"], 2) if f.get("frozen_replay_filled") else 0.0}
                _write({"kind": "settle", "key": k, "arm": d["arm"], "ticker": d["ticker"], "side": i["side"], "result": m["result"], "won": won, "expiration_value": m.get("expiration_value"),
                        "modeled_net_dollars": i.get("modeled_net_dollars"), "replays": res}); self.open.pop(k, None)

    async def run(self) -> None:
        global OFFSET
        o = sntp(); OFFSET = o if o is not None and abs(o) < 5 else 0.0
        ref.BUDGET = SHADOW_BUDGET            # per-window cap override (10 -> 50); affects ref.signal AND side_table together
        _write({"kind": "start", "mode": "shadow", "order_route": None, "arms": list(ARMS), "clock_offset_s": round(OFFSET, 4), "keys_loaded": len(self.keys), "scorer": "favt5_v3_index (archive sha256 c2db8eb2...)"})
        _write({"kind": "config", "budget": float(SHADOW_BUDGET), "prev_budget": 10.0, "note": "per-window cap raised $10 -> $50 (owner, 2026-11-15); intents after this row sized at the new cap, earlier settled/open intents keep their $10 sizing"})
        try: _write(await asyncio.to_thread(exchange_meta))
        except Exception as e: _write({"kind": "alert", "msg": f"exchange metadata unreadable at start: {str(e)[:160]}"})
        await asyncio.gather(self.ws_loop(), self.discover_loop(), self.clock_loop(), self.settle_loop())


if __name__ == "__main__":
    if os.environ.get("PREDHFT_MODE", "paper") == "live": raise SystemExit("favt5_v3_shadow is shadow-only; it has no live mode")
    from .s7_live import single_instance
    _lock = single_instance(ROOT)
    asyncio.run(Runner().run())
