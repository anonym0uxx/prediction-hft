"""P24 — LLM DIRECT trade decision arm (paper). docs/PREREG.md §P24. Owner directive (Alon, 2026-10-28): run, side by side with the
P23 probability arm, a second arm in which the model itself outputs the TRADE DECISION and we paper-act on it directly —
NO downstream edge gates (no EV threshold, no p_model blend, no Thompson / bet gate). Only the HARD risk limits remain.

Same call, same packet, same budget as P23 (src/predhft/llm_shadow.py): the model's answer carries both the calibrated
probability (P23, unchanged semantics) and a `decision` block {action, qty, limit_price, hold_plan, reason}. This module:
  * parse_decision: robust parse of the block; malformed -> NO_TRADE (or HOLD when a position is open), error logged;
  * hard_limits:    the ONLY filter. Clamp qty to [min_lot, max_lot]; per-market cost cap; per-close exposure caps shared with
                    the GLOBAL risk ledger (data/risk/events.jsonl: this arm cannot bypass SOL-v1's exposure); series daily cap;
                    loss halts (enforced for this arm even in paper mode). Refusals are logged as `risk_limited`;
  * execution:      sol_v1.paper_fill at the MODEL'S limit against the live ws/REST book (depth-weighted, partial allowed, no size
                    at/below limit -> no fill, logged); EXIT = sol_v1.exit_value (depth-weighted sale at the executable bid), fee
                    per Kalshi schedule on every taker order; settlement from the exchange result with the official-feed
                    cross-check (same code as SOL-v1); 60-s equity marks incl. idle (open positions at executable bid, 0 if none).
  * attribution:    family data/llm-direct/ {decisions.jsonl, fills.jsonl, marks.jsonl, runner.log}; Ledger family 'llm-direct'
                    (own paper bankroll accounting in marks; SHARED inventory/caps through the global ledger).
Paper only: PREDHFT_MODE=paper is required; there is no order endpoint anywhere in this file.
Every function above DirectArm is pure (no I/O) and unit-tested from fixtures.
"""
from __future__ import annotations
import datetime as dt, json, math, pathlib, re, time
from . import sol_v1 as S
from . import risk as R

VERSION = "P24-llm-direct-v1"; FAMILY = "llm-direct"; SERIES = S.SERIES
ROOT = pathlib.Path("data/llm-direct"); SOL_ROOT = pathlib.Path("data/sol-v1")
ACTIONS_OPEN = ("BUY_YES", "BUY_NO", "NO_TRADE"); ACTIONS_HELD = ("HOLD", "EXIT"); HOLD_PLANS = ("HOLD_TO_SETTLEMENT", "REVIEW_AT_T5")
CFG = {
    "bankroll": 250.0,                 # this arm's paper bankroll for marks (the loss halts live in risk.CFG and are shared)
    "min_lot": R.CFG["min_lot"], "max_lot": R.CFG["max_lot"],
    "min_px": 0.01, "max_px": 0.99,    # the model's limit must be a price in dollars inside (0, 1); no further price gate
    "max_book_age_s": 2.5,             # ws book freshness for the fill; older -> REST re-fetch; failing that the snapshot book (flagged)
    "mark_interval_s": 60.0, "settle_after_s": 90.0, "settle_poll_s": 30.0,
    "enforce_halts": True,             # loss halts refuse this arm's entries even in paper mode (hard limit per the directive)
}

# ------------------------------------------------------------------------------------------------------------ parsing
_NUM = r"[-+]?\d*\.?\d+"

def _norm_token(v) -> str:
    return re.sub(r"[\s\-]+", "_", str(v).strip().upper().strip('"\''))

def _to_price(v) -> float | None:
    """Dollars 0.01..0.99. Accepts numbers, numeric strings, '45c', '45%', and plain cents 1..99 (integers > 1)."""
    if v is None or isinstance(v, bool): return None
    s = str(v).strip().lower().replace("$", "")
    m = re.search(_NUM, s)
    if not m: return None
    try: x = float(m.group(0))
    except Exception: return None
    if "c" in s[m.end():m.end() + 2] or "%" in s[m.end():m.end() + 2] or (x > 1 and x <= 99 and abs(x - round(x)) < 1e-9): x = x / 100
    return x if 0 < x < 1 else None

def _to_qty(v) -> int | None:
    if v is None or isinstance(v, bool): return None
    try: q = float(str(v).strip())
    except Exception: return None
    if not math.isfinite(q) or q < 0: return None
    return int(math.floor(q + 1e-9))

def parse_decision(raw, holding: bool) -> dict:
    """The model's `decision` block -> {action, qty, limit_price, hold_plan, reason, error, raw_action}. Robust to case/spacing
    and string numbers; anything malformed or not allowed in the current state collapses to the no-op (NO_TRADE when flat, HOLD
    when holding) with `error` set — the caller logs it. Prices are dollars; '45c' / 45 are read as cents."""
    noop = "HOLD" if holding else "NO_TRADE"; allowed = ACTIONS_HELD if holding else ACTIONS_OPEN
    out = {"action": noop, "qty": 0, "limit_price": None, "hold_plan": None, "reason": None, "error": None, "raw_action": None}
    if raw is None: out["error"] = "decision_missing"; return out
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except Exception:
            tok = _norm_token(raw)
            if tok in allowed and tok in ("NO_TRADE", "HOLD", "EXIT"): out["action"] = tok; out["raw_action"] = tok; return out
            out["error"] = "decision_not_object"; out["raw_action"] = str(raw)[:40]; return out
    if not isinstance(raw, dict): out["error"] = "decision_not_object"; return out
    act = raw.get("action", raw.get("decision", raw.get("side")))
    tok = _norm_token(act) if act is not None else ""
    tok = {"BUY": "", "YES": "BUY_YES", "NO": "BUY_NO", "LONG_YES": "BUY_YES", "LONG_NO": "BUY_NO", "NOTRADE": "NO_TRADE", "NONE": "NO_TRADE", "PASS": "NO_TRADE", "SKIP": "NO_TRADE", "SELL": "EXIT", "CLOSE": "EXIT", "KEEP": "HOLD"}.get(tok, tok)
    out["raw_action"] = (str(act)[:40] if act is not None else None)
    reason = raw.get("reason", raw.get("rationale"))
    if reason is not None: out["reason"] = str(reason)[:300]
    hp = _norm_token(raw.get("hold_plan")) if raw.get("hold_plan") is not None else None
    out["hold_plan"] = hp if hp in HOLD_PLANS else None
    if not tok: out["error"] = "action_missing"; return out
    if tok not in allowed:
        out["error"] = f"action_not_allowed:{tok}"; return out                 # e.g. EXIT while flat, BUY while holding
    out["action"] = tok
    if tok in ("BUY_YES", "BUY_NO"):
        q = _to_qty(raw.get("qty", raw.get("quantity", raw.get("size")))); px = _to_price(raw.get("limit_price", raw.get("limit", raw.get("price"))))
        if q is None or q <= 0: out.update({"action": noop, "error": "qty_invalid"}); return out
        if px is None: out.update({"action": noop, "error": "limit_price_invalid"}); return out
        out["qty"] = q; out["limit_price"] = round(px, 4)
        if out["hold_plan"] is None: out["hold_plan"] = "HOLD_TO_SETTLEMENT"
    return out

# ------------------------------------------------------------------------------------------------------- hard limits
def hard_limits(action: str, qty: int, limit: float, ledger: R.Ledger | None, close_ts: int, series: str = SERIES, cfg: dict = CFG, rcfg: dict = R.CFG) -> dict:
    """The only filter between the model's decision and the paper fill: lot bounds (clamp), per-market cost cap, per-close exposure
    caps shared with the global ledger (clamp qty into the remaining room), series daily cap, loss halts (refuse). No edge, no
    probability, no Kelly. Returns {ok, qty, limit, reason, clamps:[...]} — reason starts with 'risk_limited:' when refused."""
    clamps = []
    if action not in ("BUY_YES", "BUY_NO"): return {"ok": False, "qty": 0, "limit": limit, "reason": "not_an_entry", "clamps": clamps}
    if limit is None or not (cfg["min_px"] <= limit <= cfg["max_px"]): return {"ok": False, "qty": 0, "limit": limit, "reason": "risk_limited:limit_out_of_bounds", "clamps": clamps}
    q = int(qty)
    if q < cfg["min_lot"]: clamps.append(f"qty_{q}->min_lot_{cfg['min_lot']}"); q = cfg["min_lot"]
    if q > cfg["max_lot"]: clamps.append(f"qty_{q}->max_lot_{cfg['max_lot']}"); q = cfg["max_lot"]
    room_market = int(math.floor(rcfg["max_cost_per_market"] / limit + 1e-9))
    if q > room_market: clamps.append(f"qty_{q}->market_cost_cap_{room_market}"); q = room_market
    if ledger is not None:
        if cfg["enforce_halts"]:
            h = R.halted(ledger)
            if h: return {"ok": False, "qty": 0, "limit": limit, "reason": f"risk_limited:halt:{h}", "clamps": clamps}
        rooms = {"settlement_ts": rcfg["max_cost_per_settlement_ts"] - ledger.cost_settlement(close_ts),
                 "settlement_ts_group": rcfg["max_cost_per_settlement_ts_group"] - ledger.cost_settlement(close_ts, group=(ledger.family or "").split("-")[0])}
        if series in rcfg["crypto_series"]: rooms["crypto_close"] = rcfg["max_cost_per_settlement_ts_crypto"] - ledger.cost_settlement(close_ts, series_set=rcfg["crypto_series"])
        rooms["series_day"] = rcfg["max_cost_per_series_day"] * (5 if rcfg.get("paper_mode", True) else 1) - ledger.cost_series_today(series)
        for name, room in rooms.items():
            rq = int(math.floor(max(room, 0.0) / limit + 1e-9))
            if q > rq: clamps.append(f"qty_{q}->{name}_{rq}"); q = rq
    if q < cfg["min_lot"]: return {"ok": False, "qty": 0, "limit": limit, "reason": f"risk_limited:{clamps[-1] if clamps else 'below_min_lot'}", "clamps": clamps}
    return {"ok": True, "qty": q, "limit": limit, "reason": "ok", "clamps": clamps}

# -------------------------------------------------------------------------------------------------------- pure helpers
def side_of(action: str) -> str: return "yes" if action == "BUY_YES" else "no"

def position_view(p: dict, book: dict | None) -> dict:
    """What the model sees about the arm's open position at T-5: side, qty, entry px, cost+fee, current bid/ask on OUR side,
    executable exit value (depth-weighted, after fee) and the unrealised P&L at that value."""
    ex = S.exit_value(p["side"], book, p["qty"]) if book else {"qty": 0, "avg_px": None, "proceeds": 0.0, "fee": 0.0, "partial": True, "best_bid": None}
    yb, ya = (book or {}).get("yes_bid"), (book or {}).get("yes_ask")
    if p["side"] == "yes": bid, ask = yb, ya
    else: bid, ask = (None if ya is None else round(1 - ya, 4)), (None if yb is None else round(1 - yb, 4))
    paid = p["cost"] + p["fee"]
    return {"side": p["side"], "qty": p["qty"], "entry_px": round(p["px"], 4), "paid_incl_fee": round(paid, 4), "current_bid": bid, "current_ask": ask,
            "exit_value_dw_after_fee": round(ex["proceeds"], 4), "exit_fillable_qty": ex["qty"], "unrealised": round(ex["proceeds"] - paid, 4), "opened_secs_left": p.get("decision_secs_left")}

def direct_block(decision_secs_left: float, position: dict | None, book: dict | None) -> dict:
    """The `direct_arm` packet block: allowed actions for this state plus the position view when holding."""
    label = f"T-{int(round(decision_secs_left / 60))}min"
    if position is None:
        return {"version": VERSION, "decision_time": label, "holding": False, "allowed_actions": list(ACTIONS_OPEN), "lot_range": [CFG["min_lot"], CFG["max_lot"]],
                "note": "paper account; your BUY is executed as given at your limit_price (depth-weighted, partial fills possible), subject only to hard risk limits"}
    return {"version": VERSION, "decision_time": label, "holding": True, "allowed_actions": list(ACTIONS_HELD), "position": position_view(position, book),
            "note": "EXIT sells the whole position now at the executable bid (depth-weighted, taker fee again); HOLD keeps it to settlement"}

def fill_entry(action: str, qty: int, limit: float, book: dict) -> dict:
    """Paper fill of the model's order at ITS limit against the given book (sol_v1.paper_fill: depth-weighted walk up to limit,
    +1 tick, partial allowed, never above displayed size). Returns sol_v1's dict plus side and the walked levels."""
    side = side_of(action); f = S.paper_fill(side, qty, limit, book)
    if f["px"] is not None: f["px"] = round(f["px"], 6); f["cost"] = round(f["cost"], 6)
    lv = S.side_levels(book, side, "buy")
    return {**f, "side": side, "best_offer": (lv[0][0] if lv else None), "depth_within_limit": sum(q for p, q in lv if p <= limit + 1e-12)}

def realized_from_fills(rows: list[dict]) -> float:
    return float(sum(r.get("pnl", 0.0) for r in rows if r.get("kind") in ("settle", "exit")))

# ----------------------------------------------------------------------------------------------------------- the arm
class DirectArm:
    """Stateful executor for the live sidecar. All I/O lives here: family files under `root`, live book from SOL-v1's ws mirror
    (data/sol-v1/book/latest.json) or Kalshi REST, settlements from the exchange result, marks every 60 s."""
    VERSION = VERSION
    def __init__(self, root: pathlib.Path = ROOT, sol_root: pathlib.Path = SOL_ROOT, red=None, ledger: R.Ledger | None = None, kalshi=None, book_fn=None, result_fn=None, series_fn=None, cfg: dict | None = None):
        if R.MODE != "paper": raise RuntimeError("P24 llm-direct is paper-only; refuse to start with PREDHFT_MODE != paper")
        self.root = root; self.sol_root = sol_root; self.cfg = {**CFG, **(cfg or {})}; self.red = red
        root.mkdir(parents=True, exist_ok=True)
        self.pos = S.Positions(root); self.led = ledger if ledger is not None else R.Ledger(root, family=FAMILY)
        self._k = kalshi; self._book_fn = book_fn; self._result_fn = result_fn; self._series_fn = series_fn
        self.last_mark = 0.0; self._last_settle_poll: dict[str, float] = {}
        self.realized = realized_from_fills(self._fill_rows())
        self._log(f"start {VERSION} family={FAMILY} mode={R.MODE} open={list(self.pos.open)} realized={self.realized:+.2f} cfg={json.dumps(self.cfg)}")

    # -- plumbing
    def _redact(self, s: str) -> str: return self.red.redact(s) if self.red is not None else s
    def _log(self, m: str):
        line = f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {self._redact(m)}"; print(line, flush=True)
        with (self.root / "runner.log").open("a", encoding="utf-8") as fh: fh.write(line + "\n")
    def _append(self, name: str, row: dict): S._append(self.root / name, row)
    def _fill_rows(self) -> list[dict]:
        if not self.pos.fills.exists(): return []
        out = []
        for l in self.pos.fills.read_text(encoding="utf-8").splitlines():
            if l.strip():
                try: out.append(json.loads(l))
                except Exception: continue
        return out
    def _kalshi(self):
        if self._k is None:
            from .kalshi_public import KalshiPublic; self._k = KalshiPublic(min_interval_s=0.25)
        return self._k
    def book(self, ticker: str, snap: dict | None = None) -> dict:
        """Live book for execution: ws mirror if fresh, else REST, else the snapshot's book (flagged)."""
        if self._book_fn is not None: return self._book_fn(ticker, snap)
        b = S._ws_book(ticker, self.sol_root, self.cfg["max_book_age_s"])
        if b is not None: return b
        try: return S._rest_book(self._kalshi(), ticker)
        except Exception as e:
            if snap is None: raise
            return {"yes_bid": snap.get("yes_bid"), "yes_ask": snap.get("yes_ask"), "bids": snap.get("bids"), "asks": snap.get("asks"), "recv_t": snap.get("t"), "fetch_s": None, "src": f"snapshot_fallback:{type(e).__name__}"}

    # -- packet block
    def position_for(self, ticker: str) -> dict | None: return self.pos.open.get(ticker)
    def block(self, snap: dict, decision_secs_left: float, now: float) -> dict:
        p = self.position_for(snap.get("ticker"))
        book = None
        if p is not None:
            try: book = self.book(snap["ticker"], snap)
            except Exception: book = None
            p["_last_book"] = book
        return direct_block(decision_secs_left, p, book)

    # -- act on one decision
    def act(self, snap: dict, decision_secs_left: float, decision_raw, dblock: dict | None, t_decision: float, prompt_hash: str, model_name: str, raw_present: bool = True) -> dict:
        tk = snap["ticker"]; close_ts = int(snap["close_ts"]); K = float(snap["K"]); held = self.position_for(tk)
        d = parse_decision(decision_raw, holding=held is not None)
        if d["error"] and not raw_present: d["error"] = "no_model_text:" + d["error"]
        tag = {"prompt_hash": prompt_hash, "model_name": model_name, "version": VERSION, "mode": R.MODE}
        rec = {"kind": "decision", "t": t_decision, "ticker": tk, "close_ts": close_ts, "K": K, "decision_secs_left": decision_secs_left, "holding": held is not None, "allowed": (dblock or {}).get("allowed_actions"),
               "raw": (json.dumps(decision_raw, default=str)[:600] if decision_raw is not None else None), "parsed": {k: d[k] for k in ("action", "qty", "limit_price", "hold_plan", "reason", "raw_action")}, "parse_error": d["error"],
               "mid": snap.get("mid"), "yes_bid": snap.get("yes_bid"), "yes_ask": snap.get("yes_ask"), "p_model": snap.get("p_model"), "snapshot_src": snap.get("snapshot_src", "sol_v1"), "risk": None, "result": None, **tag}
        result: dict = {"status": "no_action"}
        try:
            if d["action"] in ("BUY_YES", "BUY_NO"):
                if self.pos.has_close(close_ts) or held is not None: result = {"status": "refused:already_positioned"}
                else:
                    hl = hard_limits(d["action"], d["qty"], d["limit_price"], self.led, close_ts, SERIES, self.cfg); rec["risk"] = hl
                    if not hl["ok"]: result = {"status": hl["reason"]}
                    else: result = self._enter(tk, close_ts, K, d, hl, snap, decision_secs_left, t_decision, tag)
            elif d["action"] == "EXIT": result = self._exit(tk, close_ts, held, d, snap, decision_secs_left, t_decision, tag)
            elif d["action"] == "HOLD": result = {"status": "hold"}
            else: result = {"status": "no_trade" if not d["error"] else "no_trade:" + d["error"]}
        except Exception as e:
            result = {"status": "arm_error", "error": self._redact(f"{type(e).__name__}: {str(e)[:160]}")}
        rec["result"] = result; self._append("decisions.jsonl", rec)
        self._log(f"DECISION {tk} T-{decision_secs_left:.0f}s action={d['action']} qty={d['qty']} limit={d['limit_price']} plan={d['hold_plan']} err={d['error']} -> {result.get('status')}"
                  + (f" fill {result.get('qty')}/{result.get('intended_qty')}@{result.get('px')}" if result.get("status") in ("filled", "partial") else "")
                  + (f" exit {result.get('qty')}@{result.get('exit_px')} pnl={result.get('pnl'):+.2f}" if result.get("status", "").startswith("exited") else ""))
        return {"decision": {**{k: d[k] for k in ("action", "qty", "limit_price", "hold_plan", "reason")}, "error": d["error"]}, "result": result}

    def _enter(self, tk, close_ts, K, d, hl, snap, dsl, t_decision, tag) -> dict:
        intent_id = f"{FAMILY}:{tk}:{side_of(d['action'])}:T{int(dsl)}"
        if intent_id in self.pos.intents: return {"status": "refused:intent_exists", "intent_id": intent_id}
        self.pos.intents.add(intent_id)
        book = self.book(tk, snap); t_fill = time.time()
        f = fill_entry(d["action"], hl["qty"], hl["limit"], book)
        row = {"kind": "fill", "t": t_fill, "t_decision": t_decision, "intent_id": intent_id, "ticker": tk, "close_ts": close_ts, "K": K, "side": f["side"], "qty": f["qty"], "intended_qty": hl["qty"], "model_qty": d["qty"],
               "limit": hl["limit"], "px": f["px"], "cost": f["cost"], "fee": f["fee"], "partial": f["partial"], "status": ("open" if f["qty"] > 0 else "unfilled"), "fill_status": f["status"], "best_offer": f["best_offer"], "depth_within_limit": f["depth_within_limit"],
               "book": {"yes_bid": book.get("yes_bid"), "yes_ask": book.get("yes_ask"), "bids": (book.get("bids") or [])[:5], "asks": (book.get("asks") or [])[:5]}, "book_src": book.get("src"), "book_recv_t": book.get("recv_t"), "book_age_s": (round(t_fill - book["recv_t"], 3) if book.get("recv_t") else None),
               "decision_secs_left": dsl, "hold_plan": d["hold_plan"], "reason": d["reason"], "risk_clamps": hl["clamps"], "mid": snap.get("mid"), "p_model": snap.get("p_model"), **tag}
        self._append("fills.jsonl", row)
        if f["qty"] > 0:
            row["_last_book"] = book; self.pos.open[tk] = row; self.led.record_open(tk, SERIES, f["cost"], close_ts=close_ts, side=f["side"])
            self._log(f"PAPER BUY {f['side'].upper()} {f['qty']}/{hl['qty']}@{f['px']:.3f} limit={hl['limit']:.3f} {tk} fee={f['fee']:.2f} T-{dsl:.0f}s src={book.get('src')} clamps={hl['clamps']}")
        else: self._log(f"UNFILLED {tk} {f['side']} {hl['qty']} limit={hl['limit']:.3f} best_offer={f['best_offer']} src={book.get('src')}")
        return {"status": f["status"] if f["qty"] > 0 else "unfilled", "intent_id": intent_id, "side": f["side"], "qty": f["qty"], "intended_qty": hl["qty"], "px": f["px"], "limit": hl["limit"], "cost": f["cost"], "fee": f["fee"], "best_offer": f["best_offer"], "t_fill": t_fill}

    def _exit(self, tk, close_ts, held, d, snap, dsl, t_decision, tag) -> dict:
        if held is None: return {"status": "refused:no_position"}
        book = self.book(tk, snap); t_fill = time.time(); ex = S.exit_value(held["side"], book, held["qty"])
        if ex["qty"] <= 0:
            self._append("fills.jsonl", {"kind": "exit_unfilled", "t": t_fill, "t_decision": t_decision, "intent_id": held["intent_id"], "ticker": tk, "close_ts": close_ts, "side": held["side"], "qty": held["qty"], "book_src": book.get("src"), "reason": d["reason"], **tag})
            self._log(f"EXIT UNFILLED {tk} {held['side']} {held['qty']} (no bid) src={book.get('src')}"); return {"status": "exit_unfilled", "qty": 0}
        frac = ex["qty"] / held["qty"]; cost_part = held["cost"] * frac; fee_part = held["fee"] * frac; pnl = ex["proceeds"] - cost_part - fee_part
        self._append("fills.jsonl", {"kind": "exit", "t": t_fill, "t_decision": t_decision, "intent_id": held["intent_id"], "ticker": tk, "close_ts": close_ts, "side": held["side"], "qty": ex["qty"], "held_qty": held["qty"], "exit_px": ex["avg_px"], "exit_fee": ex["fee"], "proceeds": ex["proceeds"],
                                     "entry_px": held["px"], "cost": cost_part, "fee": fee_part, "pnl": pnl, "partial": ex["partial"], "decision_secs_left": dsl, "reason": d["reason"],
                                     "book": {"yes_bid": book.get("yes_bid"), "yes_ask": book.get("yes_ask"), "bids": (book.get("bids") or [])[:5], "asks": (book.get("asks") or [])[:5]}, "book_src": book.get("src"), "book_recv_t": book.get("recv_t"), **tag})
        self.pos.open.pop(tk, None); self.led.record_settle(tk, pnl); self.realized += pnl
        status = "exited"
        if ex["partial"]:                                                        # remainder stays open under a derived intent id
            rem = held["qty"] - ex["qty"]; rrow = {**{k: v for k, v in held.items() if not k.startswith("_")}, "t": t_fill, "intent_id": held["intent_id"] + f":rem{rem}", "qty": rem, "cost": held["cost"] - cost_part, "fee": held["fee"] - fee_part, "partial": True, "status": "open", "note": "remainder after partial EXIT"}
            self._append("fills.jsonl", rrow); rrow["_last_book"] = book; self.pos.open[tk] = rrow; self.pos.intents.add(rrow["intent_id"]); status = "exited_partial"
        self._log(f"PAPER EXIT {held['side'].upper()} {ex['qty']}/{held['qty']}@{ex['avg_px']:.3f} {tk} proceeds={ex['proceeds']:.2f} pnl={pnl:+.2f} src={book.get('src')}")
        return {"status": status, "qty": ex["qty"], "held_qty": held["qty"], "exit_px": ex["avg_px"], "exit_fee": ex["fee"], "proceeds": ex["proceeds"], "pnl": pnl, "t_fill": t_fill}

    # -- periodic: settlements + marks
    def _result(self, tk: str) -> dict | None:
        if self._result_fn is not None: return self._result_fn(tk)
        try: return self._kalshi().get(f"/markets/{tk}")["market"]
        except Exception: return None
    def _series(self, now: float):
        if self._series_fn is not None: return self._series_fn(now)
        try: return S._cfb_tail(now, 1200)[0]
        except Exception: return []
    def tick(self, now: float):
        for tk, p in list(self.pos.open.items()):
            if now < p["close_ts"] + self.cfg["settle_after_s"] or now - self._last_settle_poll.get(tk, 0.0) < self.cfg["settle_poll_s"]: continue
            self._last_settle_poll[tk] = now; m = self._result(tk)
            res = (m or {}).get("result")
            if res in ("yes", "no"):
                own = S.settle_from_feed(self._series(now), p["close_ts"], p["K"])
                win = res == p["side"]; pnl = (1.0 if win else 0.0) * p["qty"] - p["cost"] - p["fee"]
                row = {"kind": "settle", "t": now, "intent_id": p["intent_id"], "ticker": tk, "close_ts": p["close_ts"], "side": p["side"], "qty": p["qty"], "px": p["px"], "cost": p["cost"], "fee": p["fee"], "result": res, "win": win,
                       "expiration_value": m.get("expiration_value"), "feed_avg": (own or {}).get("avg"), "feed_result": (own or {}).get("result"), "feed_disagrees": bool(own and own["result"] != res), "pnl": pnl,
                       "prompt_hash": p.get("prompt_hash"), "model_name": p.get("model_name"), "version": VERSION, "mode": R.MODE}
                self._append("fills.jsonl", row); self.pos.open.pop(tk, None); self.led.record_settle(tk, pnl); self.realized += pnl
                self._log(f"SETTLED {tk} side={p['side']} result={res} feed={row['feed_result']} qty={p['qty']} pnl={pnl:+.2f} realized={self.realized:+.2f}")
        if now - self.last_mark >= self.cfg["mark_interval_s"]:
            self.last_mark = now; mark = 0.0
            for tk, p in self.pos.open.items():
                b = None
                try: b = self.book(tk)
                except Exception: b = p.get("_last_book")
                if b is not None: p["_last_book"] = b
                mark += S.conservative_mark(p["side"], b, p["qty"])
            open_cost = sum(p["cost"] + p["fee"] for p in self.pos.open.values())
            self._append("marks.jsonl", {"t": now, "realized": round(self.realized, 4), "open_cost": round(open_cost, 4), "open_mark": round(mark, 4), "equity": round(self.cfg["bankroll"] + self.realized - open_cost + mark, 4), "n_open": len(self.pos.open), "version": VERSION})
