"""J1 rain verifier for KXRAIN (per rules card: CLI daily precip strictly > 0; Trace/missing = 0 -> NO; close ~local midnight).

Determined states only:
  YES  when today's METAR hourly P-groups (hundredths of inch) sum >= 0.01, or preliminary CLI 'PRECIPITATION TODAY' >= 0.01.
  NO   is never determined before the final CLI (evening rain can flip); after 23:30 local with 0.00 and no RA in the last obs we
       emit p=0.03 as a near-determined state (scored; traded only with >= margin).
Everything else is scored, not traded. Trading only if the KXRAIN rules card is stored (observability 'high').
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, re, sqlite3, time
from zoneinfo import ZoneInfo
from .kalshi_public import KalshiPublic
from .fees import kalshi_taker_fee
from .s7_live import book_touch, single_instance
from .risk import Ledger, size as risk_size
from .weather_verify import tget, cli_products, parse_series_station, Paper, settle, TZ

def metar_precip(station: str, local_date: dt.date, tz: str) -> dict:
    raw = tget(f"https://aviationweather.gov/api/data/metar?ids={station}&format=raw&hours=30"); z = ZoneInfo(tz)
    total = 0.0; n = 0; last_ra = False; now = dt.datetime.now(dt.timezone.utc)
    for line in raw.splitlines():
        m = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", line)
        if not m: continue
        try: obs = dt.datetime(now.year, now.month, int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=dt.timezone.utc)
        except ValueError: continue
        if obs > now + dt.timedelta(hours=1): obs = obs.replace(month=now.month - 1 or 12)
        if obs.astimezone(z).date() != local_date: continue
        n += 1; last_ra = bool(re.search(r"\b[-+]?(RA|SN|DZ|SH|TS|PL|GR)\b", line.split(" RMK")[0]))
        p = re.search(r"\sP(\d{4})\b", line)
        if p and " SPECI " not in line: total += int(p.group(1)) / 100.0
    return {"total": round(total, 2), "n": n, "last_ra": last_ra}

def cli_precip(code: str, today: str):
    for p in cli_products(code, 3):
        if p.get("for_date") != today: continue
        m = re.search(r"PRECIPITATION \(IN\)\s*\n\s*(?:YESTERDAY|TODAY)\s+(T|\d+\.\d+)", p["text"])
        if m: return 0.0 if m.group(1) == "T" else float(m.group(1)), p["prelim"]
    return None, None

def run_once(k: KalshiPublic, paper: Paper, lot: int, margin: float, db: pathlib.Path, trade: bool):
    c = sqlite3.connect(db)
    ms = list(k.markets(series_ticker="KXRAIN", status="open", limit=500, max_pages=2)); cache: dict = {}
    for m in ms:
        st = parse_series_station(m.get("rules_primary", ""))
        if not st: continue
        z = ZoneInfo(st["tz"]); local_now = dt.datetime.now(z); today = local_now.date()
        d = re.search(r"-(\d{2}[A-Z]{3}\d{2})-", m["ticker"])
        if not d or dt.datetime.strptime(d.group(1), "%y%b%d").date() != today: continue
        try:
            if st["metar"] not in cache: cache[st["metar"]] = (metar_precip(st["metar"], today, st["tz"]), cli_precip(st["cli"], today.isoformat()))
        except Exception as e: paper.logline(f"feed error {m['ticker']}: {type(e).__name__} {str(e)[:80]}"); continue
        met, (cli_v, prelim) = cache[st["metar"]]
        p, basis = None, None
        if (cli_v or 0) >= 0.01: p, basis = 0.99, f"cli_prelim={cli_v}"
        elif met["total"] >= 0.01: p, basis = 0.98, f"metar_P={met['total']}"
        elif local_now.hour == 23 and local_now.minute >= 30 and met["n"] >= 6 and not met["last_ra"]: p, basis = 0.03, "late_day_dry"
        if p is None: continue
        try: yb, ya, ybs, yas = book_touch(k.orderbook(m["ticker"]))
        except Exception: continue
        mid = (yb + ya) / 2 if ya < 1 else yb
        paper.append(paper.score, {"t": time.time(), "family": "rain_j1", "series": "KXRAIN", "ticker": m["ticker"], "p_model": p, "p_market": mid, "basis": basis, "metar": met, "cli": cli_v})
        if not trade or m["ticker"] in paper.open: continue
        side = None
        if p >= 0.97 and p - ya >= margin and 0 < ya < 1 and yas > 0: side, price, avail = "yes", ya, yas
        elif p <= 0.03 and yb - p >= margin and 0 < yb < 1 and ybs > 0: side, price, avail = "no", 1 - yb, ybs
        if side:
            qty, why = risk_size("KXRAIN", p if side == "yes" else 1 - p, price, avail, Ledger(paper.root), lot_cap=lot)
            if qty <= 0: paper.logline(f"SKIP {m['ticker']} {side} {why}"); continue
            t = {"t": time.time(), "ticker": m["ticker"], "series": "KXRAIN", "side": side, "price": price, "qty": qty, "avail_at_touch": avail, "cost": qty * price,
                 "fee": kalshi_taker_fee(qty, price), "p_model": p, "basis": basis, "status": "open", "risk": why, "local_time": local_now.isoformat(timespec="minutes")}
            paper.append(paper.trades, t); paper.open.add(m["ticker"])
            paper.logline(f"PAPER BUY {side.upper()} {qty}@{price:.2f} {m['ticker']} p={p:.2f} basis={basis} avail={avail:.0f}")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--loop", action="store_true"); ap.add_argument("--lot", type=int, default=20)
    ap.add_argument("--margin", type=float, default=0.05); ap.add_argument("--poll", type=int, default=600); a = ap.parse_args()
    root = pathlib.Path("data/rain-j1"); _lock = single_instance(root); paper = Paper(root); k = KalshiPublic(min_interval_s=0.2); db = pathlib.Path("data/universe.db")
    card = sqlite3.connect(db).execute("SELECT card FROM rules_cards WHERE series='KXRAIN'").fetchone()
    trade = bool(card) and json.loads(card[0]).get("observability_before_settlement") == "high"
    paper.logline(f"start rain J1 lot={a.lot} margin={a.margin} trade={trade}")
    while True:
        try: settle(k, paper); run_once(k, paper, a.lot, a.margin, db, trade)
        except Exception as e: paper.logline(f"error {type(e).__name__}: {str(e)[:160]}"); Ledger(root).record_error(str(e))
        if not a.loop: break
        time.sleep(a.poll)

if __name__ == "__main__": main()
