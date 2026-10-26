"""Weather verifier (J1) — PAPER. Kalshi daily high/low temperature markets (KXHIGH*, KXLOW*) settle on the NWS CLI daily climate
report (relayed by The Weather Company). The preliminary CLI (~4:30 PM local, 'VALID TODAY AS OF 0400 PM') publishes the official
max/min-so-far from the same ASOS sensor as the final, while trading stays open until 05:00Z. Before that, hourly METAR gives a
running proxy (under-reads the CLI max by 0-2 F). This module:
  1. maps each series to its CLI location (parsed from rules_primary: 'recorded at <City> (CLIXXX)') and METAR station KXXX,
  2. fetches preliminary/final CLI products from api.weather.gov and METARs from aviationweather.gov,
  3. computes a verifier probability per open market for TODAY's local date with conservative margins,
  4. paper-fills against resting size when |p - price| clears fees + margin, logs scoreboard rows, settles from Kalshi result.
Usage: uv run python -m predhft.weather_verify --loop        (runs forever, polls every 5 min)
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, pathlib, re, sqlite3, time, urllib.request
from zoneinfo import ZoneInfo
from .kalshi_public import KalshiPublic, f
from .fees import kalshi_taker_fee
from .s7_live import book_touch

H = {"User-Agent": "predhft research (alon)"}
def jget(u, timeout=20):
    return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=timeout).read())
def tget(u, timeout=20):
    return urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=timeout).read().decode("utf-8", "ignore")

# CLI location -> (METAR station, IANA tz). Default station = 'K' + code; override where NWS CLI code differs from ICAO.
TZ = {"NYC": "America/New_York", "PHL": "America/New_York", "BOS": "America/New_York", "DCA": "America/New_York", "EWR": "America/New_York",
      "TTN": "America/New_York", "ATL": "America/New_York", "MIA": "America/New_York", "SDF": "America/New_York",
      "MDW": "America/Chicago", "MSP": "America/Chicago", "DFW": "America/Chicago", "AUS": "America/Chicago", "SAT": "America/Chicago",
      "HOU": "America/Chicago", "MSY": "America/Chicago", "OKC": "America/Chicago",
      "DEN": "America/Denver", "PHX": "America/Phoenix", "LAS": "America/Los_Angeles", "LAX": "America/Los_Angeles",
      "SFO": "America/Los_Angeles", "SAN": "America/Los_Angeles", "SEA": "America/Los_Angeles"}
STATION = {"NYC": "KNYC"}

def parse_series_station(rules_primary: str):
    m = re.search(r"\((CLI([A-Z]{3}))\)", rules_primary or "")
    if not m: return None
    code = m.group(2); return {"cli": code, "metar": STATION.get(code, "K" + code), "tz": TZ.get(code, "America/New_York")}

def cli_products(code: str, n: int = 4) -> list[dict]:
    r = jget(f"https://api.weather.gov/products/types/CLI/locations/{code}"); out = []
    for g in r.get("@graph", [])[:n]:
        txt = jget(g["@id"])["productText"]
        out.append({"issued": g["issuanceTime"], "text": txt, **parse_cli(txt)})
    return out

def parse_cli(txt: str) -> dict:
    d = {"prelim": "VALID TODAY AS OF" in txt, "max": None, "min": None, "for_date": None}
    m = re.search(r"CLIMATE SUMMARY FOR ([A-Z]+ \d{1,2} \d{4})", txt)
    if m:
        try: d["for_date"] = dt.datetime.strptime(m.group(1), "%B %d %Y").date().isoformat()
        except ValueError: pass
    mx = re.search(r"MAXIMUM\s+(-?\d+)\s", txt); mn = re.search(r"MINIMUM\s+(-?\d+)\s", txt)
    if mx: d["max"] = int(mx.group(1))
    if mn: d["min"] = int(mn.group(1))
    return d

def metar_running(station: str, local_date: dt.date, tz: str) -> dict:
    """Running max/min (F) from hourly METAR T-groups for the local calendar day so far; returns ints or None."""
    raw = tget(f"https://aviationweather.gov/api/data/metar?ids={station}&format=raw&hours=30")
    z = ZoneInfo(tz); vals = []
    for line in raw.splitlines():
        m = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", line); t = re.search(r"\bT(\d)(\d{3})(\d)(\d{3})\b", line)
        if not m: continue
        day, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
        now = dt.datetime.now(dt.timezone.utc); month, year = now.month, now.year
        try: obs = dt.datetime(year, month, day, hh, mm, tzinfo=dt.timezone.utc)
        except ValueError: continue
        if obs > now + dt.timedelta(hours=1): obs = obs.replace(month=month - 1 or 12)
        if obs.astimezone(z).date() != local_date: continue
        if t: c = (-1 if t.group(1) == "1" else 1) * int(t.group(2)) / 10.0
        else:
            g = re.search(r"\s(M?\d{2})/(M?\d{2})\s", line)
            if not g: continue
            c = -int(g.group(1)[1:]) if g.group(1).startswith("M") else int(g.group(1))
        vals.append(c * 9 / 5 + 32)
    return {"max": (math.floor(max(vals) + 0.5) if vals else None), "min": (math.floor(min(vals) + 0.5) if vals else None), "n": len(vals)}

def verify(kind: str, strike_type: str, floor_k, cap_k, cli: dict | None, metar: dict, local_now: dt.datetime) -> tuple[float | None, str]:
    """Return (p_yes, basis). kind 'high' or 'low'. Conservative: only emit when the state is (near-)determined."""
    hour = local_now.hour + local_now.minute / 60
    if kind == "high":
        # official max-so-far from prelim CLI (exact) else METAR max (under-reads by 0-2F)
        if cli and cli.get("prelim") and cli.get("max") is not None:
            M, exact, basis = cli["max"], True, "prelim_cli"
        elif metar.get("max") is not None:
            M, exact, basis = metar["max"], False, "metar"
        else: return None, "nodata"
        # chance the day's final max exceeds M further: late-day heating after 16:00 is rare; before noon it's likely
        p_more = 0.02 if hour >= 17 else 0.08 if hour >= 16 else 0.25 if hour >= 14 else None
        if p_more is None: return None, "too_early"
        slack = 0 if exact else 2   # METAR under-read margin
        if strike_type == "greater":
            if M > floor_k: return 1.0 - 0.0, basis          # already above: locked in (max can only rise)
            if M + slack < floor_k - 0: return p_more * 0.5 if floor_k - M <= 2 else 0.01, basis
            return None, "ambiguous"
        if strike_type == "less":
            if M >= cap_k: return 0.0, basis                 # already failed
            if M + slack < cap_k: return 1.0 - p_more * (1 if cap_k - M <= 2 else 0.2), basis
            return None, "ambiguous"
        if strike_type == "between":
            if M > cap_k: return 0.0, basis
            if exact and floor_k <= M <= cap_k: return 1.0 - p_more * (1 if M == cap_k else 0.5), basis
            if M + slack < floor_k: return 0.02 if floor_k - M > 2 else p_more * 0.5, basis
            return None, "ambiguous"
    else:  # low: the min is usually set near sunrise; after ~10:00 it is locked in unless a cold front arrives late
        if cli and cli.get("prelim") and cli.get("min") is not None: m_, exact, basis = cli["min"], True, "prelim_cli"
        elif metar.get("min") is not None: m_, exact, basis = metar["min"], False, "metar"
        else: return None, "nodata"
        if hour < 10: return None, "too_early"
        p_lower = 0.05 if hour < 17 else 0.10   # evening cooling before midnight can undercut the morning low
        slack = 0 if exact else 2
        if strike_type == "greater":   # low > K: fails if min <= K already
            if m_ <= floor_k: return 0.0, basis
            if m_ - slack > floor_k: return 1.0 - p_lower * (1 if m_ - floor_k <= 2 else 0.2), basis
            return None, "ambiguous"
        if strike_type == "less":
            if m_ < cap_k: return 1.0, basis
            if m_ - slack >= cap_k: return p_lower * 0.5 if m_ - cap_k <= 2 else 0.01, basis
            return None, "ambiguous"
        if strike_type == "between":
            if m_ < floor_k: return 0.0, basis
            if exact and floor_k <= m_ <= cap_k: return 1.0 - p_lower * (1 if m_ == floor_k else 0.5), basis
            if m_ - slack > cap_k: return 0.02 if m_ - cap_k > 2 else p_lower * 0.5, basis
            return None, "ambiguous"
    return None, "unhandled"

class Paper:
    def __init__(self, root: pathlib.Path):
        self.root = root; root.mkdir(parents=True, exist_ok=True)
        self.trades = root / "trades.jsonl"; self.score = root / "scoreboard.jsonl"; self.log = root / "runner.log"
        self.open = {json.loads(l)["ticker"] for l in self.trades.read_text().splitlines() if l.strip() and json.loads(l).get("status") == "open"} if self.trades.exists() else set()
    def logline(self, m):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {m}"; print(line, flush=True); self.log.open("a").write(line + "\n")
    def append(self, p, o): p.open("a").write(json.dumps(o) + "\n")

def settle(k: KalshiPublic, paper: Paper):
    if not paper.trades.exists(): return
    trades = [json.loads(l) for l in paper.trades.read_text().splitlines() if l.strip()]; changed = False
    for t in trades:
        if t.get("status") != "open": continue
        try: m = k.get(f"/markets/{t['ticker']}")["market"]
        except Exception: continue
        if m.get("result") in ("yes", "no"):
            win = m["result"] == t["side"]; t["status"] = "settled"; t["result"] = m["result"]
            t["pnl"] = (1.0 if win else 0.0) * t["qty"] - t["cost"] - t["fee"]; changed = True; paper.open.discard(t["ticker"])
            paper.logline(f"SETTLED {t['ticker']} side={t['side']} result={m['result']} qty={t['qty']} pnl={t['pnl']:+.2f} basis={t['basis']}")
    if changed: paper.trades.write_text("".join(json.dumps(t) + "\n" for t in trades))

def run_once(k: KalshiPublic, paper: Paper, lot: int, margin: float, db: pathlib.Path):
    c = sqlite3.connect(db)
    rows = c.execute("SELECT series, rules_primary FROM markets WHERE (series LIKE 'KXHIGH%' OR series LIKE 'KXLOW%') AND rules_primary LIKE '%(CLI%' GROUP BY series").fetchall()
    cli_cache: dict[str, list] = {}; metar_cache: dict[str, dict] = {}
    for series, rp in rows:
        st = parse_series_station(rp)
        if not st: continue
        kind = "high" if "HIGH" in series else "low"; z = ZoneInfo(st["tz"]); local_now = dt.datetime.now(z); today = local_now.date()
        try: ms = list(k.markets(series_ticker=series, status="open", limit=200, max_pages=1))
        except Exception as e: paper.logline(f"markets error {series}: {e}"); continue
        # only markets whose observation day is today (ticker date part e.g. 26OCT27)
        todays = [m for m in ms if re.search(r"-(\d{2}[A-Z]{3}\d{2})-", m["ticker"]) and dt.datetime.strptime(re.search(r"-(\d{2}[A-Z]{3}\d{2})-", m["ticker"]).group(1), "%y%b%d").date() == today]
        if not todays: continue
        try:
            if st["cli"] not in cli_cache: cli_cache[st["cli"]] = cli_products(st["cli"], 3)
            if st["metar"] not in metar_cache: metar_cache[st["metar"]] = metar_running(st["metar"], today, st["tz"])
        except Exception as e: paper.logline(f"feed error {series}: {type(e).__name__} {str(e)[:80]}"); continue
        cli_today = next((p for p in cli_cache[st["cli"]] if p.get("for_date") == today.isoformat()), None)
        metar = metar_cache[st["metar"]]
        for m in todays:
            p, basis = verify(kind, m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike"), cli_today, metar, local_now)
            if p is None: continue
            try: yb, ya, ybs, yas = book_touch(k.orderbook(m["ticker"]))
            except Exception: continue
            mid = (yb + ya) / 2 if ya < 1 else yb
            paper.append(paper.score, {"t": time.time(), "family": "weather_j1", "series": series, "ticker": m["ticker"], "p_model": p, "p_market": mid, "basis": basis,
                                       "cli_max": cli_today and cli_today.get("max"), "cli_min": cli_today and cli_today.get("min"), "metar": metar})
            if m["ticker"] in paper.open: continue
            side = None
            # J1 trades ONLY determined states; probabilistic cases (heating/cooling fudge factors) are scored, never bet.
            if p >= 0.97 and p - ya >= margin and 0 < ya < 1 and yas > 0: side, price, avail = "yes", ya, yas
            elif p <= 0.03 and yb - p >= margin and 0 < yb < 1 and ybs > 0: side, price, avail = "no", 1 - yb, ybs
            if side:
                qty = int(min(lot, avail)); cost = qty * price; fee = kalshi_taker_fee(qty, price)
                t = {"t": time.time(), "ticker": m["ticker"], "series": series, "side": side, "price": price, "qty": qty, "avail_at_touch": avail, "cost": cost, "fee": fee,
                     "p_model": p, "basis": basis, "status": "open", "local_time": local_now.isoformat(timespec="minutes")}
                paper.append(paper.trades, t); paper.open.add(m["ticker"])
                paper.logline(f"PAPER BUY {side.upper()} {qty}@{price:.2f} {m['ticker']} p={p:.2f} basis={basis} cli_max={cli_today and cli_today.get('max')} metar_max={metar.get('max')} avail={avail:.0f}")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--loop", action="store_true"); ap.add_argument("--lot", type=int, default=20)
    ap.add_argument("--margin", type=float, default=0.05); ap.add_argument("--poll", type=int, default=300); a = ap.parse_args()
    from .s7_live import single_instance
    root = pathlib.Path("data/weather-j1"); _lock = single_instance(root); paper = Paper(root); k = KalshiPublic(min_interval_s=0.15)
    paper.logline(f"start weather J1 verifier lot={a.lot} margin={a.margin}")
    while True:
        try: run_once(k, paper, a.lot, a.margin, pathlib.Path("data/universe.db")); settle(k, paper)
        except Exception as e: paper.logline(f"error {type(e).__name__}: {str(e)[:160]}")
        if not a.loop: break
        time.sleep(a.poll)

if __name__ == "__main__": main()
