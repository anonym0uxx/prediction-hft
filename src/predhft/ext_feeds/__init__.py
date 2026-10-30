"""P26 external signals (docs/EXTERNAL_SIGNALS_2026-10-29.md; PREREG §P26). Scored-only context for the LLM trader: free public
endpoints, no accounts, read-only, rate-limited well under each venue's published limit. Every feed writes
data/ext/<signal>.jsonl (append) and data/ext/<signal>.latest.json with the receipt time `t`; `packet_block(now)` turns the latest
rows into the packet's `external_signals` section with an age_s and a stale flag per source. Nothing here is a settlement input and
nothing here sizes anything.

Feeds (each its own module, each runnable alone; `python -m predhft.ext_feeds` runs them all in threads):
  polymarket   Polymarket 'SOL Up or Down 15m' (same quarter-hour window as KXSOL15M, Chainlink 60-s TWAP vs TWAP at open):
               Up-token book (best bid/ask, mid, top-3 depth), last trade, volume; plus the 5-min market covering the window's last leg.
  hyperliquid  Hyperliquid SOL perp: funding (hourly), premium (mark vs oracle), open interest and its 15-min change, 24-h notional,
               taker imbalance of the recent trades (60 / 300 s).
  binance_flow Binance spot SOLUSDT (market-data-only mirror data-api.binance.vision): aggressor imbalance and CVD over 60 / 300 s,
               notional, best bid/ask and the Binance mid vs the official index (bp).
  events       ForexFactory weekly calendar (next high-impact USD release; one inside / adjacent to the window) + Coinbase and
               Kraken status pages (constituent exchanges of SOLUSD_RTI).
"""
from __future__ import annotations
import json, pathlib, time, urllib.request

ROOT = pathlib.Path("data/ext")
UA = {"User-Agent": "predhft-ext/0.1 (read-only market data)"}
VERSION = "P26-ext-v1"

# staleness thresholds (s) per source: beyond this the packet marks the block stale and the model is told it is absent evidence
MAX_AGE = {"polymarket": 45.0, "hyperliquid": 90.0, "binance_flow": 90.0, "events": 3 * 3600.0}


def get_json(url: str, data: dict | None = None, timeout: float = 6.0, headers: dict | None = None):
    body = json.dumps(data).encode() if data is not None else None
    h = {**UA, **(headers or {})}
    if body is not None: h["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=timeout) as r:
        return json.loads(r.read())


def write_row(name: str, row: dict, root: pathlib.Path = ROOT) -> None:
    root.mkdir(parents=True, exist_ok=True)
    row = {"t": row.get("t", time.time()), "version": VERSION, **{k: v for k, v in row.items() if k not in ("t", "version")}}
    with (root / f"{name}.jsonl").open("a", encoding="utf-8") as fh: fh.write(json.dumps(row) + "\n")
    tmp = root / f"{name}.latest.json.tmp"; tmp.write_text(json.dumps(row), encoding="utf-8"); tmp.replace(root / f"{name}.latest.json")


def latest(name: str, root: pathlib.Path = ROOT) -> dict | None:
    p = root / f"{name}.latest.json"
    if not p.exists(): return None
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return None


def tail_rows(name: str, since: float, root: pathlib.Path = ROOT, tail_bytes: int = 400_000) -> list[dict]:
    """Rows with t >= since from the end of <name>.jsonl (reads only the tail)."""
    p = root / f"{name}.jsonl"
    if not p.exists(): return []
    size = p.stat().st_size
    with p.open("rb") as fh:
        fh.seek(max(0, size - tail_bytes)); data = fh.read()
    out = []
    for l in data.split(b"\n")[1 if size > tail_bytes else 0:]:
        if not l.strip(): continue
        try: r = json.loads(l)
        except Exception: continue
        if r.get("t") is not None and r["t"] >= since: out.append(r)
    return out


def _aged(row: dict | None, now: float, max_age: float, keep: tuple[str, ...], note: str) -> dict:
    """Packet sub-block: the kept fields of the latest row, age_s, stale flag. Missing -> {stale: True, age_s: None}."""
    if not row: return {"stale": True, "age_s": None, "note": note}
    age = round(now - float(row["t"]), 1)
    out = {k: row.get(k) for k in keep if k in row}
    out.update({"age_s": age, "stale": bool(age > max_age), "note": note})
    return out


POLY_KEEP = ("window_start_ts", "close_ts", "slug", "up_bid", "up_ask", "up_mid", "spread_c", "up_bid_qty_top3", "up_ask_qty_top3", "last_trade_px", "volume_usd", "mid_change_5m_c", "leg5m_slug", "leg5m_up_mid", "leg5m_up_bid", "leg5m_up_ask", "matched_window")
HL_KEEP = ("funding_hourly", "funding_annualised_pct", "premium_bp", "oi_usd", "oi_chg_15m_bp", "day_ntl_vlm_usd", "mark_px", "oracle_px")
BN_KEEP = ("imb_60s", "imb_300s", "notional_60s", "notional_300s", "cvd_300s_usd", "n_trades_300s", "bid", "ask", "mid", "binance_minus_official_bp", "basis_med_15m_bp", "basis_dev_bp", "official_age_s")
EV_KEEP = ("next_high_usd_title", "next_high_usd_in_min", "high_usd_in_window", "high_usd_within_30m_before", "coinbase_status", "kraken_status")


def packet_block(now: float, close_ts: int | None = None, root: pathlib.Path = ROOT) -> dict:
    """The `external_signals` section. Pure read of the latest.json files; each sub-block carries age_s and stale. The Polymarket
    block also says whether its window matches the Kalshi close being decided (`matched_window`); a mismatch is reported, not hidden."""
    pm = latest("polymarket", root)
    if pm is not None and close_ts is not None: pm = {**pm, "matched_window": (int(pm.get("close_ts") or 0) == int(close_ts))}
    return {"version": VERSION,
            "note": "scored-only context from other venues; each block has age_s and stale (stale/mismatched = treat as absent). None of it settles the contract.",
            "polymarket_sol15m": _aged(pm, now, MAX_AGE["polymarket"], POLY_KEEP,
                                       "Polymarket 'SOL Up or Down 15m' on the same quarter-hour window (Up iff Chainlink 60-s TWAP at close >= TWAP at open; Up ~ YES). up_mid is that crowd's price; leg5m = the 5-min market covering the window's last 5 minutes (TWAP at close vs price at T-5), only when it exists."),
            "hyperliquid_sol_perp": _aged(latest("hyperliquid", root), now, MAX_AGE["hyperliquid"], HL_KEEP,
                                          "Hyperliquid SOL perp: hourly funding, premium (mark vs oracle, bp), open interest (USD) and its 15-min change (bp), 24-h notional."),
            "binance_spot_flow": _aged(latest("binance_flow", root), now, MAX_AGE["binance_flow"], BN_KEEP,
                                       "Binance spot SOLUSDT: aggressor imbalance (buy-sell)/(buy+sell) and CVD over 60/300 s; binance_minus_official_bp = Binance mid vs the official index value; basis_dev_bp = that minus its 15-min median (the persistent USDT/USD basis removed): a positive deviation means Binance is currently above where the index has been tracking it."),
            "events": _aged(latest("events", root), now, MAX_AGE["events"], EV_KEEP,
                            "Next high-impact US macro release (ForexFactory) and whether one falls inside this window or in the 30 min before it; constituent-exchange status pages (Coinbase, Kraken).")}
