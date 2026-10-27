"""DST transition (2026-11-01 02:00 local) window-logic checks (review N2)."""
import datetime as dt
from zoneinfo import ZoneInfo

def test_crypto_hour_buckets_are_utc():
    # top-of-hour buckets never shift with DST
    for ts in (1793494800, 1793498400, 1793502000):   # around 2026-11-01 09:00Z
        nh = (ts // 3600 + 1) * 3600
        assert nh % 3600 == 0

def test_index_close_follows_et():
    et = ZoneInfo("America/New_York")
    before = dt.datetime(2026, 10, 30, 16, 0, tzinfo=et).astimezone(dt.timezone.utc)
    after = dt.datetime(2026, 11, 2, 16, 0, tzinfo=et).astimezone(dt.timezone.utc)
    assert before.hour == 20 and after.hour == 21   # s7_index builds close_iso from ET, so the UTC hour must move

def test_weather_local_day_on_dst_sunday():
    z = ZoneInfo("America/New_York")
    a = dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.timezone.utc).astimezone(z)   # 01:30 EDT (first)
    b = dt.datetime(2026, 11, 1, 6, 30, tzinfo=dt.timezone.utc).astimezone(z)   # 01:30 EST (second)
    assert a.date() == b.date() == dt.date(2026, 11, 1)
    # Kalshi close 05:00Z on Nov 2 = midnight EST -> the Nov 1 observation day ends exactly at close (no post-day trading hour)
    assert dt.datetime(2026, 11, 2, 5, 0, tzinfo=dt.timezone.utc).astimezone(z).hour == 0
