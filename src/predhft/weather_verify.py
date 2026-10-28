"""Weather settlement-lock taker (J1-v2) — PAPER. Kalshi daily high/low temperature markets (KXHIGH*, KXLOW*).

Settlement (per market rules_primary + GLOBALTEMPERATURE terms): The Weather Company daily climate value for station CLIXXX on the
named date = the NWS CLI daily figure (whole degrees F) for the LOCAL STANDARD TIME calendar day. Lock logic, rules resolution,
observation-day filtering, exposure grouping, population monitor and forward-fill definitions live in weather_lock.py (pure, tested);
this module only does I/O:
  1. markets  : /markets per series (open) -> resolve_rules(market) from the market's own rules text; keep markets whose settlement
                window is running now; markets with rule flags (date mismatch, unknown source/unit/station) are scored, never traded
  2. feeds    : CLI products (api.weather.gov, prelim 'VALID [TODAY] AS OF' or final) for the settlement date; METARs
                (aviationweather.gov JSON, obsTime epoch) filtered by TIMESTAMP into [window_start, window_end)
  3. evaluate : weather_lock.evaluate -> locked (basis locked_<src>) or scored-only; every row appended to scoreboard.jsonl (additive schema)
  4. trade    : locked + not suspended + executable gap >= margin -> RE-FETCH the book (book_recv_t > signal_recv_t), forward simulated
                fill at offer + tick up to displayed size, Kalshi taker fee; raw offer logged separately; station-day exposure cap
  5. monitor  : every locked call in the scoreboard population (traded or not) is compared with the result at settlement; a wrong lock
                suspends its logic cell (src x kind x strike_type) and its station via suspensions.json + an outbox alert
  6. op costs : per-cycle API call counts, feed errors and durations -> opcost.jsonl
Usage: uv run python -m predhft.weather_verify --loop --lot 20 --margin 0.025 --poll 120
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, re, sqlite3, time, urllib.request
from zoneinfo import ZoneInfo
from .kalshi_public import KalshiPublic
from .fees import kalshi_taker_fee
from .s7_live import book_sides
from .risk import Ledger, size as risk_size
from . import weather_lock as WL

H = {"User-Agent": "predhft research (alon)"}
FAMILY = "weather_j1"
CFG = {"station_day_cap": 12.0,          # max open cost per station-day group (all series/sides sharing one CLI/TWC station-day)
       "monitor_grace_s": 2 * 3600,      # check results this long after the settlement window ends
       "metar_hours": 36}

class OpCost:
    def __init__(self): self.c = {"kalshi": 0, "nws": 0, "awc": 0, "feed_errors": 0, "rows": 0, "fills": 0, "markets": 0}
    def inc(self, k, n=1): self.c[k] = self.c.get(k, 0) + n

OPS = OpCost()
def jget(u, timeout=20, kind="nws"):
    OPS.inc(kind); return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=timeout).read())
def tget(u, timeout=20):
    OPS.inc("nws"); return urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=timeout).read().decode("utf-8", "ignore")

# compatibility exports for rain_verify (station table lives in weather_lock)
TZ = WL.TZ; STATION = WL.STATION
def parse_series_station(rules_primary: str):
    m = re.search(r"\((CLI([A-Z]{3}))\)", rules_primary or "")
    if not m: return None
    code = m.group(2); return {"cli": code, "metar": STATION.get(code, "K" + code), "tz": TZ.get(code, "America/New_York")}
def ts_close(m: dict) -> float: return dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()

# ----------------------------------------------------------------------------------------------------------------------- feeds
_CLI_TEXT: dict[str, dict] = {}   # product @id -> parsed product (texts are immutable; only the list changes)
def parse_cli(txt: str) -> dict:
    """NWS CLI product -> {prelim, max, min, for_date}. Preliminary products say 'VALID TODAY AS OF ..' or 'VALID AS OF ..'; the
    final (next-morning) product has neither and reports YESTERDAY's figures under the same CLIMATE SUMMARY FOR date."""
    d = {"prelim": bool(re.search(r"VALID (TODAY )?AS OF", txt)), "max": None, "min": None, "for_date": None}
    m = re.search(r"CLIMATE SUMMARY FOR ([A-Z]+ \d{1,2} \d{4})", txt)
    if m:
        try: d["for_date"] = dt.datetime.strptime(m.group(1), "%B %d %Y").date().isoformat()
        except ValueError: pass
    mx = re.search(r"MAXIMUM\s+(-?\d+)\s", txt); mn = re.search(r"MINIMUM\s+(-?\d+)\s", txt)
    if mx: d["max"] = int(mx.group(1))
    if mn: d["min"] = int(mn.group(1))
    return d

def cli_products(code: str, n: int = 4) -> list[dict]:
    r = jget(f"https://api.weather.gov/products/types/CLI/locations/{code}"); out = []
    for g in r.get("@graph", [])[:n]:
        if g["@id"] not in _CLI_TEXT:
            txt = jget(g["@id"])["productText"]; _CLI_TEXT[g["@id"]] = {"issued": g["issuanceTime"], **parse_cli(txt)}
        out.append(_CLI_TEXT[g["@id"]])
    return out

def cli_for_date(products: list[dict], date: dt.date) -> dict | None:
    """Latest CLI product for the settlement date (prelim or final); None when NWS has not published one for that day yet."""
    ps = [p for p in products if p.get("for_date") == date.isoformat()]
    return max(ps, key=lambda p: p.get("issued") or "") if ps else None

def metar_obs(icao: str, hours: int = CFG["metar_hours"]) -> list[tuple[float, float]]:
    """[(obs epoch, temp F)] from aviationweather JSON; obsTime is explicit (no day arithmetic). T-group tenths preferred."""
    OPS.inc("awc")
    r = json.loads(urllib.request.urlopen(urllib.request.Request(f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json&hours={hours}", headers=H), timeout=20).read())
    out = []
    for o in r:
        ts = o.get("obsTime")
        if ts is None: continue
        v = WL.parse_metar_temp_f(o.get("rawOb") or "")
        if v is None and o.get("temp") is not None: v = float(o["temp"]) * 9 / 5 + 32
        if v is not None: out.append((float(ts), v))
    return sorted(out)

# ----------------------------------------------------------------------------------------------------------------------- state
class Paper:
    def __init__(self, root: pathlib.Path):
        self.root = root; root.mkdir(parents=True, exist_ok=True)
        self.trades = root / "trades.jsonl"; self.score = root / "scoreboard.jsonl"; self.log = root / "runner.log"; self.opcost = root / "opcost.jsonl"
        self.first_lock = root / "first_lock.json"; self.first_lock_v2 = root / "first_lock_v2.json"; self.results = root / "results_cache.json"
        self.suspensions = WL.Suspensions(root / "suspensions.json"); self.outbox = root / "outbox"
        self.open_positions = [t for t in self.load_trades() if t.get("status") == "open"]
        self.open = {t["ticker"] for t in self.open_positions}
    def load_trades(self) -> list[dict]:
        return [json.loads(l) for l in self.trades.read_text().splitlines() if l.strip()] if self.trades.exists() else []
    def logline(self, m):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {m}"; print(line, flush=True); self.log.open("a").write(line + "\n")
    def append(self, p, o): p.open("a").write(json.dumps(o) + "\n")
    def _json(self, p, default):
        try: return json.loads(p.read_text()) if p.exists() else default
        except Exception: return default
    def alert(self, kind: str, payload: dict):
        self.outbox.mkdir(exist_ok=True); fn = self.outbox / f"{int(time.time())}_{kind}_{payload.get('ticker', '')}.json"
        fn.write_text(json.dumps({"t": time.time(), "kind": kind, **payload}, indent=1)); self.logline(f"ALERT {kind} {json.dumps(payload)[:300]}")

def record_first_lock(paper: Paper, ticker: str, ev: dict, rules: WL.Rules, now: float | None = None) -> float | None:
    """First locked observation per ticker (legacy first_lock.json: ticker -> t; v2: the call itself for the population monitor)."""
    if not ev.get("locked"): return None
    now = now or time.time(); d = paper._json(paper.first_lock, {}); d2 = paper._json(paper.first_lock_v2, {})
    if ticker not in d: d[ticker] = now; paper.first_lock.write_text(json.dumps(d))
    if ticker not in d2:
        d2[ticker] = {"t": now, "p": ev["p"], "lock_src": ev["lock_src"], "kind": rules.kind, "strike_type": rules.strike_type, "cli": rules.cli,
                      "station_day": rules.station_day, "value": ev["lock_value"], "window_end": rules.window_end, "close_ts": rules.close_ts}
        paper.first_lock_v2.write_text(json.dumps(d2))
    return d[ticker]

# ----------------------------------------------------------------------------------------------------------------------- population monitor
def monitor(k: KalshiPublic, paper: Paper, now: float | None = None):
    """Compare EVERY locked call (traded or not) with the settled result; a wrong call suspends logic cell + station and raises an alert.
    A market still 'closed' an hour past its expected expiration (Kalshi data-error hold) raises a 'held_market' alert once."""
    now = now or time.time(); d2 = paper._json(paper.first_lock_v2, {}); res = paper._json(paper.results, {}); changed = False
    for tk, fl in d2.items():
        if tk in res or (fl.get("window_end") or 0) + CFG["monitor_grace_s"] > now: continue
        try: m = k.get(f"/markets/{tk}")["market"]; OPS.inc("kalshi")
        except Exception: OPS.inc("feed_errors"); continue
        r = m.get("result")
        if r not in ("yes", "no"):
            exp = m.get("expected_expiration_time")
            if m.get("status") == "closed" and exp and not fl.get("held_alerted") and dt.datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp() + 3600 < now:
                fl["held_alerted"] = True; paper.first_lock_v2.write_text(json.dumps(d2))
                paper.alert("held_market", {"ticker": tk, "lock": fl, "note": "closed past expected expiration without a result (possible data-error hold); excluded from projections until settled"})
            continue
        res[tk] = r; changed = True
        if WL.wrong_locks({tk: fl}, {tk: r}):
            detail = f"locked p={fl['p']} value={fl.get('value')} src={fl['lock_src']} but result={r} (expiration_value={m.get('expiration_value')})"
            paper.suspensions.suspend(fl["lock_src"], fl["kind"], fl["strike_type"], fl["cli"], tk, detail, now)
            paper.alert("wrong_lock", {"ticker": tk, "detail": detail, "suspended": [WL.logic_key(fl["lock_src"], fl["kind"], fl["strike_type"]), fl["cli"]]})
    if changed: paper.results.write_text(json.dumps(res))

# ----------------------------------------------------------------------------------------------------------------------- settle & report
def settle(k: KalshiPublic, paper: Paper):
    if not paper.trades.exists(): return
    trades = paper.load_trades(); changed = False
    for t in trades:
        if t.get("status") != "open": continue
        try: m = k.get(f"/markets/{t['ticker']}")["market"]; OPS.inc("kalshi")
        except Exception: OPS.inc("feed_errors"); continue
        if m.get("result") in ("yes", "no"):
            win = m["result"] == t["side"]; t["status"] = "settled"; t["result"] = m["result"]; t["expiration_value"] = m.get("expiration_value")
            t["pnl"] = (1.0 if win else 0.0) * t["qty"] - t["cost"] - t["fee"]; changed = True; paper.open.discard(t["ticker"])
            Ledger(paper.root, family=paper.root.name).record_settle(t["ticker"], t["pnl"])
            paper.logline(f"SETTLED {t['ticker']} side={t['side']} result={m['result']} qty={t['qty']} pnl={t['pnl']:+.2f} basis={t['basis']}")
    if changed:
        paper.trades.write_text("".join(json.dumps(t) + "\n" for t in trades)); paper.open_positions = [t for t in trades if t.get("status") == "open"]

def report(root: pathlib.Path, days: int = 14) -> str:
    """Locked states and executable gaps (>=5c with size>=20) by local hour and side (PREREG P4/P7); forward fills summary."""
    cut = time.time() - days * 86400; by: dict = {}
    for l in (root / "scoreboard.jsonl").read_text().splitlines() if (root / "scoreboard.jsonl").exists() else []:
        r = json.loads(l)
        if r["t"] < cut or not str(r.get("basis", "")).startswith("locked_") or r.get("yes_bid") is None or r.get("p_model") is None: continue
        kind = "low" if "LOW" in r["ticker"] else "high"; h = r.get("local_hour")
        gap = (1 - r["yes_ask"]) if r["p_model"] >= 0.5 else r["yes_bid"]; size = r.get("ask_size") if r["p_model"] >= 0.5 else r.get("bid_size")
        k_ = (kind, h); d = by.setdefault(k_, {"rows": 0, "tickers": set(), "opp": set()}); d["rows"] += 1; d["tickers"].add(r["ticker"])
        if gap >= 0.05 and (size or 0) >= 20: d["opp"].add(r["ticker"])
    lines = ["kind hour locked_tickers gap>=5c&size>=20"]
    for (kind, h), d in sorted(by.items(), key=lambda x: (x[0][0], x[0][1] if x[0][1] is not None else -1)): lines.append(f"{kind} {h:>2} {len(d['tickers']):>4} {len(d['opp']):>4}")
    tr = [json.loads(l) for l in (root / "trades.jsonl").read_text().splitlines() if l.strip()] if (root / "trades.jsonl").exists() else []
    st = [t for t in tr if t.get("status") == "settled"]
    lines.append(f"forward fills: {len(tr)} ({len(st)} settled, net {sum(t.get('pnl', 0) for t in st):+.2f} $); open exposure by station-day: {WL.exposure_by_group([t for t in tr if t.get('status') == 'open'])}")
    return "\n".join(lines)

# ----------------------------------------------------------------------------------------------------------------------- one cycle
def run_once(k: KalshiPublic, paper: Paper, lot: int, margin: float, db: pathlib.Path, disagree_tol: int = WL.DEFAULTS["disagree_tol_f"], station_day_cap: float = CFG["station_day_cap"]):
    now = time.time()
    series_rows = sqlite3.connect(db).execute("SELECT series, rules_primary FROM markets WHERE (series LIKE 'KXHIGH%' OR series LIKE 'KXLOW%') AND rules_primary LIKE '%(CLI%' GROUP BY series").fetchall()
    cli_cache: dict[str, list] = {}; metar_cache: dict[str, list] = {}
    for series, series_rp in series_rows:
        try: ms = list(k.markets(series_ticker=series, status="open", limit=200, max_pages=1)); OPS.inc("kalshi")
        except Exception as e: paper.logline(f"markets error {series}: {e}"); OPS.inc("feed_errors"); continue
        live = []
        for m in ms:
            ru = WL.resolve_rules(m, series_rp)
            if ru and ru.window_start <= now < (ru.close_ts or ru.window_end): live.append((m, ru))
        if not live: continue
        OPS.inc("markets", len(live)); ru0 = live[0][1]
        try:
            if ru0.cli not in cli_cache: cli_cache[ru0.cli] = cli_products(ru0.cli, 4)
            if ru0.icao not in metar_cache: metar_cache[ru0.icao] = metar_obs(ru0.icao)
        except Exception as e:
            paper.logline(f"feed error {series}: {type(e).__name__} {str(e)[:80]}"); OPS.inc("feed_errors"); Ledger(paper.root, family=paper.root.name).record_error(f"feed {series}: {e}"); continue
        for m, ru in live:
            cli = cli_for_date(cli_cache[ru.cli], ru.date)
            obs = WL.filter_obs(metar_cache[ru.icao], (ru.window_start, ru.window_end), as_of=now)
            met = WL.running_extremes(obs); z = ZoneInfo(ru.tz); ln = dt.datetime.now(z)
            ev = WL.evaluate(ru, cli, met, ln.hour + ln.minute / 60, disagree_tol)
            if ev["p"] is None and ev["basis"] != "source_disagree": continue
            try: b = book_sides(k.orderbook(m["ticker"])); OPS.inc("kalshi"); signal_recv_t = time.time()
            except Exception: OPS.inc("feed_errors"); continue
            yb, ya = b["yes_bid"], b["yes_ask"]
            mid = ((yb + ya) / 2 if (yb is not None and ya is not None) else (yb if ya is None else ya))
            susp = paper.suspensions.suspended(ev["lock_src"], ru.kind, ru.strike_type, ru.cli) if ev["locked"] else None
            row = {"t": signal_recv_t, "family": FAMILY, "series": series, "ticker": m["ticker"], "p_model": ev["p"], "p_market": mid, "basis": ev["basis"], "local_hour": ln.hour,
                   "yes_bid": (yb if yb is not None else 0.0), "yes_ask": (ya if ya is not None else 1.0), "bid_size": b["yes_bid_size"], "ask_size": b["yes_ask_size"],
                   "first_lock_t": record_first_lock(paper, m["ticker"], ev, ru, signal_recv_t), "cli_max": cli and cli.get("max"), "cli_min": cli and cli.get("min"),
                   "metar": {"max": met["max"], "min": met["min"], "n": met["n"], "max2": met["max2"], "min2": met["min2"], "first_ts": met["first_ts"], "last_ts": met["last_ts"]}, "metar_ok": met["n"] > 0,
                   # J1-v2 additive fields
                   "v": 2, "lock_src": ev["lock_src"], "lock_value": ev["lock_value"], "slack": ev["slack"], "station_day": ru.station_day, "window_start": ru.window_start, "window_end": ru.window_end,
                   "cli_prelim": cli and cli.get("prelim"), "cli_issued": cli and cli.get("issued"), "rules_flags": ru.flags, "source": ru.source, "suspended": susp,
                   "yes_bid_present": b["yes_bid_present"], "yes_ask_present": b["yes_ask_present"]}
            paper.append(paper.score, row); OPS.inc("rows")
            if not ev["locked"] or m["ticker"] in paper.open or ru.flags or susp: continue
            p = ev["p"]; side = None
            if p >= 0.97 and ya is not None and p - ya >= margin and b["yes_ask_size"] > 0: side, price0, avail0 = "yes", ya, b["yes_ask_size"]
            elif p <= 0.03 and yb is not None and yb - p >= margin and b["yes_bid_size"] > 0: side, price0, avail0 = "no", 1 - yb, b["yes_bid_size"]
            if not side: continue
            p_side = p if side == "yes" else 1 - p
            # station-day exposure group (all open positions sharing this CLI station and settlement day, any series / side)
            room = WL.group_room(paper.open_positions, ru.station_day, station_day_cap)
            if room < price0 * 5: paper.logline(f"SKIP {m['ticker']} {side} station-day cap {ru.station_day} room={room:.2f}"); continue
            led = Ledger(paper.root, family=paper.root.name)
            qty, why = risk_size(series, p_side, price0, avail0, led, lot_cap=lot, p_market=(mid if side == "yes" else 1 - mid) if mid is not None else None,
                                 fee_per_contract=kalshi_taker_fee(10, price0) / 10, close_ts=ru.close_ts, event_date=ru.date.isoformat())
            if qty <= 0: paper.logline(f"SKIP {m['ticker']} {side} {why}"); continue
            qty = int(min(qty, room // max(price0, 0.01)))
            # forward fill: re-fetch the book AFTER the signal; fill at re-checked offer + tick up to re-checked size; the signal book never sets the price
            try: b2 = book_sides(k.orderbook(m["ticker"])); OPS.inc("kalshi"); book_recv_t = time.time()
            except Exception: OPS.inc("feed_errors"); continue
            fill = WL.simulate_fill(side, b2, qty, book_recv_t, signal_recv_t, kalshi_taker_fee)
            if not fill or p_side - fill["price"] < margin:
                paper.logline(f"NOFILL {m['ticker']} {side} recheck offer={fill and fill['raw_offer_price']} size={fill and fill['raw_offer_size']} signal_offer={price0:.2f}"); continue
            if fill["qty"] < 5: paper.logline(f"NOFILL {m['ticker']} {side} recheck size {fill['qty']} < min lot"); continue
            t = {"t": book_recv_t, "ticker": m["ticker"], "series": series, "side": side, "price": fill["price"], "qty": fill["qty"], "cost": fill["cost"], "fee": fill["fee"],
                 "avail_at_touch": fill["raw_offer_size"], "raw_offer_price": fill["raw_offer_price"], "raw_offer_size": fill["raw_offer_size"], "signal_offer_price": price0, "signal_offer_size": avail0,
                 "signal_recv_t": signal_recv_t, "book_recv_t": book_recv_t, "recheck_latency_s": fill["recheck_latency_s"], "p_model": p, "basis": ev["basis"], "lock_src": ev["lock_src"], "lock_value": ev["lock_value"],
                 "station_day": ru.station_day, "cli": ru.cli, "kind": ru.kind, "strike_type": ru.strike_type, "source": ru.source, "close_ts": ru.close_ts, "status": "open", "risk": why,
                 "local_time": ln.isoformat(timespec="minutes"), "v": 2}
            paper.append(paper.trades, t); paper.open.add(m["ticker"]); paper.open_positions.append(t); OPS.inc("fills")
            led.record_open(m["ticker"], series, t["cost"], close_ts=ru.close_ts, event_date=ru.date.isoformat(), side=side)
            paper.logline(f"PAPER FILL {side.upper()} {t['qty']}@{t['price']:.2f} {m['ticker']} p={p:.2f} basis={ev['basis']} value={ev['lock_value']} cli={cli and (cli.get('max'), cli.get('min'))} metar=({met['max']},{met['min']},n={met['n']}) raw_offer={fill['raw_offer_price']:.2f}x{fill['raw_offer_size']:.0f} latency={fill['recheck_latency_s']:.2f}s group={ru.station_day} room={room:.2f}")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--loop", action="store_true"); ap.add_argument("--lot", type=int, default=20)
    ap.add_argument("--margin", type=float, default=0.05); ap.add_argument("--poll", type=int, default=300); ap.add_argument("--report", action="store_true")
    ap.add_argument("--disagree-tol", type=int, default=WL.DEFAULTS["disagree_tol_f"]); ap.add_argument("--station-day-cap", type=float, default=CFG["station_day_cap"]); a = ap.parse_args()
    if a.report: print(report(pathlib.Path("data/weather-j1"))); return
    from .s7_live import single_instance
    root = pathlib.Path("data/weather-j1"); _lock = single_instance(root); paper = Paper(root); k = KalshiPublic(min_interval_s=0.15)
    paper.logline(f"start weather J1-v2 lock taker lot={a.lot} margin={a.margin} poll={a.poll} disagree_tol={a.disagree_tol} station_day_cap={a.station_day_cap} suspensions={paper.suspensions.d}")
    while True:
        t0 = time.time(); OPS.__init__()
        try: run_once(k, paper, a.lot, a.margin, pathlib.Path("data/universe.db"), a.disagree_tol, a.station_day_cap); settle(k, paper); monitor(k, paper)
        except Exception as e: paper.logline(f"error {type(e).__name__}: {str(e)[:160]}"); Ledger(paper.root, family=paper.root.name).record_error(str(e)); OPS.inc("feed_errors")
        paper.append(paper.opcost, {"t": t0, "dur_s": round(time.time() - t0, 2), "poll_s": a.poll, **OPS.c})
        if not a.loop: break
        time.sleep(max(1.0, a.poll - (time.time() - t0)))

if __name__ == "__main__": main()
