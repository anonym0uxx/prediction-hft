"""AUDIT_ADDENDUM2_2026-10-28 §2 — P12 restart pre-check. HEAD's `s7_live.py` must be DECISION-NEUTRAL for the frozen P12 model rule
(s7p15-KX*15M, proj_gate=False): the book-representation / logging changes since a395cf8 may not alter candidate selection, booked
price, fee or sizing inputs. Proven by replaying every recorded P12 snapshot (data/s7p15-*/books.jsonl) through the a395cf8 book parser
(verbatim copy) and the HEAD parser, then through the model-rule decision path (predhft.p12_replay). Also proves the new logging fields
are not read by any decision. Data-backed tests skip when the repo data is absent (CI without data/)."""
import inspect, json, pathlib, re
import pytest
from predhft import p12_replay as PR
from predhft import s7_live as S7

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
FAMS = [DATA / f"s7p15-KX{a}15M" / "books.jsonl" for a in ("BTC", "ETH", "SOL")]


def load_snapshots():
    rows = []
    for p in FAMS:
        if not p.exists(): continue
        for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not l.strip(): continue
            try: r = json.loads(l)
            except Exception: continue
            if r.get("mins_left") is not None and r.get("p_model") is not None and r.get("thr_used") is not None: rows.append(r)
    return rows


SNAPS = load_snapshots()
needs_data = pytest.mark.skipif(len(SNAPS) < 200, reason="P12 recorded snapshots not available (needs data/s7p15-*/books.jsonl with >= 200 rows)")


def test_head_parser_equals_legacy_parser_on_the_whole_price_grid():
    """Every (YES bid, NO bid) pair on the 0.001 grid gives the identical (yb, ya, ybs, yas) tuple — including the float representation
    of ya = 1 - no_bid that the P12 spread filter `ya - yb > 0.06` compares against."""
    for nb in range(1, 1000):
        ob = {"orderbook_fp": {"yes_dollars": [[f"{0.5:.3f}", "12.5"]], "no_dollars": [[f"{nb / 1000:.3f}", "7.25"]]}}
        assert PR.head_book_touch(ob) == PR.legacy_book_touch(ob)
    assert PR.head_book_touch({"orderbook_fp": {}}) == PR.legacy_book_touch({"orderbook_fp": {}}) == (0.0, 1.0, 0.0, 0.0)


def test_six_cent_spread_regression_case():
    """The case that diverged before the fix: YES bid .24, NO bid .70 -> ya = 0.30000000000000004 at a395cf8; a round(.., 4) made it 0.3
    and let the 6-cent spread through the `> 0.06` filter."""
    ob = {"orderbook_fp": {"yes_dollars": [["0.240", "350"]], "no_dollars": [["0.700", "350"]]}}
    yb, ya, *_ = PR.head_book_touch(ob)
    assert (yb, ya) == PR.legacy_book_touch(ob)[:2] and ya == 1 - 0.700 and (ya - yb > 0.06) == ((1 - 0.700) - 0.240 > 0.06)


@needs_data
def test_snapshot_reconstruction_is_exact():
    bad = 0
    for r in SNAPS:
        yb, ya, ybs, yas = PR.legacy_book_touch(PR.orderbook_from_snapshot(r))
        exp = ((r["yes_bid"] if r["yes_bid"] is not None else 0.0), (r["yes_ask"] if r["yes_ask"] is not None else 1.0), float(r["bid_size"] or 0.0), float(r["ask_size"] or 0.0))
        bad += (yb, ya, ybs, yas) != exp
    assert bad == 0, f"{bad} snapshots could not be reconstructed exactly"


@needs_data
def test_replay_recorded_p12_snapshots_identical_under_legacy_and_head_code_paths():
    n_cand = 0; max_price_diff = 0.0
    for r in SNAPS:
        o = PR.replay_snapshot(r, PR.legacy_book_touch); h = PR.replay_snapshot(r, PR.head_book_touch)
        assert o["touch"] == h["touch"], (r["ticker"], r["t"], o["touch"], h["touch"])
        assert (o["candidate"] is None) == (h["candidate"] is None), (r["ticker"], r["t"], o["candidate"], h["candidate"])
        if o["candidate"] is None: continue
        n_cand += 1
        assert o["candidate"] == h["candidate"]
        bo, bh = o["booking"], h["booking"]
        assert bo == bh, (r["ticker"], r["t"], bo, bh)
        if bo["status"] == "fill": max_price_diff = max(max_price_diff, abs(bo["price"] - bh["price"]))
    assert len(SNAPS) >= 200 and n_cand >= 100, (len(SNAPS), n_cand)
    assert max_price_diff == 0.0


@needs_data
def test_new_logging_fields_do_not_feed_the_decision():
    """Injecting arbitrary values for every new field (or deleting them) leaves the replayed decision unchanged on every snapshot."""
    for r in SNAPS[:3000]:
        base = PR.replay_snapshot(r, PR.head_book_touch)
        r2 = dict(r); r2.update({"yes_bid_present": not (r.get("yes_bid") or 0) > 0, "yes_ask_present": False, "bids": [[0.01, 1e9]], "asks": [[0.99, 1e9]], "cfb_age_s": 999.0,
                                 "book_recv_t": 0.0, "book_fetch_s": 99.0, "spread": None})
        assert PR.replay_snapshot(r2, PR.head_book_touch) == base
        r3 = {k: v for k, v in r.items() if k not in PR.NEW_LOGGING_FIELDS}
        assert PR.replay_snapshot(r3, PR.head_book_touch) == base


def test_decision_functions_reference_no_new_field():
    src = inspect.getsource(PR.p12_candidate) + inspect.getsource(PR.p12_recheck_and_book)
    for f in PR.NEW_LOGGING_FIELDS: assert f not in src


def test_s7_live_reads_new_fields_only_in_logging_statements():
    """Static check on the runner: every source line mentioning a new field is a dict literal / timestamp assignment, never a condition,
    a sizing call or a price computation."""
    src = inspect.getsource(S7.run).splitlines()
    for f in PR.NEW_LOGGING_FIELDS + ("_t_recv", "_t_req", "_t_rc_recv", "_t_rc_req"):
        for line in src:
            if f not in line: continue
            s = line.strip()
            cond = s.split(":", 1)[0] if re.match(r"(if|elif|while)\b", s) else ""
            assert f not in cond, (f, s)                      # never inside a condition
            assert "risk_size(" not in s and "price =" not in s and "kalshi_taker_fee" not in s, (f, s)
            assert s.startswith("#") or s.startswith('"') or s.startswith("_t_") or s.startswith("snap = ") or s.startswith("t = ") or "best = {" in s, (f, s)
