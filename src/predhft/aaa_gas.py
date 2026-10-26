"""Nowcast verifier for AAA national average gas price markets (KXAAAGASD) — recorder first, trading gated by the rules card.
Market dated D closes 03:59Z on D (≈ 11:59 PM ET the evening before) and settles ~11:14Z from AAA's national average for D.
This runner polls https://gasprices.aaa.com/ for the displayed national average (and 'yesterday'), records it against the open
books of the next market, and — only if the stored rules card rates observability 'high' and the AAA value for D is visible
before close — paper-trades determined states (AAA value vs strike with a 0.3c margin for the 4-dp official value).
Usage: uv run python -m predhft.aaa_gas --loop
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, re, time, urllib.request
from .kalshi_public import KalshiPublic, f
from .fees import kalshi_taker_fee
from .s7_live import book_touch, single_instance
from .universe import get_card
from .risk import Ledger, size as risk_size

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}
def aaa_national() -> dict:
    html = urllib.request.urlopen(urllib.request.Request("https://gasprices.aaa.com/", headers=H), timeout=20).read().decode("utf-8", "ignore")
    # national average table: Current Avg / Yesterday Avg / Week Ago ... first numeric cells after 'Regular'
    vals = re.findall(r"\$\s?(\d\.\d{3})", html)
    labels = ["today", "yesterday", "week_ago", "month_ago", "year_ago"]
    out = {labels[i]: float(v) for i, v in enumerate(vals[:5])}
    m = re.search(r"(?:Price as of|as of)\s*([^<]{4,40})", html); out["as_of"] = m.group(1).strip() if m else None
    return out

def run(loop: bool, poll: int, lot: int):
    root = pathlib.Path("data/aaa-gas"); _lock = single_instance(root); root.mkdir(parents=True, exist_ok=True)
    k = KalshiPublic(min_interval_s=0.3); log = root / "runner.log"; score = root / "scoreboard.jsonl"; trades = root / "trades.jsonl"
    def logline(s):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {s}"; print(line, flush=True); log.open("a").write(line + "\n")
    logline("start AAA gas nowcast recorder")
    while True:
        try:
            card = get_card("KXAAAGASD"); tradable = bool(card and card.get("observability_before_settlement") == "high")
            aaa = aaa_national(); ms = list(k.markets(series_ticker="KXAAAGASD", status="open", limit=200, max_pages=1))
            if ms:
                closes = sorted({m["close_time"] for m in ms}); nxt = [m for m in ms if m["close_time"] == closes[0]]
                for m in nxt:
                    K = f(m, "floor_strike") if m.get("floor_strike") is not None else None
                    try: yb, ya, ybs, yas = book_touch(k.orderbook(m["ticker"]))
                    except Exception: continue
                    v = aaa.get("today")
                    p = None
                    if v is not None and K is not None and m.get("strike_type") in ("greater", "greater_or_equal"):
                        p = 1.0 if v > K + 0.003 else 0.0 if v < K - 0.003 else None
                    score.open("a").write(json.dumps({"t": time.time(), "family": "aaa_gas", "ticker": m["ticker"], "p_model": p, "p_market": (yb + ya) / 2, "aaa": aaa, "K": K, "tradable": tradable}) + "\n")
                    if tradable and p is not None:
                        open_t = {json.loads(l)["ticker"] for l in trades.read_text().splitlines() if l.strip()} if trades.exists() else set()
                        if m["ticker"] in open_t: continue
                        side = None
                        if p == 1.0 and 1 - ya >= 0.05 and yas > 0: side, price, avail = "yes", ya, yas
                        elif p == 0.0 and yb >= 0.05 and ybs > 0: side, price, avail = "no", 1 - yb, ybs
                        if side:
                            qty, why = risk_size("KXAAAGASD", 0.99, price, avail, Ledger(root), lot_cap=lot)
                            if qty <= 0: continue
                            t = {"t": time.time(), "ticker": m["ticker"], "series": "KXAAAGASD", "side": side, "price": price, "qty": qty, "cost": qty * price, "fee": kalshi_taker_fee(qty, price),
                                 "p_model": p, "aaa": v, "K": K, "status": "open", "close_ts": dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp(), "risk": why}
                            trades.open("a").write(json.dumps(t) + "\n"); logline(f"PAPER BUY {side.upper()} {qty}@{price:.2f} {m['ticker']} aaa={v} K={K}")
            logline(f"aaa={aaa.get('today')} yday={aaa.get('yesterday')} as_of={aaa.get('as_of')} open_mkts={len(ms)} tradable={tradable}")
            # settle
            if trades.exists():
                from .s7_live import State, settle; settle(k, State(root))
        except Exception as e: logline(f"error {type(e).__name__}: {str(e)[:160]}")
        if not loop: break
        time.sleep(poll)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--loop", action="store_true"); ap.add_argument("--poll", type=int, default=600); ap.add_argument("--lot", type=int, default=20)
    a = ap.parse_args(); run(a.loop, a.poll, a.lot)
