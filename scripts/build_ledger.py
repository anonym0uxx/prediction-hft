"""Build the joined decision/fill ledger (docs/AUDIT_ADDENDUM_2026-10-28.md §8) -> data/ledger/decisions_fills.{jsonl,csv} + SCHEMA.json.
Read-only over data/ (paper); settlement results come from the cached market fetch (data/p22/audit_results_2026-10-28.json merged
with data/p22/markets_cache.json); no network.

Usage: uv run python scripts/build_ledger.py [--data data] [--out data/ledger] [--families s7p15-KXBTC15M,...]
"""
from __future__ import annotations
import argparse, collections, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft.ledger_join import build_rows, write_ledger, decomposition, SCHEMA


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--data", default="data"); ap.add_argument("--out", default="data/ledger"); ap.add_argument("--families", default=None)
    a = ap.parse_args(); data = pathlib.Path(a.data)
    markets = {}
    for name in ("markets_cache.json", "audit_results_2026-10-28.json"):
        p = data / "p22" / name
        if p.exists():
            try: markets.update(json.loads(p.read_text(encoding="utf-8")))
            except Exception: pass
    rows = build_rows(data, markets, a.families.split(",") if a.families else None)
    pj, pc = write_ledger(rows, pathlib.Path(a.out))
    kinds = collections.Counter(r["row_kind"] for r in rows); fams = collections.Counter(r["family"] for r in rows)
    print(f"rows={len(rows)} kinds={dict(kinds)} fields={len(SCHEMA)} -> {pj} , {pc}")
    print("by family:", dict(sorted(fams.items())))
    print("decomposition (settled fills):", json.dumps(decomposition(rows)))
    print("null-field counts:", {k: sum(1 for r in rows if r.get(k) is None) for k in SCHEMA if any(r.get(k) is None for r in rows)})


if __name__ == "__main__":
    main()
