"""S6 sports observer (observe-only, no paper trades until G1 skill is shown): maps Kalshi NBA/NFL game-winner markets to ESPN
scoreboard events, polls score/clock during games, computes a simple in-game win probability and records it against the Kalshi
book for the scoreboard (Brier skill vs market). Also records any post-final non-extreme quotes (verifier edge measurement).
Usage: uv run python -m predhft.sports_observe --loop
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, pathlib, re, time, urllib.request
from .kalshi_public import KalshiPublic, f
from .s7_live import book_touch, single_instance, Phi

H = {"User-Agent": "predhft research"}
ESPN = {"KXNBAGAME": ("basketball/nba", 48.0, 11.5), "KXNFLGAME": ("football/nfl", 60.0, 13.5)}  # (path, regulation minutes, final-margin sd)
def jget(u): return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=20).read())

def scoreboard(path: str) -> list[dict]:
    r = jget(f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"); out = []
    for ev in r.get("events", []):
        comp = ev["competitions"][0]; st_ = comp["status"]; teams = {}
        for c in comp["competitors"]:
            teams[c["team"]["abbreviation"].upper()] = {"score": float(c.get("score") or 0), "home": c["homeAway"] == "home", "name": c["team"]["displayName"]}
        out.append({"id": ev["id"], "date": ev["date"], "state": st_["type"]["state"], "completed": st_["type"]["completed"], "period": st_.get("period", 0),
                    "clock": st_.get("clock", 0.0), "teams": teams, "name": ev.get("shortName", "")})
    return out

def win_prob(margin: float, mins_left: float, final_sd: float, reg_minutes: float) -> float:
    if mins_left <= 0: return 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
    sd = final_sd * math.sqrt(mins_left / reg_minutes)
    return Phi(margin / sd)

def match_event(ticker: str, events: list[dict]):
    # KXNBAGAME-26OCT27PORLAL-LAL -> teams POR/LAL, side LAL
    m = re.match(r"KX(NBA|NFL)GAME-(\d{2}[A-Z]{3}\d{2})([A-Z]+)-([A-Z]+)$", ticker)
    if not m: return None, None
    side = m.group(4); both = m.group(3)
    for ev in events:
        abbrs = set(ev["teams"]); 
        if side in abbrs and any(a != side and both.endswith(a) or both.startswith(a) for a in abbrs if a != side): return ev, side
    return None, None

def run(loop: bool, poll: int):
    root = pathlib.Path("data/sports-s6"); _lock = single_instance(root); root.mkdir(parents=True, exist_ok=True)
    k = KalshiPublic(min_interval_s=0.3); log = root / "runner.log"; score = root / "scoreboard.jsonl"; postfinal = root / "postfinal.jsonl"
    def logline(s):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {s}"; print(line, flush=True); log.open("a").write(line + "\n")
    logline("start sports observer (observe-only)")
    while True:
        try:
            for series, (path, reg, sd) in ESPN.items():
                evs = scoreboard(path); live = [e for e in evs if e["state"] in ("in", "post")]
                if not live: continue
                ms = list(k.markets(series_ticker=series, status="open", limit=200, max_pages=1))
                for m in ms:
                    ev, side = match_event(m["ticker"], live)
                    if not ev: continue
                    other = [a for a in ev["teams"] if a != side]
                    if not other: continue
                    margin = ev["teams"][side]["score"] - ev["teams"][other[0]]["score"]
                    per = ev["period"] or 1; clock = ev["clock"] or 0.0
                    per_len = reg / 4
                    mins_left = max(0.0, (4 - per) * per_len + clock / 60) if per <= 4 else clock / 60
                    p = win_prob(margin, mins_left, sd, reg)
                    try: yb, ya, ybs, yas = book_touch(k.orderbook(m["ticker"]))
                    except Exception: continue
                    mid = (yb + ya) / 2 if ya < 1 else yb
                    row = {"t": time.time(), "family": "sports_s6", "series": series, "ticker": m["ticker"], "p_model": p, "p_market": mid, "margin": margin, "period": per,
                           "mins_left": round(mins_left, 2), "state": ev["state"], "yes_bid": yb, "yes_ask": ya, "bid_size": ybs, "ask_size": yas}
                    score.open("a").write(json.dumps(row) + "\n")
                    if ev["state"] == "post" and ev["completed"] and 0.05 < mid < 0.95:
                        postfinal.open("a").write(json.dumps(row) + "\n"); logline(f"POST-FINAL non-extreme quote {m['ticker']} margin={margin:+.0f} mid={mid:.2f}")
        except Exception as e: logline(f"error {type(e).__name__}: {str(e)[:160]}")
        if not loop: break
        time.sleep(poll)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--loop", action="store_true"); ap.add_argument("--poll", type=int, default=45); a = ap.parse_args(); run(a.loop, a.poll)
