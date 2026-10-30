"""Polymarket 'SOL Up or Down 15m' feed (read-only; Gamma + CLOB public endpoints, no account, no orders, no VPN).
Window alignment: Polymarket's 15-min markets start on quarter hours (slug sol-updown-15m-<window_start_unix>) and resolve Up iff the
Chainlink SOL/USD 60-s TWAP at the end of the window >= the TWAP value at its start - the same structure as KXSOL15M (CF Benchmarks
60-s average before close vs before open), a different index provider. The market for the window that contains `now` is the one
whose close is the Kalshi close being decided at T-10 / T-5.

Cadence: book every POLL_S (10 s; 2 token books per poll); Gamma metadata once per window (+ volume refresh every 60 s).
Writes data/ext/polymarket.jsonl / .latest.json. History helper `price_history()` serves the backfill script.
"""
from __future__ import annotations
import json, sys, time
from . import get_json, write_row, tail_rows

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
POLL_S = 10.0
WINDOW_S = 900


def slug_for(close_ts: int, horizon_s: int = WINDOW_S, asset: str = "sol") -> str:
    """Polymarket slug of the up/down market whose window ends at close_ts (15m: start = close - 900)."""
    tag = {900: "15m", 300: "5m", 3600: "1h", 14400: "4h"}[horizon_s]
    return f"{asset}-updown-{tag}-{int(close_ts) - horizon_s}"


def parse_market(ev: dict) -> dict | None:
    """Gamma event -> {slug, condition_id, up_token, down_token, start, end, volume_usd, closed} (None if malformed)."""
    try:
        m = ev["markets"][0]; toks = json.loads(m["clobTokenIds"]); outs = json.loads(m.get("outcomes") or '["Up","Down"]')
        up_i = outs.index("Up") if "Up" in outs else 0
        return {"slug": ev.get("slug"), "condition_id": m.get("conditionId"), "up_token": toks[up_i], "down_token": toks[1 - up_i], "start": m.get("eventStartTime") or ev.get("startDate"), "end": m.get("endDate") or ev.get("endDate"),
                "volume_usd": float(m.get("volume") or 0.0), "closed": bool(ev.get("closed")), "outcome_prices": m.get("outcomePrices")}
    except Exception: return None


def fetch_market(slug: str) -> dict | None:
    ev = get_json(f"{GAMMA}/events?slug={slug}")
    return parse_market(ev[0]) if ev else None


def parse_book(book: dict, levels: int = 3) -> dict:
    """CLOB book for the Up token -> best bid/ask, mid, spread_c, top-3 depth (shares). CLOB lists bids ascending and asks descending."""
    bids = sorted(((float(x["price"]), float(x["size"])) for x in book.get("bids") or []), key=lambda b: -b[0])
    asks = sorted(((float(x["price"]), float(x["size"])) for x in book.get("asks") or []), key=lambda a: a[0])
    bb = bids[0][0] if bids else None; ba = asks[0][0] if asks else None
    mid = (bb + ba) / 2 if bb is not None and ba is not None else (bb if ba is None else ba)
    return {"up_bid": bb, "up_ask": ba, "up_mid": (round(mid, 4) if mid is not None else None), "spread_c": (round(100 * (ba - bb), 2) if bb is not None and ba is not None else None),
            "up_bid_qty_top3": round(sum(q for _, q in bids[:levels]), 1), "up_ask_qty_top3": round(sum(q for _, q in asks[:levels]), 1), "book_ts_ms": (int(book["timestamp"]) if str(book.get("timestamp", "")).isdigit() else None)}


def fetch_book(token: str) -> dict:
    return parse_book(get_json(f"{CLOB}/book?token_id={token}"))


def price_history(token: str, start_ts: int, end_ts: int, fidelity_min: int = 1) -> list[tuple[int, float]]:
    """CLOB prices-history (Up token) between start_ts and end_ts, ~1 point/minute at fidelity 1 -> [(t, p)]. Backfill only."""
    h = get_json(f"{CLOB}/prices-history?market={token}&startTs={int(start_ts)}&endTs={int(end_ts)}&fidelity={fidelity_min}")
    return [(int(x["t"]), float(x["p"])) for x in h.get("history") or []]


def price_at(hist: list[tuple[int, float]], t: float, max_age_s: float = 180.0) -> tuple[float | None, float | None]:
    """Last history point at or before t (and its age); None if none within max_age_s."""
    best = None
    for ts, p in hist:
        if ts <= t: best = (ts, p)
        else: break
    if best is None or t - best[0] > max_age_s: return None, None
    return best[1], round(t - best[0], 1)


def one_poll(state: dict, now: float) -> dict:
    """One observation of the current window's 15m market (+ the 5-min last-leg market when live). `state` caches tokens per slug."""
    close_ts = int(now // WINDOW_S) * WINDOW_S + WINDOW_S
    slug = slug_for(close_ts)
    row = {"t": now, "close_ts": close_ts, "window_start_ts": close_ts - WINDOW_S, "slug": slug}
    mk = state.get(slug)
    if mk is None or now - state.get(slug + ":meta_t", 0) > 60:
        try: mk = fetch_market(slug); state[slug] = mk; state[slug + ":meta_t"] = now
        except Exception as e: row["gamma_err"] = f"{type(e).__name__}:{str(e)[:60]}"
    if mk:
        row["volume_usd"] = mk.get("volume_usd")
        try: row.update(fetch_book(mk["up_token"]))
        except Exception as e: row["book_err"] = f"{type(e).__name__}:{str(e)[:60]}"
        try:
            tr = get_json(f"https://data-api.polymarket.com/trades?market={mk['condition_id']}&limit=1")
            if tr: row["last_trade_px"] = float(tr[0]["price"]) if tr[0].get("outcome", "Up") == "Up" else round(1 - float(tr[0]["price"]), 4); row["last_trade_t"] = tr[0].get("timestamp")
        except Exception: pass
    else: row["market"] = "missing"
    # 5-min market covering the last leg of this window (exists for the last 5 minutes; before that its start is in the future)
    leg_close = close_ts; leg_slug = slug_for(leg_close, 300)
    if now >= leg_close - 300 - 120:
        lk = state.get(leg_slug)
        if lk is None:
            try: lk = fetch_market(leg_slug); state[leg_slug] = lk
            except Exception: lk = None
        if lk:
            try:
                b = fetch_book(lk["up_token"]); row.update({"leg5m_slug": leg_slug, "leg5m_up_mid": b["up_mid"], "leg5m_up_bid": b["up_bid"], "leg5m_up_ask": b["up_ask"]})
            except Exception: pass
    # 5-min mid change from our own tape
    prev = [r for r in tail_rows("polymarket", now - 330) if r.get("slug") == slug and r.get("up_mid") is not None and r["t"] <= now - 270]
    if prev and row.get("up_mid") is not None: row["mid_change_5m_c"] = round(100 * (row["up_mid"] - prev[0]["up_mid"]), 2)
    for k in list(state):
        if k.split(":")[0] not in (slug, leg_slug): state.pop(k, None)
    return row


def loop(every: float = POLL_S):
    state: dict = {}
    while True:
        t0 = time.time()
        try: write_row("polymarket", one_poll(state, t0))
        except Exception as e: print("polymarket feed error", type(e).__name__, str(e)[:80], file=sys.stderr)
        time.sleep(max(1.0, every - (time.time() - t0)))


if __name__ == "__main__":
    loop()
