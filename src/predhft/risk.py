"""Risk module shared by all paper runners (gap 6): toxicity-aware per-series caps, fractional Kelly sizing, kill switches.

size(...)  -> contracts to trade, 0 if blocked. Reads rules cards from the universe DB (toxicity flags, observability) and
the family's own ledger (daily realized PnL, error counts). All limits are in config below; paper and live use the same code.
"""
from __future__ import annotations
import json, math, pathlib, sqlite3, time, datetime as dt

CFG = {
    "bankroll": 250.0,              # paper bankroll in $, used for Kelly fractions (user: dedicated bankroll wallet later)
    "kelly_fraction": 0.25,
    "max_lot": 20,                  # hard contract cap per market
    "max_cost_per_market": 15.0,    # $ at risk per market
    "max_cost_per_series_day": 60.0,
    "daily_loss_limit": 40.0,       # $ realized loss per family per UTC day -> family halts for the day
    "max_errors_per_hour": 30,      # feed/API errors -> family halts 1 h
    "toxicity": {                   # substring (lower-case) in a toxicity flag -> lot multiplier
        "manipulable": 0.5, "order-book index on thin": 0.5, "discretion": 0.8, "last fair price": 0.7, "no data": 0.9,
        "maintenance": 0.8, "adverse": 1.0, "paywalled": 0.9, "template": 1.0, "dst": 1.0,
    },
    "observability_mult": {"high": 1.0, "partial": 0.6, "none": 0.3, None: 0.5},
}
DB = pathlib.Path("data/universe.db")

def card(series: str) -> dict | None:
    try:
        r = sqlite3.connect(DB).execute("SELECT card FROM rules_cards WHERE series=?", (series,)).fetchone()
        return json.loads(r[0]) if r else None
    except Exception: return None

def series_multiplier(series: str) -> float:
    c = card(series)
    if not c: return 0.5                                     # no card -> half size (gap: card coverage)
    m = CFG["observability_mult"].get(c.get("observability_before_settlement"), 0.5)
    for flag in c.get("toxicity_flags", []):
        fl = flag.lower()
        for k, v in CFG["toxicity"].items():
            if k in fl: m *= v
    return max(0.2, min(1.0, m))

def kelly_contracts(p: float, price: float, bankroll: float = CFG["bankroll"], frac: float = CFG["kelly_fraction"]) -> int:
    """Binary contract paying $1: edge = p - price; Kelly fraction of bankroll f* = (p - price) / (1 - price) for a YES buy."""
    if not (0 < price < 1) or p <= price: return 0
    f = (p - price) / (1 - price) * frac
    return int(bankroll * f / price)

class Ledger:
    """Per-family realized PnL / error tracking from the family's own jsonl files."""
    def __init__(self, root: pathlib.Path, trades_file: str = "trades.jsonl"):
        self.root = root; self.trades = root / trades_file; self.errors = root / "errors.jsonl"
    def realized_today(self) -> float:
        if not self.trades.exists(): return 0.0
        day = dt.datetime.now(dt.timezone.utc).date().isoformat(); tot = 0.0
        for l in self.trades.read_text().splitlines():
            if not l.strip(): continue
            t = json.loads(l)
            if t.get("status") == "settled" and dt.datetime.fromtimestamp(t.get("fill_t") or t["t"], dt.timezone.utc).date().isoformat() == day:
                tot += t.get("pnl", 0.0)
        return tot
    def cost_series_today(self, series: str) -> float:
        if not self.trades.exists(): return 0.0
        day = dt.datetime.now(dt.timezone.utc).date().isoformat(); tot = 0.0
        for l in self.trades.read_text().splitlines():
            if not l.strip(): continue
            t = json.loads(l)
            if t.get("series", t.get("ticker", "").split("-")[0]) == series and dt.datetime.fromtimestamp(t["t"], dt.timezone.utc).date().isoformat() == day:
                tot += t.get("cost", t.get("qty", 0) * t.get("price", 0))
        return tot
    def record_error(self, msg: str):
        self.errors.open("a").write(json.dumps({"t": time.time(), "msg": msg[:200]}) + "\n")
    def errors_last_hour(self) -> int:
        if not self.errors.exists(): return 0
        cut = time.time() - 3600
        return sum(1 for l in self.errors.read_text().splitlines() if l.strip() and json.loads(l)["t"] > cut)

def halted(ledger: Ledger) -> str | None:
    if ledger.realized_today() <= -CFG["daily_loss_limit"]: return f"daily loss limit {CFG['daily_loss_limit']}$ hit"
    if ledger.errors_last_hour() >= CFG["max_errors_per_hour"]: return "error rate kill switch"
    return None

def size(series: str, p: float, price: float, avail: float, ledger: Ledger, lot_cap: int | None = None) -> tuple[int, str]:
    """Returns (qty, reason). qty=0 means do not trade."""
    h = halted(ledger)
    if h: return 0, h
    k = kelly_contracts(p, price)
    if k <= 0: return 0, "no edge"
    cap = min(lot_cap or CFG["max_lot"], CFG["max_lot"], int(CFG["max_cost_per_market"] / max(price, 0.01)))
    mult = series_multiplier(series)
    q = int(min(k, cap * mult, avail))
    if q <= 0: return 0, "cap/mult/avail -> 0"
    if ledger.cost_series_today(series) + q * price > CFG["max_cost_per_series_day"]: return 0, "series daily cost cap"
    return q, f"kelly={k} cap={cap} mult={mult:.2f}"
