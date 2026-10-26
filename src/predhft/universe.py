"""Universe store (SQLite): all Kalshi series + open/recent markets, refreshed on a schedule; rules-card queue and store.
Rules cards are produced per SERIES (rules are templated), filled with market-level parameters mechanically.

Usage:
  uv run python -m predhft.universe refresh            # pull series + open markets into data/universe.db
  uv run python -m predhft.universe queue [--limit N]  # print series needing a rules card, priority first
  uv run python -m predhft.universe show KXBTCD        # print the raw material a rules-reader subagent needs
  uv run python -m predhft.universe put-card KXBTCD path/to/card.json
"""
from __future__ import annotations
import argparse, json, pathlib, sqlite3, time, datetime as dt
from .kalshi_public import KalshiPublic, f

DB = pathlib.Path("data/universe.db")
PRIORITY = ["KXBTCD", "KXETHD", "KXSOLD", "KXBTC", "KXETH", "KXSOL", "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHDEN", "KXHIGHAUS",
            "KXRAIN", "KXINXU", "KXNASDAQ100U", "KXNBAGAME", "KXNFLGAME", "KXNCAAFGAME", "KXMLBGAME", "KXAAAGASD", "KXTRUMPMENTION", "KXRT"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (ticker TEXT PRIMARY KEY, title TEXT, category TEXT, frequency TEXT, fee_type TEXT, fee_multiplier REAL,
  contract_terms_url TEXT, settlement_sources TEXT, additional_prohibitions TEXT, product_metadata TEXT, raw TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, series TEXT, event TEXT, title TEXT, yes_sub_title TEXT, status TEXT, open_time TEXT,
  close_time TEXT, expiration_time TEXT, strike_type TEXT, floor_strike REAL, cap_strike REAL, rules_primary TEXT, rules_secondary TEXT,
  settlement_timer_seconds INTEGER, can_close_early INTEGER, volume REAL, volume_24h REAL, open_interest REAL, yes_bid REAL, yes_ask REAL,
  result TEXT, updated_at REAL);
CREATE INDEX IF NOT EXISTS idx_markets_series ON markets(series);
CREATE TABLE IF NOT EXISTS rules_cards (series TEXT PRIMARY KEY, card TEXT, model TEXT, created_at REAL, rules_hash TEXT);
CREATE TABLE IF NOT EXISTS refresh_log (ts REAL, series_n INTEGER, markets_n INTEGER, seconds REAL);
"""

def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True); c = sqlite3.connect(DB); c.executescript(SCHEMA); return c

def refresh(max_pages: int = 60) -> tuple[int, int]:
    k = KalshiPublic(min_interval_s=0.12); c = db(); t0 = time.time(); now = time.time()
    # series
    sr = k.get("/series", limit=1000)
    series = sr.get("series", []) if isinstance(sr, dict) else []
    for s in series:
        c.execute("INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (s["ticker"], s.get("title"), s.get("category"), s.get("frequency"), s.get("fee_type"), s.get("fee_multiplier"),
                   s.get("contract_terms_url"), json.dumps(s.get("settlement_sources")), json.dumps(s.get("additional_prohibitions")),
                   json.dumps(s.get("product_metadata")), json.dumps(s), now))
    # open markets (nested through events to get series tickers)
    n = 0; cursor = None
    for _ in range(max_pages):
        r = k.get("/events", limit=200, status="open", with_nested_markets="true", cursor=cursor); evs = r.get("events", [])
        for e in evs:
            for m in e.get("markets", []):
                c.execute("INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (m["ticker"], e.get("series_ticker"), e.get("event_ticker"), m.get("title") or e.get("title"), m.get("yes_sub_title"), m.get("status"),
                           m.get("open_time"), m.get("close_time"), m.get("expiration_time"), m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"),
                           m.get("rules_primary"), m.get("rules_secondary"), m.get("settlement_timer_seconds"), int(bool(m.get("can_close_early"))),
                           f(m, "volume_fp"), f(m, "volume_24h_fp"), f(m, "open_interest_fp"), f(m, "yes_bid_dollars"), f(m, "yes_ask_dollars"), m.get("result"), now))
                n += 1
        cursor = r.get("cursor")
        if not cursor or not evs: break
    c.execute("INSERT INTO refresh_log VALUES (?,?,?,?)", (now, len(series), n, time.time() - t0)); c.commit()
    return len(series), n

def queue(limit: int = 50) -> list[dict]:
    c = db()
    rows = c.execute("""SELECT m.series, COUNT(*) n, SUM(COALESCE(m.volume_24h,0)) v24, MIN(m.close_time) next_close
                        FROM markets m LEFT JOIN rules_cards r ON r.series=m.series WHERE r.series IS NULL AND m.series IS NOT NULL
                        GROUP BY m.series""").fetchall()
    def pri(r): return (0 if r[0] in PRIORITY else 1, PRIORITY.index(r[0]) if r[0] in PRIORITY else 0, -(r[2] or 0))
    rows.sort(key=pri)
    return [{"series": r[0], "open_markets": r[1], "volume_24h": r[2], "next_close": r[3]} for r in rows[:limit]]

def show(series: str) -> dict:
    """Raw material for a rules-reader subagent: series record + rules text + 3 sample markets (distinct templates)."""
    c = db(); s = c.execute("SELECT raw FROM series WHERE ticker=?", (series,)).fetchone()
    ms = c.execute("SELECT ticker,title,yes_sub_title,open_time,close_time,expiration_time,strike_type,floor_strike,cap_strike,rules_primary,rules_secondary,"
                   "settlement_timer_seconds,can_close_early,volume_24h,yes_bid,yes_ask FROM markets WHERE series=? ORDER BY volume_24h DESC LIMIT 3", (series,)).fetchall()
    keys = ["ticker","title","yes_sub_title","open_time","close_time","expiration_time","strike_type","floor_strike","cap_strike","rules_primary","rules_secondary",
            "settlement_timer_seconds","can_close_early","volume_24h","yes_bid","yes_ask"]
    return {"series": json.loads(s[0]) if s else None, "sample_markets": [dict(zip(keys, m)) for m in ms]}

def put_card(series: str, card: dict, model: str = "subagent"):
    import hashlib
    c = db(); rp = c.execute("SELECT rules_primary FROM markets WHERE series=? LIMIT 1", (series,)).fetchone()
    h = hashlib.sha256((rp[0] or "").encode()).hexdigest()[:16] if rp else ""
    c.execute("INSERT OR REPLACE INTO rules_cards VALUES (?,?,?,?,?)", (series, json.dumps(card), model, time.time(), h)); c.commit()

def get_card(series: str) -> dict | None:
    r = db().execute("SELECT card FROM rules_cards WHERE series=?", (series,)).fetchone(); return json.loads(r[0]) if r else None

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh"); q = sub.add_parser("queue"); q.add_argument("--limit", type=int, default=50)
    s = sub.add_parser("show"); s.add_argument("series"); p = sub.add_parser("put-card"); p.add_argument("series"); p.add_argument("path")
    a = ap.parse_args()
    if a.cmd == "refresh": print(refresh())
    elif a.cmd == "queue": print(json.dumps(queue(a.limit), indent=1))
    elif a.cmd == "show": print(json.dumps(show(a.series), indent=1))
    elif a.cmd == "put-card": put_card(a.series, json.load(open(a.path))); print("ok")
