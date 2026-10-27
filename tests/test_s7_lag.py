import json, urllib.request, time
from predhft.s7_lag import coinbase_minutes

def test_candle_keyed_by_bucket_end(monkeypatch):
    # synthetic candle: bucket starting at 1000 with close 2.0 -> spot at 1060 must be 2.0, spot at 1000 must be absent (or prior bucket close)
    class R:
        def __init__(self, b): self.b = b
        def read(self): return self.b
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=20: R(json.dumps([[1000, 1, 3, 1.5, 2.0, 10]]).encode()))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = coinbase_minutes(900, 1100)
    assert out.get(1060) == 2.0 and 1000 not in out
