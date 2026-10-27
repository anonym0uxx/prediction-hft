"""Unit tests for the P15 sentiment leg (docs/SENTIMENT_DESIGN_2026-10-27.md §3, §4, §5). No network."""
import json, math, pathlib, sys, time
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from predhft.sentiment_lex import load_lexicon, score_text, detect_assets, is_sticky, is_question, normalize, tokenize, census
from predhft import social_features as sf
from predhft import reddit_feed as rf
from predhft import stocktwits_feed as stf

LEX = load_lexicon(pathlib.Path(__file__).resolve().parents[1] / "data/lexicon")
NOW = 1_800_000_000.0


# ---------------------------------------------------------------- lexicon / tokenizer (§1.2 census items)
def test_word_boundaries_kill_substring_hits():
    for t in ["bitcoin will no longer belong to the few", "shorter and longer chains", "reddit subreddit red-teaming referred hundred",
              "Shortwave Coffee accepts bitcoin", "whether together ethic", "Google searches for 'Bitcoin' have hit a 5-year low"]:
        sc = score_text(t, "", "Bitcoin", LEX)
        assert sc.n_val == 0 and sc.s_doc == 0.0, (t, sc.hits)


def test_whether_never_attributes_eth():
    assert detect_assets("whether coinbase hr together", "", "CryptoCurrency", LEX) == set()
    assert detect_assets("together we go", "", "Bitcoin", LEX) == {"BTC"}      # implicit sub only
    assert detect_assets("ETH and $SOL both up", "", "CryptoCurrency", LEX) == {"ETH", "SOL"}
    assert detect_assets("ether rally", "", "CryptoCurrency", LEX) == {"ETH"}
    assert detect_assets("no asset named", "", "CryptoCurrency", LEX) == set()


def test_six_red_candles_is_mildly_bearish():
    sc = score_text("Me: I'm here for the long term. Also me after 6 red candles: 👀", "", "Bitcoin", LEX)
    assert -0.4 <= sc.s_doc <= -0.2 and any(h.startswith("red candles") for h in sc.hits)
    assert not any(h.startswith("long") for h in sc.hits)      # 'long term' stop-listed


def test_negation_flips_within_three_tokens():
    sc = score_text("Bitcoin Will Never Exceed its ATH by More than 10%, Bitcoin's Bottom Will Be Below 40K", "", "Bitcoin", LEX)
    assert sc.s_doc < 0 and sc.hits[0].endswith("*")
    sc2 = score_text("bitcoin will no longer see substantial upside", "", "Bitcoin", LEX)
    assert sc2.s_doc < 0
    assert score_text("not a crash", "", "Bitcoin", LEX).s_doc > 0
    assert score_text("a huge rally and the dip was bought", "", "Bitcoin", LEX).hits[0] == "rally+2.34"   # modifier x1.3


def test_sarcasm_and_question_damping():
    assert score_text("this is a bull market /s", "", "Bitcoin", LEX).s_doc < 0
    q = score_text("Is this a bull market?", "", "Bitcoin", LEX); s = score_text("This is a bull market", "", "Bitcoin", LEX)
    assert q.is_question and not s.is_question and abs(q.s_doc - 0.3 * s.s_doc) < 1e-6
    assert is_question("What makes a bitcoin transaction trustworthy")


def test_entity_stoplist_and_context_constraints():
    assert score_text("Is Pump still a launchpad? Pump dot fun's GitHub activity", "pump.fun and pumpswap", "solana", LEX).n_val == 0
    assert score_text("earned 500 Moons today", "", "CryptoCurrency", LEX).n_val == 0
    assert score_text("sol is mooning today", "", "Bitcoin", LEX).n_val == 1
    assert score_text("liquidated my savings into bitcoin", "", "Bitcoin", LEX).n_val == 0
    assert score_text("$100m longs liquidated", "", "Bitcoin", LEX).hits == ["liquidated-1.50"]
    assert score_text("green candle", "", "Bitcoin", LEX).n_val == 1 and score_text("green energy", "", "Bitcoin", LEX).n_val == 0
    assert score_text("the last bear market was 2022", "", "Bitcoin", LEX).n_val == 0
    assert score_text("buy the dip", "", "Bitcoin", LEX).hits == ["buy the dip+1.50"]        # phrase wins over 'dip'


def test_emoji_and_clip():
    sc = score_text("🚀🚀🚀🚀🚀🚀🚀🚀 moon", "", "Bitcoin", LEX)
    assert 0 < sc.s_doc <= 1.0 and sc.n_val == 9
    assert score_text("👀", "", "Bitcoin", LEX).n_val == 0   # attention-only emoji


def test_sticky_detection():
    assert is_sticky("Daily Discussion, October 27, 2026", LEX)
    assert is_sticky("Daily Crypto Discussion - October 27, 2026 (GMT+0)", LEX)
    assert is_sticky("Solana Ecosystem News - October 18-2026", LEX)
    assert is_sticky("Mentor Monday, October 26, 2026: Ask all your bitcoin questions!", LEX)
    assert not is_sticky("Why BTC is going down", LEX)


def test_census_exact_word_share():
    posts = [{"title": "bitcoin rally, no longer bearish; shorter chains", "body": "whether reddit", "url": "https://www.reddit.com/r/Bitcoin/comments/abc/x/"},
             {"title": "Daily Discussion, October 1, 2026", "body": "", "url": "https://www.reddit.com/r/Bitcoin/comments/abd/x/"}]
    c = census(posts, LEX)
    assert c["exact_word_share"] == 1.0 and c["eth_without_alias"] == 0 and c["sticky_detected"] == 1 and c["hits"] == 2


# ---------------------------------------------------------------- store / features
def _doc(i, src="reddit", sub="Bitcoin", assets=("BTC",), created=NOW - 600, fetched=None, s=0.5, n_val=1, author=None, title=None, **kw):
    d = {"src": src, "id": f"{src}_{i}", "sub": sub, "author": author or f"a{i}", "created_ts": created, "fetched_ts": fetched if fetched is not None else created + 30,
         "title": title or f"unique title {i}".replace("_", ""), "body": "", "url": None, "label": None, "assets": list(assets), "is_sticky": False, "is_question": False,
         "is_news": False, "pos": max(s, 0), "neg": max(-s, 0), "n_val": n_val, "s_doc": s, "hits": [], "dup_of": None, "lex_version": "t"}
    d.update(kw); return d


def test_ingest_dedupes_by_id_and_near_title(tmp_path):
    store = tmp_path / "s.jsonl"
    a = _doc(1, title="Bitcoin hits new all time high today"); b = _doc(2, sub="CryptoCurrency", title="Bitcoin hits new all time high today!")
    assert sf.ingest([a, b], store) == 2
    docs = sf.read_store(store)
    assert docs[0]["dup_of"] is None and docs[1]["dup_of"] == "reddit_1"
    assert sf.ingest([a, _doc(3)], store) == 1 and len(sf.read_store(store)) == 3


def test_load_window_is_causal(tmp_path):
    store = tmp_path / "s.jsonl"
    sf.ingest([_doc(1, created=NOW - 100, fetched=NOW + 50), _doc(2, created=NOW - 100, fetched=NOW - 10), _doc(3, created=NOW - 5 * 3600)], store)
    ids = {d["id"] for d in sf.load_window(store, NOW)}
    assert ids == {"reddit_2"}


def test_features_definitions():
    docs = [_doc(i, s=0.4, author=f"u{i}", created=NOW - 60 * i) for i in range(1, 5)]            # 4 authors, 60m
    docs += [_doc(9, s=-0.9, author="u1", created=NOW - 120)]                                        # same author -> author-mean
    docs += [_doc(10, s=0.9, is_question=True), _doc(11, s=0.9, is_sticky=True), _doc(12, s=0.9, n_val=0), _doc(13, s=0.9, dup_of="reddit_1")]
    docs += [_doc(20, created=NOW - 2 * 3600, s=-0.5, author="u20")]
    f = sf.features("BTC", NOW, docs, None, fetch_state={"reddit": {"Bitcoin": NOW - 100}})
    assert f["rd"]["posts_60m"] == 7 and f["rd"]["posts_180m"] == 8           # sticky + dup excluded from counts; question & n_val=0 counted
    assert f["rd"]["authors_60m"] == 6
    u1 = (0.4 - 0.9) / 2; exp = (u1 + 0.4 * 3) / 4
    assert abs(f["rd"]["dir_60m"] - exp) < 1e-3 and f["rd"]["n_dir_180m"] == 5
    assert f["rd"]["disag_180m"] == pytest.approx(2 / 6, abs=1e-4)             # per doc: pos u1..u4 (4), neg doc9, doc20 (2)
    assert f["rd"]["dir_ewma_120"] is not None and f["rd"]["stale_s"] == 100 and f["rd"]["subs_fresh"] == ["Bitcoin"]
    assert f["rd"]["z_posts_60m"] is None and f["st"]["msgs_60m"] == 0 and f["st"]["bull_ratio_60m"] is None
    f2 = sf.features("BTC", NOW, docs[:2], None, fetch_state={})
    assert f2["rd"]["dir_60m"] is None and f2["rd"]["dir_ewma_120"] is None   # < 3 authors -> null


def test_stocktwits_block_and_news_flag():
    docs = [_doc(i, src="stocktwits", sub="BTC.X", label=("Bullish" if i % 3 else "Bearish"), s=0.2, author=f"s{i}", created=NOW - 30 * i) for i in range(1, 9)]
    docs.append(_doc(99, src="stocktwits", sub="BTC.X", label=None, s=0.0, n_val=0, is_news=True, created=NOW - 100))
    f = sf.features("BTC", NOW, docs, None, fetch_state={"stocktwits": {"BTC.X": NOW - 7}})
    st = f["st"]
    assert st["msgs_60m"] == 9 and st["msgs_15m"] == 9 and st["users_60m"] == 9
    assert st["lab_bull_60m"] == 6 and st["lab_bear_60m"] == 2 and st["bull_ratio_60m"] == pytest.approx(0.75)
    assert st["disag_60m"] == pytest.approx(2 / 8) and st["stale_s"] == 7 and f["news_flag"] is True
    assert st["dir_lex_60m"] == pytest.approx(0.2)


def test_baselines_and_z(tmp_path):
    store = tmp_path / "s.jsonl"; docs = []
    for day in range(7):
        for h in range(24):
            base = NOW - (day * 24 + h) * 3600
            k = 2 if h % 2 else 4
            docs += [_doc(f"{day}_{h}_{j}", created=base - j * 60, s=0.1) for j in range(k)]
    sf.ingest(docs, store)
    b = sf.rebuild_baselines(store, NOW, out=tmp_path / "b.json", priors_out=tmp_path / "p.json")
    assert any(k.startswith("reddit|BTC|posts_60m|h") for k in b["stats"])
    pri = json.loads((tmp_path / "p.json").read_text())["priors"]["reddit:Bitcoin"]
    assert pri["n"] > 400 and pri["s_doc_mean"] == pytest.approx(0.1, abs=1e-3)
    win = sf.load_window(store, NOW); win = [dict(d, fetched_ts=0) for d in win]
    f = sf.features("BTC", NOW, win, b, fetch_state={})
    assert f["rd"]["z_posts_60m"] is not None
    big = win + [_doc(f"x{j}", created=NOW - 10 - j, fetched=NOW - 1) for j in range(40)]
    assert sf.features("BTC", NOW, big, b, fetch_state={})["rd"]["z_posts_60m"] > 2


def test_emit_and_latest(tmp_path, monkeypatch):
    root = tmp_path; store = root / "social_posts.jsonl"
    sf.ingest([_doc(i, s=0.3, author=f"u{i}") for i in range(5)], store)
    monkeypatch.setattr(sf, "FETCH_STATE", root / "fs.json"); sf.mark_fetched("reddit", "Bitcoin", NOW - 10, root / "fs.json")
    row = sf.emit(NOW, store, root)
    assert set(row["by_asset"]) == {"BTC", "ETH", "SOL"} and row["lex_version"] == LEX.version
    monkeypatch.setattr(time, "time", lambda: NOW + 100)
    l = sf.latest("BTC", root)
    assert l and l["sent_age_s"] == 100 and l["lex_version"] == LEX.version and l["rd_dir_ewma_120"] is not None and set(l) >= {"rd_z_posts_60m", "st_bull_ratio_60m", "st_users_60m", "sent_as_of"}
    assert sf.latest("BTC", root, max_age=50) is None and sf.latest("XRP", root) is None


# ---------------------------------------------------------------- reddit feed
def test_reddit_to_doc_and_allowlist():
    p = {"title": "Why BTC is going down", "author": "bob", "created": "2026-10-27T18:00:00+00:00", "url": "https://www.reddit.com/r/Bitcoin/comments/1oh2abc/why_btc/", "body": "sell everything, bear"}
    d = rf.to_doc(p, NOW, LEX)
    assert d["id"] == "t3_1oh2abc" and d["sub"] == "Bitcoin" and d["author"] == "u/bob" and d["created_ts"] == 1793124000.0 and d["fetched_ts"] == NOW
    assert d["assets"] == ["BTC"] and d["s_doc"] < 0 and d["n_val"] >= 2 and d["label"] is None and d["lex_version"] == LEX.version
    off = dict(p, url="https://www.reddit.com/r/wallstreetbets/comments/zzz/x/")
    assert rf.to_docs([p, off], NOW, "search", LEX)[0]["id"] == "t3_1oh2abc" and len(rf.to_docs([p, off], NOW, "search", LEX)) == 1
    assert len(rf.to_docs([p, off], NOW, "sub", LEX)) == 2
    assert rf.to_doc({"title": "x", "url": "https://example.com"}, NOW, LEX) is None


def test_reddit_schedule_multi_feed_rotation():
    clock = {"t": 0.0}; g = rf.schedule(lambda: clock["t"])
    seq = [next(g) for _ in range(len(rf.ROTATION) * 2)]
    assert ("sub", "ethereum") not in seq                  # ethereum is covered by the multi-sub feed
    assert seq[:len(rf.ROTATION)] == rf.ROTATION and seq[len(rf.ROTATION):] == rf.ROTATION
    assert sum(1 for k, t in rf.ROTATION if k == "sub" and "+" in t) == 4 and sum(1 for k, _ in rf.ROTATION if k == "search") == 1
    assert all(s in rf.MULTI for s in ("Bitcoin", "ethereum", "solana", "CryptoCurrency"))


def test_reddit_fetch_error_row_has_no_by_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "LOG", tmp_path / "reddit.jsonl"); monkeypatch.setattr(rf, "ROOT", tmp_path)
    monkeypatch.setattr(rf.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("boom")))
    posts, t0 = rf.fetch("sub", "Bitcoin")
    rows = [json.loads(l) for l in (tmp_path / "reddit.jsonl").read_text().splitlines()]
    assert posts == [] and rows[0]["ok"] is False and "by_asset" not in rows[0] and rows[0]["kind"] == "sub" and "TimeoutError" in rows[0]["err"]


def test_reddit_fetch_passes_utf8_env_and_limit_100(monkeypatch):
    seen = {}
    class R: returncode = 0; stderr = ""; stdout = "[]"
    def run(args, **kw): seen["args"] = args; seen["kw"] = kw; return R()
    monkeypatch.setattr(rf.subprocess, "run", run)
    rf.fetch("search", "bitcoin OR ethereum OR solana")
    assert seen["kw"]["env"]["PYTHONIOENCODING"] == "utf-8" and seen["kw"]["env"]["PYTHONUTF8"] == "1" and seen["kw"]["encoding"] == "utf-8"
    assert seen["args"][2:] == ["--json", "search", "bitcoin OR ethereum OR solana", "--sort", "new", "--limit", "100"]


# ---------------------------------------------------------------- stocktwits
def test_stocktwits_normalize_message():
    m = {"id": 667479550, "body": "$BTC.X Options keeping this pinned until we know what direction the FED moves. crash incoming", "created_at": "2026-10-27T19:41:47Z",
         "entities": {"sentiment": {"basic": "Bearish"}}, "user": {"id": 123, "username": "x"}, "symbols": [{"symbol": "BTC.X"}, {"symbol": "ETH.X"}]}
    d = stf.normalize_message(m, NOW, "BTC.X", LEX)
    assert d["id"] == "st_667479550" and d["src"] == "stocktwits" and d["sub"] == "BTC.X" and d["author"] == "st_123" and d["label"] == "Bearish"
    assert d["created_ts"] == 1793130107.0 and d["fetched_ts"] == NOW and d["assets"] == ["BTC", "ETH"] and d["s_doc"] < 0 and d["is_news"] and d["body"] == ""
    d2 = stf.normalize_message({"id": 1, "body": "hi", "created_at": "2026-10-27T19:41:47Z", "entities": {}, "user": {}, "symbols": []}, NOW, "SOL.X", LEX)
    assert d2["label"] is None and d2["assets"] == ["SOL"]


def test_stocktwits_poll_cursor_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(stf, "ROOT", tmp_path); monkeypatch.setattr(stf, "LOG", tmp_path / "st.jsonl")
    monkeypatch.setattr(sf, "STORE", tmp_path / "posts.jsonl"); monkeypatch.setattr(sf, "FETCH_STATE", tmp_path / "fs.json")
    calls = []
    def fake(symbol, since_id=None, max_id=None, timeout=15.0):
        calls.append((since_id, max_id)); stf.LAST["cursor"] = {"more": True, "since": 200, "max": 100}
        return [{"id": i, "body": f"$BTC.X msg {i}", "created_at": "2026-10-27T19:41:47Z", "entities": {}, "user": {"id": i}, "symbols": [{"symbol": "BTC.X"}]} for i in (200, 150, 100)]
    monkeypatch.setattr(stf, "fetch_stream", fake)
    state = {}
    assert stf.poll("BTC.X", state, backfill_pages=2, lex=LEX) == 3 and calls[-1] == (None, None)
    stf.poll("BTC.X", state, backfill_pages=2, lex=LEX); assert calls[-1] == (None, 100)         # back-page with ?max=
    stf.poll("BTC.X", state, backfill_pages=2, lex=LEX); assert calls[-1] == (200, None)         # then ?since=
    assert sf.load_fetch_state(tmp_path / "fs.json")["stocktwits"]["BTC.X"] > 0
    monkeypatch.setattr(stf, "fetch_stream", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert stf.poll("BTC.X", state, lex=LEX) == 0
    rows = [json.loads(l) for l in (tmp_path / "st.jsonl").read_text().splitlines()]
    assert rows[-1]["ok"] is False and "OSError" in rows[-1]["err"]


# ---------------------------------------------------------------- eval script
def test_eval_feature_join_is_causal_and_brier_skill():
    import eval_p15_sentiment as ev
    feats = [{"as_of": NOW - 100, "lex_version": "t", "by_asset": {"BTC": {"rd": {"dir_ewma_120": 0.5}, "st": {}}}},
             {"as_of": NOW + 100, "lex_version": "t", "by_asset": {"BTC": {"rd": {"dir_ewma_120": -0.5}, "st": {}}}}]
    f, age, lv = ev.feature_at(feats, NOW, "BTC")
    assert f["rd_dir_ewma_120"] == 0.5 and age == 100
    assert ev.feature_at(feats, NOW + 2000, "BTC")[0] == {}                     # stale (> 900 s)
    assert ev.feature_at([dict(feats[0], lex_version=None)], NOW, "BTC")[0] == {}  # unversioned refused
    import random; rng = random.Random(1); rows = []
    for i in range(400):
        x = rng.uniform(-1, 1); p_mid = 0.5; p_true = 1 / (1 + math.exp(-2 * x)); y = int(rng.random() < p_true)
        rows.append(ev.Row("BTC", NOW + 3600 * i, NOW + 3600 * i - 180, p_mid, y, {"f": x}, 10.0, "t", i))
    r = ev.brier_skill(rows, "f")
    assert r["n_oos"] == 352 and r["delta_brier"] > 0 and r["pass"] and r["sign_stable"]
    noise = [ev.Row("BTC", NOW + 3600 * i, NOW + 3600 * i - 180, 0.5, rng.randint(0, 1), {"f": rng.uniform(-1, 1)}, 10.0, "t", i) for i in range(400)]
    assert not ev.brier_skill(noise, "f")["pass"]
    assert ev.cluster_se([1.0, 1.0, 1.0, 1.0], [1, 1, 2, 2]) == pytest.approx(0.0)


def test_eval_contested_filter_and_spearman():
    import eval_p15_sentiment as ev
    feats = [{"as_of": NOW + 60 * i - 1, "lex_version": "t", "by_asset": {"BTC": {"rd": {}, "st": {"dir_ewma_30": ((0.95 if i % 4 == 1 else 0.9) if i % 2 else -0.05)}}}} for i in range(60)]
    trades = [{"t": NOW + 60 * i, "side": "yes", "fav_at_decision": 0.8, "result": ("no" if i % 2 else "yes")} for i in range(60)]
    r = ev.contested_filter("BTC", trades, feats, "st_dir_ewma_30")
    assert r["n_contested"] == 60 and r["hit_all"] == 0.5 and r["hit_filtered"] == 0.5 and r["n_vetoed"] == 0   # positive x agrees with 'yes'
    trades2 = [dict(t, side="no", result=("yes" if i % 2 else "no")) for i, t in enumerate(trades)]
    r2 = ev.contested_filter("BTC", trades2, feats, "st_dir_ewma_30")
    assert r2["n_vetoed"] > 0 and r2["hit_filtered"] > r2["hit_all"] and r2["sign_disagree_frac"] == 0.5
    assert ev.spearman([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0) and ev.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    rows = [ev.Row("BTC", NOW + 3600 * i, NOW + 3600 * i - 180, 0.5, 1, {"rd_z_posts_60m": float(i % 7)}, 10.0, "t", i, spot=100.0 * (1 + 0.001 * (i % 7) * (-1) ** i), sigma1=0.001) for i in range(40)]
    supply = [{"close_ts": NOW + 3600 * i, "traded": bool(i % 2), "pinned_at_T55": True} for i in range(40)]
    out = ev.attention_regime("BTC", [], rows, supply, "rd_z_posts_60m")
    assert out["y1_realised_z"]["n"] == 39 and out["y2_p13_supply"]["n"] == 40 and out["y3_pinned_frac"]["n"] == 40 and len(out["y1_realised_z"]["quintile_means"]) == 5
