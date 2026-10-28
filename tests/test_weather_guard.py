"""J1-v2 replaces the J1 obs-count guard (metar n <= local_hour + 2) with explicit timestamp filtering into the settlement day
(local standard time). These tests pin the behaviour the guard was approximating, now derived from timestamps only."""
import datetime as dt
from predhft import weather_lock as WL

def U(y, m, d, h=0, mi=0): return dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc).timestamp()

def test_prior_day_observations_cannot_lock_at_local_midnight():
    """The 3/165 wrong J1 locks: Austin/Dallas 'low' at 00:00 CDT Oct 27 scored with a window holding Oct 26's 24 obs (min 54 -> locked
    'low < 56' YES; actual Oct 27 min was higher). With the LST window Oct 27 begins 06:00Z; at 05:00Z there is nothing to lock on."""
    w = WL.settlement_window(dt.date(2026, 10, 27), "America/Chicago")
    prior_day = [(U(2026, 10, 26, h, 53), 54.0 if h == 11 else 70.0) for h in range(24)]
    obs = WL.filter_obs(prior_day, w, as_of=U(2026, 10, 27, 5, 0))
    met = WL.running_extremes(obs); assert met["n"] == 0 and met["min"] is None
    assert WL.established_value("low", None, met) == (None, 0, "nodata")
    assert WL.lock("low", "less", None, 56, None) is None

def test_same_day_observations_lock_when_consistent():
    w = WL.settlement_window(dt.date(2026, 10, 27), "America/Chicago")
    obs = WL.filter_obs([(U(2026, 10, 27, 6 + h, 53), 60.0 + h) for h in range(14)], w, as_of=U(2026, 10, 27, 20, 10))   # 00:53 .. 13:53 CST
    met = WL.running_extremes(obs); assert met["n"] == 14 and met["max"] == 73 and met["max2"] == 72
    v, slack, src = WL.established_value("high", None, met); assert (v, slack, src) == (73, 1, "metar")
    assert WL.lock("high", "greater", 70, None, v, slack) == 1.0
    assert WL.lock("high", "greater", 72, None, v, slack) is None     # 73 - slack 1 = 72, not >= 73
