"""v2c (P26) forward read: does Opus / Polymarket / p_model beat the Kalshi mid? Read-only. Outcomes from Kalshi public REST (cached)."""
import json, time, math, pathlib, urllib.request, statistics as S, collections, random
ROOT = pathlib.Path('.')
API = "https://api.elections.kalshi.com/trade-api/v2/markets/"
cache_p = pathlib.Path('reports/quant/outcomes_cache.json'); cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}
fc = [json.loads(l) for l in open('data/llm-shadow/forecasts.jsonl', encoding='utf-8')]
fc = [f for f in fc if f.get('prompt_variant') == 'v2c' and f.get('mid') is not None and f.get('close_ts', 0) < time.time() - 120]
for tk in sorted({f['ticker'] for f in fc}):
    if tk in cache: continue
    try:
        m = json.load(urllib.request.urlopen(urllib.request.Request(API + tk, headers={'User-Agent': 'x'}), timeout=20))['market']
        cache[tk] = {'yes': 1, 'no': 0}.get(m.get('result'))
    except Exception as e: cache[tk] = None
    time.sleep(0.4)
cache_p.write_text(json.dumps(cache))
# polymarket same-window book at decision time
poly = collections.defaultdict(list)
for l in open('data/ext/polymarket.jsonl', encoding='utf-8'):
    try: d = json.loads(l)
    except Exception: continue
    if d.get('up_mid') is not None: poly[d['close_ts']].append((d['t'], d['up_mid'], d.get('spread_c')))
def poly_at(close_ts, t):
    xs = [x for x in poly.get(close_ts, []) if x[0] <= t + 1]
    if not xs: return None
    x = max(xs); return x[1] if t - x[0] <= 30 else None
rows = []
for f in fc:
    y = cache.get(f['ticker'])
    if y is None: continue
    rows.append(dict(tk=f['ticker'], h=int(f['decision_secs_left']), y=y, mid=f['mid'], spr=(f['yes_ask'] - f['yes_bid']) if f.get('yes_ask') is not None else None,
                     llm=f.get('p_llm'), pm=f.get('p_model'), poly=poly_at(f['close_ts'], f.get('t_sent') or f.get('snapshot_t') or f['close_ts'] - f['decision_secs_left'])))
def brier(k, rs): return S.mean((r[k] - r['y']) ** 2 for r in rs)
def boot(rs, a, b, n=2000):
    random.seed(7); d = [(r[a] - r['y']) ** 2 - (r[b] - r['y']) ** 2 for r in rs]; m = S.mean(d)
    bs = sorted(S.mean(random.choices(d, k=len(d))) for _ in range(n)); return m, bs[int(.025 * n)], bs[int(.975 * n)]
out = {'n_forecasts': len(rows), 'n_closes': len({r['tk'] for r in rows})}
L = [r for r in rows if r['llm'] is not None]
out['brier'] = {'mid': round(brier('mid', L), 4), 'opus': round(brier('llm', L), 4), 'p_model': round(brier('pm', [r for r in L if r['pm'] is not None]), 4), 'n': len(L)}
out['opus_minus_mid'] = [round(x, 4) for x in boot(L, 'llm', 'mid')]
P = [r for r in L if r['poly'] is not None]
out['with_poly'] = {'n': len(P), 'mid': round(brier('mid', P), 4), 'poly': round(brier('poly', P), 4), 'opus': round(brier('llm', P), 4)}
for r in P: r['blend'] = 0.5 * r['mid'] + 0.5 * r['poly']
out['with_poly']['blend50'] = round(brier('blend', P), 4); out['poly_minus_mid'] = [round(x, 4) for x in boot(P, 'poly', 'mid')]; out['blend_minus_mid'] = [round(x, 4) for x in boot(P, 'blend', 'mid')]
# disagreement tests: does the outcome lean the challenger's way?
def lean(rs, k, thr):
    sel = [r for r in rs if abs(r[k] - r['mid']) >= thr]
    if not sel: return None
    # realized minus mid, signed toward challenger
    v = [(r['y'] - r['mid']) * (1 if r[k] > r['mid'] else -1) for r in sel]
    return {'n': len(sel), 'lean_pp': round(100 * S.mean(v), 1), 'se_pp': round(100 * S.stdev(v) / math.sqrt(len(v)), 1) if len(v) > 1 else None}
out['opus_disagree'] = {f'>={t}c': lean(L, 'llm', t / 100) for t in (2, 3, 5, 10)}
out['poly_disagree'] = {f'>={t}c': lean(P, 'poly', t / 100) for t in (3, 5, 10)}
out['pmodel_disagree'] = {f'>={t}c': lean([r for r in L if r['pm'] is not None], 'pm', t / 100) for t in (3, 5, 10)}
# by horizon
out['by_horizon'] = {h: {'n': len(g), 'mid': round(brier('mid', g), 4), 'opus': round(brier('llm', g), 4)} for h in (600, 300) for g in [[r for r in L if r['h'] == h]] if g}
# does opus add anything once poly is known? opus-vs-blend
out['opus_minus_blend'] = [round(x, 4) for x in boot(P, 'llm', 'blend')]
# opus anchoring: share within 2c of mid
out['opus_within_2c_of_mid_pct'] = round(100 * S.mean(abs(r['llm'] - r['mid']) <= .02 for r in L), 1)
out['opus_closer_to_poly_than_mid_when_poly_disagrees5c'] = (lambda s: round(100 * S.mean(abs(r['llm'] - r['poly']) < abs(r['llm'] - r['mid']) for r in s), 1) if s else None)([r for r in P if abs(r['poly'] - r['mid']) >= .05])
print(json.dumps(out, indent=1)); pathlib.Path('reports/quant/v2c_read_2026-10-31.json').write_text(json.dumps(out, indent=1))
