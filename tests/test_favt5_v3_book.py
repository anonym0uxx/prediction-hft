"""FavT5 v3 passive depth/trade recorder: sequence rule and tape format. No network."""
import gzip, json

from predhft import favt5_v3_book as book


def test_sequence_continues_only_on_plus_one():
    assert book.next_seq_ok(None, 7)            # first frame of a subscription
    assert book.next_seq_ok(7, 8)
    assert not book.next_seq_ok(7, 9)           # a lost delta
    assert not book.next_seq_ok(7, 7)           # a repeat is not a continuation either


def test_tape_keeps_every_message_in_arrival_order(tmp_path, monkeypatch):
    monkeypatch.setattr(book, "ROOT", tmp_path)
    t = book.Tape()
    rows = [{"r": 1794502785.1, "s": 1794502785140, "q": 1, "y": "orderbook_snapshot", "m": {"yes_dollars_fp": [["0.4000", "25.00"]]}},
            {"r": 1794502785.2, "s": 1794502785240, "q": 2, "y": "orderbook_delta", "m": {"price_dollars": "0.4100", "delta_fp": "10.00", "side": "yes"}},
            {"r": 1794502785.3, "s": 1794502785340, "q": 1, "y": "trade", "m": {"yes_price_dollars": "0.4100", "count_fp": "3.00"}}]
    for r in rows: t.write("KXBTC15M-TEST", r)
    t.close(["KXBTC15M-TEST"])
    f = tmp_path / "20261112" / "KXBTC15M-TEST.jsonl.gz"
    assert [json.loads(l) for l in gzip.open(f, "rt")] == rows


def test_recorder_is_btc_and_eth_only_and_has_no_order_code():
    assert set(book.SERIES) == {"KXBTC15M", "KXETH15M"}
    src = open(book.__file__, encoding="utf-8").read()
    assert "/portfolio/" not in src and "create_order" not in src
