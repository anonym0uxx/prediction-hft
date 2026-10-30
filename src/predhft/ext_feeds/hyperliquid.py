"""Hyperliquid SOL perp feed (public info API, no account; POST https://api.hyperliquid.xyz/info).
metaAndAssetCtxs (weight 20) every POLL_S -> 60 weight/min of the 1200/min IP budget.
Fields: hourly funding, premium (mark vs oracle), open interest (coins x mark -> USD) and its 15-min change from our own tape,
24-h notional volume. (recentTrades returns only the last 10 prints, ~10 s of tape, so no trade-side flow is derived here; that
would need the websocket `trades` subscription.) Writes data/ext/hyperliquid.jsonl / .latest.json.
Hyperliquid's front end is geo-restricted for US persons; the info API is a public read endpoint and we send no transactions."""
from __future__ import annotations
import sys, time
from . import get_json, write_row, tail_rows

URL = "https://api.hyperliquid.xyz/info"
POLL_S = 20.0
COIN = "SOL"


def parse_ctx(payload: list, coin: str = COIN) -> dict:
    """[meta, ctxs] -> the coin's context in packet units."""
    meta, ctxs = payload[0], payload[1]
    names = [u["name"] for u in meta["universe"]]
    i = names.index(coin); c = ctxs[i]
    mark = float(c["markPx"]); oracle = float(c["oraclePx"]); oi = float(c["openInterest"]); f = float(c["funding"])
    return {"funding_hourly": f, "funding_annualised_pct": round(f * 24 * 365 * 100, 2), "premium_bp": round(1e4 * float(c["premium"]), 2) if c.get("premium") is not None else round(1e4 * (mark / oracle - 1), 2),
            "oi_coins": oi, "oi_usd": round(oi * mark), "day_ntl_vlm_usd": round(float(c["dayNtlVlm"])), "mark_px": mark, "oracle_px": oracle, "mid_px": (float(c["midPx"]) if c.get("midPx") else None)}


def taker_imbalance(trades: list[dict], now_ms: float, windows=(60, 300)) -> dict:
    """recentTrades rows {side 'B' (taker buy) | 'A' (taker sell), px, sz, time ms} -> (buy - sell)/(buy + sell) notional per window."""
    out = {}
    for w in windows:
        buy = sell = 0.0; n = 0
        for t in trades:
            if now_ms - float(t["time"]) > w * 1000: continue
            v = float(t["px"]) * float(t["sz"]); n += 1
            if t["side"] == "B": buy += v
            else: sell += v
        out[f"taker_imb_{w}s"] = round((buy - sell) / (buy + sell), 3) if buy + sell > 0 else None
        out[f"taker_notional_{w}s"] = round(buy + sell); out[f"n_trades_{w}s"] = n
    return out


def one_poll(now: float) -> dict:
    row = {"t": now, "coin": COIN}
    try: row.update(parse_ctx(get_json(URL, {"type": "metaAndAssetCtxs"})))
    except Exception as e: row["ctx_err"] = f"{type(e).__name__}:{str(e)[:60]}"
    if row.get("oi_usd"):
        prev = [r for r in tail_rows("hyperliquid", now - 960) if r.get("oi_usd") and r["t"] <= now - 840]
        if prev: row["oi_chg_15m_bp"] = round(1e4 * (row["oi_usd"] / prev[0]["oi_usd"] - 1), 2)
    return row


def loop(every: float = POLL_S):
    while True:
        t0 = time.time()
        try: write_row("hyperliquid", one_poll(t0))
        except Exception as e: print("hyperliquid feed error", type(e).__name__, str(e)[:80], file=sys.stderr)
        time.sleep(max(1.0, every - (time.time() - t0)))


if __name__ == "__main__":
    loop()
