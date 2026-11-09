"""FavT5 LIVE arm — actual execution of the proven paper strategy. Thin shim, zero strategy logic of its own.

Everything that decides WHAT to trade is imported from predhft.fav_t5 (SERIES, CFG, decide, size_trade,
equity_at, book_sides) — the same code paths that produced the 99.6% paper record. The ONLY new behaviour
is the last step: send a real IOC order instead of writing a paper fill, and record what actually happened.

Execution model (parity with paper's book walk): paper walks the favourite's asks at prices <= max_px up to
the budget. Live sends ONE immediate_or_cancel order for the walked quantity at the walk's worst acceptable
price (bid at max_px for a YES favourite; ask at 1-max_px for a NO favourite — V2 prices are YES-leg, so
buying NO is selling YES). It crosses the same resting orders and fills at their prices (price improvement
is possible, never worse than the limit). Partial fills and any paper-vs-live deltas are recorded as
kind=parity rows for the deviation ledger.

Modes / switches:
  PREDHFT_MODE must be 'live' (refuses otherwise; paper arm refuses 'live' — the two can never cross).
  REHEARSAL=1      sign+build orders but do not send; rows marked mode=rehearsal (bankroll from
                   REHEARSAL_BANKROLL, default 100). Used to prove the pipeline before funding.
  data/fav-t5-live/STOP file   stops NEW entries (open positions still settle; remove file to re-arm).
  Circuit breaker: 3 losing settles in 24 h -> halt new entries (kind=halt row; restart to re-arm).

Ledger: data/fav-t5-live/fills.jsonl — same schema as paper (entry/settle) plus live fields
(order_id, fill_count, avg_px per the favourite's leg, fee_actual, mode) and kind=parity deviation rows.
Equity: bankroll = live account balance at start; then identical epoch accounting (equity_at from fav_t5).
"""
from __future__ import annotations
import json, os, pathlib, time, urllib.error, urllib.request, uuid
from datetime import datetime, timezone
from . import kalshi_auth
from .fav_t5 import SERIES, CFG, _ts, book_sides, decide, equity_at, kalshi_taker_fee   # strategy: single source of truth

assert "KXSOL15M" not in SERIES, "FavT5 is BTC/ETH-only by owner decision 2026-11-08"
MODE = os.environ.get("PREDHFT_MODE", "paper")
if MODE != "live":
    raise RuntimeError(f"fav_t5_live requires PREDHFT_MODE=live (got {MODE!r}); the paper arm is predhft.fav_t5")
REHEARSAL = os.environ.get("REHEARSAL") == "1"

API = "https://api.elections.kalshi.com/trade-api/v2"
ROOT = pathlib.Path("data/fav-t5-rehearsal" if REHEARSAL else "data/fav-t5-live")   # rehearsal never pollutes the live ledger
ORDERS_PATH = "/portfolio/events/orders"
BALANCE_PATH = "/portfolio/balance"
STOP = ROOT / "STOP"
CFG_LIVE = dict(CFG, max_contracts_per_order=500, min_balance_usd=1.0)


def _append(rec: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "fills.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def _req(method: str, path: str, body: dict | None = None, timeout: int = 15) -> tuple[int, dict | str]:
    """Signed request. Signs path WITHOUT query (Kalshi spec); query rides in the URL only."""
    url = API + path
    h = kalshi_auth.headers(method, path.split("?")[0])
    h["Content-Type"] = "application/json"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(1 + attempt); continue
            return e.code, e.read()[:300].decode("utf-8", "replace")
        except Exception as e:
            if attempt < 2: time.sleep(1 + attempt); continue
            return -1, f"{type(e).__name__}: {e}"
    return -1, "unreachable"


def _get_public(path: str) -> dict:
    try:
        with urllib.request.urlopen(urllib.request.Request(API + path, headers={"User-Agent": "predhft-fav-t5-live/1"}), timeout=20) as r:
            return json.load(r)
    except Exception:
        return {}


def balance_usd() -> float | None:
    code, b = _req("GET", BALANCE_PATH)
    if code == 200 and isinstance(b, dict):
        return float(b.get("balance_dollars") or 0.0)
    return None


def order_payload(ticker: str, side: str, qty: int, max_px: float) -> dict:
    """V2 order for the favourite's walked quantity. YES favourite: bid at max_px.
    NO favourite: buy NO = sell YES at 1-max_px (V2 prices are YES-leg). IOC, never rests."""
    book_side, price = ("bid", max_px) if side == "yes" else ("ask", round(1.0 - max_px, 4))
    return {"ticker": ticker, "client_order_id": str(uuid.uuid4()), "side": book_side,
            "count": f"{qty:.2f}", "price": f"{price:.4f}", "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross", "post_only": False,
            "cancel_order_on_pause": True, "reduce_only": False}


def execute(ticker: str, side: str, qty: int, max_px: float) -> dict:
    """Send the IOC order and return the ACTUAL fill economics in the favourite's leg scale."""
    payload = order_payload(ticker, side, qty, max_px)
    if REHEARSAL:
        return {"mode": "rehearsal", "payload": payload, "fill_count": qty, "avg_px": max_px, "fee": kalshi_taker_fee(qty, max_px)}
    code, r = _req("POST", ORDERS_PATH, payload)
    if code != 201:
        return {"mode": "live", "error": f"HTTP {code}: {r}"}
    fc = float(r.get("fill_count") or 0)
    if fc <= 0:
        return {"mode": "live", "order_id": r.get("order_id"), "fill_count": 0, "remaining_count": r.get("remaining_count")}
    afp = float(r.get("average_fill_price") or 0)                      # YES-leg VWAP
    avg_px = afp if side == "yes" else round(1.0 - afp, 6)             # convert to the favourite's leg for parity
    fee = round(float(r.get("average_fee_paid") or 0) * fc, 6)         # actual per-contract fee x fills
    return {"mode": "live", "order_id": r.get("order_id"), "fill_count": int(fc + 1e-9),
            "remaining_count": r.get("remaining_count"), "avg_px": avg_px, "fee": fee}


def paper_counterpart(ticker: str, path: str = "data/fav-t5/fills.jsonl") -> dict | None:
    """The paper arm's entry for the same ticker (it runs the same windows) — for the deviation ledger."""
    p = pathlib.Path(path)
    if not p.exists(): return None
    for l in p.read_text(encoding="utf-8").splitlines():
        try: r = json.loads(l)
        except Exception: continue
        if r.get("kind") == "entry" and r.get("ticker") == ticker and (r.get("qty") or 0) > 0: return r
    return None


def halted_loss_count(rows: list) -> int:
    return sum(1 for r in rows if r.get("kind") == "settle" and not r.get("win"))


def run(cfg: dict = CFG_LIVE) -> None:
    bankroll = float(os.environ.get("REHEARSAL_BANKROLL", 100)) if REHEARSAL else balance_usd()
    if bankroll is None:
        raise RuntimeError("could not read live balance; refusing to start")
    _append({"kind": "start", "t": time.time(), "mode": "rehearsal" if REHEARSAL else "live",
             "bankroll": bankroll, "series": SERIES, "cfg": {k: cfg[k] for k in ("bankroll", "stake_frac", "max_px", "fav_min_mid")}})
    epoch = time.time(); eq = bankroll; held = 0.0
    done: set = set(); open_pos: dict[str, dict] = {}; losses_24h: list = []
    fills = ROOT / "fills.jsonl"
    if fills.exists():
        for l in fills.read_text(encoding="utf-8").splitlines():
            try: r = json.loads(l)
            except Exception: continue
            done.add(r.get("ticker"))
            if r.get("kind") == "entry" and (r.get("qty") or 0) > 0: open_pos[r["ticker"]] = r
            if r.get("kind") == "settle":
                open_pos.pop(r["ticker"], None)
                if r["t"] >= epoch - 86400 and not r.get("win"): losses_24h.append(r["t"])
    halt = len(losses_24h) >= 3
    while True:
        now = time.time()
        try:
            if not STOP.exists() and not halt:
                for ser in SERIES:
                    ms = _get_public(f"/markets?series_ticker={ser}&status=open&limit=20").get("markets", [])
                    for m in ms:
                        tk = m["ticker"]; c = _ts(m["close_time"]); left = c - now
                        if tk in done or not (cfg["secs_before_close"] - cfg["window_s"] < left <= cfg["secs_before_close"]): continue
                        budget = min(cfg["stake_frac"] * eq, eq - held)
                        d = decide(m, _get_public(f"/markets/{tk}/orderbook"), cfg, budget); done.add(tk)
                        if not (d and d.get("qty", 0) > 0):
                            _append({"kind": "entry", "t": time.time(), "ticker": tk, "series": ser, "close_ts": c,
                                     "secs_left": round(left, 1), "qty": 0, "reason": d.get("reason") if d else "no book"})
                            continue
                        qty = min(d["qty"], cfg["max_contracts_per_order"])
                        x = execute(tk, d["side"], qty, cfg["max_px"])
                        fq = int(x.get("fill_count") or 0)
                        rec = {"kind": "entry", "t": time.time(), "ticker": tk, "series": ser, "close_ts": c,
                               "secs_left": round(left, 1), "side": d["side"], "qty": fq, "mid": d["mid"],
                               "budget": round(budget, 2), "mode": x.get("mode"),
                               "order_id": x.get("order_id"), "avg_px": x.get("avg_px"),
                               "fee": x.get("fee"), "cost": round(fq * (x.get("avg_px") or 0) + (x.get("fee") or 0), 4) if fq else 0,
                               "planned_qty": qty, "planned_avg_px": d["avg_px"], "planned_cost": d["cost"],
                               **({"error": x["error"]} if x.get("error") else {})}
                        _append(rec)
                        if fq > 0:
                            open_pos[tk] = rec; held += rec["cost"]
                            pp = paper_counterpart(tk)
                            if pp:
                                _append({"kind": "parity", "t": time.time(), "ticker": tk,
                                         "paper_qty": pp["qty"], "live_qty": fq,
                                         "paper_avg_px": pp["avg_px"], "live_avg_px": rec["avg_px"],
                                         "paper_cost": pp["cost"], "live_cost": rec["cost"],
                                         "delta_qty": fq - pp["qty"], "delta_cost": round(rec["cost"] - pp["cost"], 4)})
                        if x.get("error"):
                            _append({"kind": "alert", "t": time.time(), "msg": f"order rejected {tk}: {x['error']}"})
            for tk, r in list(open_pos.items()):
                if now < r["close_ts"] + cfg["settle_after_s"]: continue
                mk = _get_public(f"/markets/{tk}").get("market", {}); res = mk.get("result")
                if res not in ("yes", "no"): continue
                win = res == r["side"]; pnl = round((r["qty"] if win else 0) - r["cost"], 4)
                _append({"kind": "settle", "t": time.time(), "ticker": tk, "side": r["side"], "qty": r["qty"],
                         "result": res, "win": win, "pnl": pnl})
                open_pos.pop(tk, None); eq += pnl; held -= r["cost"]
                if not win:
                    losses_24h.append(now)
                    if len([t for t in losses_24h if now - t < 86400]) >= 3 and not halt:
                        halt = True
                        _append({"kind": "halt", "t": now, "msg": "3 losses in 24 h: new entries halted (P32 kill condition); remove STOP-style halt by restart after review"})
            if not REHEARSAL and int(now) % 600 < cfg["poll_s"]:
                b = balance_usd()
                if b is not None and abs((eq - held) - b) > 0.05 and not open_pos:
                    _append({"kind": "alert", "t": now, "msg": f"balance reconciliation off: computed {eq - held:.2f} vs actual {b:.2f}"})
        except Exception as e:
            _append({"kind": "alert", "t": time.time(), "msg": f"error {type(e).__name__}: {e}"})
        time.sleep(cfg["poll_s"])


if __name__ == "__main__":
    run()
