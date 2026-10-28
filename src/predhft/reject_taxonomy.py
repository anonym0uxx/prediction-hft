"""Mutually exclusive first-failure rejection taxonomy for the final-minute (P13/P16/P18/P20) rule — docs/AUDIT_2026-10-28.md §1.3.

One snapshot (= one "check": one market, one poll inside the trading window) gets exactly ONE primary reason, chosen in this
fixed order of classes:

    data_defect > feed_health > timing_exclusion > risk_constraint > no_economic_opportunity

plus a list of ALL failing conditions (`flags`) for overlap diagnostics. The legacy NOFILL counters in s7_live were per-snapshot
increments in the runner's own control-flow order (d_small > ts_timing > ts_floor > fav_gt_95_or_size > p16 edge > gate >
kelly) and lumped three different states into `fav_gt_95_or_size` (no offer at all / real offer above the cap / offer with
size < lot). This module is pure (no I/O, no network) so it can be unit-tested and replayed over books.jsonl.

Book representation. A Kalshi orderbook carries BIDS only; the YES ask is 1 - best NO bid. When the NO side has no bids there
is NO offer for YES at any price. The legacy `book_touch` encoded that as yes_ask=1.00 / ask_size=0 (indistinguishable from a
real 1.00 offer); `s7_live.book_sides` now returns None for an absent side and the books rows carry `yes_ask_present` /
`yes_bid_present`. `side_offer()` below accepts both the new (None) and the legacy sentinel encodings.
"""
from __future__ import annotations
import math
from .fees import kalshi_taker_fee

CLASSES = ("data_defect", "feed_health", "timing_exclusion", "risk_constraint", "no_economic_opportunity", "candidate")
RUNNER_WINDOW_S = (9.0, 55.2)          # t_close 0.15 min .. t_open 0.92 min (the s7p13 runners)
FEED_MAX_AGE_S = 2.5                   # cfb_feed.latest max_age: older => runner fell back to the replica
PARTIAL_WARMUP_S = 57.0                # partial_n >= 3 cannot be true before T-57 s: that is timing, not a feed fault
PINNED = 0.97                          # a one-sided book whose present side is priced beyond this is a pinned market, not a defect
D_MIN_BP = {"KXBTC15M": 1.0, "KXETH15M": 1.0, "KXSOL15M": 2.0, "KXBTCD": 1.0, "KXETHD": 1.0, "KXSOLD": 2.0}


def Phi(x: float) -> float: return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def p_cal(d_bp: float, sig1: float, mins_left: float, basis_noise_bp: float) -> float:
    """Same formula as s7_live.p_cal (REBASE §5); duplicated so this module never imports the runner."""
    r = max(min(mins_left, 1.0), 0.02); sd_bp = math.sqrt((1.5 * sig1 * 1e4 * r * math.sqrt(r / 3.0)) ** 2 + basis_noise_bp ** 2)
    return Phi(d_bp / sd_bp) if sd_bp > 0 else (1.0 if d_bp > 0 else 0.0)


def side_present(row: dict) -> tuple[bool, bool]:
    """(yes_bid_present, yes_ask_present). New rows carry explicit booleans; legacy rows use the sentinels
    yes_bid=0/bid_size=0 (no YES bids => no NO offer) and yes_ask=1.0/ask_size=0 (no NO bids => no YES offer)."""
    if "yes_bid_present" in row and "yes_ask_present" in row:
        return bool(row["yes_bid_present"]), bool(row["yes_ask_present"])
    yb, ya = row.get("yes_bid"), row.get("yes_ask"); ybs, yas = row.get("bid_size") or 0.0, row.get("ask_size") or 0.0
    bid_ok = yb is not None and yb > 0 and not (yb == 0.0 and ybs == 0.0)
    ask_ok = ya is not None and ya < 1.0 and not (ya >= 1.0 and yas == 0.0)
    return bid_ok, ask_ok


def side_offer(row: dict, side: str) -> tuple[float | None, float]:
    """(offer price, offer size) for buying `side`, in that side's price space; price None when nobody offers it."""
    bid_ok, ask_ok = side_present(row)
    if side == "yes":
        return (float(row["yes_ask"]), float(row.get("ask_size") or 0.0)) if ask_ok else (None, 0.0)
    return ((1.0 - float(row["yes_bid"])), float(row.get("bid_size") or 0.0)) if bid_ok else (None, 0.0)


def classify(row: dict, series: str, *, lot: int = 10, cap: float = 0.95, edge_min: float = 0.05, arm_window_s: tuple[float, float] | None = None,
             runner_window_s: tuple[float, float] = RUNNER_WINDOW_S, official_era: bool = True, tick: float = 0.01) -> dict:
    """Classify one books.jsonl snapshot. Returns {cls, reason, flags, side, offer, offer_size, d_bp, p_side, edge_c, secs_left}.
    `reason` is "<cls>:<sub>"; cls == "candidate" means every check passed (a paper order would have been attempted).
    arm_window_s: the live Thompson arm's window (None = no P18 restriction). official_era: feed_health checks apply."""
    flags: list[str] = []; first: str | None = None
    def fail(reason: str):
        nonlocal first
        flags.append(reason)
        if first is None: first = reason
    secs_left = (row["close_ts"] - row["t"]) if row.get("close_ts") and row.get("t") else (row.get("mins_left") or 0.0) * 60.0
    mins_left = secs_left / 60.0
    K, spot = row.get("K"), row.get("spot"); partial, n = row.get("partial_twap"), int(row.get("partial_n") or 0)
    bid_ok, ask_ok = side_present(row)
    # ---- 1. data defects: things that should never happen with a healthy book / feed row
    if not K or not spot or spot <= 0: fail("data_defect:spot_or_strike_missing")
    if not bid_ok and not ask_ok: fail("data_defect:book_empty")
    elif (not bid_ok and row.get("yes_ask") is not None and ask_ok and float(row["yes_ask"]) > 1 - PINNED) or (not ask_ok and bid_ok and float(row["yes_bid"]) < PINNED):
        fail("data_defect:one_sided_book_unpinned")     # one side missing while the other side is NOT at certainty: suspicious payload
    if partial is not None and n > 0 and partial <= 0: fail("data_defect:partial_nonpositive")
    # ---- 2. feed health (official-feed era)
    if official_era:
        if row.get("spot_src") != "cfb": fail("feed_health:replica_fallback")
        age = row.get("cfb_age_s")
        if age is not None and age > FEED_MAX_AGE_S: fail("feed_health:feed_stale")
        if mins_left < 1.0 and secs_left <= PARTIAL_WARMUP_S and (partial is None or n < 3): fail("feed_health:partial_missing")
        exp_n = max(0.0, min(60.0, 60.0 - secs_left))
        if mins_left < 1.0 and n and (n < exp_n - 3 or n > exp_n + 2): fail("feed_health:partial_n_inconsistent")
    # ---- 3. timing exclusions
    if mins_left >= 1.0: fail("timing_exclusion:outside_final_minute")
    elif not (runner_window_s[0] <= secs_left <= runner_window_s[1]): fail("timing_exclusion:outside_runner_window")
    if mins_left < 1.0 and secs_left > PARTIAL_WARMUP_S and (partial is None or n < 3): fail("timing_exclusion:partial_warmup")
    if arm_window_s is not None and mins_left < 1.0 and not (arm_window_s[0] <= secs_left <= arm_window_s[1]): fail("timing_exclusion:ts_arm_window")
    # ---- projection / side (needs a partial)
    d_bp = p_side = None; side = None; offer = None; osz = 0.0; edge_c = None
    if K and spot and partial and n >= 3 and mins_left < 1.0:
        r = max(min(mins_left, 1.0), 0.02); proj = (1 - r) * partial + r * spot; d_bp = 1e4 * (proj / K - 1)
        side = "yes" if d_bp > 0 else "no"
        pc = p_cal(d_bp, row.get("sigma1") or 0.0006, mins_left, 0.0 if row.get("spot_src") == "cfb" else 0.6)
        p_side = pc if side == "yes" else 1 - pc
        offer, osz = side_offer(row, side)
    # ---- 4. deliberate risk constraints (need a real offer on our side)
    if side is not None and offer is not None:
        if offer > cap + 1e-9: fail("risk_constraint:fav_cap")
        elif osz < lot: fail("risk_constraint:depth_lt_lot")
    # ---- 5. absence of economic opportunity
    if side is not None and offer is None:
        other_px = (float(row["yes_bid"]) if bid_ok else None) if side == "yes" else (1.0 - float(row["yes_ask"]) if ask_ok else None)
        fail("no_economic_opportunity:no_offer_pinned" if (other_px is not None and other_px >= PINNED) else "no_economic_opportunity:no_offer")
    if d_bp is not None and abs(d_bp) < D_MIN_BP.get(series, 1.0): fail("no_economic_opportunity:d_small")
    if side is not None and offer is not None and 0 < offer < 1:
        price = min(offer + tick, 0.99); fee = kalshi_taker_fee(lot, price) / lot
        edge_c = round(100 * (p_side - price - fee), 2)
        if edge_c < 100 * edge_min: fail("no_economic_opportunity:edge_lt_min")
    reason = first or "candidate:ok"
    return {"cls": reason.split(":")[0], "reason": reason, "flags": flags, "side": side, "offer": offer, "offer_size": osz, "d_bp": (round(d_bp, 3) if d_bp is not None else None),
            "p_side": (round(p_side, 4) if p_side is not None else None), "edge_c": edge_c, "secs_left": round(secs_left, 2)}


def primary_from_flags(flags: list[str]) -> str:
    """Re-derive the primary reason from a flag list using the class order (useful when flags were logged, primary was not)."""
    for cls in CLASSES:
        for f in flags:
            if f.startswith(cls + ":"): return f
    return "candidate:ok"
