"""Replay the J1-v2 lock logic over the recorded scoreboard (docs/WEATHER_J1V2_2026-10-28.md §8).

Inputs (all local files, no network):
  data/weather-j1/scoreboard.jsonl          every scored row (t, ticker, basis, cli_max/min as seen by the runner at t, book)
  data/weather-j1/replay/markets.json       Kalshi market objects incl. rules_primary, strike, result (fetched 2026-10-28)
  data/weather-j1/replay/asos_<CLI>.csv     IEM ASOS archive, hourly METARs with UTC timestamps (tmpf = T-group tenths when present)
For each row the METAR running extremes are recomputed from TIMESTAMPED observations inside the settlement window (local standard
time day) available `latency_s` before the row time; the CLI value is the one the runner had recorded at that row. The first lock per
ticker under the new logic is compared with the settled result, and with the old runner's first lock.
Usage: uv run python scripts/weather_replay.py [--latency 420] [--json out.json]
"""
from __future__ import annotations
import argparse, collections, csv, datetime as dt, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from predhft import weather_lock as WL
from zoneinfo import ZoneInfo

ROOT = pathlib.Path("data/weather-j1"); RP = ROOT / "replay"

def load_asos(cli: str) -> list[tuple[float, float]]:
    p = RP / f"asos_{cli}.csv"
    if not p.exists(): return []
    out = []
    for row in csv.reader(p.read_text(encoding="utf-8").splitlines()):
        if len(row) < 3 or row[0] == "station": continue
        try: ts = dt.datetime.strptime(row[1], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).timestamp(); v = float(row[2])
        except ValueError: continue
        out.append((ts, v))
    return sorted(out)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--latency", type=float, default=420.0); ap.add_argument("--json", default=None); a = ap.parse_args()
    sb = [json.loads(l) for l in (ROOT / "scoreboard.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    mk = json.loads((RP / "markets.json").read_text())
    asos: dict[str, list] = {}; rules: dict[str, WL.Rules | None] = {}
    new_first: dict[str, dict] = {}; old_first: dict[str, dict] = {}; new_rows = collections.Counter(); reasons = collections.Counter()
    for r in sorted(sb, key=lambda x: x["t"]):
        tk = r["ticker"]; m = mk.get(tk)
        if not m: continue
        if tk not in rules: rules[tk] = WL.resolve_rules(m)
        ru = rules[tk]
        if ru is None: continue
        if ru.cli not in asos: asos[ru.cli] = load_asos(ru.cli)
        obs = WL.filter_obs(asos[ru.cli], (ru.window_start, ru.window_end), as_of=r["t"] - a.latency)
        met = WL.running_extremes(obs)
        cli = {"max": r.get("cli_max"), "min": r.get("cli_min")}
        lh = dt.datetime.fromtimestamp(r["t"], ZoneInfo(ru.tz)); lh = lh.hour + lh.minute / 60
        ev = WL.evaluate(ru, cli, met, lh)
        new_rows[ev["basis"]] += 1
        if ev["locked"] and tk not in new_first: new_first[tk] = {"t": r["t"], "p": ev["p"], "lock_src": ev["lock_src"], "kind": ru.kind, "strike_type": ru.strike_type, "cli": ru.cli, "value": ev["lock_value"], "metar_n": met["n"]}
        if not ev["locked"] and tk not in new_first: reasons[(tk, ev["basis"])] += 1
        if str(r.get("basis", "")).startswith("locked_") and tk not in old_first: old_first[tk] = {"t": r["t"], "p": r["p_model"], "basis": r["basis"], "kind": ru.kind, "strike_type": ru.strike_type, "cli": ru.cli}
    results = {t: m["result"] for t, m in mk.items() if m.get("result") in ("yes", "no")}
    def table(first, label):
        cells = collections.defaultdict(lambda: [0, 0])
        for tk, fl in first.items():
            if tk not in results: continue
            k = (fl.get("lock_src") or fl.get("basis"), fl["kind"], fl["strike_type"]); cells[k][0] += 1
            if (fl["p"] >= 0.5) == (results[tk] == "yes"): cells[k][1] += 1
        lines = [f"{label}: locked tickers={len(first)} settled={sum(1 for t in first if t in results)} correct={sum(v[1] for v in cells.values())}/{sum(v[0] for v in cells.values())}", "  src | kind | strike_type | settled | correct"]
        for k, (n, c) in sorted(cells.items()): lines.append(f"  {k[0]} | {k[1]} | {k[2]} | {n} | {c}")
        return "\n".join(lines), {"|".join(k): {"n": n, "correct": c} for k, (n, c) in cells.items()}
    t_old, c_old = table(old_first, "OLD runner first locks (basis locked_*)"); t_new, c_new = table(new_first, "NEW logic first locks (replayed)")
    print(t_old); print(t_new)
    wrong_new = WL.wrong_locks(new_first, results); wrong_old = WL.wrong_locks(old_first, results)
    print("wrong NEW:", [(w["ticker"], w["result"], w["p"], w["lock_src"], w["value"]) for w in wrong_new])
    print("wrong OLD:", [(w["ticker"], w["result"], w["p"], w["basis"]) for w in wrong_old])
    dropped = sorted(set(old_first) - set(new_first)); added = sorted(set(new_first) - set(old_first))
    print(f"old locks NOT locked by new logic: {len(dropped)}"); 
    drop_detail = []
    for tk in dropped:
        rs = [b for (t_, b), n in reasons.items() if t_ == tk]; cnt = collections.Counter(rs)
        drop_detail.append({"ticker": tk, "old": old_first[tk]["basis"], "result": results.get(tk), "new_reasons": dict(cnt)})
        print(f"  {tk} old={old_first[tk]['basis']} result={results.get(tk)} new={dict(cnt)}")
    print(f"new locks not in old: {len(added)}", added[:30])
    lat = [new_first[t]["t"] - old_first[t]["t"] for t in new_first if t in old_first]
    print(f"first-lock time shift new-old (s): median={sorted(lat)[len(lat)//2] if lat else None} n={len(lat)}")
    print("new row basis counts:", dict(new_rows))
    # pending/held markets with a lock under either logic
    held = [t for t in set(new_first) | set(old_first) if mk[t]["status"] == "closed"]
    print("locked but unsettled (held) markets:", held)
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps({"old_cells": c_old, "new_cells": c_new, "wrong_new": wrong_new, "wrong_old": wrong_old, "dropped": drop_detail, "added": added, "new_first": new_first, "old_first": old_first, "new_row_basis": dict(new_rows), "held": held}, indent=1, default=str))

if __name__ == "__main__": main()
