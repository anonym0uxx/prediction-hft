"""Recompute the historical P22 trading-value results under measurement version p22-v2 (docs/AUDIT_ADDENDUM2_2026-10-28.md §1).

Reads the ORIGINAL v1 record file (data/p22/p22_paired_2026-10-28.jsonl — never modified) in which `offer_off` / `offer_rep` are the RAW
walked offers (the v1 cap was applied only inside edge_c/real_c), re-derives exec price / status / fee / edge / realised net per
`predhft.p22_exec`, writes
    data/p22/p22_paired_2026-10-28.v2.jsonl        one record per (market, horizon), measurement_version = p22-v2, v1 values kept as v1_*
    data/p22/p22_v2_cells_2026-10-28.json          R1/R2/R3 cells v1 vs v2 with denominators and the list of changed cells
and prints the comparison. Records with a book but no raw offer: exec_status no_offer; without a book: no_book; a record whose raw
offer were missing while v1 had booked it would be 'unavailable' (count reported; 0 in this file).

Usage: uv run python scripts/p22_v2_recompute.py [--src data/p22/p22_paired_2026-10-28.jsonl]
"""
from __future__ import annotations
import argparse, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft.p22_exec import recompute_record, rule_cells, MEASUREMENT_VERSION, RULE_TEXT, HORIZONS


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--src", default="data/p22/p22_paired_2026-10-28.jsonl"); a = ap.parse_args()
    src = pathlib.Path(a.src); recs = [json.loads(l) for l in src.open(encoding="utf-8") if l.strip()]
    out = [recompute_record(recompute_record(r, "off"), "rep") for r in recs]
    unavailable = sum(1 for r in recs if r.get("book_src") and r.get("offer_off") is None and r.get("edge_c_off") is not None)
    dst = src.with_suffix(".v2.jsonl"); dst.write_text("".join(json.dumps(r) + "\n" for r in out), encoding="utf-8")
    v1 = rule_cells(recs, "off", 10, "v1"); v2 = rule_cells(recs, "off", 10, "v2")
    changed = []
    for h in HORIZONS:
        for k in RULE_TEXT:
            a1, a2 = v1[h][k], v2[h][k]
            if (a1["n"], a1["mean_edge_c"], a1["realized_net_c"]) != (a2["n"], a2["mean_edge_c"], a2["realized_net_c"]):
                changed.append({"horizon_s": h, "rule": k, "v1": {"n": a1["n"], "mean_edge_c": a1["mean_edge_c"], "realized_net_c": a1["realized_net_c"], "se_close": a1["se_close"]},
                                "v2": {"n": a2["n"], "mean_edge_c": a2["mean_edge_c"], "realized_net_c": a2["realized_net_c"], "se_close": a2["se_close"], "excluded_outside_modeled_range": a2["n_excluded_outside_modeled_range"]}})
    status = {}
    for r in out: status[r["exec_status_off"]] = status.get(r["exec_status_off"], 0) + 1
    summary = {"measurement_version": MEASUREMENT_VERSION, "source_v1_file": str(src), "v2_file": str(dst), "n_records": len(out), "exec_status_off_counts": status, "unrecoverable_marked_unavailable": unavailable,
               "cells_v1": v1, "cells_v2": v2, "changed_cells": changed, "rule_text": RULE_TEXT}
    cells_p = src.parent / "p22_v2_cells_2026-10-28.json"; cells_p.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"# P22 {MEASUREMENT_VERSION} recompute: {len(out)} records from {src} -> {dst}; exec_status_off = {status}; unavailable = {unavailable}\n")
    print("| horizon T-h (s) | rule | v1 n | v1 mean edge c | v1 realised c ± SE(close) | v2 n | excluded (raw > 0.98) | v2 mean edge c | v2 realised c ± SE(close) |")
    print("|---|---|---|---|---|---|---|---|---|")
    f = lambda v: "insufficient data" if v is None else f"{v:+.1f}"
    se = lambda x: "" if x["se_close"] is None else f" ± {x['se_close']:.1f}"
    for h in HORIZONS:
        for k in RULE_TEXT:
            a1, a2 = v1[h][k], v2[h][k]
            print(f"| {h} | {k} | {a1['n']} | {f(a1['mean_edge_c'])} | {f(a1['realized_net_c'])}{se(a1)} | {a2['n']} | {a2['n_excluded_outside_modeled_range']} | {f(a2['mean_edge_c'])} | {f(a2['realized_net_c'])}{se(a2) if a2['n_clusters_close'] > 1 else (' (SE suppressed: 1 close)' if a2['n'] else '')} |")
    print(f"\nchanged cells: {len(changed)} of {len(HORIZONS) * len(RULE_TEXT)}; unchanged: " + ", ".join(f"{k}@T-{h}" for h in HORIZONS for k in RULE_TEXT if not any(c['horizon_s'] == h and c['rule'] == k for c in changed)))


if __name__ == "__main__":
    main()
