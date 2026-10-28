"""Score the P19 quote path: join settled Kalshi outcomes and print calibration / Brier / entry-availability per asset x minutes-left.
Usage: cd D:/prediction-hft && uv run python scripts/score_quote_path.py [--no-fetch] [--quotes data/open_quotes/quotes.jsonl]
Writes reports/quote_path/score.md and score.json. Results are cached in data/ts/results.json (shared with ts_levers)."""
from __future__ import annotations
import argparse, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft.quote_path import load_rows, score, fmt_report
from predhft.ts_levers import fetch_results

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--quotes", default="data/open_quotes/quotes.jsonl"); ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--out", default="reports/quote_path"); a = ap.parse_args()
    rows = load_rows(pathlib.Path(a.quotes))
    tickers = sorted({r["ticker"] for r in rows if r["close_ts"] < time.time() - 150})
    res = fetch_results(tickers, pathlib.Path("data/ts/results.json"), fetch=not a.no_fetch)
    sc = score(rows, res); rep = fmt_report(sc)
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / "score.md").write_text(rep, encoding="utf-8")
    (out / "score.json").write_text(json.dumps({"buckets": {f"{a}:{b}": v for (a, b), v in sc["buckets"].items()}, "calibration": sc["calibration"], "first_available": {a: {str(k): v for k, v in d.items()} for a, d in sc["first_available"].items()}, "n_rows": sc["n_rows"], "n_joined": sc["n_joined"]}, indent=1), encoding="utf-8")
    print(rep)
