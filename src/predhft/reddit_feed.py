"""Reddit sentiment feed (P15 sentiment leg) via the Hermes reddit-reading skill script (anonymous Atom feeds, ~1 req/min/IP,
no account). Round-robin over subreddits, one request every 75 s (-> each sub refreshed every 5 min). Per sub: posts in the
last 60/180 min, lexicon sentiment of titles+bodies (bull/bear word counts), and asset mention counts. Writes
data/context/reddit.jsonl and reddit.latest.json {t, subs:{sub:{...}}, by_asset:{BTC:{posts_60m, bull, bear, score}}}."""
from __future__ import annotations
import calendar, json, pathlib, subprocess, sys, time
ROOT = pathlib.Path("data/context"); SCRIPT = pathlib.Path.home() / "AppData/Local/hermes/skills/social-media/reddit-reading/scripts/reddit.py"
SUBS = {"Bitcoin": ["BTC"], "ethereum": ["ETH"], "solana": ["SOL"], "CryptoCurrency": ["BTC", "ETH", "SOL"]}
BULL = ["bull", "moon", "pump", "rally", "breakout", "ath", "buy the dip", "accumulate", "long", "green", "surge", "soar", "up only", "undervalued"]
BEAR = ["bear", "dump", "crash", "sell", "capitulat", "liquidat", "red", "plunge", "collapse", "rug", "short", "fear", "bubble", "overvalued", "scam"]
KEYS = {"BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "eth ", "ether "], "SOL": ["solana", "sol "]}

def fetch(sub: str, limit: int = 25) -> list[dict]:
    r = subprocess.run([sys.executable, str(SCRIPT), "--json", "sub", sub, "--sort", "new", "--limit", str(limit)], capture_output=True, text=True, timeout=180)
    try: return json.loads(r.stdout)
    except Exception: return []

def score(posts: list[dict], now: float) -> dict:
    out = {"posts_60m": 0, "posts_180m": 0, "bull": 0, "bear": 0, "by_asset": {a: {"mentions": 0, "bull": 0, "bear": 0} for a in KEYS}}
    for p in posts:
        try: ts = calendar.timegm(time.strptime(p["created"][:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception: continue
        age = now - ts
        if age <= 3600: out["posts_60m"] += 1
        if age <= 10800: out["posts_180m"] += 1
        txt = ((p.get("title") or "") + " " + (p.get("body") or "")).lower()
        b = sum(txt.count(w) for w in BULL); s = sum(txt.count(w) for w in BEAR); out["bull"] += b; out["bear"] += s
        for a, ks in KEYS.items():
            if any(k in txt for k in ks): out["by_asset"][a]["mentions"] += 1; out["by_asset"][a]["bull"] += b; out["by_asset"][a]["bear"] += s
    out["sent"] = round((out["bull"] - out["bear"]) / (out["bull"] + out["bear"]), 3) if out["bull"] + out["bear"] else None
    return out

def loop(every: float = 75.0):
    ROOT.mkdir(parents=True, exist_ok=True); state = {"t": 0, "subs": {}}; subs = list(SUBS); i = 0
    while True:
        t0 = time.time(); sub = subs[i % len(subs)]; i += 1
        posts = fetch(sub)
        if posts: state["subs"][sub] = {"t": t0, "n": len(posts), **score(posts, t0)}
        agg = {}
        for a in KEYS:
            m = b = s = 0
            for sub2, d in state["subs"].items():
                if a in SUBS[sub2] and t0 - d["t"] <= 1200: x = d["by_asset"][a]; m += x["mentions"]; b += x["bull"]; s += x["bear"]
            agg[a] = {"mentions_recent": m, "bull": b, "bear": s, "sent": (round((b - s) / (b + s), 3) if b + s else None)}
        state["t"] = t0; state["by_asset"] = agg
        (ROOT / "reddit.jsonl").open("a").write(json.dumps({"t": t0, "sub": sub, "ok": bool(posts), **({"sub_stats": state["subs"].get(sub)} if posts else {}), "by_asset": agg}) + "\n")
        (ROOT / "reddit.latest.json").write_text(json.dumps(state))
        time.sleep(max(5.0, every - (time.time() - t0)))

def latest(asset: str, root: pathlib.Path = ROOT, max_age: float = 1500.0) -> dict | None:
    p = root / "reddit.latest.json"
    if not p.exists(): return None
    try: d = json.loads(p.read_text())
    except Exception: return None
    return d.get("by_asset", {}).get(asset) if time.time() - d["t"] <= max_age else None

if __name__ == "__main__":
    loop()
