"""P22 simulated-execution model, measurement version **p22-v2** (docs/AUDIT_ADDENDUM2_2026-10-28.md §1).

v1 (scripts/p22_paired.py before 2026-10-28 and `reject_taxonomy.classify`) booked the paper price as ``min(raw_offer + 0.01, 0.99)``.
Two defects follow from the cap: for raw offers in (0.98, 0.99] the one-tick haircut is truncated (0.985 -> 0.99 = +0.5 c instead
of +1 c), and for raw offers above 0.99 the paper price is *below* the offer actually displayed (0.995 -> 0.99). 161 of the 269
"offered" records of the 2026-10-28 run were affected; every R2 "win" of +0.9 c was this artefact.

v2 keeps the RAW observed offer separately from every simulated adjustment, never clips, and reports a status:

    ok                     0 < raw_offer <= 0.98 : exec_price = raw_offer + tick (<= 0.99, inside the modeled price range)
    outside_modeled_range  raw_offer  > 0.98     : exec_price = raw_offer + tick is still computed (> 0.99) but the record is
                                                  excluded from every executable-edge / realised-net denominator
    unavailable            raw_offer is None or not in (0, 1): nothing can be booked

Pure functions, no I/O. Fee = Kalshi taker 0.07 x q x P x (1-P), ceil-to-cent per order of `lot`, divided by `lot` (as in v1).
"""
from __future__ import annotations
from .fees import kalshi_taker_fee

MEASUREMENT_VERSION = "p22-v2"
TICK = 0.01
MAX_MODELED_PRICE = 0.99          # highest price the paper model is allowed to book; raw_offer + TICK must not exceed it
STATUS_OK = "ok"; STATUS_OUTSIDE = "outside_modeled_range"; STATUS_UNAVAILABLE = "unavailable"


def exec_price(raw_offer: float | None, tick: float = TICK, max_price: float = MAX_MODELED_PRICE) -> dict:
    """{raw_offer, exec_price, status}. exec_price is raw + tick with NO cap; status says whether it is inside the modeled range."""
    if raw_offer is None or not (0.0 < float(raw_offer) < 1.0):
        return {"raw_offer": raw_offer, "exec_price": None, "status": STATUS_UNAVAILABLE}
    raw = float(raw_offer); px = raw + tick
    status = STATUS_OK if px <= max_price + 1e-9 else STATUS_OUTSIDE
    return {"raw_offer": raw, "exec_price": round(px, 6), "status": status}


def legacy_v1_price(raw_offer: float, tick: float = TICK, cap: float = MAX_MODELED_PRICE) -> float:
    """The v1 formula, kept only so tests and the recompute can state exactly what changed."""
    return min(float(raw_offer) + tick, cap)


def fee_per_contract(price: float, lot: int = 10) -> float:
    return kalshi_taker_fee(lot, price) / lot


def book_record(raw_offer: float | None, p_side: float | None, win: float | None, lot: int = 10, tick: float = TICK) -> dict:
    """One simulated execution: raw offer, v2 exec price/status, fee, model-implied edge (c/contract) and realised net (c/contract).
    edge_c / real_c / fee are None unless status == ok (outside-range records are never clipped into the denominators)."""
    e = exec_price(raw_offer, tick)
    out = {"raw_offer": e["raw_offer"], "exec_price": e["exec_price"], "exec_status": e["status"], "fee_per_contract": None, "edge_c": None, "real_c": None,
           "measurement_version": MEASUREMENT_VERSION}
    if e["status"] != STATUS_OK: return out
    px = e["exec_price"]; fee = fee_per_contract(px, lot); out["fee_per_contract"] = round(fee, 6)
    if p_side is not None: out["edge_c"] = round(100 * (p_side - px - fee), 4)
    if win is not None: out["real_c"] = round(100 * (win - px - fee), 4)
    return out


def recompute_record(rec: dict, tag: str = "off", lot: int = 10) -> dict:
    """Re-derive the v2 fields of one v1 `p22_paired` record (which stores the RAW walked offer as `offer_<tag>` and the forecast
    `p_<tag>`, side `side_<tag>`, outcome `y`). Adds exec_price_<tag>, exec_status_<tag>, fee_<tag>, edge_c_<tag>, real_c_<tag> and keeps
    the v1 values under v1_edge_c_<tag> / v1_real_c_<tag> when they were present. Records without a book / offer get an explicit status."""
    r = dict(rec); r["measurement_version"] = MEASUREMENT_VERSION
    for k in (f"edge_c_{tag}", f"real_c_{tag}"):
        if k in rec: r[f"v1_{k}"] = rec[k]
    if not rec.get("book_src"):
        r[f"exec_status_{tag}"] = "no_book"; r[f"exec_price_{tag}"] = None; r[f"edge_c_{tag}"] = None; r[f"real_c_{tag}"] = None; r[f"fee_{tag}"] = None; return r
    raw = rec.get(f"offer_{tag}")
    if raw is None:
        r[f"exec_status_{tag}"] = "no_offer"; r[f"exec_price_{tag}"] = None; r[f"edge_c_{tag}"] = None; r[f"real_c_{tag}"] = None; r[f"fee_{tag}"] = None; return r
    p = rec.get(f"p_{tag}"); side = rec.get(f"side_{tag}"); y = rec.get("y")
    p_side = (p if side == "yes" else 1 - p) if (p is not None and side) else None
    win = (y if side == "yes" else 1 - y) if (y is not None and side) else None
    b = book_record(raw, p_side, win, lot)
    r[f"exec_price_{tag}"] = b["exec_price"]; r[f"exec_status_{tag}"] = b["exec_status"]; r[f"fee_{tag}"] = b["fee_per_contract"]; r[f"edge_c_{tag}"] = b["edge_c"]; r[f"real_c_{tag}"] = b["real_c"]
    return r


# ------------------------------------------------------------------------------------------------------------ rule cells
HORIZONS = (60, 45, 30, 20, 10)
RULES = {"ALL_offered": lambda ps, raw: True, "R1": lambda ps, raw: ps >= 0.6, "R2": lambda ps, raw: ps >= 0.9, "R3": lambda ps, raw: ps >= 0.6 and raw <= 0.95}
RULE_TEXT = {"ALL_offered": "every record whose intended side is offered with depth >= lot (status ok)", "R1": "p_side >= 0.6 and intended-side depth >= lot", "R2": "p_side >= 0.9 and intended-side depth >= lot",
             "R3": "R1 and raw offer <= 0.95 (inside the P13 fav cap)"}


def _cluster_se(vals, clusters):
    import collections, math
    n = len(vals)
    if n < 2: return None
    m = sum(vals) / n; by = collections.defaultdict(float)
    for v, c in zip(vals, clusters): by[c] += v - m
    G = len(by)
    return math.sqrt(sum(s * s for s in by.values()) * G / (G - 1)) / n if G > 1 else None


def rule_cells(recs: list[dict], tag: str = "off", lot: int = 10, version: str = "v2") -> dict:
    """R1/R2/R3 cells per horizon. version 'v2': denominator = records with exec_status ok (raw offer <= 0.98, depth >= lot);
    'v1': denominator = every offered record with the capped price (reproduces the addendum table from the same raw inputs).
    Each cell: n, n_clusters (closes), mean model edge c/ct, realised net c/ct, SE by close (None when < 2 clusters), n excluded as
    outside_modeled_range, and the v1 -> v2 delta of the denominator."""
    out = {}
    ps_of = lambda r: (r[f"p_{tag}"] if r[f"side_{tag}"] == "yes" else 1 - r[f"p_{tag}"])
    for h in HORIZONS:
        with_book = [r for r in recs if r["h"] == h and r.get("book_src")]
        offered = [r for r in with_book if r.get(f"offer_{tag}") is not None and r.get(f"depth_{tag}", lot) >= lot]
        cells = {"with_book": len(with_book), "two_sided": sum(1 for r in with_book if r.get("two_sided")), "offered_any_price": len(offered),
                 "outside_modeled_range": sum(1 for r in offered if exec_price(r[f"offer_{tag}"])["status"] != STATUS_OK), "no_offer_on_intended_side": sum(1 for r in with_book if r.get(f"offer_{tag}") is None)}
        for name, rule in RULES.items():
            R = [r for r in offered if rule(ps_of(r), r[f"offer_{tag}"])]
            excl = [r for r in R if exec_price(r[f"offer_{tag}"])["status"] != STATUS_OK]
            if version == "v2": R = [r for r in R if exec_price(r[f"offer_{tag}"])["status"] == STATUS_OK]
            e = []; rl = []
            for r in R:
                if version == "v2":
                    b = book_record(r[f"offer_{tag}"], ps_of(r), (r["y"] if r[f"side_{tag}"] == "yes" else 1 - r["y"]), lot)
                    e.append(b["edge_c"]); rl.append(b["real_c"])
                else:
                    px = legacy_v1_price(r[f"offer_{tag}"]); fee = fee_per_contract(px, lot); win = r["y"] if r[f"side_{tag}"] == "yes" else 1 - r["y"]
                    e.append(100 * (ps_of(r) - px - fee)); rl.append(100 * (win - px - fee))
            G = len({r["close_ts"] for r in R})
            cells[name] = {"n": len(R), "n_clusters_close": G, "n_excluded_outside_modeled_range": len(excl), "mean_edge_c": (round(sum(e) / len(e), 2) if e else None),
                           "realized_net_c": (round(sum(rl) / len(rl), 2) if rl else None), "se_close": (_cluster_se(rl, [r["close_ts"] for r in R]) if G > 1 else None),
                           "share_edge_gt_0": (round(sum(1 for v in e if v > 0) / len(e), 2) if e else None), "n_edge_ge_5c": sum(1 for v in e if v >= 5)}
            if cells[name]["se_close"] is not None: cells[name]["se_close"] = round(cells[name]["se_close"], 2)
        out[h] = cells
    return out
