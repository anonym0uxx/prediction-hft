"""Binance spot SOLUSDT order flow via the market-data-only mirror (https://data-api.binance.vision; Binance documents it as the
no-authentication public market data host; api.binance.com itself answers HTTP 451 from this location and is not used).
Every POLL_S: aggTrades?limit=1000 (weight 4) + ticker/bookTicker (weight 2) -> aggressor imbalance and CVD over 60 / 300 s,
notional, best bid/ask, and the Binance mid vs the official SOLUSD_RTI value from data/cfb/SOL.latest.json (bp).
Binance is the largest SOL spot venue and leads price discovery in the lead-lag literature; the official index is built from
Coinbase / Kraken / Bitstamp / Gemini / crypto.com books, so a persistent Binance premium is a candidate lead of the index.
Writes data/ext/binance_flow.jsonl / .latest.json. Backfill helper `agg_trades_between()` (startTime/endTime <= 1 h)."""
from __future__ import annotations
import json, pathlib, sys, time
import statistics
from . import get_json, write_row, tail_rows

HOST = "https://data-api.binance.vision"
SYMBOL = "SOLUSDT"
POLL_S = 15.0
CFB_LATEST = pathlib.Path("data/cfb/SOL.latest.json")


def flow_stats(trades: list[dict], now_ms: float, windows=(60, 300)) -> dict:
    """aggTrades rows {p, q, T, m}: m = buyer is maker -> the TAKER sold. Imbalance (buy-sell)/(buy+sell) in notional, CVD in USD."""
    out = {}
    for w in windows:
        buy = sell = 0.0; n = 0
        for t in trades:
            if now_ms - float(t["T"]) > w * 1000: continue
            v = float(t["p"]) * float(t["q"]); n += 1
            if t["m"]: sell += v
            else: buy += v
        out[f"imb_{w}s"] = round((buy - sell) / (buy + sell), 3) if buy + sell > 0 else None
        out[f"notional_{w}s"] = round(buy + sell); out[f"cvd_{w}s_usd"] = round(buy - sell); out[f"n_trades_{w}s"] = n
    if trades: out["oldest_trade_age_s"] = round((now_ms - float(min(float(t["T"]) for t in trades))) / 1000, 1)
    return out


def agg_trades_between(start_ms: int, end_ms: int, symbol: str = SYMBOL) -> list[dict]:
    """Historical aggTrades in [start_ms, end_ms] (<= 1 h span; pages of 1000). Backfill only."""
    out = []; cur = start_ms
    for _ in range(60):
        page = get_json(f"{HOST}/api/v3/aggTrades?symbol={symbol}&startTime={cur}&endTime={end_ms}&limit=1000")
        if not page: break
        out += page
        last = int(page[-1]["T"])
        if len(page) < 1000 or last >= end_ms: break
        cur = last + 1
    return out


def one_poll(now: float) -> dict:
    row = {"t": now, "symbol": SYMBOL}
    try: row.update(flow_stats(get_json(f"{HOST}/api/v3/aggTrades?symbol={SYMBOL}&limit=1000"), now * 1000))
    except Exception as e: row["trades_err"] = f"{type(e).__name__}:{str(e)[:60]}"
    try:
        bt = get_json(f"{HOST}/api/v3/ticker/bookTicker?symbol={SYMBOL}")
        row["bid"] = float(bt["bidPrice"]); row["ask"] = float(bt["askPrice"]); row["mid"] = round((row["bid"] + row["ask"]) / 2, 4)
        try:
            cfb = json.loads(CFB_LATEST.read_text(encoding="utf-8"))
            row["official_age_s"] = round(now - cfb["value_ts_ms"] / 1000, 1); row["binance_minus_official_bp"] = round(1e4 * (row["mid"] / float(cfb["value"]) - 1), 2)
            hist = [r["binance_minus_official_bp"] for r in tail_rows("binance_flow", now - 900) if r.get("binance_minus_official_bp") is not None]
            if len(hist) >= 10:
                row["basis_med_15m_bp"] = round(statistics.median(hist), 2); row["basis_dev_bp"] = round(row["binance_minus_official_bp"] - row["basis_med_15m_bp"], 2)
        except Exception: pass
    except Exception as e: row["ticker_err"] = f"{type(e).__name__}:{str(e)[:60]}"
    return row


def loop(every: float = POLL_S):
    while True:
        t0 = time.time()
        try: write_row("binance_flow", one_poll(t0))
        except Exception as e: print("binance_flow feed error", type(e).__name__, str(e)[:80], file=sys.stderr)
        time.sleep(max(1.0, every - (time.time() - t0)))


if __name__ == "__main__":
    loop()
