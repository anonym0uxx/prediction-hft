"""Joined decision / fill ledger (AUDIT_ADDENDUM_2026-10-28 §8). Pure functions over the PAPER data roots; no network, no writes
except through `write_ledger`. Every row is one DECISION: a paper fill (trades.jsonl row), or an attempted submission that did not
fill (STALE / SKIP / GATE REJECT lines in runner.log). The join attaches the configuration in force (last `start` line before the
decision), the nearest book snapshot (<= JOIN_WINDOW_S before the decision), the risk-ledger portfolio state at the decision, the
settlement outcome, and prospective regime tags. Fields that the historical data cannot supply are null with a `limitation`.

Timestamps: `decision_ts` is the runner's loop-start `t` of the poll that produced the decision (the trade row carries no separate
t_dec; `decision_latency_s` = time from candidate to re-check). There is no exchange-side timestamp anywhere (paper), so no row can
establish quote lifetime or exchange receipt ordering.
"""
from __future__ import annotations
import collections, csv, datetime as dt, json, pathlib, re

PT = dt.timezone(dt.timedelta(hours=-7))
JOIN_WINDOW_S = 6.0
BANKROLL = 250.0
GLOBAL_DAILY_HALT = 25.0
# boundaries (PT) — see docs/AUDIT_2026-10-28.md §1.1 and the addendum §1
REBASE_TS = dt.datetime(2026, 10, 27, 15, 20, tzinfo=PT).timestamp()
FREEZE_TS = dt.datetime(2026, 10, 27, 16, 5, tzinfo=PT).timestamp()          # P12 freeze boundary = official feed live / s7p15 restart
SUSPEND_TS = dt.datetime(2026, 10, 28, 9, 5, 51, tzinfo=PT).timestamp()      # s7p13-KX*15M -> --scored-only
FOMC_WINDOW = (dt.datetime(2026, 10, 28, 10, 45, tzinfo=PT).timestamp(), dt.datetime(2026, 10, 28, 12, 30, tzinfo=PT).timestamp())  # statement 11:00, presser 11:30 PT
EXEC_ASSUMPTIONS = {"price": "re-checked touch on our side + 0.01 (ten 0.001 ticks), capped at 0.99", "qty": "min(Kelly, lot cap x equity scale, displayed size at touch; x0.5 if displayed < 50); integer",
                    "fee": "ceil-to-cent per order of 0.07 x qty x price x (1-price) (Kalshi quadratic, multiplier 1)", "latency_guard_s": {"proj_gate": 1.5, "model_rule": None},
                    "re_check": "second orderbook fetch; fill only if our side still offered at <= cap (proj_gate) / edge still >= thr (model rule)", "settlement": "qty x 1{result == side} - cost - fee; no exchange order or fill id exists (paper)"}

SCHEMA = ["row_id", "row_kind", "family", "version_tag", "config_flags", "config_cmd", "series", "asset", "ticker", "close_ts", "close_pt", "decision_ts", "decision_pt", "decision_latency_s",
          "intended_side", "p_model", "p_cal", "p_side", "p_market_mid", "fav_at_decision", "d_bp", "partial_n", "spot_src", "cfb_age_s", "sigma1", "quoted_touch", "booked_price", "avail_at_touch",
          "snap_t", "snap_age_s", "snap_yes_bid", "snap_yes_ask", "snap_bid_size", "snap_ask_size", "snap_bids_top3", "snap_asks_top3", "qty", "cost", "fee", "execution_penalty", "exec_assumptions",
          "status", "result", "expiration_value", "floor_strike", "proceeds", "net_pnl", "portfolio_open_cost", "portfolio_open_cost_s7", "portfolio_equity", "portfolio_realized_utc_day", "halt_state", "regime_tod_block",
          "regime_vol_tercile", "regime_fomc_window", "era_pre_post_freeze", "era_pre_post_suspension", "reject_reason", "limitation"]


def pt(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts, PT).strftime("%Y-%m-%d %H:%M:%S PT") if ts is not None else None


def version_tag(fam: str, ts: float) -> str:
    """Same partition as scripts/audit_2026_10_28.version_of (kept here so the ledger does not import the script)."""
    if fam.startswith("s7live") or fam.startswith("s7final"): return "P1/P2 hourly model-rule (stopped)"
    if fam.startswith("s7p10"): return "P10 hourly redesign"
    if fam.startswith("s7p15"):
        if ts < REBASE_TS: return "P12 15M model-rule | replica, pre-rebase"
        if ts < FREEZE_TS: return "P12 15M model-rule | replica, post-rebase"
        return "P12 15M model-rule | official feed (FROZEN eval)"
    if fam.startswith("s7p13"):
        if ts < REBASE_TS: return "P13 15M projection | replica, pre-rebase (floor .97)"
        if ts < dt.datetime(2026, 10, 27, 16, 0, tzinfo=PT).timestamp(): return "P13/P16 | replica, post-rebase"
        if ts < dt.datetime(2026, 10, 27, 21, 51, 36, tzinfo=PT).timestamp(): return "P16 | official feed, no gate"
        if ts < SUSPEND_TS: return "P16+P18+P20 | official feed, gate on"
        return "P16+P18+P20 | scored-only (suspended)"
    return fam


def tod_block(ts: float) -> str:
    h = dt.datetime.fromtimestamp(ts, PT).hour + dt.datetime.fromtimestamp(ts, PT).minute / 60
    if h < 6: return "00-06 PT overnight"
    if h < 9.5: return "06-09:30 PT pre-US-cash"
    if h < 16: return "09:30-16 PT US-cash"
    return "16-24 PT evening"


def fomc_window(ts: float) -> bool: return FOMC_WINDOW[0] <= ts <= FOMC_WINDOW[1]


def tercile_cuts(values: list[float]) -> tuple[float, float] | None:
    v = sorted(x for x in values if x is not None)
    if len(v) < 3: return None
    return v[len(v) // 3], v[(2 * len(v)) // 3]


def vol_tercile(sigma1: float | None, cuts: tuple[float, float] | None) -> str | None:
    if sigma1 is None or cuts is None: return None
    return "low" if sigma1 < cuts[0] else ("mid" if sigma1 < cuts[1] else "high")


def parse_log(path: pathlib.Path) -> tuple[list[tuple[float, str]], list[dict]]:
    """(start lines [(ts, text)], attempted-but-unfilled decisions [{t, kind, ticker, side, price, reason}])."""
    starts, dec = [], []
    if not path.exists(): return starts, dec
    for l in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try: ts = dt.datetime.fromisoformat(l[:25]).timestamp()
        except Exception: continue
        body = l[26:]
        if body.startswith("start "): starts.append((ts, body)); continue
        m = re.match(r"STALE (\S+) (yes|no) touch moved before fill \(latency ([\d.]+)s\)", body)
        if m: dec.append({"t": ts, "kind": "stale", "ticker": m.group(1), "side": m.group(2), "latency": float(m.group(3)), "reason": "stale: touch moved before fill"}); continue
        m = re.match(r"SKIP (\S+) (yes|no) (.*)", body)
        if m: dec.append({"t": ts, "kind": "skip", "ticker": m.group(1), "side": m.group(2), "reason": "skip: " + m.group(3)[:160]}); continue
        m = re.match(r"GATE REJECT (\S+) (yes|no) ([\d.]+) reasons=(\[.*?\])", body)
        if m: dec.append({"t": ts, "kind": "gate_reject", "ticker": m.group(1), "side": m.group(2), "price": float(m.group(3)), "reason": "gate_reject: " + m.group(4)}); continue
    return starts, dec


def config_at(starts: list[tuple[float, str]], ts: float) -> str | None:
    best = None
    for st, text in starts:
        if st <= ts: best = text
    return best


def flags_from_start(text: str | None) -> dict | None:
    if not text: return None
    out = {}
    for m in re.finditer(r"(\w+)=([^\s]+)", text): out[m.group(1)] = m.group(2)
    return out


def nearest_snapshot(snaps: list[dict], ts: float, window: float = JOIN_WINDOW_S) -> dict | None:
    best = None
    for r in snaps:
        if r["t"] <= ts + 0.001 and ts - r["t"] <= window and (best is None or r["t"] > best["t"]): best = r
    return best


class Portfolio:
    """Replays data/risk/events.jsonl chronologically; `state_at(ts)` = (open cost, equity, realized this UTC day, halt state)."""
    def __init__(self, events: list[dict], reset_at: float = 0.0, bankroll: float = BANKROLL, halt: float = GLOBAL_DAILY_HALT):
        self.ev = sorted(events, key=lambda e: e["t"]); self.reset_at = reset_at; self.bankroll = bankroll; self.halt = halt
    def state_at(self, ts: float) -> dict:
        """Open cost counts an `open` event until its `settle` event or, failing that, until close_ts + 120 s (s1-maker posts were
        ledgered as opens but mostly never settled). `open_cost_s7` restricts to the S7 taker families (the fills in this ledger)."""
        open_cost: dict[tuple, tuple[float, float | None]] = {}; realized_total = 0.0; realized_day = collections.defaultdict(float)
        for e in self.ev:
            if e["t"] > ts: break
            key = (e.get("family"), e.get("ticker"))
            if e["kind"] == "open": open_cost[key] = (open_cost.get(key, (0.0, None))[0] + float(e.get("cost") or 0.0), e.get("close_ts"))
            elif e["kind"] == "settle":
                open_cost.pop(key, None); realized_day[int(e["t"] // 86400)] += float(e.get("pnl") or 0.0)
                if e["t"] >= self.reset_at: realized_total += float(e.get("pnl") or 0.0)
        live = {k: v for k, v in open_cost.items() if v[1] is None or ts < float(v[1]) + 120}
        rd = realized_day.get(int(ts // 86400), 0.0)
        return {"open_cost": round(sum(v[0] for v in live.values()), 2), "open_cost_s7": round(sum(v[0] for k, v in live.items() if (k[0] or "").startswith("s7")), 2),
                "equity": round(max(self.bankroll * 0.5, self.bankroll + realized_total), 2), "realized_utc_day": round(rd, 2),
                "halt_state": ("shadow_halt:global_daily" if rd <= -self.halt else "none"), "n_open": len(live)}


def load_jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists(): return []
    out = []
    for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if l.strip():
            try: out.append(json.loads(l))
            except Exception: continue
    return out


def build_rows(data: pathlib.Path, markets: dict | None = None, families: list[str] | None = None) -> list[dict]:
    markets = markets or {}
    fams = families or sorted(p.parent.name for p in data.glob("*/trades.jsonl"))
    events = load_jsonl(data / "risk" / "events.jsonl")
    try: reset_at = json.loads((data / "RESET.json").read_text()).get("reset_at", 0.0)
    except Exception: reset_at = 0.0
    port = Portfolio(events, reset_at)
    rows: list[dict] = []
    for fam in fams:
        root = data / fam
        trades = load_jsonl(root / "trades.jsonl"); snaps = [r for r in load_jsonl(root / "books.jsonl") if r.get("mins_left") is not None]
        starts, dec = parse_log(root / "runner.log")
        by_tk = collections.defaultdict(list)
        for r in snaps: by_tk[r["ticker"]].append(r)
        cuts = tercile_cuts([r.get("sigma1") for r in snaps])
        items = [("fill", t) for t in trades] + [(d["kind"], d) for d in dec]
        for kind, x in items:
            ts = x["t"]; tk = x["ticker"]; snap = nearest_snapshot(by_tk.get(tk, []), ts); cfg = config_at(starts, ts); m = markets.get(tk, {})
            lim = []
            if cfg is None: lim.append("no runner.log start line precedes this decision (log began later): config_flags unknown")
            if snap is None: lim.append("no book snapshot within %.0f s before the decision" % JOIN_WINDOW_S)
            elif "bids" not in snap: lim.append("snapshot predates top-3 depth logging: only top-of-book sizes")
            lim.append("decision_ts is the poll loop-start t; no exchange-side timestamp exists (paper)")
            st = port.state_at(ts - 1e-6)
            base = {"row_kind": kind, "family": fam, "version_tag": version_tag(fam, ts), "config_flags": flags_from_start(cfg), "config_cmd": cfg, "series": (x.get("series") or (snap or {}).get("series") or tk.split("-")[0]),
                    "asset": tk[2:5], "ticker": tk, "close_ts": x.get("close_ts") or (snap or {}).get("close_ts"), "decision_ts": ts, "decision_pt": pt(ts), "decision_latency_s": x.get("decision_latency_s", x.get("latency")),
                    "intended_side": x.get("side"), "spot_src": x.get("spot_src") or (snap or {}).get("spot_src"), "cfb_age_s": (snap or {}).get("cfb_age_s"), "sigma1": x.get("sigma1") or (snap or {}).get("sigma1"),
                    "snap_t": (snap or {}).get("t"), "snap_age_s": (round(ts - snap["t"], 2) if snap else None), "snap_yes_bid": (snap or {}).get("yes_bid"), "snap_yes_ask": (snap or {}).get("yes_ask"),
                    "snap_bid_size": (snap or {}).get("bid_size"), "snap_ask_size": (snap or {}).get("ask_size"), "snap_bids_top3": (snap or {}).get("bids"), "snap_asks_top3": (snap or {}).get("asks"),
                    "exec_assumptions": EXEC_ASSUMPTIONS, "result": m.get("result", x.get("result")), "expiration_value": m.get("expiration_value", x.get("expiration_value")), "floor_strike": m.get("floor_strike", x.get("K")),
                    "portfolio_open_cost": st["open_cost"], "portfolio_open_cost_s7": st["open_cost_s7"], "portfolio_equity": st["equity"], "portfolio_realized_utc_day": st["realized_utc_day"], "halt_state": st["halt_state"],
                    "regime_tod_block": tod_block(ts), "regime_vol_tercile": vol_tercile(x.get("sigma1") or (snap or {}).get("sigma1"), cuts), "regime_fomc_window": fomc_window(ts),
                    "era_pre_post_freeze": ("post" if ts >= FREEZE_TS else "pre"), "era_pre_post_suspension": ("post" if ts >= SUSPEND_TS else "pre")}
            base["close_pt"] = pt(base["close_ts"]) if base["close_ts"] else None
            if kind == "fill":
                t = x; win = (t.get("result") == t.get("side")) if t.get("status") == "settled" else None
                p_side = (t["p_model"] if t["side"] == "yes" else 1 - t["p_model"]) if t.get("p_model") is not None else None
                base.update({"p_model": t.get("p_model"), "p_cal": t.get("p_cal"), "p_side": p_side, "p_market_mid": t.get("p_market"), "fav_at_decision": t.get("fav_at_decision"), "d_bp": t.get("d_bp"), "partial_n": t.get("partial_n"),
                             "quoted_touch": round(t["price"] - t.get("slippage", 0.01), 4), "booked_price": t["price"], "avail_at_touch": t.get("avail_at_touch"), "qty": t["qty"], "cost": round(t["cost"], 4), "fee": t["fee"],
                             "execution_penalty": round(t["qty"] * t.get("slippage", 0.01), 4), "status": t.get("status"), "proceeds": (float(t["qty"]) * (1.0 if win else 0.0) if win is not None else None), "net_pnl": t.get("pnl"), "reject_reason": None})
                if t.get("p_cal") is None and t.get("proj_gate"): lim.append("p_cal missing on this proj_gate trade row (pre-REBASE build)")
            else:
                pm = (snap or {}).get("p_model"); side = x["side"]
                base.update({"p_model": pm, "p_cal": (snap or {}).get("p_cal"), "p_side": ((pm if side == "yes" else 1 - pm) if pm is not None else None),
                             "p_market_mid": (((snap["yes_bid"] + snap["yes_ask"]) / 2) if snap and snap.get("yes_bid") is not None and snap.get("yes_ask") is not None else None), "fav_at_decision": None, "d_bp": None,
                             "partial_n": (snap or {}).get("partial_n"), "quoted_touch": ((snap["yes_ask"] if side == "yes" else (1 - snap["yes_bid"] if snap.get("yes_bid") is not None else None)) if snap else None),
                             "booked_price": x.get("price"), "avail_at_touch": ((snap["ask_size"] if side == "yes" else snap["bid_size"]) if snap else None), "qty": 0, "cost": 0.0, "fee": 0.0, "execution_penalty": 0.0,
                             "status": "not_filled", "proceeds": 0.0, "net_pnl": 0.0, "reject_reason": x["reason"]})
                lim.append("unfilled decision: forecast/book fields come from the nearest snapshot, not the decision's own re-check")
            base["limitation"] = "; ".join(lim) if lim else None
            rows.append(base)
    rows.sort(key=lambda r: (r["decision_ts"], r["family"]))
    for i, r in enumerate(rows): r["row_id"] = f"{i:05d}"
    return [{k: r.get(k) for k in SCHEMA} for r in rows]


def write_ledger(rows: list[dict], out_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pj = out_dir / "decisions_fills.jsonl"; pc = out_dir / "decisions_fills.csv"
    pj.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")
    with pc.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SCHEMA); w.writeheader()
        for r in rows: w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    (out_dir / "SCHEMA.json").write_text(json.dumps({"fields": SCHEMA, "exec_assumptions": EXEC_ASSUMPTIONS, "join_window_s": JOIN_WINDOW_S,
                                                       "boundaries_pt": {"rebase": pt(REBASE_TS), "p12_freeze_official_feed": pt(FREEZE_TS), "p13_suspension": pt(SUSPEND_TS), "fomc_window": [pt(FOMC_WINDOW[0]), pt(FOMC_WINDOW[1])]}}, indent=1), encoding="utf-8")
    return pj, pc


def decomposition(rows: list[dict]) -> dict:
    """proceeds - quoted cost - fees - execution penalty == net (settled fills only)."""
    F = [r for r in rows if r["row_kind"] == "fill" and r["status"] == "settled"]
    proceeds = sum(r["proceeds"] for r in F); cost = sum(r["cost"] for r in F); fees = sum(r["fee"] for r in F); pen = sum(r["execution_penalty"] for r in F); net = sum(r["net_pnl"] for r in F)
    return {"n": len(F), "contracts": sum(r["qty"] for r in F), "proceeds": round(proceeds, 2), "quoted_cost": round(cost - pen, 2), "gross_vs_quoted": round(proceeds - (cost - pen), 2), "fees": round(fees, 2),
            "execution_penalty": round(pen, 2), "net": round(net, 2), "identity_residual": round(proceeds - (cost - pen) - fees - pen - net, 6)}
