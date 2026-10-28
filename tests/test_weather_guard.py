import datetime as dt
from predhft import weather_verify as W

def test_metar_lock_rejected_when_obs_count_exceeds_hours_elapsed():
    """3/165 wrong J1 locks: 'low' at local hour 0 with a 24-obs METAR window (prior day). Guard: n <= hour+2."""
    now0 = dt.datetime(2026, 10, 27, 0, 30)
    metar_bad = {"max": 79, "min": 54, "n": 24}
    ok = metar_bad["n"] <= now0.hour + 2
    assert not ok
    p, basis = W.verify("low", "less", None, 56, None, {"max": None, "min": None, "n": 0}, now0)
    assert not str(basis).startswith("locked_")

def test_metar_lock_allowed_when_consistent():
    now = dt.datetime(2026, 10, 27, 15, 10)
    metar = {"max": 79, "min": 54, "n": 15}
    assert metar["n"] <= now.hour + 2
    p, basis = W.verify("high", "greater", 70, None, None, metar, now)
    assert p == 1.0 and basis.startswith("locked_")
