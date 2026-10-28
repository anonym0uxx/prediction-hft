"""Official settlement feed: Kalshi websocket channel cfbenchmarks_value (CF Benchmarks index tick + running 60-s average).
Read-only. Writes data/cfb/<ASSET>.jsonl (every message) and data/cfb/<ASSET>.latest.json {t, value, avg60, avg60_n, window_start_ms,
window_end_ms, received_at_ms, lag_ms}. Index ids: BRTI -> BTC, ETHUSD_RTI -> ETH, SOLUSD_RTI -> SOL. Reconnects with backoff."""
from __future__ import annotations
import asyncio, json, os, pathlib, sys, time
import websockets
from predhft.kalshi_auth import headers
ROOT = pathlib.Path("data/cfb"); WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"; PATH = "/trade-api/ws/v2"
INDEX = {"BRTI": "BTC", "ETHUSD_RTI": "ETH", "SOLUSD_RTI": "SOL"}

def _log(row: dict):
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "feed.log.jsonl").open("a", encoding="utf-8") as f: f.write(json.dumps(row) + "\n")

PARTIAL: dict[str, list] = {}; WIN: dict[str, int] = {}
def _partial(asset: str, ts_ms: int, value: float) -> tuple[int, float | None, int]:
    """Settlement-aligned partial average: official 1-Hz ticks since (next 15-min boundary - 60 s). Hourly closes are a subset."""
    sec = ts_ms // 1000; nxt = (sec // 900 + 1) * 900; wstart = nxt - 60
    if WIN.get(asset) != wstart: WIN[asset] = wstart; PARTIAL[asset] = []
    if sec >= wstart: PARTIAL[asset].append(value)
    p = PARTIAL[asset]; return wstart, (sum(p) / len(p) if p else None), len(p)

async def run():
    backoff = 2
    while True:
        try:
            async with websockets.connect(WS, additional_headers=headers("GET", PATH), ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["cfbenchmarks_value"], "index_ids": list(INDEX)}}))
                _log({"t": time.time(), "event": "connected"}); backoff = 2; n = 0
                async for raw in ws:
                    m = json.loads(raw); typ = m.get("type")
                    if typ == "cfbenchmarks_value":
                        msg = m["msg"]; asset = INDEX.get(msg.get("index_id"))
                        if not asset: continue
                        try: d = json.loads(msg.get("data") or "{}")
                        except Exception: d = {}
                        a60 = msg.get("avg_60s_data") or {}; now = time.time(); recv = msg.get("received_at")
                        row = {"t": now, "value": float(d.get("value")) if d.get("value") else None, "value_ts_ms": d.get("time"), "received_at_ms": recv,
                               "lag_ms": (int(now * 1000) - int(recv)) if recv else None, "avg60": float(a60["value"]) if a60.get("value") else None,
                               "avg60_n": a60.get("window_size"), "window_start_ms": a60.get("window_start_ts_ms"), "window_end_ms": a60.get("window_end_ts_exclusive"),
                               "q15": (msg.get("last_60s_windowed_average_15min") or {}).get("value")}
                        if row["value"] is not None and d.get("time"):
                            ws_, pt, pn = _partial(asset, int(d["time"]), row["value"]); row.update({"window_start": ws_, "partial_twap": pt, "partial_n": pn})
                        ROOT.mkdir(parents=True, exist_ok=True)
                        with (ROOT / f"{asset}.jsonl").open("a") as f: f.write(json.dumps(row) + "\n")
                        if RAW_CAPTURE: _raw_write(asset, now, raw)                    # verbatim message + local receipt time (AUDIT_2026-10-28 §3)
                        (ROOT / f"{asset}.latest.json").write_text(json.dumps(row)); n += 1
                    elif typ in ("subscribed", "error", "cfbenchmarks_value_indexlist"): _log({"t": time.time(), "event": typ, "msg": m})
        except Exception as e:
            _log({"t": time.time(), "event": "disconnect", "err": f"{type(e).__name__}: {str(e)[:200]}"}); await asyncio.sleep(backoff); backoff = min(backoff * 2, 60)

RAW_CAPTURE = os.environ.get("PREDHFT_CFB_RAW", "1") not in ("0", "false", "no")   # default on; set PREDHFT_CFB_RAW=0 to disable

def _raw_write(asset: str, t_local: float, raw, root: pathlib.Path = ROOT) -> None:
    """Append the verbatim websocket payload with the local receipt time to data/cfb/raw/<ASSET>.<UTC day>.raw.jsonl. The parsed
    rows in <ASSET>.jsonl are what the live decision saw; this file lets a later (possibly revised) CF Benchmarks value be
    compared against the exact message received, and is never read by the runner. Never raises."""
    try:
        d = root / "raw"; d.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y%m%d", time.gmtime(t_local))
        with (d / f"{asset}.{day}.raw.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t_local": t_local, "raw": raw if isinstance(raw, str) else raw.decode("utf-8", "replace")}) + "\n")
    except Exception: pass

def latest(asset: str, root: pathlib.Path = ROOT, max_age: float = 2.5) -> dict | None:
    p = root / f"{asset}.latest.json"
    if not p.exists(): return None
    try: d = json.loads(p.read_text())
    except Exception: return None
    return d if time.time() - d["t"] <= max_age else None

if __name__ == "__main__":
    asyncio.run(run())
