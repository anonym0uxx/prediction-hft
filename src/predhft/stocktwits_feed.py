"""Stocktwits public symbol stream (P15 sentiment leg, docs/SENTIMENT_DESIGN_2026-10-27.md §3.1 (b), §5 item 2). No key.
GET https://api.stocktwits.com/api/2/streams/symbol/<SYM>.json [?since=<id>|?max=<id>] -> 30 messages/page, newest first,
cursor {more, since, max}, per-message user label entities.sentiment.basic in {Bullish, Bearish, None}.
Budget: <= 90 req/h total -> 3 symbols x 1 request / 2 min (one request every 40 s, round-robin). Cold start back-pages with ?max=
up to `backfill_pages` per symbol (budgeted; a full 7-day history is not fetched). Features are re-emitted from the store every 60 s.
Must run with PYTHONIOENCODING=utf-8 (hidden scheduled task, no console)."""
from __future__ import annotations
import calendar, json, pathlib, sys, time, urllib.parse, urllib.request
from .sentiment_lex import load_lexicon, score_text, detect_assets, is_news
from . import social_features as sf

ROOT = pathlib.Path("data/context"); LOG = ROOT / "stocktwits.jsonl"
BASE = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"
UA = {"User-Agent": "predhft/0.1 (research; contact via github)", "Accept": "application/json"}
SYMBOLS = ("BTC.X", "ETH.X", "SOL.X")
ASSET_OF = {"BTC.X": "BTC", "ETH.X": "ETH", "SOL.X": "SOL"}
LAST = {"err": "", "cursor": None, "status": None}


def fetch_stream(symbol: str, since_id: int | None = None, max_id: int | None = None, timeout: float = 15.0) -> list[dict]:
    """One request. Returns the raw message list (newest first); LAST['cursor'] holds {more, since, max}; raises on HTTP/parse error."""
    q = {}
    if since_id: q["since"] = int(since_id)
    if max_id: q["max"] = int(max_id)
    url = BASE.format(sym=symbol) + (("?" + urllib.parse.urlencode(q)) if q else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace")); LAST["status"] = r.status
    LAST["cursor"] = d.get("cursor"); LAST["err"] = ""
    return d.get("messages") or []


def _ts(iso: str) -> float:
    return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))


def normalize_message(m: dict, fetched_ts: float, symbol: str | None = None, lex=None) -> dict:
    """Stocktwits message -> §3.3 document. Message text is stored as `title` (single-field message) so question detection and
    scoring see it once; body is ''."""
    lex = lex or load_lexicon()
    syms = [s.get("symbol") for s in (m.get("symbols") or []) if s.get("symbol")]
    sym = symbol or (syms[0] if syms else "")
    text = m.get("body") or ""
    lab = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
    assets = detect_assets(text, "", "", lex) | {ASSET_OF[s] for s in syms if s in ASSET_OF}
    if sym in ASSET_OF: assets.add(ASSET_OF[sym])
    sc = score_text(text, "", sym, lex)
    user = m.get("user") or {}
    return {"src": "stocktwits", "id": f"st_{m['id']}", "sub": sym, "author": f"st_{user.get('id')}" if user.get("id") else None,
            "created_ts": _ts(m["created_at"]), "fetched_ts": fetched_ts, "title": text[:4000], "body": "", "url": None, "label": lab,
            "assets": sorted(assets), "is_sticky": False, "is_question": sc.is_question, "is_news": is_news(text, lex), "pos": sc.pos, "neg": sc.neg,
            "n_val": sc.n_val, "s_doc": sc.s_doc, "hits": sc.hits[:20], "dup_of": None, "lex_version": lex.version}


def _log(row: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f: f.write(json.dumps(row) + "\n")


def poll(symbol: str, state: dict, backfill_pages: int = 6, lex=None) -> int:
    """One budgeted request for `symbol`: cold start pages back with ?max= (up to backfill_pages), then ?since=<newest id>.
    Ingests normalised docs, marks the fetch; returns number of lines written."""
    t0 = time.time(); s = state.setdefault(symbol, {"since": None, "max": None, "pages": 0})
    try:
        if s["since"] is None or (s["pages"] < backfill_pages and s.get("more", True) and s["max"]):
            msgs = fetch_stream(symbol, max_id=s["max"] if s["since"] is not None else None)
        else:
            msgs = fetch_stream(symbol, since_id=s["since"])
    except Exception as e:
        LAST["err"] = f"{type(e).__name__}: {str(e)[:100]}"; _log({"t": t0, "symbol": symbol, "ok": False, "err": LAST["err"]}); return 0
    cur = LAST["cursor"] or {}
    if msgs:
        ids = [m["id"] for m in msgs]
        s["since"] = max(s["since"] or 0, max(ids)); s["max"] = min(ids) if (s["max"] is None or min(ids) < s["max"]) else s["max"]
        s["pages"] += 1; s["more"] = bool(cur.get("more", True))
    docs = [normalize_message(m, t0, symbol, lex) for m in msgs]
    n = sf.ingest(docs, sf.STORE); sf.mark_fetched("stocktwits", symbol, t0)
    _log({"t": t0, "symbol": symbol, "ok": True, "n": len(msgs), "n_new": n, "since": s["since"], "max": s["max"], "pages": s["pages"]})
    return n


def loop(symbols: tuple[str, ...] = SYMBOLS, every: float = 40.0, emit_every: float = 60.0, baseline_every: float = 3600.0):
    lex = load_lexicon(); state: dict = {}; i = 0; last_emit = 0.0; last_base = 0.0
    while True:
        t0 = time.time(); sym = symbols[i % len(symbols)]; i += 1
        poll(sym, state, lex=lex)
        if t0 - last_base >= baseline_every:
            try: sf.rebuild_baselines(sf.STORE, t0); last_base = t0
            except Exception as e: _log({"t": t0, "ok": False, "err": f"baselines {type(e).__name__}: {str(e)[:80]}"})
        if t0 - last_emit >= emit_every:
            try: sf.emit(time.time()); last_emit = t0
            except Exception as e: _log({"t": t0, "ok": False, "err": f"emit {type(e).__name__}: {str(e)[:80]}"})
        time.sleep(max(5.0, every - (time.time() - t0)))


if __name__ == "__main__":
    loop(tuple(sys.argv[1].split(",")) if len(sys.argv) > 1 else SYMBOLS)
