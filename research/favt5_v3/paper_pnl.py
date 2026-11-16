"""FavT5 v3 paper P&L book: the ONE place paper P&L is computed. Read-only on the shadow ledger.

Definitions (all paper; no order is ever sent):
  intent    the frozen rule decided to buy (side, qty, limit, $10 cap)
  executed  the intent would have filled against the displayed book at the MEASURED arrival time
            (qty filled can be partial; nothing is assumed beyond displayed size)
  realized  an executed trade whose market has officially settled:  payout - cost - fees
            payout = $1 x contracts if our side won, else $0; fees = exchange taker fee per fill
  open      executed, not yet settled (shown as cost at risk, no P&L counted)
Days are Pacific (PT) calendar days by settlement time; the UTC decision day is kept in trades.csv for the
owner's precommitted UTC-day review. Arms are never mixed: btc_v3 is the paper account (start balance from
paper_account.json); btc_fav_t5 (favourite control) and eth_challenge are shown on the same $150 for comparison only.

Budget eras: the per-window cap changed $10 -> $50 on 2026-11-15 (owner). The forward account below counts ONLY the
$50 era (decisions at/after the change); the $10 era is summarized separately under `historical` as a frozen reference
and is never mixed back in. This keeps a clean, like-for-like P&L for the strategy as it will actually be sized.

Every trade is recomputed from its fill row and the official result and checked against the runner's own
settle row; any mismatch, missing settle, duplicate decision or orphan fill is listed under `checks`.

python research/favt5_v3/paper_pnl.py   -> reports/favt5_v3/paper/{trades.csv,daily.csv,summary.json} + printout
"""
from __future__ import annotations
import csv, json, pathlib, time
from datetime import datetime, timedelta, timezone

W = pathlib.Path(__file__).resolve().parents[2]
LEDGER = W / "data/favt5-v3/shadow/fills.jsonl"; ACCT = W / "data/favt5-v3/shadow/paper_account.json"; OUT = W / "reports/favt5_v3/paper"
ARMS = {"btc_v3": "BTC v3 (paper account)", "btc_fav_t5": "Favourite control (comparison)", "eth_challenge": "ETH challenge (separate)"}
try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles"); datetime.now(PT)
except Exception:
    PT = timezone(timedelta(hours=-8))


def rows(path=LEDGER):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try: out.append(json.loads(line))
                except ValueError: pass                      # torn last line while the runner writes
    except OSError: pass
    return out


def budget_epoch(R):
    """Earliest `config` row that moved the budget off $10 -> the start of the $50 era (its row time). 0 if none."""
    cands = [r.get("t") for r in R if r.get("kind") == "config" and float(r.get("budget") or 10) > 10]
    return min(cands) if cands else 0.0


def pt_day(t): return datetime.fromtimestamp(t, PT).strftime("%Y-%m-%d")
def pt_time(t): return datetime.fromtimestamp(t, PT).strftime("%Y-%m-%d %H:%M:%S")
def utc_time(t): return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def book(path=LEDGER, acct=ACCT, now=None):
    now = now or time.time(); R = rows(path); a = json.loads(pathlib.Path(acct).read_text())
    start, t0 = float(a["start_balance_usd"]), float(a["start_ts"])
    dec, fills, sets, checks = {}, {}, {}, []
    for r in R:
        k = r.get("key"); kind = r.get("kind")
        if kind == "decision":
            if k in dec: checks.append(f"duplicate decision {k}")
            dec[k] = r
        elif kind == "fill" and r.get("replay") == "measured": fills[k] = r
        elif kind == "settle": sets[k] = r
    epoch = budget_epoch(R) or t0                     # the forward account begins at the $50 change (or launch, if never changed)
    trades, windows = [], {}
    for k, d in dec.items():
        arm = d.get("arm")
        if arm not in ARMS or d["decision_ts"] < epoch - 1: continue
        w = windows.setdefault((arm, pt_day(d["decision_ts"])), {"windows": 0, "intents": 0, "missed": 0})
        w["windows"] += 1; w["intents"] += bool(d.get("intent")); w["missed"] += str(d.get("skip", "")).startswith("missed")
        i = d.get("intent")
        if not i: continue
        f = fills.get(k)
        if f is None:
            if now > d["close"] + 300: checks.append(f"{k}: intent without a measured fill row")
            continue
        n = int(f.get("filled") or 0)
        if n == 0: continue                                 # intent not executed (no displayed offer at the limit)
        cost, fee = float(f["cost"]), float(f["fee"]); s = sets.get(k)
        t = {"arm": arm, "label": ARMS[arm], "ticker": d["ticker"], "decision_pt": pt_time(d["decision_ts"]), "decision_utc_day": utc_time(d["decision_ts"])[:10],
             "side": i["side"], "limit": i["limit"], "qty_intended": i["qty"], "qty_filled": n, "fill": f["status"], "avg_price": f.get("avg_px"),
             "cost": round(cost, 4), "fee": round(fee, 4), "cash_out": round(cost + fee, 4), "entry_band": d.get("entry_band"), "vol_regime": d.get("vol_regime"),
             "model_prob": i.get("modeled_probability"), "model_net": i.get("modeled_net_dollars"), "status": "open", "result": None, "payout": None, "realized_pnl": None,
             "settle_pt": None, "pt_day": None, "close_ts": d["close"]}
        if s:
            won = (s["result"] == "yes") == (i["side"] == "YES"); pay = float(n) if won else 0.0; pnl = round(pay - cost - fee, 4)
            mine = (s.get("replays") or {}).get("measured", {}).get("net_exchange_fees")
            if mine is None or abs(mine - pnl) > 0.0005: checks.append(f"{k}: recomputed {pnl:+.4f} vs runner {mine}")
            if bool(s.get("won")) != won: checks.append(f"{k}: win flag disagrees with official result")
            t.update(status="settled", result=s["result"].upper(), won=won, payout=pay, realized_pnl=pnl, settle_pt=pt_time(s["t"]), pt_day=pt_day(s["t"]))
        elif now > d["close"] + 1800: checks.append(f"{k}: closed {int((now - d['close']) / 60)} min ago, still unsettled")
        trades.append(t)
    trades.sort(key=lambda x: x["close_ts"])
    days = sorted({t0d for (_, t0d) in windows} | {t["pt_day"] for t in trades if t["pt_day"]})
    daily, summary = [], {}
    for arm in ARMS:
        cum = 0.0; peak = 0.0; mdd = 0.0; tr = [t for t in trades if t["arm"] == arm]
        for day in days:
            st = [t for t in tr if t["pt_day"] == day]; w = windows.get((arm, day), {"windows": 0, "intents": 0, "missed": 0})
            net = round(sum(t["realized_pnl"] for t in st), 4); cum = round(cum + net, 4); peak = max(peak, cum); mdd = max(mdd, peak - cum)
            daily.append({"arm": arm, "label": ARMS[arm], "pt_day": day, "windows": w["windows"], "intents": w["intents"], "missed_boundaries": w["missed"],
                          "executed": sum(1 for t in tr if t["decision_pt"][:10] == day), "settled": len(st), "wins": sum(1 for t in st if t["won"]),
                          "losses": sum(1 for t in st if not t["won"]), "fees": round(sum(t["fee"] for t in st), 4), "realized_pnl": net,
                          "cumulative_pnl": cum, "equity": round(start + cum, 2)})
        st = [t for t in tr if t["status"] == "settled"]; op = [t for t in tr if t["status"] == "open"]
        wins = [t["realized_pnl"] for t in st if t["won"]]; losses = [t["realized_pnl"] for t in st if not t["won"]]
        summary[arm] = {"label": ARMS[arm], "start_balance": start, "start_pt": pt_time(t0), "windows": sum(v["windows"] for (x, _), v in windows.items() if x == arm),
                        "intents": sum(v["intents"] for (x, _), v in windows.items() if x == arm), "executed": len(tr), "settled": len(st), "open": len(op),
                        "open_cost_at_risk": round(sum(t["cash_out"] for t in op), 2), "wins": len(wins), "losses": len(losses),
                        "fees": round(sum(t["fee"] for t in st), 4), "realized_pnl": round(sum(wins + losses), 4), "equity": round(start + sum(wins + losses), 2),
                        "avg_win": round(sum(wins) / len(wins), 2) if wins else None, "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
                        "max_drawdown": round(mdd, 2), "best": round(max(wins), 2) if wins else None, "worst": round(min(losses), 2) if losses else None}
    hist = {}
    for arm in ARMS:
        hs = [s for s in sets.values() if dec.get(s["key"], {}).get("arm") == arm and dec[s["key"]].get("decision_ts", 0) < epoch]
        hw = sum(1 for d in dec.values() if d.get("arm") == arm and d.get("decision_ts", 0) < epoch and d["decision_ts"] >= t0 - 1)
        hist[arm] = {"windows": hw, "settled": len(hs), "realized_pnl": round(sum(s["replays"].get("measured", {}).get("net_exchange_fees", 0) for s in hs), 4)}
        hist[arm]["filled"] = sum(1 for s in hs if (s["replays"].get("measured", {}).get("filled") or 0) > 0)
    hist["budget"] = 10.0; hist["changed_pt"] = pt_time(epoch) if epoch > t0 else (pt_time(t0) + " (no $50 change yet)")
    return {"generated_pt": pt_time(now), "start_balance": start, "start_pt": pt_time(epoch) if epoch > t0 else pt_time(t0), "epoch": epoch,
            "budget": 10.0 if epoch <= t0 else 50.0, "summary": summary, "daily": daily, "trades": trades, "historical": hist, "checks": checks}


def write(b):
    OUT.mkdir(parents=True, exist_ok=True)
    cols = ["arm", "label", "ticker", "decision_pt", "decision_utc_day", "side", "limit", "qty_intended", "qty_filled", "fill", "avg_price", "cost", "fee", "cash_out",
            "status", "result", "payout", "realized_pnl", "settle_pt", "pt_day", "entry_band", "vol_regime", "model_prob", "model_net"]
    with open(OUT / "trades.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(b["trades"])
    with open(OUT / "daily.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(b["daily"][0]) if b["daily"] else ["arm"]); w.writeheader(); w.writerows(b["daily"])
    (OUT / "summary.json").write_text(json.dumps({k: b[k] for k in ("generated_pt", "start_balance", "start_pt", "budget", "epoch", "summary", "historical", "checks")}, indent=1))


if __name__ == "__main__":
    b = book(); write(b)
    for arm, s in b["summary"].items():
        print(f"{s['label']}: start ${s['start_balance']:.2f} | realized {s['realized_pnl']:+.2f} | equity ${s['equity']:.2f} | executed {s['executed']} settled {s['settled']} ({s['wins']}W/{s['losses']}L) open {s['open']}")
    for d in b["daily"]:
        if d["arm"] == "btc_v3": print(" ", d["pt_day"], f"windows {d['windows']} executed {d['executed']} settled {d['settled']} net {d['realized_pnl']:+.2f} cum {d['cumulative_pnl']:+.2f} equity ${d['equity']:.2f}")
    print("checks:", b["checks"] or "all trades reconcile")
