# Crypto social-sentiment feed: audit and redesign (P15 sentiment leg)

Date: 2026-10-27 (PT). Scope: `src/predhft/reddit_feed.py` (one-shot lexicon feed) and the sentiment slots in `ctx`
(`reddit_mentions`, `reddit_sent`) attached by `s7_live.py` to S7 book snapshots and trades. Status under
`docs/PREREG.md` P15: scored-only; nothing here sizes a trade until a pre-registered test passes.

Everything measured below was measured today on this machine: 5 anonymous Atom requests (r/Bitcoin, r/ethereum,
r/solana, r/CryptoCurrency at `--sort new --limit 25`, r/Bitcoin `--limit 100`), 1 site-wide `search bitcoin`, the 27 rows
of `data/context/reddit.jsonl`, the `ctx` blocks in `data/s7p1*/books.jsonl`, and the public Stocktwits / CoinGecko /
alternative.me endpoints. Where I could not measure something I say so.

---

## 1. Audit of the current lexicon and aggregation

### 1.1 What the code does (reddit_feed.py)
- Round-robin over 4 subs, one Atom request / 75 s, 25 newest posts per request; `score()` concatenates title + body
  (body is up to 4 000 chars, not 300 — the skill script truncates at `[:4000]`), lower-cases, and counts
  `str.count(w)` for 14 BULL and 15 BEAR substrings; `sent = (bull-bear)/(bull+bear)`.
- Asset attribution: a post "mentions" an asset if any of `bitcoin|btc`, `ethereum|"eth "|"ether "`, `solana|"sol "` is a
  substring; the post's whole bull/bear count is then added to every asset it "mentions".
- Aggregation: per asset, sum over subs refreshed in the last 1 200 s; written to `reddit.latest.json`; `s7_live.py`
  copies `mentions_recent` and `sent` into `ctx` with `max_age=1500 s`.

### 1.2 Measured false positives (100 posts, 4 subs, 2026-10-27 ~19:20Z)
Term-hit census across all 100 posts (substring hits vs. hits that are the actual word):

| term | hits | exact-word hits | what it actually matched |
|---|---|---|---|
| `red` (BEAR) | **97** | **2** | `subreddit` ×22, `old.reddit.com` ×11, `preview.redd.it`, `shared`, `reduction`, `appeared`, `hundred`, `credit`, `red-teaming`, `referred`, `required`, `fired` |
| `pump` (BULL) | 14 | 11 | 11 of the 14 are **Pump.fun the product** ("Pump dot fun's GitHub activity", "is Pump still a launchpad", "pump.fun, pumpswap") — not price talk |
| `long` (BULL) | 13 | 5 | `longer` ×6 ("no longer in effect", "bitcoin will no longer see substantial…" — a *bearish* thesis), `belongs` |
| `ath` (BULL) | 12 | 3 | `rather`, `math`, `mathematics`, `path`, `underneath` |
| `moon` (BULL) | 9 | 9 | all 9 are **r/CryptoCurrency "Moons"** (the sub's reward token) in the sticky "Daily Crypto Discussion" rules text |
| `scam` (BEAR) | 7 | 1 | "beware of scammers" in the same sticky rules text |
| `liquidat` (BEAR) | 9 | — | a historical back-test post about LTV loans ("Even at 20% LTV, some got liquidated") and Chainlink marketing copy |
| `short` (BEAR) | 4 | 0 | `Shortwave Coffee` ×3, `shorter` |
| `sell` (BEAR) | 4 | 1 | "without **selling** my tokens", "instead of selling" (i.e. *not* selling) |
| `bull` (BULL) | 1 | 0 | `bullseye` |
| `bear` (BEAR) | 1 | 0 | `bearish` inside "when gurus turn bearish… best time to accumulate" (contrarian-bullish post) |

Totals: BULL hits 52, BEAR hits 135 → `sent = -0.44` for a sample in which no post reads as bearish on price. 72 % of all
BEAR hits are the substring `red`. The live r/ethereum reading in `reddit.jsonl` (`bull 1, bear 39, sent -0.95`) is
entirely `red` ⊂ `subreddit`/`old.reddit.com` from 11 copies of the daily-discussion sticky. **The `reddit_sent` currently
stored in `ctx` has no sentiment content; it is a count of the word "reddit".**

Asset attribution is also wrong: `"ether "` matches `whether`/`together` (12 of the 22 "ETH mention" assignments in the
sample came from `whether …` / `together …`), so r/Bitcoin posts about Coinbase HR and r/CryptoCurrency sticky rules
are counted as ETH mentions. `"sol "` is fine in this sample but will match `aerosol `, `parasol `; `btc` matches
`btcstack`; `bitcoin` matches `bitcoinmagazine`, `bitcoiners` (harmless).

### 1.3 Semantic failures the lexicon cannot see (real titles)
- Sarcasm/humour: *"Me: I'm here for the long term. Also me after 6 red candles: 👀"* → +long, −red (sentiment ≈ 0, true tone: anxious joke).
- Negation/contrarian: *"When Bitcoin looks terrible, that's usually when the best accumulation opportunities appear"* → scored −bear −red +accumulate.
- Bearish thesis scored bullish: *"Bitcoin Will Never Exceed its ATH by More than 10%, Bitcoin's Bottom Will Be Below 40K"* → +ath ×2, +long ×4 (from `longer`), net **bullish**.
- Questions and how-tos (the majority of r/Bitcoin `new`): "What makes a bitcoin transaction trustworthy?", "Paid in bitcoin: can your employer follow what you spend?" — no directional content; they dilute or randomly tilt the ratio.
- Attention vs. direction confusion: *"Google searches for 'Bitcoin' have hit a 5-year low"* (posted twice within 4 min in the site-wide search) is an attention datum, not a bull/bear word hit.

### 1.4 Aggregation / timing defects (measured)
1. **The "60-min" window is not applied to sentiment.** `posts_60m` counts recent posts, but `bull/bear/sent/mentions` are computed over all 25 fetched posts regardless of age. Measured spans of the 25 newest posts: r/Bitcoin 23.7 h, r/solana 78.9 h, r/CryptoCurrency 71.5 h, **r/ethereum 263.8 h (11 days)**. `ctx.reddit_sent` for ETH at 19:19Z was computed from posts up to 11 days old.
2. **Posting rates are far below the horizon.** Measured from the newest-25 spans: r/Bitcoin 1.08 posts/h (0.96/h over the 100-post, 104-h window), r/solana 0.32/h, r/CryptoCurrency 0.38/h (Atom `new` appears to expose only mod-approved posts), r/ethereum 0.10/h. Posts in the last 60 min at sample time: 2, 0, 0, 0. A 15-min or 60-min directional reading from sub listings alone is structurally undefined most of the time.
3. **No de-duplication across rotations.** Each sub is re-fetched every 5 min and the same 25 posts are re-scored; rows 19:03→19:19Z show four identical cycles. Stickies ("Daily General Discussion", "Daily Crypto Discussion") recur daily with identical rule text and dominate r/ethereum and r/CryptoCurrency counts. Cross-posts appear twice (the "6 red candles" meme in r/Bitcoin and r/CryptoCurrency within 10 min).
4. **No baseline.** r/Bitcoin is structurally bullish (adoption/holding culture), r/CryptoCurrency is meta/skeptical, r/solana is ecosystem-news (near-zero price talk). Raw ratios mix sub priors with the state of the world; only deviations from each sub's own trailing distribution can carry information.
5. **Timestamps.** The script's `created` field is Atom `<updated>` (Reddit's Atom carries no `<published>`; it tracks submission time in practice but is not guaranteed on edit). Nothing records `fetched_ts`; the stored row's `t` is the fetch time of the *last* sub only, and `s7_live.py` accepts `reddit.latest.json` up to 1 500 s old. For a causal evaluation every feature must be stamped with the fetch time it became available, not the post time.
6. **Failure handling.** 12 of the first 12 rows were `ok:false` (429/JSONDecodeError while another process shared the IP throttle) yet a `by_asset` block of zeros was still written and would have been served as "0 mentions, sent=None". Today's audit sampling (same IP) also collided with the running loop; both got 429-and-sleep.
7. **Anonymous feed limits.** No scores, no comment counts, no comments, no author karma, no flair, no `over_18`/`removed` flags. Posts are the only unit, and posts are ~1/h.
8. **Unused capacity.** The Atom endpoint honours `?limit=100` (verified: 100 posts in one request). The feed fetches 25.

### 1.5 Verdict
The current `reddit_sent` is noise with a strong negative bias; `reddit_mentions` is a count of posts of any age that contain `bitcoin`/`whether`/… It must not be interpreted in any P15 test. Rows written before the redesign ships should be excluded from evaluation (they can be re-scored offline only if raw posts are stored — they are not).

---

## 2. Literature-grounded design for 15-min / 1-h horizons

### 2.1 What the evidence actually supports (honest summary)
- **Message-board sentiment predicts volume and volatility, not returns.** Antweiler & Frank (2004, *J. Finance* 59:1259) on 1.5 M Yahoo!/Raging Bull posts: bullishness has a statistically significant but economically negligible effect on next-day returns; message *volume* predicts volatility; *disagreement* (1 − |B−S|/(B+S)) is associated with trading volume. Das & Chen (2007, *Mgmt Sci*) replicate: sentiment inversely related to disagreement, little return predictability.
- **Crypto-specific daily studies** find mixed, small Granger-causal effects. Kraaijeveld & De Smedt (2020, *J. Int. Fin. Markets Inst. & Money* 65) with a crypto-tailored VADER: Twitter sentiment Granger-causes BTC/BCH/LTC returns at daily/hourly resolution, after removing bots (which were a large share of volume); tweet volume correlates only with LTC/XRP. Several theses/papers (e.g. Hurjui 2022, Erasmus) find Reddit attention Granger-causes BTC/ETH *volatility*, not returns. Urquhart (2018) finds Google Trends attention predicts volatility and volume, not returns.
- **Hourly horizon:** a 2025 benchmarking study (Springer *Social Network Analysis and Mining* 15, "Benchmarking modeling architectures for cryptocurrency price…", BERT sentiment of hourly Twitter + market indicators) reports Twitter sentiment had "almost negligible influence" at the 1-h horizon while market indicators retained predictive power. Mai et al. (2015/2018) report hourly-lag effects from forum posts but with 2–4-lag structures and small magnitudes.
- **Explicit crowd buy/sell signals** (Springer *Electronic Markets* 35, 2025, 28 700 LLM-extracted signals from X/Reddit/Stocktwits/Telegram): abnormal return at the event hour ≈ +0.35 % for buy signals (55.8 % positive), a *reversal* at hour +1 (−0.05 %), nothing beyond; effect concentrated in low-cap coins with recent negative returns — i.e. not BTC/ETH.
- **Lexicons:** Loughran & McDonald (2011) show ~¾ of Harvard-IV "negative" words are not negative in finance; their lists were built for 10-Ks and transfer badly to social text. Chen, De, Hu & Hwang (2019/2021, StockTwits crypto lexicon incl. emojis/slang/profanity, trained on user-labelled bullish/bearish posts about CRIX) report 32 % better out-of-sample classification than L&M. Kulakowski & Frasincar (2023) build a language-universal crypto *emoji* lexicon (F1 ≈ 0.48 alone — emojis help as features, not as a classifier). Hutto & Gilbert (2014, VADER) establish the engineering pattern: valence lexicon + negation window (3 tokens), intensifiers/dampeners, caps/punctuation emphasis, contrastive "but".
- **Attention / investor base:** change in subreddit followers predicts idiosyncratic crypto volatility (arXiv 2211.13274); social volume predicts BTC *trading volume* more than price (multi-platform hourly study, 16 coins, 2017–18).

Implication for Kalshi hourly/15-min up/down markets (settled on a 60-s CF index average): the realistic prior is that
**directional** social sentiment at these horizons has ≈ 0 incremental Brier skill over the market mid, and that
**attention/disagreement** may carry information about realised |move| (regime/sizing, hypothesis (c)), not sign. The
design below therefore separates *direction*, *attention* and *disagreement*, normalises each against its own
trailing baseline, and pre-registers the null as the expected outcome.

### 2.2 Design principles adopted
1. **Token-level matching** with word boundaries, stems listed explicitly (`liquidat(ed|ion|ions)` as a regex group), multi-word phrases first, and a **product/entity stop-list** (`pump.fun`, `Pump` as proper noun, `Moons` on r/CC, `Shortwave`, `red-teaming`, `subreddit`, URLs stripped before scoring).
2. **Valence weights** in [−3, +3] per term (VADER convention) rather than ±1 counts; a crypto-social seed list (section 3.6) rather than Loughran–McDonald, with L&M used only as a *negative-word sanity filter* for additions.
3. **Negation scope**: flip and dampen (×−0.74, VADER's constant) any valence token within 3 tokens after a negator (`not, no, never, don't, isn't, won't, without, instead of, stop`), and treat "but" as contrastive (down-weight the clause before, up-weight after). Questions (title ends with `?` or starts with wh-word) get direction weight ×0.3 (they are attention, not opinion).
4. **Emojis** as valence tokens (🚀 +2.5, 📈 +1.5, 💎🙌 +1.5, 🐂 +2, 🌕 +1.5; 📉 −1.5, 🐻 −2, 💀 −1.5, 🩸 −2, 😭 −1, 🤡 −1, 👀 0 (attention only)).
5. **Per-document score** `s_doc = clip((Σ pos − Σ |neg|)/(Σ pos + Σ |neg| + 3), −1, 1)` (the +3 shrinks one-hit documents toward 0, cf. the α=15 normaliser in VADER); documents with zero valence tokens are *neutral*, not dropped: they count toward attention but not toward direction.
6. **Per-sub baselines**: every raw statistic is converted to a z-score against the sub's (or symbol's) trailing 7-day distribution for the same hour-of-day ± 1 h (minimum 20 observations; otherwise against the full trailing 7 days; otherwise `null`). Direction and attention features are reported only as z-scores plus the raw count for transparency.
7. **Decay**: exponentially-weighted sums with half-life 30 min (15-min markets) and 120 min (hourly), computed over *post creation time*, but only including posts whose `fetched_ts ≤ decision_ts`.
8. **De-duplication**: key = Reddit post id (from URL `/comments/<id>/`), Stocktwits message id; near-duplicate titles (normalised, Jaccard ≥ 0.8 on token sets within 24 h) collapse to the first occurrence; sticky/daily threads are excluded from direction but counted once/day for attention.
9. **Author weighting**: unique authors per window is a first-class attention feature; direction is averaged over *authors* (one vote per author per window, the author's mean `s_doc`) to blunt spam/flooding. Author karma is unavailable anonymously; with app credentials, weight by `log1p(min(score, 50))` of the post (score is itself post-hoc and must be snapshotted at fetch time, never back-filled).
10. **Disagreement** (Antweiler–Frank): `disag = 1 − |n_pos − n_neg|/(n_pos + n_neg)` over labelled/strongly-valenced documents.
11. **Attention surprise**: `z_posts = (posts_60m − μ_{7d,same-hour}) / σ_{7d,same-hour}` per source; the paper record says this is the feature most likely to relate to realised volatility.

---

## 3. Specification

### 3.1 Sources (free, no login), measured today
| source | endpoint / tool | measured volume | fields | cost / limit | verdict |
|---|---|---|---|---|---|
| Reddit sub listings (anonymous Atom) | skill script `sub <name> --sort new --limit 100` | r/Bitcoin 1.0 post/h; r/solana 0.3; r/CryptoCurrency 0.4; r/ethereum 0.1 | title, author, `updated`, url, body≤4000 | ~1 req/min/IP, shared by all processes on the host | keep for **attention** and daily tone; useless alone for 15-min direction |
| Reddit site-wide search (anonymous Atom) | `search "bitcoin" --sort new --limit 100` | ≈ 22 posts/h across all subs (measured 18:19→19:16Z) | same | same budget | higher volume, lower quality (casino spam, non-English, x-posts); use with a sub allow-list |
| Reddit OAuth app-only (free script app; needs a Reddit account to register once; no user login in operation) | same script with `REDDIT_CLIENT_ID/SECRET` | — (not measured; documented ~100 req/min) | + `score`, `num_comments`, `created_utc`, flair, **comments** | free | the only way to get comments (r/Bitcoin daily thread runs hundreds of comments/day) and engagement; **recommend registering**; proposed, not assumed |
| **Stocktwits public symbol stream** | `GET api.stocktwits.com/api/2/streams/symbol/{BTC.X,ETH.X,SOL.X}.json[?max=<id>]` | **BTC.X 44 msgs/h (22 unique users / 41 min), ETH.X 13.5/h, SOL.X 3/h**; 17 of 30 BTC messages carried a user label (14 Bullish / 3 Bearish) | id, `created_at` (s), body, **user-labelled Bullish/Bearish**, user id/followers/ideas, likes, `sentiment_change`, `volume_change` | no key; documented 200 req/h/IP; cursor pagination verified | **highest-value free upgrade**: labelled direction at ~10× Reddit's rate and the field the crypto-lexicon literature was trained on |
| CoinGecko `/coins/{id}?community_data=true` | public | `community_data` fields are **null** for bitcoin today; only `sentiment_votes_up/down_percentage` (60/40, daily poll) and `watchlist_portfolio_users` | — | 30 req/min | low value; one daily attention row at most |
| alternative.me Fear & Greed | already in `context_feed` | daily | — | — | keep as daily regime only |
| Google Trends via pytrends | — | **not usable**: pytrends archived 2025-04, 429/NID-cookie failures; official API is invite-only alpha | — | — | drop; "Google searches for Bitcoin hit a 5-year low" posts are a weak proxy |
| Nitter / X | nitter.net returns "offline"; other instances churn | — | — | — | drop |
| Bitcointalk RSS | `index.php?type=rss;…` → 400 today | — | — | — | drop unless a working feed URL is found; forum is slow (hours/day) |
| Pushshift | 403 (mod-only since 2023) | — | — | — | drop |
| LunarCrush / Santiment | 401 / paid | — | — | — | out of scope (paid) |

### 3.2 Timestamp discipline (causal contract)
Every stored document carries three times:
- `created_ts` — author-side time (Reddit Atom `updated`; Stocktwits `created_at`). Used only for windowing/decay.
- `fetched_ts` — wall-clock when our request returned. **This is the availability time.** A document may enter a feature for decision time `T` only if `fetched_ts ≤ T`.
- `scored_ts` — when the feature row was computed (for audit).
Feature rows are written with `as_of = fetched_ts` of the request that produced them, and `s7_live.py` must record `ctx.sent_as_of` (the feature row's `as_of`) and `ctx.sent_age_s = decision_ts − as_of`. Rows with `sent_age_s > 900` are evaluated as *missing*. Nothing is ever back-filled into a feature row after `as_of`; later-arriving posts with earlier `created_ts` only affect later rows.

### 3.3 Storage schema (all JSONL under `data/context/`)
`social_posts.jsonl` — one line per *new* document (deduped):
```
{"src":"reddit"|"stocktwits", "id":"t3_abc" | 667476962, "sub":"Bitcoin"|"BTC.X", "author":"u/..."|user_id,
 "created_ts":1793128000, "fetched_ts":1793128071, "title":"...", "body":"...", "url":"...",
 "label": null|"Bullish"|"Bearish",              # Stocktwits user label only
 "assets":["BTC"], "is_sticky":false, "is_question":false, "dup_of":null,
 "s_doc":0.31, "pos":4.5, "neg":1.2, "n_val":3, "hits":["rally+1.8","not crash-1.6*"], "lex_version":"2026-10-27a"}
```
`social_features.jsonl` — one line per asset per feature update (`as_of`):
```
{"as_of":1793128071, "asset":"BTC", "lex_version":"2026-10-27a",
 "rd": {"posts_60m":2, "posts_180m":5, "authors_60m":2, "dir_60m":null, "dir_180m":0.12, "dir_ewma_120":0.08,
        "disag_180m":null, "n_dir_180m":1, "z_posts_60m":0.9, "z_dir_180m":null, "stale_s":380, "subs_fresh":["Bitcoin","CryptoCurrency"]},
 "st": {"msgs_15m":11, "msgs_60m":44, "users_60m":22, "lab_bull_60m":14, "lab_bear_60m":3, "bull_ratio_60m":0.82,
        "dir_lex_60m":0.21, "dir_ewma_30":0.25, "disag_60m":0.35, "z_msgs_60m":1.4, "z_bull_ratio_60m":0.6, "stale_s":55},
 "news_flag": false}
```
`social_baselines.json` — per (src, sub|symbol, feature, hour-of-day): trailing-7-day mean/sd/n, rebuilt hourly from `social_posts.jsonl` (bootstrapped from the `limit=100` listing on first run; Stocktwits back-paged via `?max=` on first run, within the 200/h budget).

`ctx` additions (replace `reddit_mentions`/`reddit_sent`): `sent_as_of`, `sent_age_s`, `rd_z_posts_60m`, `rd_dir_ewma_120`, `rd_n_dir_180m`, `st_bull_ratio_60m`, `st_z_msgs_60m`, `st_dir_ewma_30`, `st_disag_60m`, `st_users_60m`. Keep raw `reddit_sent`/`reddit_mentions` columns null after the switch (do not reuse the names).

### 3.4 Feature definitions (per asset, as of `as_of`)
Let D(A, w) = documents with `A ∈ assets`, `created_ts ∈ (as_of − w, as_of]`, `fetched_ts ≤ as_of`, `dup_of = null`, `is_sticky = false`.
- `posts_w` = |D(A,w)|; `authors_w` = |{author : D(A,w)}|.
- `dir_w` = mean over authors of (mean `s_doc` of that author's docs in D with `n_val ≥ 1` and `is_question=false`); `null` if fewer than 3 such authors (`n_dir_w` reported).
- `dir_ewma_h` = Σ s_doc·2^{−(as_of−created_ts)/h} / Σ 2^{−(as_of−created_ts)/h} over the same documents in the last 4h (h = 30 min for 15M, 120 min for hourly); null if effective n < 3.
- `disag_w` = 1 − |n_pos − n_neg|/(n_pos+n_neg) with pos = s_doc > 0.15 (or label Bullish), neg = s_doc < −0.15 (or label Bearish); null if n_pos+n_neg < 4.
- Stocktwits: `bull_ratio_w` = lab_bull/(lab_bull+lab_bear), null if < 5 labelled; `msgs_w`, `users_w`.
- `z_x` = (x − μ)/σ from `social_baselines.json` for the same hour-of-day ±1 h, trailing 7 days (min n 20; else trailing 7 days all hours; else null). Counts are z-scored after `log1p`.
- `news_flag` = any document in D(A,15m) whose title matches the breaking-news pattern list (`etf|sec |hack|exploit|halt|liquidat|fed |fomc|cpi |tariff|ban |lawsuit|bankrupt`) from an allow-listed news sub or ≥ 3 authors within 15 min posting near-duplicate titles. Attention only.

### 3.5 Update cadence within the budgets
- Reddit anonymous: 1 request / 75 s shared across *everything* on the host (only the feed may call Reddit; audits must stop the feed first). Schedule: `Bitcoin new 100` → `search "bitcoin OR ethereum OR solana" new 100` → `solana new 100` → `CryptoCurrency new 100` → repeat; r/ethereum once per 30 min (0.1 post/h). Each sub refreshed ≤ 5 min; Reddit features are re-emitted every 75 s with `stale_s` per sub. With app credentials: also `r/Bitcoin` daily-thread comments (`thread … --limit 200`) every 5 min and `search` every 2 min.
- Stocktwits: 3 symbols × 1 request / 2 min = 90 req/h (< 200/h documented; headers expose no limit so stay at ≤ 50 %); back-page with `?max=` only on cold start. Features emitted every 60 s from the local store (no network).
- Baselines rebuilt hourly; lexicon versioned (`lex_version`) and never edited in place during an evaluation window.
- Optional LLM batch pass (proposed, not required for the tests): every 10 min, one Hermes subagent call classifying the ≤ 60 new documents since the last pass into {bull, bear, neutral, question/news} with a short JSON output. ≈ 60 docs × ~120 tokens ≈ 8 k input + ~1 k output per call, 144 calls/day ≈ 1.2 M in + 0.15 M out per day. At Haiku-class list prices (~$1/M in, ~$5/M out) ≈ **$2/day**; Sonnet-class (~$3/$15) ≈ **$6/day**. Stored as `llm_label` per document with `scored_ts`; it is a *second* direction feature evaluated identically, never a per-tick call.

### 3.6 Lexicon and weight files to add under `data/lexicon/`
- `crypto_sentiment.tsv` — columns `pattern\tweight\tkind\tnote`; `kind ∈ {word, phrase, regex}`; word-boundary matching; seed (≈ 70 entries, extend only with versioned diffs):
  - bullish: `rally +1.8`, `breakout +1.8`, `pump(ing|ed)? +1.5 (regex; excluded when followed by \.fun|swap|dot fun or capitalised Pump)`, `moon(ing|ed)?|to the moon +2.0 (regex; excluded on r/CryptoCurrency when 'moons')`, `ath|all[- ]time high +1.5 (word)`, `bull(ish|s|run)? +2.0`, `buy the dip +1.5`, `accumulat(e|ing|ion) +1.2`, `bottom is in +2.0`, `undervalued +1.2`, `send it +1.5`, `green +0.8 (word, only with candle|day|week|dildo)`, `long(ing|ed)? +1.0 (word; excluded in long[- ]term, longer, belongs)`, `hodl|diamond hands +1.0`, `squeeze +1.0`, `supercycle +1.5`, `bid(ding)? +0.8`, `recover(y|ing|ed) +1.0`, `surge|soar(ing|ed)? +1.5`, `new high(s)? +1.5`, `institutional (buying|inflows) +1.2`, `inflows +1.0`.
  - bearish: `dump(ing|ed)? −1.8 (excluded in pump and dump when quoted/rules text)`, `crash(ing|ed|es)? −2.0`, `capitulat(e|ion|ing) −2.0`, `liquidat(ed|ion|ions)? −1.5 (only with long|cascade|\$|m\b|k\b within 5 tokens)`, `plunge|tank(ing|ed)?|nuke(d)? −1.8`, `collapse −2.0`, `rug(ged|pull)? −2.0`, `bear(ish|s)? −2.0`, `short(ing|ed|s)? −1.0 (word; excluded shortwave|shorter|short[- ]term|in short)`, `sell(ing|off)?|sell-off −1.2 (word)`, `red (candle|day|week|month) −1.0 (phrase only)`, `fear −1.0`, `panic −1.5`, `bubble −1.5`, `overvalued −1.2`, `scam(mer|s)? −1.0 (excluded in rules/sticky)`, `ponzi −1.5`, `dead cat −1.5`, `lower low(s)? −1.5`, `bleed(ing)? −1.5`, `rekt −2.0`, `outflows −1.0`, `bottom (is )?not in −1.5`, `new low(s)? −1.5`, `bagholder|bags −1.0`, `cope|copium −1.0`.
- `emoji.tsv` — `emoji\tweight`: 🚀 2.5, 📈 1.5, 💎 1.5, 🙌 1.0, 🐂 2.0, 🌕 1.5, 🟢 1.0, 🔥 0.8, 📉 −1.5, 🐻 −2.0, 💀 −1.5, 🩸 −2.0, 🔴 −1.0, 😭 −1.0, 🤡 −1.0, ⚰️ −1.5, 🚨 0 (attention), 👀 0 (attention).
- `negators.txt` — `not, no, never, n't, without, instead of, stop, won't, isn't, aren't, don't, doesn't, didn't, hardly, barely, nothing, nobody, neither, nor, cannot`.
- `modifiers.tsv` — `very 1.3, extremely 1.4, absolutely 1.3, massive 1.3, huge 1.3, slightly 0.7, kinda 0.8, somewhat 0.8, maybe 0.7, might 0.7, probably 0.9, lol 0.8, /s −1 (sarcasm marker: flips sign)`.
- `entity_stoplist.txt` — phrases removed before scoring: `pump.fun, pumpswap, pump dot fun, moons (r/CryptoCurrency), shortwave, red-teaming, subreddit, reddit, redd.it, bitcoin magazine, bull market media, bear market (as a historical reference when preceded by 'last'|'2022'|'2018')`.
- `asset_aliases.tsv` — `asset\tpattern\tscope`: BTC: `\bbitcoin(s)?\b|\bbtc\b|\$btc(\.x)?\b|\bsats\b` (+ implicit on r/Bitcoin when no other asset named); ETH: `\bethereum\b|\beth\b|\$eth(\.x)?\b|\bether\b` (word-bounded — kills `whether/together`; implicit on r/ethereum); SOL: `\bsolana\b|\bsol\b|\$sol(\.x)?\b` (implicit on r/solana).
- `sticky_patterns.txt` — `^Daily (General |Crypto )?Discussion`, `^Daily Discussion, `, `Ecosystem News - `, `This Week in Media`, `Weekly`.
- `news_patterns.txt` — the breaking-news list from 3.4.
- `sub_priors.json` — **estimated, not hand-set**: written by the baseline job (trailing-7-day mean/sd per sub); never edited by hand.

---

## 4. Pre-registered evaluation (sentiment leg of P15)

Prior stated now, before any outcome is joined: **(a) is expected to fail** (no directional Brier skill at 1 h), **(b) is
expected to fail**, **(c) attention→|move| is the only hypothesis with literature support.** Passing (a) or (b) would be
a surprise and must be replicated on a second non-overlapping window before any sizing use.

Common rules: per asset; no row is used unless `sent_age_s ≤ 900` and the feature is non-null; rows before the lexicon
switch (`lex_version` null) are excluded; hour-clustered standard errors (cluster = settlement hour; for 15M, cluster =
hour containing the close); all tests two-sided at 95 % unless stated; Holm correction across the pre-named features
within each test; lexicon frozen per evaluation window (a lexicon change starts a new window).

### 4.1 Test (a): hourly Brier skill over the market mid (P10 scored rows, T-3)
- Unit: one row per asset-hour = the `books.jsonl` snapshot of the family's traded/scored strike closest to T-3 min (as P10) with `y = 1{settle ≥ K}`, `p_mid = (yes_bid+yes_ask)/2` (require spread ≤ 6c, mid ∈ [.03,.97]).
- Features (pre-named, exactly these): `F1 = st_dir_ewma_30` (Stocktwits lexicon direction, signed toward UP), `F2 = st_bull_ratio_60m − baseline mean` (labelled), `F3 = rd_dir_ewma_120`, `F4 = llm_dir_60m` if the batch pass exists.
- Model: expanding-window logistic recalibration `p' = σ(logit(p_mid) + β·F)` with β fit only on hours strictly before the evaluated hour (first 48 hours burn-in, no fit on fewer than 48). Score `ΔBrier_h = (p_mid − y)² − (p' − y)²`.
- Pass: mean ΔBrier > 0 with hour-clustered 95 % CI excluding 0 on ≥ 150 hours per asset (PREREG P15), *and* β's sign stable (same sign in the first and second halves). Power note: with per-hour SD of ΔBrier ≈ 0.05 (near-money hours), 150 hours detect ≈ 0.011 at 80 % power; a 0.005 improvement needs ≈ 600 hours. Report the minimal detectable effect alongside the result.
- Also report (diagnostic, not a pass criterion): Granger-style regression of the hourly 60-s-average return sign on lagged F at lags 1–3 h.

### 4.2 Test (b): P13 contested-case pre-filter (15M and hourly final minute)
- Unit: P13 decisions (or scored snaps) with `fav_at_decision ∈ [.6,.95]` ("contested"), outcome `hit = 1{side correct}`.
- Rule tested: side agrees with the sign of `st_dir_ewma_30` (fallback `rd_dir_ewma_120`) when |feature| > its trailing 7-day 70th percentile; otherwise unchanged.
- Pass: hit(filtered) − hit(price alone) ≥ 3 pts on ≥ 40 contested cases per asset (PREREG). Power note: SE of a hit rate at n=40, p≈.9 is ≈ 4.7 pts; 40 cases cannot establish 3 pts — treat a pass at 40 as *provisional* and require replication to 120 cases before any use.
- Also report a sign-only version: fraction of contested cases where the sentiment sign alone matched the outcome, vs. 50 %.

### 4.3 Test (c): attention regime vs. realised |z| and P13 supply
- Unit: asset-hour. `X = z_posts_60m` (Reddit) and `z_msgs_60m`, `z_users_60m`, `disag_60m` (Stocktwits) at T-3; `Y1 = |hourly realised z|` (the P10 `z` of the actual close vs. the T-3 spot and sigma), `Y2 = P13 opportunities in the hour` (supply.jsonl), `Y3 = 15-min range in sigma units`.
- Test: Spearman ρ(X, Y) with hour-clustered bootstrap CI; and top-quintile vs bottom-quintile mean |z|. Pre-registered expectation: ρ > 0 for attention vs |z|. Pass for *sizing by regime* only after ≥ 10 days (PREREG) **and** ρ's CI excludes 0 for the same feature in both the first and second 5 days. Even then the only permitted use is a volatility-regime multiplier already contemplated by P13, not direction.

### 4.4 Decision rule and stopping
- Evaluate at 150 hours (a), 40 contested cases (b), 10 days (c) per asset; interim looks at 50 % only to check data quality (null rates, `sent_age_s` distribution), never for stopping on effect.
- Any pass → replicate on the next non-overlapping window of the same size before amending PREREG. Any fail → the feature stays scored-only; the lexicon may be revised (new `lex_version`) and the clock restarts.
- Report always: n, null rate, mean `sent_age_s`, Reddit/Stocktwits coverage (fraction of hours with ≥ 3 direction-bearing authors), and the audit census of section 1.2 recomputed on the new tokenizer (target: exact-word share ≥ 95 % of hits).

---

## 5. Ranked implementation list (≤ 5; code only after this doc is accepted)

1. **Tokenizer + versioned lexicon scorer** (`src/predhft/sentiment_lex.py`, stdlib only).
   `load_lexicon(root: Path = Path("data/lexicon")) -> Lexicon` (dataclass: terms, emojis, negators, modifiers, stoplist, aliases, sticky/news patterns, version);
   `normalize(text: str) -> str` (unescape, strip URLs/markdown, lower except for proper-noun checks);
   `tokenize(text: str) -> list[str]` (word-boundary + emoji-aware);
   `score_text(title: str, body: str, sub: str, lex: Lexicon) -> TextScore(pos: float, neg: float, n_val: int, s_doc: float, is_question: bool, hits: list[str])`;
   `detect_assets(title: str, body: str, sub: str, lex: Lexicon) -> set[str]`;
   `is_sticky(title: str, lex: Lexicon) -> bool`.
   Files: all of section 3.6. Acceptance: rerun the section-1.2 census on the 100 saved posts → exact-word share ≥ 95 %, zero `whether→ETH`, "6 red candles" ≈ −0.3, "Never exceed ATH" < 0.
2. **Stocktwits public stream ingester** (`src/predhft/stocktwits_feed.py`).
   `fetch_stream(symbol: str, since_id: int | None = None, max_id: int | None = None, timeout: float = 8.0) -> list[dict]`;
   `normalize_message(m: dict, fetched_ts: float) -> dict` (schema 3.3, `label` from `entities.sentiment.basic`);
   `loop(symbols=("BTC.X","ETH.X","SOL.X"), every: float = 120.0, store: Path = ROOT/"social_posts.jsonl")`.
   Budget ≤ 90 req/h; cold-start back-paging ≤ 7 days via `?max=`.
3. **Deduplicated document store + feature builder** (`src/predhft/social_features.py`).
   `ingest(docs: list[dict], store: Path) -> int` (id de-dup, near-dup by normalised-title Jaccard ≥ .8 within 24 h, writes `social_posts.jsonl`);
   `load_window(store: Path, as_of: float, max_age_s: float = 4*3600) -> list[dict]` (filters `fetched_ts ≤ as_of`);
   `features(asset: str, as_of: float, docs: list[dict], baselines: dict, hl_15m: float = 1800, hl_1h: float = 7200) -> dict` (section 3.4 exactly);
   `rebuild_baselines(store: Path, as_of: float, days: int = 7) -> dict` → `social_baselines.json`;
   `latest(asset: str, root: Path = ROOT, max_age: float = 900.0) -> dict | None` (returns `as_of` so `s7_live.py` can stamp `sent_as_of`/`sent_age_s`).
4. **Reddit feed rewrite** (`src/predhft/reddit_feed.py`, replacing the current loop body; keep the skill-script transport).
   `fetch(kind: Literal["sub","search"], target: str, limit: int = 100, sort: str = "new") -> tuple[list[dict], float]` (returns `fetched_ts`; sole Reddit caller on the host; writes `err` rows without a `by_asset` block);
   `schedule() -> Iterator[tuple[str,str]]` (3.5 rotation; r/ethereum every 30 min);
   `loop(every: float = 75.0)` → `ingest()` + `features()` for BTC/ETH/SOL every cycle. Remove `score()` and the `BULL/BEAR/KEYS` constants.
5. **P15 sentiment evaluation script** (`scripts/eval_p15_sentiment.py`, stdlib + existing repo helpers).
   `join_hourly(asset: str, family_dir: Path, feats: Path, t_minus_min: float = 3.0) -> list[Row]`;
   `brier_skill(rows, feature: str, burn_in: int = 48) -> dict` (expanding-window β, ΔBrier, hour-clustered CI, MDE);
   `contested_filter(asset: str, trades: Path, feats: Path, pct: float = .70) -> dict`;
   `attention_regime(asset: str, feats: Path, hourly: Path, supply: Path) -> dict`;
   `report(out: Path = Path("reports/p15_sentiment_<date>.md"))`. Must refuse rows with `lex_version` null or `sent_age_s > 900`.

Not in the list (deliberately): an LLM labelling pass (optional, 3.5), Reddit OAuth credentials (a registration decision for the user; the code in item 4 should read the env vars the skill script already honours and gain `score/num_comments/comments` automatically), Google Trends / Nitter / Bitcointalk (not viable today).
