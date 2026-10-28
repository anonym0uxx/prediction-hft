"""Weather settlement-lock logic (J1-v2, 2026-10-28) — pure functions, no I/O, no network. The runner (weather_verify.py) is a thin shell.

What settles a Kalshi daily temperature market (read from /markets/{ticker}.rules_primary + series terms GLOBALTEMPERATURE):
  "If the maximum temperature recorded at <City> (CLIXXX) for <Mon D, YYYY>, is <phrase> fahrenheit according to The Weather Company,
   then the market resolves to Yes."  Settlement source = The Weather Company daily climate table (weather.com/kalshi), which republishes
  the NWS CLI daily climate figures (station CLIXXX) in whole degrees F; TWC ranks before NWS in the terms. The CLI day is the LOCAL
  STANDARD TIME calendar day (midnight-to-midnight LST, never DST), so during DST the settlement day runs 01:00-01:00 local clock.
Supporting observations (never settlement by themselves): the preliminary / final CLI products from api.weather.gov and hourly METARs
  from the same ASOS (aviationweather.gov). They establish an outcome only when the observed running extreme, after precision slack
  and a source-disagreement check, mathematically forces the outcome regardless of any later temperature:
      high  greater K  : running max >= K+1+slack  -> YES ;  running max below K          -> nothing (the max can still rise)
      high  less K     : running max >= K+slack    -> NO  ;  never YES intraday
      high  between    : running max >  cap+slack  -> NO  ;  inside or below the bucket   -> nothing
      low   greater K  : running min <= K-slack    -> NO  ;  never YES intraday
      low   less K     : running min <= K-1-slack  -> YES ;  running min above K          -> nothing (the min can still fall)
      low   between    : running min <  floor-slack-> NO  ;  inside or above the bucket   -> nothing
  A running extreme never establishes a 'between' bucket as YES and never establishes 'less'(high) / 'greater'(low) as YES.
Why the disagreement check: KXHIGHTMIN-26OCT27 — the CLI (and TWC) published a 61 F maximum timed 03:41 AM LST while every hourly
  and 1-minute METAR of the day stayed <= 56 F; Kalshi held the event past its expected expiration. A single source therefore does not
  lock; CLI and METAR must agree within `disagree_tol` and the conservative one is used; a METAR extreme must be supported by a second
  observation within `disagree_tol` (single-reading spikes do not lock).
Everything that is not a lock is scored only (probability kept for the scoreboard, never traded).
"""
from __future__ import annotations
import dataclasses, datetime as dt, json, math, pathlib, re, time
from zoneinfo import ZoneInfo

# CLI location code -> IANA tz. METAR station = 'K' + code unless overridden (NYC = Central Park KNYC).
TZ = {"NYC": "America/New_York", "PHL": "America/New_York", "BOS": "America/New_York", "DCA": "America/New_York", "EWR": "America/New_York",
      "TTN": "America/New_York", "ATL": "America/New_York", "MIA": "America/New_York", "SDF": "America/New_York", "PIT": "America/New_York",
      "PVD": "America/New_York", "DTW": "America/New_York", "CMH": "America/New_York", "CVG": "America/New_York", "IND": "America/New_York",
      "LEX": "America/New_York", "JAX": "America/New_York", "GNV": "America/New_York", "TPA": "America/New_York", "SPG": "America/New_York", "DJT": "America/New_York",
      "MDW": "America/Chicago", "ORD": "America/Chicago", "MSP": "America/Chicago", "DFW": "America/Chicago", "AUS": "America/Chicago", "SAT": "America/Chicago",
      "HOU": "America/Chicago", "IAH": "America/Chicago", "MSY": "America/Chicago", "OKC": "America/Chicago", "MKE": "America/Chicago", "MSN": "America/Chicago",
      "STL": "America/Chicago", "SGF": "America/Chicago", "CLL": "America/Chicago", "PNS": "America/Chicago",
      "DEN": "America/Denver", "ABQ": "America/Denver", "PHX": "America/Phoenix", "LAS": "America/Los_Angeles", "LAX": "America/Los_Angeles",
      "SFO": "America/Los_Angeles", "SAN": "America/Los_Angeles", "SEA": "America/Los_Angeles", "SJC": "America/Los_Angeles", "APC": "America/Los_Angeles", "ONT": "America/Los_Angeles"}
STATION = {"NYC": "KNYC"}
SOURCES = {"the weather company": "TWC", "national weather service": "NWS"}
DEFAULTS = {"precision_f": 1, "metar_slack_f": 1, "disagree_tol_f": 3}

_RULES_RE = re.compile(r"(maximum|minimum) temperature recorded at (.+?) \((CLI([A-Z]{3}))\) for ([A-Z][a-z]{2,8} \d{1,2}, \d{4}),? is (.+?) (?:°\s*)?(fahrenheit|celsius)[^.]*?according to ([^,.]+?)(?:,| then|\.)", re.I)
_TICKER_DATE_RE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})-")

@dataclasses.dataclass
class Rules:
    ticker: str; series: str; kind: str; city: str; cli: str; icao: str; tz: str; std_offset_h: float; date: dt.date
    strike_type: str; floor: float | None; cap: float | None; source: str | None; unit: str; precision_f: int
    window_start: float; window_end: float; close_ts: float | None; flags: list
    @property
    def station_day(self) -> str: return f"{self.cli}:{self.date.isoformat()}"
    @property
    def tradable(self) -> bool: return not self.flags

def std_offset_hours(tz: str, year: int) -> float:
    """Standard-time UTC offset of an IANA zone = the smaller (more negative, for US) of the January / July offsets."""
    z = ZoneInfo(tz); a = dt.datetime(year, 1, 15, 12, tzinfo=z).utcoffset(); b = dt.datetime(year, 7, 15, 12, tzinfo=z).utcoffset()
    return min(a, b).total_seconds() / 3600.0

def settlement_window(date: dt.date, tz: str) -> tuple[float, float]:
    """[start, end) epoch seconds of the NWS climate day = local STANDARD time midnight-to-midnight of `date` (DST ignored)."""
    off = std_offset_hours(tz, date.year)
    start = dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc).timestamp() - off * 3600.0
    return start, start + 86400.0

def ticker_date(ticker: str) -> dt.date | None:
    m = _TICKER_DATE_RE.search(ticker or "")
    try: return dt.datetime.strptime(m.group(1), "%y%b%d").date() if m else None
    except ValueError: return None

def resolve_rules(market: dict, series_rules_primary: str | None = None) -> Rules | None:
    """Station, settlement day, source, unit, strike from the market's own rules text (fallback: series rules text). Any inconsistency
    (unknown source, non-F unit, rules date != ticker date, unknown station tz) lands in Rules.flags and makes the market non-tradable."""
    rp = market.get("rules_primary") or series_rules_primary or ""
    m = _RULES_RE.search(rp)
    if not m: return None
    kind = "high" if m.group(1).lower() == "maximum" else "low"
    city, cli, unit, src = m.group(2).strip(), m.group(4), m.group(7).lower(), m.group(8).strip().lower()
    flags = []
    try: date = dt.datetime.strptime(m.group(5), "%b %d, %Y").date()
    except ValueError:
        try: date = dt.datetime.strptime(m.group(5), "%B %d, %Y").date()
        except ValueError: return None
    td = ticker_date(market.get("ticker", ""))
    if td and td != date: flags.append(f"date_mismatch ticker={td} rules={date}")
    source = next((v for k, v in SOURCES.items() if k in src), None)
    if source is None: flags.append(f"unknown_source {src!r}")
    if unit != "fahrenheit": flags.append(f"unit {unit}")
    tz = TZ.get(cli)
    if tz is None: flags.append(f"unknown_station {cli}"); tz = "UTC"
    st = market.get("strike_type"); fl = market.get("floor_strike"); cp = market.get("cap_strike")
    if st not in ("greater", "less", "between"): flags.append(f"strike_type {st}")
    if st == "between" and (fl is None or cp is None): flags.append("between_without_bounds")
    if st == "greater" and fl is None: flags.append("greater_without_floor")
    if st == "less" and cp is None: flags.append("less_without_cap")
    ws, we = settlement_window(date, tz)
    close_ts = None
    if market.get("close_time"):
        try: close_ts = dt.datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp()
        except ValueError: pass
    return Rules(ticker=market.get("ticker", ""), series=market.get("event_ticker", "").split("-")[0] if market.get("event_ticker") else market.get("series", ""), kind=kind, city=city, cli=cli,
                 icao=STATION.get(cli, "K" + cli), tz=tz, std_offset_h=std_offset_hours(tz, date.year), date=date, strike_type=st, floor=fl, cap=cp, source=source, unit=unit,
                 precision_f=DEFAULTS["precision_f"], window_start=ws, window_end=we, close_ts=close_ts, flags=flags)

# ----------------------------------------------------------------------------------------------------------------------- observations
def filter_obs(obs, window: tuple[float, float], as_of: float | None = None) -> list[tuple[float, float]]:
    """Keep (ts, tempF) with window.start <= ts < window.end and (if given) ts <= as_of. Explicit timestamps, no counting heuristics."""
    s, e = window
    return [(t, v) for t, v in obs if v is not None and s <= t < e and (as_of is None or t <= as_of)]

def running_extremes(obs) -> dict:
    vals = [v for _, v in obs]
    if not vals: return {"max": None, "min": None, "n": 0, "first_ts": None, "last_ts": None, "max2": None, "min2": None}
    s = sorted(vals)
    return {"max": _round_f(s[-1]), "min": _round_f(s[0]), "n": len(vals), "first_ts": min(t for t, _ in obs), "last_ts": max(t for t, _ in obs),
            "max2": _round_f(s[-2]) if len(s) > 1 else None, "min2": _round_f(s[1]) if len(s) > 1 else None}

def _round_f(x: float) -> int: return int(math.floor(x + 0.5))

def parse_metar_temp_f(raw: str) -> float | None:
    """Temperature in F from a raw METAR: T-group tenths of C when present, else the whole-degree TT/TD group."""
    t = re.search(r"\bT(\d)(\d{3})(\d)(\d{3})\b", raw or "")
    if t: c = (-1 if t.group(1) == "1" else 1) * int(t.group(2)) / 10.0
    else:
        g = re.search(r"\s(M?\d{2})/(M?\d{2})?\s", (raw or "") + " ")
        if not g: return None
        c = -int(g.group(1)[1:]) if g.group(1).startswith("M") else int(g.group(1))
    return c * 9 / 5 + 32

# ----------------------------------------------------------------------------------------------------------------------- lock logic
def established_value(kind: str, cli_val: int | None, metar: dict | None, disagree_tol: int = DEFAULTS["disagree_tol_f"], metar_slack: int = DEFAULTS["metar_slack_f"]) -> tuple[int | None, int, str]:
    """(value, slack, source_tag) for the running extreme that may be used to lock.
    * both sources: must agree within disagree_tol, else nothing ('source_disagree'); the CLI figure is then used with slack 0
    * METAR only  : extreme must be backed by a second observation within disagree_tol (single spikes do not lock); slack = metar_slack (rounding)
    * CLI only    : nothing — an uncorroborated climate-product figure is exactly the Minneapolis failure mode."""
    mv = (metar or {}).get("max" if kind == "high" else "min"); mv2 = (metar or {}).get("max2" if kind == "high" else "min2")
    if cli_val is not None and mv is not None:
        e = (cli_val - mv) if kind == "high" else (mv - cli_val)      # how much MORE extreme the CLI figure is than the METAR record
        if e > disagree_tol: return None, 0, "source_disagree"
        if e >= 0: return cli_val, 0, "cli+metar"     # TWC republishes the CLI figure itself (weather.com/kalshi daily table), so the corroborated CLI value is the settlement value
        # METAR is already more extreme than the (earlier-issued) CLI product: the CLI is stale, fall through to the METAR path
    if mv is not None:
        if mv2 is None: return None, 0, "metar_single_obs"
        if abs(mv - mv2) > disagree_tol: return None, 0, "metar_spike"
        return mv, metar_slack, "metar"
    if cli_val is not None: return None, 0, "cli_only"
    return None, 0, "nodata"

def lock(kind: str, strike_type: str, floor_k, cap_k, value: int | None, slack: int = 0) -> float | None:
    """Lock table (module docstring). Returns 1.0 / 0.0 when the outcome is forced by the running extreme, else None."""
    if value is None: return None
    if kind == "high":
        if strike_type == "greater" and floor_k is not None and value - slack >= floor_k + 1: return 1.0
        if strike_type == "less" and cap_k is not None and value - slack >= cap_k: return 0.0
        if strike_type == "between" and cap_k is not None and value - slack > cap_k: return 0.0
        return None
    if kind == "low":
        if strike_type == "greater" and floor_k is not None and value + slack <= floor_k: return 0.0
        if strike_type == "less" and cap_k is not None and value + slack <= cap_k - 1: return 1.0
        if strike_type == "between" and floor_k is not None and value + slack < floor_k: return 0.0
        return None
    return None

def score_probability(kind: str, strike_type: str, floor_k, cap_k, value: int | None, exact: bool, local_hour: float) -> tuple[float | None, str]:
    """Scored-only heuristic for states that are not locked (kept from J1 for scoreboard continuity; never traded)."""
    if value is None: return None, "nodata"
    M = value
    if kind == "high":
        p_more = 0.02 if local_hour >= 17 else 0.08 if local_hour >= 16 else 0.25 if local_hour >= 14 else None
        if p_more is None: return None, "too_early"
        s = 0 if exact else 2
        if strike_type == "greater":
            if M + s < floor_k: return (p_more * 0.5 if floor_k - M <= 2 else 0.01), "scored"
            return None, "ambiguous"
        if strike_type == "less":
            if M + s < cap_k: return 1.0 - p_more * (1 if cap_k - M <= 2 else 0.2), "scored"
            return None, "ambiguous"
        if strike_type == "between":
            if exact and floor_k <= M <= cap_k: return 1.0 - p_more * (1 if M == cap_k else 0.5), "scored"
            if M + s < floor_k: return (0.02 if floor_k - M > 2 else p_more * 0.5), "scored"
            return None, "ambiguous"
    else:
        if local_hour < 10: return None, "too_early"
        p_lower = 0.05 if local_hour < 17 else 0.10
        s = 0 if exact else 2
        if strike_type == "greater":
            if M - s > floor_k: return 1.0 - p_lower * (1 if M - floor_k <= 2 else 0.2), "scored"
            return None, "ambiguous"
        if strike_type == "less":
            if M - s >= cap_k: return (p_lower * 0.5 if M - cap_k <= 2 else 0.01), "scored"
            return None, "ambiguous"
        if strike_type == "between":
            if exact and floor_k <= M <= cap_k: return 1.0 - p_lower * (1 if M == floor_k else 0.5), "scored"
            if M - s > cap_k: return (0.02 if M - cap_k > 2 else p_lower * 0.5), "scored"
            return None, "ambiguous"
    return None, "unhandled"

def evaluate(rules: Rules, cli: dict | None, metar: dict | None, local_hour: float, disagree_tol: int = DEFAULTS["disagree_tol_f"]) -> dict:
    """One market, one instant -> {'p', 'basis', 'locked', 'lock_value', 'lock_src', 'slack'}. basis is 'locked_<src>' or a scored tag."""
    cli_val = (cli or {}).get("max" if rules.kind == "high" else "min")
    value, slack, src = established_value(rules.kind, cli_val, metar, disagree_tol)
    p = lock(rules.kind, rules.strike_type, rules.floor, rules.cap, value, slack)
    if p is not None: return {"p": p, "basis": "locked_" + src, "locked": True, "lock_value": value, "lock_src": src, "slack": slack}
    if src == "source_disagree": return {"p": None, "basis": "source_disagree", "locked": False, "lock_value": None, "lock_src": src, "slack": 0}
    # scored only: the corroborated CLI value when that is the established path, else the METAR running extreme
    mv = (metar or {}).get("max" if rules.kind == "high" else "min")
    sv, exact = (cli_val, True) if src == "cli+metar" else (mv, False) if mv is not None else (cli_val, True)
    ps, tag = score_probability(rules.kind, rules.strike_type, rules.floor, rules.cap, sv, exact, local_hour)
    return {"p": ps, "basis": (src if src in ("metar_spike", "metar_single_obs", "cli_only") else tag), "locked": False, "lock_value": value, "lock_src": src, "slack": slack}

# ----------------------------------------------------------------------------------------------------------------------- exposure grouping
def exposure_by_group(open_positions, key=lambda t: t.get("station_day")) -> dict[str, float]:
    """Sum of open cost per station-day group (positions on the same station and settlement day share the same CLI/TWC data and
    the same failure modes, whatever the series or side). Net per ticker so a YES and a NO on one market hedge rather than add."""
    by_tk: dict[tuple, float] = {}
    for t in open_positions:
        g = key(t)
        if g is None: continue
        sgn = -1.0 if t.get("side") == "no" else 1.0
        by_tk[(g, t["ticker"])] = by_tk.get((g, t["ticker"]), 0.0) + sgn * float(t.get("cost", 0.0))
    out: dict[str, float] = {}
    for (g, _), v in by_tk.items(): out[g] = out.get(g, 0.0) + abs(v)
    return out

def group_room(open_positions, group: str, cap: float) -> float:
    return max(0.0, cap - exposure_by_group(open_positions).get(group, 0.0))

# ----------------------------------------------------------------------------------------------------------------------- population monitor
def logic_key(lock_src: str, kind: str, strike_type: str) -> str: return f"{lock_src}|{kind}|{strike_type}"

class Suspensions:
    """Persisted suspension file the runner honors: {'logic': {key: {...}}, 'stations': {cli: {...}}}. A wrong lock anywhere in the
    scoreboard population (traded or not) suspends its logic cell and its station until an operator clears the entry."""
    def __init__(self, path: pathlib.Path):
        self.path = path
        try: self.d = json.loads(path.read_text()) if path.exists() else {"logic": {}, "stations": {}}
        except Exception: self.d = {"logic": {}, "stations": {}}
        self.d.setdefault("logic", {}); self.d.setdefault("stations", {})
    def save(self): self.path.parent.mkdir(parents=True, exist_ok=True); self.path.write_text(json.dumps(self.d, indent=1))
    def suspended(self, lock_src: str, kind: str, strike_type: str, cli: str) -> str | None:
        k = logic_key(lock_src, kind, strike_type)
        if k in self.d["logic"]: return f"logic {k}"
        if cli in self.d["stations"]: return f"station {cli}"
        return None
    def suspend(self, lock_src: str, kind: str, strike_type: str, cli: str, ticker: str, detail: str, now: float | None = None):
        now = now or time.time(); k = logic_key(lock_src, kind, strike_type)
        self.d["logic"].setdefault(k, {"since": now, "tickers": [], "detail": detail})["tickers"].append(ticker)
        self.d["stations"].setdefault(cli, {"since": now, "tickers": [], "detail": detail})["tickers"].append(ticker)
        self.save()

def wrong_locks(first_locks: dict, results: dict) -> list[dict]:
    """first_locks: ticker -> {'p', 'lock_src', 'kind', 'strike_type', 'cli', 't'}; results: ticker -> 'yes'|'no'. Returns the wrong calls."""
    out = []
    for tk, fl in first_locks.items():
        r = results.get(tk)
        if r not in ("yes", "no") or fl.get("p") is None: continue
        if (fl["p"] >= 0.5) != (r == "yes"): out.append({"ticker": tk, "result": r, **fl})
    return out

# ----------------------------------------------------------------------------------------------------------------------- forward fills
TICK = 0.01

def simulate_fill(side: str, book: dict, qty_wanted: int, book_recv_t: float, signal_recv_t: float, fee_fn, tick: float = TICK) -> dict | None:
    """Forward simulated taker fill from a RE-FETCHED book (book_sides() dict, YES price space) obtained at book_recv_t >= signal_recv_t.
    YES: pay best YES ask + tick, up to yes_ask_size.  NO: pay best NO ask (= 1 - best YES bid) + tick, up to yes_bid_size.
    The raw offer (price, size) is recorded separately from the fill price. Returns None when the side is not offered."""
    if book_recv_t < signal_recv_t: raise ValueError("book must be fetched after the signal (no look-ahead)")
    if side == "yes":
        raw_px, raw_sz = book.get("yes_ask"), book.get("yes_ask_size") or 0.0
    else:
        yb = book.get("yes_bid"); raw_px, raw_sz = (None if yb is None else 1.0 - yb), book.get("yes_bid_size") or 0.0
    if raw_px is None or raw_sz <= 0: return None
    px = min(round(raw_px + tick, 4), 0.99)
    qty = int(min(qty_wanted, math.floor(raw_sz)))
    if qty <= 0 or not (0 < px < 1): return None
    return {"side": side, "price": px, "qty": qty, "raw_offer_price": raw_px, "raw_offer_size": raw_sz, "fee": fee_fn(qty, px), "cost": qty * px,
            "signal_recv_t": signal_recv_t, "book_recv_t": book_recv_t, "recheck_latency_s": book_recv_t - signal_recv_t}
