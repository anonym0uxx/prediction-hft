"""Risk module shared by all paper runners: global ledger, per-settlement exposure caps, fee-aware shrunk Kelly, kill switches.

size(...) -> (qty, reason). qty=0 means do not trade. Paper and live use the same code.
Review fixes (docs/QUANT_REVIEW_2026-10-26.md R1-R5, N4):
  * GLOBAL append-only ledger data/risk/events.jsonl: every family writes open/settle events; aggregate daily loss halt ($25 = 10%),
    weekly $50, per-family daily $15; per-settlement-timestamp exposure cap ($20); per-event-date cap for weather ($30).
  * Kelly on the fused/shrunk belief (fusion.blend: 50/50 toward market until an out-of-sample fit exists) and on fee-inclusive price.
  * Toxicity multiplier from explicit card fields (observability, discretionary settlement, verifier false rate), not flag counts.
  * Minimum 5 contracts (fee rounding makes 1-2 lots pay >5% of cost); 50% of touch on thin books (<50 at touch).
"""
from __future__ import annotations
import json, pathlib, sqlite3, time, datetime as dt

CFG = {
    "paper_mode": True,   # halts shadow-logged only; set False for live
    "bankroll": 250.0, "kelly_fraction": 0.25, "max_lot": 20, "min_lot": 5,
    "max_cost_per_market": 15.0, "max_cost_per_series_day": 60.0,
    "family_daily_loss": 15.0, "global_daily_loss": 25.0, "global_weekly_loss": 50.0,
    "max_cost_per_settlement_ts": 60.0,  # global per close_ts (net per ticker); plus per rule-group cap below (P13 diag 2026-10-27)
    "max_cost_per_settlement_ts_group": 30.0,
    "max_cost_per_settlement_ts_crypto": 30.0,   # 2026-10-27 11:20 PT: was 15 (one bet per close); user judged over-corrected -> up to three lot-10 crypto bets per close (rho .86-.94 noted; Sharpe-neutral, EV x3)
    "crypto_series": ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXBTCD", "KXETHD", "KXSOLD"],   # same close_ts within one rule group (s7p13-*, s7p15-*, s7p10-*): 3 assets x lot 10
    "max_cost_per_event_date": 30.0,
    "max_errors_per_hour": 30,
    "observability_mult": {"high": 1.0, "partial": 0.6, "none": 0.3, None: 0.5},
    "thin_book": 50, "thin_fraction": 0.5,
}
DB = pathlib.Path("data/universe.db"); GLOBAL = pathlib.Path("data/risk/events.jsonl")

def card(series: str) -> dict | None:
    try:
        r = sqlite3.connect(DB).execute("SELECT card FROM rules_cards WHERE series=?", (series,)).fetchone()
        return json.loads(r[0]) if r else None
    except Exception: return None

def series_multiplier(series: str) -> float:
    c = card(series)
    if not c: return 0.5
    m = CFG["observability_mult"].get(c.get("observability_before_settlement"), 0.5)
    flags = " ".join(c.get("toxicity_flags", [])).lower()
    if "source agency is kalshi" in flags or ("sole discretion" in flags and "last fair price" in flags): m *= 0.8
    if "template-derived" in flags or "unverified" in flags: m *= 0.8
    fr = c.get("verifier_false_rate")
    if isinstance(fr, (int, float)) and fr > 0.01: m *= 0.5
    return max(0.2, min(1.0, m))

def kelly_contracts(p: float, price: float, fee_per_contract: float = 0.0, bankroll: float = CFG["bankroll"], frac: float = CFG["kelly_fraction"]) -> int:
    c = price + fee_per_contract
    if not (0 < c < 1) or p <= c: return 0
    return int(bankroll * (p - c) / (1 - c) * frac / c)

def blend(family: str, p_model: float, p_market: float | None) -> float:
    if p_market is None: return p_model
    try:
        from .fusion import blend as fb
        return fb(family, p_model, p_market)
    except Exception: return 0.5 * p_model + 0.5 * p_market

class Ledger:
    def __init__(self, root: pathlib.Path, trades_file: str = "trades.jsonl", family: str | None = None):
        self.root = root; self.trades = root / trades_file; self.errors = root / "errors.jsonl"; self.family = family or root.name
        GLOBAL.parent.mkdir(parents=True, exist_ok=True)
    def _ev(self, **e):
        e.setdefault("t", time.time()); e["family"] = self.family; GLOBAL.open("a").write(json.dumps(e) + "\n")
    def record_open(self, ticker: str, series: str, cost: float, close_ts: float | None = None, event_date: str | None = None, side: str | None = None):
        self._ev(kind="open", ticker=ticker, series=series, cost=cost, close_ts=close_ts, event_date=event_date, side=side)
    def record_settle(self, ticker: str, pnl: float): self._ev(kind="settle", ticker=ticker, pnl=pnl)
    def record_error(self, msg: str):
        self.errors.open("a").write(json.dumps({"t": time.time(), "msg": msg[:200]}) + "\n")
    @staticmethod
    def events(since: float) -> list[dict]:
        if not GLOBAL.exists(): return []
        out = []
        for l in GLOBAL.read_text().splitlines():
            if not l.strip(): continue
            e = json.loads(l)
            if e["t"] >= since: out.append(e)
        return out
    @staticmethod
    def _day0() -> float:
        return dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    def realized_today(self, family: str | None = "self") -> float:
        fam = self.family if family == "self" else family
        return sum(e["pnl"] for e in self.events(self._day0()) if e["kind"] == "settle" and (fam is None or e["family"] == fam))
    def realized_week(self) -> float:
        return sum(e["pnl"] for e in self.events(time.time() - 7 * 86400) if e["kind"] == "settle")
    def cost_series_today(self, series: str) -> float:
        return sum(e["cost"] for e in self.events(self._day0()) if e["kind"] == "open" and e.get("series") == series)
    def equity(self) -> float:
        """Paper equity = base bankroll + realized P&L since the architecture reset (all families, global ledger).
        Kelly sizes on this, so stakes compound with results; the dollar loss halts stay fixed (they protect the base)."""
        try: t0 = json.loads((pathlib.Path("data") / "RESET.json").read_text()).get("reset_at", 0)
        except Exception: t0 = 0
        pnl = sum(e.get("pnl", 0.0) for e in self.events(max(t0, time.time() - 90 * 86400)) if e["kind"] == "settle" and e["t"] >= t0)
        return max(CFG["bankroll"] * 0.5, CFG["bankroll"] + pnl)
    def cost_settlement(self, close_ts: float, group: str | None = None, series_set: list | None = None) -> float:
        # net exposure per ticker (a YES and a NO on the same market hedge, not add); optionally restricted to one rule group (family name prefix before the first '-')
        by_tk: dict[str, float] = {}
        for e in self.events(time.time() - 3 * 86400):
            if e["kind"] != "open" or not e.get("close_ts") or abs(e["close_ts"] - close_ts) >= 60: continue
            if group and (e.get("family") or "").split("-")[0] != group: continue
            if series_set and e.get("series") not in series_set: continue
            sgn = -1.0 if e.get("side") == "no" else 1.0
            by_tk[e["ticker"]] = by_tk.get(e["ticker"], 0.0) + sgn * e["cost"]
        return sum(abs(v) for v in by_tk.values())
    def cost_event_date(self, event_date: str) -> float:
        return sum(e["cost"] for e in self.events(time.time() - 3 * 86400) if e["kind"] == "open" and e.get("event_date") == event_date)
    def errors_last_hour(self) -> int:
        if not self.errors.exists(): return 0
        cut = time.time() - 3600
        return sum(1 for l in self.errors.read_text().splitlines() if l.strip() and json.loads(l)["t"] > cut)

def halted(ledger: Ledger) -> str | None:
    if ledger.realized_today(None) <= -CFG["global_daily_loss"]: return f"GLOBAL daily loss {CFG['global_daily_loss']}$ hit"
    if ledger.realized_week() <= -CFG["global_weekly_loss"]: return f"GLOBAL weekly loss {CFG['global_weekly_loss']}$ hit"
    if ledger.realized_today() <= -CFG["family_daily_loss"]: return f"family daily loss {CFG['family_daily_loss']}$ hit"
    if ledger.errors_last_hour() >= CFG["max_errors_per_hour"]: return "error rate kill switch"
    return None

def size(series: str, p: float, price: float, avail: float, ledger: Ledger, lot_cap: int | None = None, p_market: float | None = None,
         fee_per_contract: float = 0.0, close_ts: float | None = None, event_date: str | None = None, family: str | None = None) -> tuple[int, str]:
    h = halted(ledger)
    if h and not CFG.get("paper_mode", True): return 0, h
    if h: ledger._ev(kind="halt_shadow", reason=h)       # PAPER MODE (user 2026-10-27): loss halts are recorded, not enforced — we are collecting data. Live mode enforces.
    pb = blend(family or ledger.family, p, p_market)
    eq = ledger.equity(); scale = min(4.0, eq / CFG["bankroll"])          # compounding: Kelly and per-market caps scale with equity (max 4x until live review)
    k = kelly_contracts(pb, price, fee_per_contract, bankroll=eq)
    if k <= 0: return 0, f"no edge after shrink (p={p:.2f} p_blend={pb:.2f} c={price + fee_per_contract:.2f})"
    cap = min(round((lot_cap or CFG["max_lot"]) * scale), round(CFG["max_lot"] * scale), int(CFG["max_cost_per_market"] * scale / max(price, 0.01)))
    mult = series_multiplier(series)
    av = avail * (CFG["thin_fraction"] if avail < CFG["thin_book"] else 1.0)
    q = int(min(k, cap * mult, av))
    if q < CFG["min_lot"]: return 0, f"below min lot (kelly={k} cap={cap} mult={mult:.2f} avail={avail:.0f})"
    if ledger.cost_series_today(series) + q * price > CFG["max_cost_per_series_day"]: return 0, "series daily cost cap"
    if close_ts and ledger.cost_settlement(close_ts) + q * price > CFG["max_cost_per_settlement_ts"]: return 0, "settlement-timestamp exposure cap"
    if close_ts and ledger.cost_settlement(close_ts, group=(ledger.family or "").split("-")[0]) + q * price > CFG["max_cost_per_settlement_ts_group"]: return 0, "settlement-timestamp group cap"
    if close_ts and series in CFG["crypto_series"]:
        if ledger.cost_settlement(close_ts, series_set=CFG["crypto_series"]) + q * price > CFG["max_cost_per_settlement_ts_crypto"]: return 0, "crypto close_ts cap (one bet per close)"
        if (ledger.family or "").startswith("s7p15-") and ledger.cost_settlement(close_ts, group="s7p13", series_set=[series]) > 0: return 0, "p13_holds_ticker"   # P12 yields only on the same asset
    if event_date and ledger.cost_event_date(event_date) + q * price > CFG["max_cost_per_event_date"]: return 0, "event-date exposure cap"
    return q, f"kelly={k} p_blend={pb:.2f} cap={cap} mult={mult:.2f} equity={eq:.2f}"
