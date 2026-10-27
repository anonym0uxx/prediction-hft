"""Tokenizer + versioned lexicon scorer for the P15 sentiment leg (docs/SENTIMENT_DESIGN_2026-10-27.md §3.6, §5 item 1).
Stdlib only. Word-boundary matching, explicit stems, phrases before words, entity stop-list applied before scoring, VADER-style
negation (flip x-0.74 within 3 tokens after a negator), modifiers, contrastive 'but', '/s' sarcasm flip, question damping x0.3,
emojis as valence tokens, s_doc = clip((pos-neg)/(pos+neg+3), -1, 1). Also: asset attribution (word-bounded aliases + implicit sub),
sticky detection, and the §1.2 hit census (`python -m predhft.sentiment_lex census <posts.jsonl>`)."""
from __future__ import annotations
import dataclasses, html, json, pathlib, re, sys

LEX_ROOT = pathlib.Path("data/lexicon")
NEG_SCALAR = -0.74          # VADER negation constant
NEG_WINDOW = 3              # tokens after a negator that are flipped
MOD_WINDOW = 2              # tokens before a hit scanned for a modifier
BUT_BEFORE, BUT_AFTER = 0.5, 1.5
QUESTION_W = 0.3
SHRINK = 3.0                # +3 in the s_doc denominator
WH = ("what", "why", "how", "when", "where", "who", "which", "whether")

URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w.-]+\.(?:com|org|net|io|it|fun|xyz|co|gg)\b(?:/\S*)?", re.I)
MD_RE = re.compile(r"[*_`>#~\[\]()|\\]+")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]\uFE0F?")
TOKEN_RE = re.compile(r"/s\b|\$?[a-z0-9][a-z0-9'.$]*[a-z0-9]|\$?[a-z0-9]|[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]\uFE0F?|[?!]")
PUMP_PROPER_RE = re.compile(r"(?<![.!?:\n]\s)(?<!^)(?<!^\s)\bPump\b(?!\s+(?:it|and|the|is|incoming|hard|harder|up|time))")


@dataclasses.dataclass
class Term:
    pattern: str
    weight: float
    kind: str
    note: str
    rx: re.Pattern
    ctx: re.Pattern | None = None


@dataclasses.dataclass
class Lexicon:
    terms: list[Term]
    emojis: dict[str, float]
    negators: list[str]
    modifiers: dict[str, float]
    stoplist: list[tuple[re.Pattern, str | None]]
    aliases: dict[str, tuple[re.Pattern, str | None]]
    sticky: list[re.Pattern]
    news: list[re.Pattern]
    version: str
    root: pathlib.Path = LEX_ROOT


@dataclasses.dataclass
class TextScore:
    pos: float
    neg: float
    n_val: int
    s_doc: float
    is_question: bool
    hits: list[str]


def _rows(path: pathlib.Path, ncols: int | None = None) -> list[list[str]]:
    out = []
    if not path.exists(): return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"): continue
        cols = line.rstrip("\n").split("\t")
        out.append(cols if ncols is None else (cols + [""] * ncols)[:ncols])
    return out


def _wrap(pat: str, kind: str) -> re.Pattern:
    if kind == "regex" and (pat.startswith("^") or pat.startswith("(?<")): return re.compile(pat, re.I)
    body = re.escape(pat) if kind in ("word", "phrase") else pat
    if kind == "phrase": body = re.escape(pat).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![\w$]){'(?:' + body + ')'}(?![\w])", re.I)


_CACHE: dict[str, Lexicon] = {}


def load_lexicon(root: pathlib.Path = LEX_ROOT) -> Lexicon:
    root = pathlib.Path(root); key = str(root.resolve())
    if key in _CACHE: return _CACHE[key]
    terms = []
    for pat, w, kind, note, ctx in _rows(root / "crypto_sentiment.tsv", 5):
        terms.append(Term(pat, float(w), kind or "word", note, _wrap(pat, kind or "word"), re.compile(ctx, re.I) if ctx else None))
    # phrases / longer patterns first so 'buy the dip' wins over 'dip'
    terms.sort(key=lambda t: (-(t.kind == "phrase"), -len(t.pattern)))
    emojis = {e: float(w) for e, w, *_ in _rows(root / "emoji.tsv")}
    negators = [r[0].strip().lower() for r in _rows(root / "negators.txt")]
    modifiers = {r[0].strip().lower(): float(r[1]) for r in _rows(root / "modifiers.tsv", 2)}
    stop = [(_wrap(r[0], "regex"), (r[1].strip() or None) if len(r) > 1 else None) for r in _rows(root / "entity_stoplist.txt")]
    aliases = {a: (re.compile(p, re.I), (s.strip() if s.strip() not in ("", "-") else None)) for a, p, s in _rows(root / "asset_aliases.tsv", 3)}
    sticky = [re.compile(r[0], re.I) for r in _rows(root / "sticky_patterns.txt")]
    news = [re.compile(r"(?<!\w)" + re.escape(r[0].rstrip()) + (r"\b" if not r[0].endswith(" ") else r"(?:\s|$)"), re.I) for r in _rows(root / "news_patterns.txt")]
    vfile = root / "VERSION"
    version = vfile.read_text(encoding="utf-8").strip() if vfile.exists() else "unversioned"
    lex = Lexicon(terms, emojis, negators, modifiers, stop, aliases, sticky, news, version, root)
    _CACHE[key] = lex
    return lex


def normalize(text: str) -> str:
    """HTML-unescape, strip URLs and markdown, drop the proper-noun product 'Pump' (mid-sentence capitalised), lower-case."""
    t = html.unescape(text or "")
    t = URL_RE.sub(" ", t)
    t = PUMP_PROPER_RE.sub(" ", t)
    t = MD_RE.sub(" ", t)
    t = t.replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def tokenize(text: str) -> list[str]:
    """Word-boundary + emoji-aware tokens over an already-normalised (lower-cased) string."""
    toks = TOKEN_RE.findall(text)
    out = []
    for tok in toks:
        if tok.endswith("n't") and len(tok) > 3 and tok not in ("won't", "isn't", "aren't", "don't", "doesn't", "didn't", "can't"):
            out.append(tok)          # keep contraction whole; negation check uses endswith("n't")
        else: out.append(tok.strip("."))
    return [t for t in out if t]


def _apply_stoplist(text: str, sub: str | None, lex: Lexicon) -> str:
    for rx, scope in lex.stoplist:
        if scope is None or (sub or "").lower() == scope.lower(): text = rx.sub(" ", text)
    return text


def is_question(title: str) -> bool:
    t = (title or "").strip().lower()
    if not t: return False
    return t.endswith("?") or t.split(" ", 1)[0].strip("\"'(") in WH


def is_sticky(title: str, lex: Lexicon) -> bool:
    return any(rx.search(title or "") for rx in lex.sticky)


def is_news(title: str, lex: Lexicon) -> bool:
    t = normalize(title)
    return any(rx.search(t) for rx in lex.news)


def _token_spans(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group()) for m in TOKEN_RE.finditer(text)]


def _index_at(spans: list[tuple[int, int, str]], pos: int) -> int:
    lo, hi = 0, len(spans)
    while lo < hi:
        mid = (lo + hi) // 2
        if spans[mid][1] <= pos: lo = mid + 1
        else: hi = mid
    return lo


def _is_negator(tok_list: list[str], i: int, lex: Lexicon) -> bool:
    t = tok_list[i]
    if t in lex.negators or (t.endswith("n't") and "n't" in lex.negators): return True
    # two-word negators ("instead of", "no longer") ending at i
    if i > 0 and f"{tok_list[i-1]} {t}" in lex.negators: return True
    return False


def _term_hits(text: str, lex: Lexicon) -> list[tuple[int, int, int, float, str]]:
    """Non-overlapping lexicon hits on a normalised, stop-listed text: (char_start, char_end, token_index, weight, label).
    Phrases/longer patterns are tried first; `ctx` terms need their context regex within +-5 tokens."""
    spans = _token_spans(text); used = bytearray(len(text)); out = []
    for term in lex.terms:
        for m in term.rx.finditer(text):
            a, b = m.span()
            if b <= a or any(used[a:b]): continue
            ti = _index_at(spans, a)
            if term.ctx is not None:
                lo, hi = max(0, ti - 5), min(len(spans), ti + 6)
                window = text[spans[lo][0]:spans[hi - 1][1]] if spans else ""
                if not term.ctx.search(window): continue
            for k in range(a, b): used[k] = 1
            out.append((a, b, ti, term.weight, m.group().strip()))
    for m in EMOJI_RE.finditer(text):
        w = lex.emojis.get(m.group()) or lex.emojis.get(m.group().rstrip("️"))
        if w is None: continue
        out.append((m.start(), m.end(), _index_at(spans, m.start()), w, m.group()))
    return out


def score_text(title: str, body: str, sub: str, lex: Lexicon) -> TextScore:
    q = is_question(title) if title else is_question(body[:300] if body else "")
    text = normalize((title or "") + " . " + (body or ""))
    text = _apply_stoplist(text, sub, lex)
    toks = [s[2] for s in _token_spans(text)]
    sarcasm = "/s" in toks
    but_idx = toks.index("but") if "but" in toks else None
    pos = neg = 0.0; n_val = 0; hits = []
    for _, _, ti, w, label in _term_hits(text, lex):
        if w == 0.0: hits.append(f"{label}+0"); continue
        negated = False
        for j in range(max(0, ti - NEG_WINDOW), ti):
            if j < len(toks) and _is_negator(toks, j, lex): negated = True; break
        for j in range(max(0, ti - MOD_WINDOW), ti):
            if j < len(toks) and toks[j] in lex.modifiers and lex.modifiers[toks[j]] > 0: w *= lex.modifiers[toks[j]]
        if negated: w *= NEG_SCALAR
        if but_idx is not None: w *= BUT_BEFORE if ti < but_idx else BUT_AFTER
        if sarcasm: w = -w
        if w > 0: pos += w
        else: neg += -w
        n_val += 1
        hits.append(f"{'not ' if negated else ''}{label}{w:+.2f}{'*' if negated else ''}")
    s = (pos - neg) / (pos + neg + SHRINK) if n_val else 0.0
    if q: s *= QUESTION_W
    s = max(-1.0, min(1.0, s))
    return TextScore(round(pos, 3), round(neg, 3), n_val, round(s, 4), q, hits)


def detect_assets(title: str, body: str, sub: str, lex: Lexicon) -> set[str]:
    text = normalize((title or "") + " . " + (body or ""))
    found = {a for a, (rx, _) in lex.aliases.items() if rx.search(text)}
    if not found:
        for a, (_, implicit) in lex.aliases.items():
            if implicit and (sub or "").lower() == implicit.lower(): found.add(a)
    return found


# ---------------------------------------------------------------- §1.2 census on the new tokenizer
OLD_BULL = ["bull", "moon", "pump", "rally", "breakout", "ath", "buy the dip", "accumulate", "long", "green", "surge", "soar", "up only", "undervalued"]
OLD_BEAR = ["bear", "dump", "crash", "sell", "capitulat", "liquidat", "red", "plunge", "collapse", "rug", "short", "fear", "bubble", "overvalued", "scam"]


def census(posts: list[dict], lex: Lexicon) -> dict:
    """Recompute the §1.2 term-hit census: for every hit of the new scorer, is the matched span a whole word (exact-word share)?
    Also: 'whether/together'->ETH attributions, sticky detections, old substring-lexicon totals for comparison."""
    n_hits = n_exact = 0; per_term: dict[str, int] = {}; eth_bad = 0; sticky = 0; old_bull = old_bear = 0; scored = []
    for p in posts:
        title, body, sub = p.get("title") or "", p.get("body") or "", p.get("sub") or p.get("subreddit") or ""
        if not sub and p.get("url"):
            m = re.search(r"/r/([^/]+)/", p["url"]); sub = m.group(1) if m else ""
        text = _apply_stoplist(normalize(title + " . " + body), sub, lex)
        for a, b, _, _, label in _term_hits(text, lex):
            if EMOJI_RE.fullmatch(label): continue
            n_hits += 1
            ok = (a == 0 or not (text[a-1].isalnum() or text[a-1] == "_")) and (b >= len(text) or not (text[b].isalnum() or text[b] == "_"))
            n_exact += ok; key = label.lower(); per_term[key] = per_term.get(key, 0) + 1
        low = (title + " " + body).lower()
        old_bull += sum(low.count(w) for w in OLD_BULL); old_bear += sum(low.count(w) for w in OLD_BEAR)
        assets = detect_assets(title, body, sub, lex)
        if "ETH" in assets and not re.search(r"\bethereum\b|\beth\b|\$eth|\bether\b", normalize(title + " " + body), re.I): eth_bad += 1
        st = is_sticky(title, lex); sticky += st
        sc = score_text(title, body, sub, lex)
        scored.append({"title": title[:90], "sub": sub, "sticky": st, "assets": sorted(assets), "s_doc": sc.s_doc, "hits": sc.hits})
    return {"n_posts": len(posts), "hits": n_hits, "exact_word_hits": n_exact, "exact_word_share": (n_exact / n_hits if n_hits else None),
            "per_term": dict(sorted(per_term.items(), key=lambda kv: -kv[1])), "eth_without_alias": eth_bad, "sticky_detected": sticky,
            "old_lexicon_substring_hits": {"bull": old_bull, "bear": old_bear}, "scored": scored}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "census":
        raw = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
        posts = json.loads(raw) if raw.lstrip().startswith("[") else [json.loads(l) for l in raw.splitlines() if l.strip()]
        c = census(posts, load_lexicon())
        c.pop("scored") if "--full" not in sys.argv else None
        print(json.dumps(c, indent=1, ensure_ascii=False))
    else:
        lex = load_lexicon(); t = " ".join(sys.argv[1:]) or "Bitcoin Will Never Exceed its ATH by More than 10%"
        print(dataclasses.asdict(score_text(t, "", "Bitcoin", lex)), detect_assets(t, "", "Bitcoin", lex))
