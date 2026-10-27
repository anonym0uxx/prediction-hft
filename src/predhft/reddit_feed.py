"""Reddit feed for the P15 sentiment leg (docs/SENTIMENT_DESIGN_2026-10-27.md §3.1 (a), §5 item 4).
Transport: the Hermes reddit-reading skill script (anonymous Atom feeds, ~1 req/min/IP, no account) run as a subprocess with
PYTHONIOENCODING=utf-8 / PYTHONUTF8=1 (the hidden scheduled task has no console). This module is the ONLY Reddit caller.
One request every 75 s, limit=100, rotation: r/Bitcoin, site search (bitcoin OR ethereum OR solana, sort=new, sub allow-list),
r/solana, r/CryptoCurrency; r/ethereum (quiet) at most every 30 min. Every fetch -> normalised docs -> social_features.ingest();
features re-emitted every cycle from the store (causal: docs carry fetched_ts). Failures write {t, kind, target, ok:false, err} rows to
data/context/reddit.jsonl (never a by_asset block). Old pre-redesign rows in reddit.jsonl are left in place."""
from __future__ import annotations
import calendar, json, os, pathlib, re, subprocess, sys, time
from .sentiment_lex import load_lexicon, score_text, detect_assets, is_sticky, is_news
from . import social_features as sf

ROOT = pathlib.Path("data/context"); LOG = ROOT / "reddit.jsonl"
SCRIPT = pathlib.Path.home() / "AppData/Local/hermes/skills/social-media/reddit-reading/scripts/reddit.py"
SEARCH_QUERY = "bitcoin OR ethereum OR solana"
# Latency design 2026-10-27: ONE multi-subreddit Atom request covers all tracked subs (100 newest posts across them), so every sub is
# refreshed every cycle instead of once per rotation (5 min -> ~60-75 s). Site search every 5th cycle for off-sub coverage.
MULTI_SUBS = ["Bitcoin", "ethereum", "solana", "CryptoCurrency", "BitcoinMarkets", "ethtrader", "CryptoMarkets", "ethfinance"]
MULTI = "+".join(MULTI_SUBS)
ROTATION = [("sub", MULTI), ("sub", MULTI), ("sub", MULTI), ("sub", MULTI), ("search", SEARCH_QUERY)]
ETH_SUB, ETH_EVERY = "ethereum", 1e12   # ethereum is inside MULTI; dedicated fetch disabled
SEARCH_ALLOW = {"bitcoin", "ethereum", "solana", "cryptocurrency", "cryptomarkets", "bitcoinmarkets", "ethtrader", "ethfinance", "btc",
                "cryptocurrencytrading", "altcoin", "solanatrading", "ethstaker", "bitcoinbeginners", "cryptomoonshots", "defi", "web3"}
LAST_ERR = {"msg": ""}


def _log(row: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f: f.write(json.dumps(row) + "\n")


def fetch(kind: str, target: str, limit: int = 100, sort: str = "new") -> tuple[list[dict], float]:
    """kind in {'sub','search'}; returns (posts, fetched_ts). On any failure logs an error row and returns ([], fetched_ts)."""
    t0 = time.time()
    args = [sys.executable, str(SCRIPT), "--json", kind, target, "--sort", sort, "--limit", str(limit)]
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        LAST_ERR["msg"] = f"rc={r.returncode} {r.stderr[-160:].strip()}"
        posts = json.loads(r.stdout)
        if not isinstance(posts, list): raise ValueError("non-list json")
        return posts, t0
    except Exception as e:
        LAST_ERR["msg"] = f"exc {type(e).__name__}: {str(e)[:80]} | " + LAST_ERR["msg"]
        _log({"t": t0, "kind": kind, "target": target, "ok": False, "err": LAST_ERR["msg"]})
        return [], t0


def _created(p: dict) -> float | None:
    if p.get("created_utc") is not None:
        try: return float(p["created_utc"])
        except Exception: pass
    c = p.get("created")
    if not c: return None
    try: return float(calendar.timegm(time.strptime(c[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception: return None


def to_doc(p: dict, fetched_ts: float, lex=None) -> dict | None:
    """Atom/OAuth post -> §3.3 document (id t3_<id> from the permalink, sub from the permalink or 'subreddit')."""
    lex = lex or load_lexicon(); url = p.get("url") or ""
    m = re.search(r"/r/([^/]+)/comments/([a-z0-9]+)", url)
    if not m: return None
    sub, pid = m.group(1), m.group(2); sub = p.get("subreddit") or sub
    ts = _created(p)
    if ts is None: return None
    title, body = p.get("title") or "", p.get("body") or ""
    sc = score_text(title, body, sub, lex)
    return {"src": "reddit", "id": f"t3_{pid}", "sub": sub, "author": (f"u/{p['author']}" if p.get("author") else None), "created_ts": ts,
            "fetched_ts": fetched_ts, "title": title[:1000], "body": body[:4000], "url": url, "label": None,
            "assets": sorted(detect_assets(title, body, sub, lex)), "is_sticky": is_sticky(title, lex), "is_question": sc.is_question,
            "is_news": is_news(title, lex), "pos": sc.pos, "neg": sc.neg, "n_val": sc.n_val, "s_doc": sc.s_doc, "hits": sc.hits[:20],
            "dup_of": None, "lex_version": lex.version}


def to_docs(posts: list[dict], fetched_ts: float, kind: str = "sub", lex=None) -> list[dict]:
    lex = lex or load_lexicon(); out = []
    for p in posts:
        d = to_doc(p, fetched_ts, lex)
        if d is None: continue
        if kind == "search" and d["sub"].lower() not in SEARCH_ALLOW: continue
        out.append(d)
    return out


def schedule(clock=time.time):
    """Infinite (kind, target) generator: ROTATION round-robin, with ('sub','ethereum') inserted when 30 min have passed since its last turn."""
    last_eth = float("-inf"); i = 0
    while True:
        if clock() - last_eth >= ETH_EVERY:
            last_eth = clock(); yield ("sub", ETH_SUB); continue
        yield ROTATION[i % len(ROTATION)]; i += 1


def step(kind: str, target: str, lex=None) -> dict:
    """One fetch -> ingest -> fetch-state -> features emit. Returns the status row written to reddit.jsonl."""
    posts, t0 = fetch(kind, target)
    row = {"t": t0, "kind": kind, "target": target, "ok": bool(posts), "n": len(posts)}
    if posts:
        docs = to_docs(posts, t0, kind, lex); row["n_docs"] = len(docs); row["n_new"] = sf.ingest(docs, sf.STORE)
        row["n_sticky"] = sum(d["is_sticky"] for d in docs)
        sf.mark_fetched("reddit", "search" if kind == "search" else ("multi" if "+" in target else target), t0)
        for s in ({d["sub"] for d in docs} if kind == "search" else (set(target.split("+")) if "+" in target else set())): sf.mark_fetched("reddit", s, t0)
        _log(row)
    else: row["err"] = LAST_ERR["msg"]  # error row already logged by fetch()
    try: sf.emit(time.time())
    except Exception as e: _log({"t": time.time(), "kind": "emit", "ok": False, "err": f"{type(e).__name__}: {str(e)[:80]}"})
    return row


def loop(every: float = 62.0, baseline_every: float = 3600.0):
    ROOT.mkdir(parents=True, exist_ok=True); lex = load_lexicon(); last_base = 0.0
    for kind, target in schedule():
        t0 = time.time()
        step(kind, target, lex)
        if t0 - last_base >= baseline_every:
            try: sf.rebuild_baselines(sf.STORE, t0); last_base = t0
            except Exception as e: _log({"t": t0, "kind": "baselines", "ok": False, "err": f"{type(e).__name__}: {str(e)[:80]}"})
        time.sleep(max(5.0, every - (time.time() - t0)))


def latest(asset: str, root: pathlib.Path | None = None, max_age: float = 900.0) -> dict | None:
    """Backward-compatible shim: the s7_live ctx now reads social_features.latest()."""
    return sf.latest(asset, root, max_age)


if __name__ == "__main__":
    loop()
