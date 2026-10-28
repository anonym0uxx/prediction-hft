"""P12 decision-neutrality replay (docs/AUDIT_ADDENDUM2_2026-10-28.md §2). Pure functions, no I/O.

The P12 runners (s7p15-KX*15M, model rule, proj_gate=False) have run commit a395cf8 since the 2026-10-27 16:05 PT freeze. HEAD
changed how the order book is *represented and logged* (`book_sides`: None for an absent side, top-3 depth, `cfb_age_s`, post-fill
check rows, reject taxonomy for proj_gate families only). This module re-implements, side by side, the a395cf8 book parsing
(`legacy_book_touch`, verbatim) and the model-rule decision path of `s7_live.run` (candidate construction -> re-check -> booked
price -> sizing inputs) as a pure function, so that recorded snapshots can be replayed through both parsers and the outputs compared.

Replay inputs are reconstructed Kalshi `orderbook_fp` payloads: a recorded snapshot stores yes_bid (best YES bid), yes_ask
(= 1 - best NO bid as the legacy parser computed it) and both sizes; the raw NO bid is recovered as round(1 - yes_ask, 3) (Kalshi
15M prices sit on the 0.001 grid), which is exact for every recorded row (checked by the test).
"""
from __future__ import annotations
import math
from .fees import kalshi_taker_fee
from .risk import kelly_contracts, CFG
from . import s7_live as S7

NEW_SNAPSHOT_LOGGING_FIELDS = ("yes_bid_present", "yes_ask_present", "bids", "asks", "cfb_age_s", "book_recv_t", "book_fetch_s")
NEW_TRADE_LOGGING_FIELDS = ("book_recv_t", "recheck_recv_t", "recheck_fetch_s", "quote_age_s", "intended_lot", "avail_qty")
NEW_LOGGING_FIELDS = NEW_SNAPSHOT_LOGGING_FIELDS + NEW_TRADE_LOGGING_FIELDS


def legacy_book_touch(ob: dict):
    """a395cf8 `s7_live.book_touch`, verbatim."""
    o = ob.get("orderbook_fp") or {}
    yes = [(float(p), float(q)) for p, q in (o.get("yes_dollars") or [])]
    no = [(float(p), float(q)) for p, q in (o.get("no_dollars") or [])]
    yb, ybs = (max(yes)[0], max(yes)[1]) if yes else (0.0, 0.0)
    if no:
        nb = max(no); ya, yas = 1 - nb[0], nb[1]
    else: ya, yas = 1.0, 0.0
    return yb, ya, ybs, yas


def head_book_touch(ob: dict):
    """HEAD `s7_live.book_touch` (via book_sides) — the tuple the rule arithmetic consumes at HEAD."""
    return S7.book_touch(ob)


def orderbook_from_snapshot(row: dict) -> dict:
    """Rebuild the orderbook_fp payload a snapshot was computed from. Legacy rows: yes_bid 0.0/size 0 = no YES bids, yes_ask 1.0/size 0 =
    no NO bids. New rows: None for an absent side."""
    yb, ya = row.get("yes_bid"), row.get("yes_ask"); ybs = float(row.get("bid_size") or 0.0); yas = float(row.get("ask_size") or 0.0)
    yes = [[f"{yb:.3f}", f"{ybs}"]] if (yb is not None and yb > 0) else []
    no = [[f"{round(1 - ya, 3):.3f}", f"{yas}"]] if (ya is not None and ya < 1) else []
    return {"orderbook_fp": {"yes_dollars": yes, "no_dollars": no}}


def p12_candidate(yb: float, ya: float, ybs: float, yas: float, p: float, z: float, thr_used: float, mins_left: float, partial, partial_n: int, lot: int) -> dict | None:
    """Model-rule candidate construction (s7_live.run lines 'else:' branch for proj_gate=False, identical at a395cf8 and HEAD).
    Returns {side, edge, score, mid, spread} or None."""
    mid = (yb + ya) / 2 if 0 < ya < 1 else None
    if mins_left < 1.0 and (partial is None or partial_n < 3): return None
    if ya - yb > 0.06 or not (mid and 0.03 <= mid <= 0.97): return None
    side = None
    if p - ya >= thr_used and 0 < ya < 1 and yas >= lot: side, edge = "yes", p - ya
    elif (yb - p) >= thr_used and 0 < yb < 1 and ybs >= lot: side, edge = "no", yb - p
    if side is None: return None
    return {"side": side, "edge": edge, "score": edge / math.sqrt(max(p * (1 - p), 1e-4)), "mid": mid, "spread": round(ya - yb, 2)}


def p12_recheck_and_book(cand: dict, yb2: float, ya2: float, ybs2: float, yas2: float, p: float, thr_used: float, lot: int, bankroll: float = CFG["bankroll"]) -> dict:
    """Re-check + booking (s7_live.run: ok test for the model rule, price = touch + 0.01 capped .99, fee, sizing INPUTS and the
    pure parts of risk.size: Kelly on the blended belief is ledger-dependent, so the replay reports kelly on p_side with the
    fixed bankroll, the thin-book factor and the lot cap; the ledger caps are identical code at both commits)."""
    side = cand["side"]
    ok = (side == "yes" and p - ya2 >= thr_used and 0 < ya2 < 1 and yas2 > 0) or (side == "no" and yb2 - p >= thr_used and 0 < yb2 < 1 and ybs2 > 0)
    if not ok: return {"status": "stale"}
    price = min((ya2 if side == "yes" else 1 - yb2) + 0.01, 0.99); avail = yas2 if side == "yes" else ybs2
    p_side = p if side == "yes" else 1 - p; fee_pc = kalshi_taker_fee(10, price) / 10
    k = kelly_contracts(p_side, price, fee_pc, bankroll=bankroll)
    av = avail * (CFG["thin_fraction"] if avail < CFG["thin_book"] else 1.0)
    cap = min(lot, CFG["max_lot"], int(CFG["max_cost_per_market"] / max(price, 0.01)))
    q = int(min(k, cap, av)) if k > 0 else 0
    q = q if q >= CFG["min_lot"] else 0
    return {"status": "fill" if q > 0 else "skip", "side": side, "price": price, "avail": avail, "p_side": p_side, "fee_per_contract_10": fee_pc, "kelly": k, "qty": q,
            "cost": q * price, "fee": (kalshi_taker_fee(q, price) if q > 0 else 0.0)}


def replay_snapshot(row: dict, parser, lot: int = 10) -> dict:
    """Run one recorded snapshot through `parser` (legacy or HEAD) as both the decision book and the re-check book."""
    yb, ya, ybs, yas = parser(orderbook_from_snapshot(row))
    cand = p12_candidate(yb, ya, ybs, yas, row["p_model"], row.get("z", 0.0), row["thr_used"], row["mins_left"], row.get("partial_twap"), int(row.get("partial_n") or 0), lot)
    out = {"touch": (yb, ya, ybs, yas), "candidate": cand}
    if cand: out["booking"] = p12_recheck_and_book(cand, yb, ya, ybs, yas, row["p_model"], row["thr_used"], lot)
    return out
