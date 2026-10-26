"""Inference queue + trigger engine (gaps 5 and 7).

* J4 feed watcher: polls public RSS/Atom feeds (Fed, BLS, NWS alerts, ESPN, AP), dedupes into data/j4/items.jsonl for the hourly
  J4 cron agent, which maps items to markets and stores deltas with `put-forecast --family j4`.
* J3 candidates: liquid markets >= 24 h out in knowable categories -> data/j3/candidates.json for the daily J3 cron agent (which
  forecasts WITHOUT seeing the price; the price is captured here at store time).
* put-forecast: stores an agent forecast as a scoreboard row {family, ticker, p_model, p_market(at store), evidence_ts, ...} so
  predhft.scoreboard scores it against the market once resolved. No trading from J3/J4 until G1 skill is shown.
* staleness detector: markets where a verifier family has emitted a determined p for >= 2 consecutive polls while the mid stays
  >= 5c away -> data/triggers/stale.jsonl (audit trail of unexploited edge).

Usage:
  uv run python -m predhft.inference_queue watch-feeds          # one pass (cron'd every 10 min via task)
  uv run python -m predhft.inference_queue j3-candidates --limit 30
  uv run python -m predhft.inference_queue put-forecast --family j3 --ticker KXCPI-26NOV-T3.1 --p 0.62 --evidence-ts 2026-10-27T10:00:00Z --note "..."
  uv run python -m predhft.inference_queue stale
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, pathlib, re, sqlite3, time, urllib.request
from xml.etree import ElementTree as ET
from .kalshi_public import KalshiPublic, f
from .s7_live import book_touch

H = {"User-Agent": "predhft research"}
FEEDS = {
    "fed_press": "https://www.federalreserve.gov/feeds/press_all.xml",
    "bls": "https://www.bls.gov/feed/news_release/bls_latest.rss",
    "nws_alerts_us": "https://api.weather.gov/alerts/active.atom?status=actual&message_type=alert&severity=Extreme,Severe",
    "espn_nba": "https://www.espn.com/espn/rss/nba/news", "espn_nfl": "https://www.espn.com/espn/rss/nfl/news",
    "ap_top": "https://rsshub.app/apnews/topics/apf-topnews",
}
J3_CATEGORIES = ("Economics", "Financials", "Science and Technology", "Companies", "Health", "Climate and Weather", "Commodities")
ROOT = pathlib.Path("data")

def watch_feeds() -> int:
    out = ROOT / "j4" / "items.jsonl"; out.parent.mkdir(parents=True, exist_ok=True)
    seen = {json.loads(l)["id"] for l in out.read_text().splitlines() if l.strip()} if out.exists() else set(); n = 0
    for name, url in FEEDS.items():
        try: xml = urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=20).read()
        except Exception as e: print(f"{name}: fetch error {e}"); continue
        try: root = ET.fromstring(xml)
        except ET.ParseError: continue
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for it in items[:40]:
            title = (it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link = it.findtext("link") or (it.find("{http://www.w3.org/2005/Atom}link").get("href") if it.find("{http://www.w3.org/2005/Atom}link") is not None else "")
            pub = it.findtext("pubDate") or it.findtext("{http://www.w3.org/2005/Atom}updated") or ""
            iid = hashlib.sha1(f"{name}|{title}|{link}".encode()).hexdigest()[:16]
            if iid in seen or not title: continue
            out.open("a").write(json.dumps({"id": iid, "feed": name, "title": title, "link": link, "published": pub, "seen_at": time.time(), "processed": False}) + "\n"); seen.add(iid); n += 1
    return n

def j3_candidates(limit: int = 30) -> list[dict]:
    c = sqlite3.connect(ROOT / "universe.db"); now = dt.datetime.now(dt.timezone.utc)
    rows = c.execute("""SELECT m.ticker, m.series, s.category, m.title, m.yes_sub_title, m.close_time, m.volume_24h, m.yes_bid, m.yes_ask, m.rules_primary
                        FROM markets m JOIN series s ON s.ticker=m.series WHERE s.category IN (%s) AND m.volume_24h > 500 ORDER BY m.volume_24h DESC LIMIT 400"""
                     % ",".join("?" * len(J3_CATEGORIES)), J3_CATEGORIES).fetchall()
    out = []
    for r in rows:
        close = dt.datetime.fromisoformat(r[5].replace("Z", "+00:00"))
        h = (close - now).total_seconds() / 3600
        if h < 24 or h > 24 * 60: continue
        if r[7] is None or r[8] is None or r[8] - r[7] > 0.12 or r[7] < 0.03 or r[8] > 0.97: continue
        out.append({"ticker": r[0], "series": r[1], "category": r[2], "title": r[3], "sub": r[4], "close_time": r[5], "hours": round(h, 1), "volume_24h": r[6], "rules_primary": r[9]})
        if len(out) >= limit: break
    (ROOT / "j3").mkdir(parents=True, exist_ok=True); (ROOT / "j3" / "candidates.json").write_text(json.dumps(out, indent=1))
    return out

def put_forecast(family: str, ticker: str, p: float, evidence_ts: str, note: str = "", confidence: float | None = None, delta_logit: float | None = None):
    k = KalshiPublic(min_interval_s=0.2)
    try: yb, ya, _, _ = book_touch(k.orderbook(ticker)); mid = (yb + ya) / 2 if ya < 1 else yb
    except Exception: mid = None
    row = {"t": time.time(), "family": family, "ticker": ticker, "p_model": p, "p_market": mid, "evidence_ts": evidence_ts, "note": note[:300], "confidence": confidence, "delta_logit": delta_logit}
    d = ROOT / family; d.mkdir(parents=True, exist_ok=True); (d / "scoreboard.jsonl").open("a").write(json.dumps(row) + "\n")
    return row

def stale(min_gap: float = 0.05) -> list[dict]:
    """Scan verifier scoreboards (weather_j1, aaa_gas) for determined p persisting vs a distant mid."""
    cut = time.time() - 1800; by: dict[str, list] = {}
    for fam in ("weather-j1", "aaa-gas"):
        p = ROOT / fam / "scoreboard.jsonl"
        if not p.exists(): continue
        for l in p.read_text().splitlines():
            if not l.strip(): continue
            r = json.loads(l)
            if r["t"] < cut or r.get("p_model") is None or r.get("p_market") is None: continue
            if (r["p_model"] >= 0.97 or r["p_model"] <= 0.03) and abs(r["p_model"] - r["p_market"]) >= min_gap: by.setdefault(r["ticker"], []).append(r)
    out = [{"ticker": t, "n": len(v), "p_model": v[-1]["p_model"], "p_market": v[-1]["p_market"], "family": v[-1].get("family")} for t, v in by.items() if len(v) >= 2]
    d = ROOT / "triggers"; d.mkdir(exist_ok=True)
    if out: (d / "stale.jsonl").open("a").write(json.dumps({"t": time.time(), "stale": out}) + "\n")
    return out

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("watch-feeds"); j = sub.add_parser("j3-candidates"); j.add_argument("--limit", type=int, default=30)
    pf = sub.add_parser("put-forecast"); pf.add_argument("--family", required=True); pf.add_argument("--ticker", required=True); pf.add_argument("--p", type=float, required=True)
    pf.add_argument("--evidence-ts", required=True); pf.add_argument("--note", default=""); pf.add_argument("--confidence", type=float); pf.add_argument("--delta-logit", type=float)
    sub.add_parser("stale"); a = ap.parse_args()
    if a.cmd == "watch-feeds": print("new items", watch_feeds())
    elif a.cmd == "j3-candidates": print(json.dumps(j3_candidates(a.limit), indent=1)[:4000])
    elif a.cmd == "put-forecast": print(put_forecast(a.family, a.ticker, a.p, a.evidence_ts, a.note, a.confidence, a.delta_logit))
    elif a.cmd == "stale": print(json.dumps(stale(), indent=1))
