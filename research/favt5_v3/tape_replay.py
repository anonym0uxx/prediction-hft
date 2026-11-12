"""Replay the v3 recorder's depth tapes through the SAME shadow runner code (decide / fills / settle arithmetic).
Research only. Used to exercise the shadow end to end on real recorded depth, and later for independent re-runs.

  python research/favt5_v3/tape_replay.py            -> every fully recorded window so far
Output: data/favt5-v3/shadow_replay/fills.jsonl (rewritten each run) and a printed summary.
Differences from the forward shadow, all stated in the rows: the clock offset is a fixed 0.04 s (the forward runner
measures it), the strike is taken from the settled market record, and the round trip is a fixed 0.30 s."""
from __future__ import annotations
import calendar, glob, gzip, heapq, json, pathlib, sys, time, urllib.request

W = pathlib.Path(__file__).resolve().parents[2]; sys.path.insert(0, str(W / "src")); sys.path.insert(0, str(W / "research/favt5_v3"))
from predhft import favt5_v3_shadow as sh
from predhft.favt5_v3_book import tape_rows
from offline import local_ticks
BOOK = W / "data/favt5-v3/book"; OFF = 0.04


def rows(paths: list[str], tk: str):
    last = 0.0
    for n, path in enumerate(paths):
        if n: yield last, tk, {"y": "gap"}
        for r in tape_rows(path):
            last = r["r"] + OFF if r.get("r") else last; yield last, tk, r


def main() -> None:
    sh.ROOT = W / "data/favt5-v3/shadow_replay"; sh.ROOT.mkdir(parents=True, exist_ok=True); (sh.ROOT / "fills.jsonl").write_text("")
    files: dict[str, list[str]] = {}
    for p in sorted(glob.glob(str(BOOK / "*" / "*.jsonl.gz")), key=lambda p: (pathlib.Path(p).name.split(".")[0], len(p), p)): files.setdefault(pathlib.Path(p).name.split(".")[0], []).append(p)
    closes = sorted({tk.split("-", 1)[1] for tk in files}); clock = [0.0]; sh.now = lambda: clock[0]; sh.RTT.append(0.30)
    span = [None, None]; meta = {}
    for tk in files:
        m = json.load(urllib.request.urlopen(urllib.request.Request(f"{sh.API}/markets/{tk}", headers={"User-Agent": "predhft-research"}), timeout=15))["market"]
        meta[tk] = m; c = calendar.timegm(time.strptime(m["close_time"], "%Y-%m-%dT%H:%M:%SZ")); span = [min(span[0] or c, c), max(span[1] or c, c)]; time.sleep(0.15)
    ticks = {a: local_ticks(a, span[0] - 3000, span[1] + 60) for a in ("BTC", "ETH")}
    for t in ticks.values():
        for x in t: x["received"] += OFF
    sh.index_ticks = lambda asset, tail_bytes=0: [x for x in ticks[asset] if clock[0] - 2000 <= x["event"] and x["received"] <= clock[0] + 1]
    settled = []
    for suffix in closes:
        r = sh.Runner(); r.sids = {1: "orderbook_delta", 2: "trade"}; r.keys = set(); r.open = {}; streams = []
        for tk in [t for t in files if t.endswith(suffix)]:
            m = meta[tk]; close = calendar.timegm(time.strptime(m["close_time"], "%Y-%m-%dT%H:%M:%SZ"))
            r.mk[tk] = {"series": tk.split("-")[0], "close": close, "strike": float(m["floor_strike"]), "strike_recv": close - 899.0}
            streams.append(rows(files[tk], tk))
        for t, tk, row in heapq.merge(*streams, key=lambda x: x[0]):
            clock[0] = t
            r.due(t)
            if row["y"] == "gap":
                for b in r.books.values(): b.valid = False
                continue
            sid = 2 if row["y"] == "trade" else 1
            r.frame({"type": row["y"], "sid": sid, "seq": None, "sending_ts_ms": row.get("s"), "msg": {**row["m"], "market_ticker": tk}}, t)
            if r.targets: r.fills(None, t, idle=True)
        for k, d in r.open.items():                             # settle with the same arithmetic as Runner.settle_loop
            m = meta[d["ticker"]]
            if m.get("result") not in ("yes", "no"): continue
            i = d["intent"]; won = (m["result"] == "yes") == (i["side"] == "YES"); res = {}
            for f in (json.loads(l) for l in (sh.ROOT / "fills.jsonl").open() if f'"{k}"' in l and '"kind": "fill"' in l):
                n = f.get("filled") or 0
                res[f["replay"]] = {"status": f["status"], "filled": n, "net_exchange_fees": round(n * won - f.get("cost", 0) - f.get("fee", 0), 4) if n else 0.0,
                                    "net_frozen_replay": round(i["qty"] * won - i["max_total_cost"], 2) if f.get("frozen_replay_filled") else 0.0}
            row = {"kind": "settle", "key": k, "arm": d["arm"], "ticker": d["ticker"], "side": i["side"], "result": m["result"], "won": won, "expiration_value": m.get("expiration_value"), "modeled_net_dollars": i.get("modeled_net_dollars"), "replays": res}
            sh._write(row); settled.append(row)
    dec = [json.loads(l) for l in (sh.ROOT / "fills.jsonl").open()]; dec = [d for d in dec if d.get("kind") == "decision"]
    by = {}
    for d in dec: by.setdefault(d["arm"], {}).setdefault("intent" if d.get("intent") else d.get("skip"), []).append(d["ticker"])
    print(json.dumps({a: {k: len(v) for k, v in x.items()} for a, x in by.items()}, indent=1)); print("settled intents:", len(settled))


if __name__ == "__main__":
    main()
