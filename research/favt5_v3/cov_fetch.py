"""FavT5 v3 coverage fetcher (READ-ONLY on the exchange; no order route exists in this file).

Stages (argv[1]): windows | index | candles | trades
  windows  every KXBTC15M / KXETH15M market that closed inside the old live span (metadata, strike, official outcome)
  index    official CF Benchmarks history (BRTI, ETHUSD_RTI) through Kalshi's authenticated REST passthrough, raw per hour.
           These are SOURCE timestamps only: a backfill cannot reconstruct when this machine would have received a tick.
  candles  Kalshi 1-minute bid/ask candles for every window (batch endpoint)
  trades   every public trade print for every window (resumable; old live tickers first)
Raw payloads are stored as received under data/favt5-v3/coverage/ with the local fetch time (UTC epoch).
"""
from __future__ import annotations
import gzip, json, pathlib, sys, time, urllib.error, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "src"))
from predhft import kalshi_auth  # noqa: E402  (signing only)

OUT = REPO / "data" / "favt5-v3" / "coverage"
PUB = "https://api.elections.kalshi.com/trade-api/v2"
EXT = "https://external-api.kalshi.com"
SERIES = {"KXBTC15M": "BRTI", "KXETH15M": "ETHUSD_RTI"}
SPAN_START = 1794241924            # 2026-11-09 16:32:04 UTC  first FavT5 live start row
SPAN_END = 1794497700              # 2026-11-12 15:35:00 UTC  owner pause
V1_LIVE_LEDGER = pathlib.Path("D:/prediction-hft/data/fav-t5-live/fills.jsonl")


def pub(path: str, tries: int = 6) -> dict:
    for i in range(tries):
        try:
            time.sleep(0.12)
            return json.load(urllib.request.urlopen(urllib.request.Request(PUB + path, headers={"User-Agent": "predhft-research"}), timeout=60))
        except urllib.error.HTTPError as e:
            if e.code == 400: raise
            time.sleep(2 + 3 * i)
        except Exception:
            time.sleep(2 + 3 * i)
    raise RuntimeError(f"gave up: {path}")


def signed(host: str, path: str, query: str) -> dict:
    full = "/trade-api/v2" + path                                  # Kalshi signs the path without the query string
    h = kalshi_auth.headers("GET", full); h["User-Agent"] = "predhft-research"
    for i in range(6):
        try:
            time.sleep(0.25)
            return json.load(urllib.request.urlopen(urllib.request.Request(host + full + query, headers=h), timeout=90))
        except Exception:
            time.sleep(3 + 4 * i); h = kalshi_auth.headers("GET", full); h["User-Agent"] = "predhft-research"
    raise RuntimeError(f"gave up: {path}{query}")


def windows() -> None:
    out = {}
    for ser in SERIES:
        cur = ""
        while True:
            d = pub(f"/markets?series_ticker={ser}&min_close_ts={SPAN_START}&max_close_ts={SPAN_END}&limit=1000" + (f"&cursor={cur}" if cur else ""))
            for m in d.get("markets", []):
                out[m["ticker"]] = {k: m.get(k) for k in ("ticker", "event_ticker", "open_time", "close_time", "floor_strike", "expiration_value", "result", "status",
                                                          "volume_fp", "open_interest_fp", "strike_type", "price_level_structure", "price_ranges",
                                                          "settlement_ts", "rules_primary", "created_time")} | {"series": ser, "index_id": SERIES[ser]}
            cur = d.get("cursor") or ""
            if not cur or not d.get("markets"): break
    series = {s: pub(f"/series/{s}")["series"] for s in SERIES}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "windows.json").write_text(json.dumps({"fetched_at": time.time(), "span": [SPAN_START, SPAN_END], "series": series, "markets": out}))
    print("windows", len(out), {s: sum(1 for m in out.values() if m["series"] == s) for s in SERIES})


def index() -> None:
    first = (SPAN_START - 3 * 3600) // 3600 * 3600                 # 3 h of lead-in for volatility history
    for idx in SERIES.values():
        d = OUT / "cf_history" / idx; d.mkdir(parents=True, exist_ok=True)
        for h in range(first, SPAN_END + 3600, 3600):
            stamp = time.strftime("%Y%m%dT%H", time.gmtime(h)); f = d / f"{stamp}.json.gz"
            if f.exists(): continue
            iso = time.strftime("%Y-%m-%dT%H:00:00.000Z", time.gmtime(h))
            r = signed(EXT, "/cfbenchmarks/history/values", f"?id={idx}&timespan=HOUR&timestamp={iso}")
            pl = (r.get("data") or {}).get("payload") or []
            with gzip.open(f, "wt", encoding="utf-8") as g:
                json.dump({"index_id": idx, "hour_start": h, "fetched_at": time.time(), "server_time": (r.get("data") or {}).get("serverTime"), "payload": pl}, g)
            print(idx, stamp, len(pl), flush=True)


def candles() -> None:
    mk = json.loads((OUT / "windows.json").read_text())["markets"]
    import calendar
    rows = sorted((calendar.timegm(time.strptime(m["close_time"], "%Y-%m-%dT%H:%M:%SZ")), tk) for tk, m in mk.items())
    out = {}; i = 0
    while i < len(rows):                                            # API cap: tickers x minutes in range <= 10,000
        j = i + 1
        while j < len(rows) and (j + 1 - i) * ((rows[j][0] - rows[i][0]) // 60 + 16) <= 9000 and j - i < 60: j += 1
        ch = rows[i:j]; i = j
        try:
            d = pub(f"/markets/candlesticks?market_tickers={','.join(t for _, t in ch)}&start_ts={ch[0][0] - 900}&end_ts={ch[-1][0]}&period_interval=1")
        except urllib.error.HTTPError as e:
            print("candles 400", ch[0][1], ch[-1][1], e.read()[:200], flush=True); continue
        for m in d.get("markets", []): out[m["market_ticker"]] = m.get("candlesticks", [])
    (OUT / "candles.json").write_text(json.dumps({"fetched_at": time.time(), "candles": out}))
    print("candles", len(out), "of", len(rows))


def trades(budget_s: float) -> None:
    mk = json.loads((OUT / "windows.json").read_text())["markets"]
    live = {json.loads(l).get("ticker") for l in open(V1_LIVE_LEDGER, encoding="utf-8") if '"entry"' in l and '"order_id"' in l}
    order = sorted(mk, key=lambda t: (t not in live, mk[t]["close_time"]))
    d = OUT / "trades"; d.mkdir(parents=True, exist_ok=True); t0 = time.time(); done = 0
    for tk in order:
        f = d / f"{tk}.json.gz"
        if f.exists(): done += 1; continue
        if time.time() - t0 > budget_s: break
        allt = []; cur = ""
        while True:
            r = pub(f"/markets/trades?ticker={tk}&limit=1000" + (f"&cursor={cur}" if cur else ""))
            allt += r.get("trades", []); cur = r.get("cursor") or ""
            if not cur or not r.get("trades"): break
        tmp = f.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as g: json.dump({"ticker": tk, "fetched_at": time.time(), "trades": allt}, g)
        tmp.replace(f); done += 1
        print(tk, len(allt), f"{done}/{len(order)}", flush=True)
    print("trades done", done, "of", len(order))


if __name__ == "__main__":
    stage = sys.argv[1]
    if stage == "windows": windows()
    elif stage == "index": index()
    elif stage == "candles": candles()
    elif stage == "trades": trades(float(sys.argv[2]) if len(sys.argv) > 2 else 3000)
