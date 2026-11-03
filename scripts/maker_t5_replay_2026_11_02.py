"""Maker-vs-taker replay of Opus v2c T-5 calls (read-only). For each settled close: side = sign(p_llm - mid).
Maker: rest 10 at our side's best bid from decision time to close-60s; fill if a later trade prints on our level
(touch = optimistic, ignores queue) or THROUGH it (through = conservative). No fee. Taker: cross at ask, Kalshi fee."""
import json, math, time, pathlib, urllib.request, statistics as S
from datetime import datetime
from predhft.fees import kalshi_taker_fee
API = "https://api.elections.kalshi.com/trade-api/v2"
oc_p = pathlib.Path('reports/quant/outcomes_cache.json'); oc = json.loads(oc_p.read_text()) if oc_p.exists() else {}
tr_p = pathlib.Path('reports/quant/trades_cache.json'); trc = json.loads(tr_p.read_text()) if tr_p.exists() else {}
def get(u):
    for i in range(4):
        try: return json.load(urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'x'}), timeout=30))
        except Exception: time.sleep(1 + 2 * i)
    return {}
fc = [json.loads(l) for l in open('data/llm-shadow/forecasts.jsonl', encoding='utf-8')]
fc = [f for f in fc if f.get('prompt_variant') == 'v2c' and f.get('decision_secs_left') == 300 and f.get('p_llm') is not None
      and f.get('yes_bid') is not None and f.get('yes_ask') is not None and f['close_ts'] < time.time() - 300]
rows = []
for f in fc:
    tk = f['ticker']
    if tk not in oc:
        m = get(f"{API}/markets/{tk}").get('market', {}); oc[tk] = {'yes': 1, 'no': 0}.get(m.get('result')); time.sleep(.35)
    if oc[tk] is None: continue
    if tk not in trc:
        out, cur = [], None
        while True:
            d = get(f"{API}/markets/trades?ticker={tk}&limit=1000" + (f"&cursor={cur}" if cur else "")); out += d.get('trades', []); cur = d.get('cursor')
            if not cur or not d.get('trades'): break
            time.sleep(.35)
        trc[tk] = [(datetime.fromisoformat(x['created_time'].replace('Z', '+00:00')).timestamp(), float(x['yes_price_dollars']), x['taker_side'], float(x['count_fp'])) for x in out]
        time.sleep(.35)
    t0 = f.get('t_recv') or f.get('t_sent') or f['snapshot_t']; tend = f['close_ts'] - 60
    later = [x for x in trc[tk] if t0 < x[0] <= tend]
    y = oc[tk]; side = 'yes' if f['p_llm'] > f['mid'] else 'no'; edge = abs(f['p_llm'] - f['mid'])
    if side == 'yes':
        b = f['yes_bid']; touch = any(p <= b and ts == 'no' for _, p, ts, _ in later); thru = any(p < b for _, p, ts, _ in later)
        mk_pnl = 10 * (y - b); tk_px = f['yes_ask']; tk_pnl = 10 * (y - tk_px) - kalshi_taker_fee(10, tk_px)
    else:
        b = 1 - f['yes_ask']; touch = any(p >= f['yes_ask'] and ts == 'yes' for _, p, ts, _ in later); thru = any(p > f['yes_ask'] for _, p, ts, _ in later)
        mk_pnl = 10 * ((1 - y) - b); tk_px = 1 - f['yes_bid']; tk_pnl = 10 * ((1 - y) - tk_px) - kalshi_taker_fee(10, tk_px)
    rows.append(dict(tk=tk, side=side, edge=edge, touch=touch, thru=thru, mk=mk_pnl, tk_pnl=tk_pnl, px=b))
oc_p.write_text(json.dumps(oc)); tr_p.write_text(json.dumps(trc))
def summ(rs, key, fillkey=None):
    sel = [r for r in rs if (r[fillkey] if fillkey else True)]
    if not sel: return {'n': 0}
    v = [r[key] for r in sel]; return {'n': len(sel), 'net_$': round(sum(v), 2), 'per_trade_$': round(S.mean(v), 3), 'se': round(S.stdev(v) / math.sqrt(len(v)), 3) if len(v) > 1 else None}
out = {'closes': len(rows)}
for thr in (0.0, 0.02, 0.05):
    rs = [r for r in rows if r['edge'] >= thr]
    out[f'edge>={int(thr*100)}c'] = {'n_calls': len(rs), 'taker_all': summ(rs, 'tk_pnl'), 'maker_fill_touch': summ(rs, 'mk', 'touch'), 'maker_fill_through': summ(rs, 'mk', 'thru'),
        'fill_rate_touch': round(sum(r['touch'] for r in rs) / max(1, len(rs)), 2), 'fill_rate_through': round(sum(r['thru'] for r in rs) / max(1, len(rs)), 2),
        'maker_unfilled_would_have_won_pct': round(100 * S.mean([(r['mk'] > 0) for r in rs if not r['touch']]), 1) if any(not r['touch'] for r in rs) else None,
        'maker_filled_won_pct': round(100 * S.mean([(r['mk'] > 0) for r in rs if r['touch']]), 1) if any(r['touch'] for r in rs) else None}
print(json.dumps(out, indent=1)); pathlib.Path('reports/quant/maker_t5_replay_2026-11-02.json').write_text(json.dumps(out, indent=1))
