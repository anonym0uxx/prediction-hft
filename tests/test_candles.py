"""Unit tests for predhft.candles on synthetic data (no network)."""
import math
import numpy as np, pandas as pd, pytest
from predhft.candles import features_at, window_outcomes, minute_avg_proxy, FEATURE_NAMES, DIMENSIONS, _resample, _adx


def synth(n=3000, start=1_700_000_000 // 900 * 900, drift_bp=0.0, seed=0):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift_bp * 1e-4, 3e-4, n); c = 100.0 * np.exp(np.cumsum(r))
    o = np.r_[100.0, c[:-1]]; h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 1e-4, n))); l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 1e-4, n)))
    return pd.DataFrame({"ts": start + 60 * np.arange(n), "open": o, "high": h, "low": l, "close": c, "volume": rng.uniform(1, 10, n)})


def test_window_outcomes_label_matches_proxy():
    df = synth(); w = window_outcomes(df); d = df.set_index("ts")
    assert len(w) > 100 and set(w.y.unique()) <= {0, 1}
    r = w.iloc[5]
    assert math.isclose(r.avg_open, minute_avg_proxy(d, r.open_ts)) and math.isclose(r.avg_close, minute_avg_proxy(d, r.close_ts))
    assert r.y == int(r.avg_close > r.avg_open)
    assert minute_avg_proxy(d, r.open_ts) == pytest.approx(d.loc[r.open_ts - 60, ["open", "high", "low", "close"]].mean())


def test_no_lookahead():
    """Features at t must not change when any candle opening at or after t-60 is perturbed."""
    df = synth(); t = int(df.ts.iloc[2000]) // 900 * 900 + 840
    f0 = features_at(df, t, window_open=t - 840, ref_df=synth(seed=1), prior_outcomes=[1, 1, 0])
    df2 = df.copy(); fut = df2.ts >= t - 59; df2.loc[fut, ["open", "high", "low", "close"]] *= 1.05; df2.loc[fut, "volume"] *= 50
    f1 = features_at(df2, t, window_open=t - 840, ref_df=synth(seed=1), prior_outcomes=[1, 1, 0])
    for k in FEATURE_NAMES:
        a, b = f0[k], f1[k]
        assert (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b), k
    # and it DOES change when the last closed candle (opening at t-60) changes
    df3 = df.copy(); df3.loc[df3.ts == t - 60, "close"] *= 1.01
    assert features_at(df3, t, window_open=t - 840)["ret_1"] != pytest.approx(f0["ret_1"])


def test_feature_keys_and_dimensions():
    df = synth(); t = int(df.ts.iloc[2500]) // 900 * 900 + 600
    f = features_at(df, t, ref_df=synth(seed=2), prior_outcomes=[0, 0, 0])
    assert set(f) == set(FEATURE_NAMES)
    assert all(k in FEATURE_NAMES for v in DIMENSIONS.values() for k in v)
    assert f["mins_elapsed"] == 10.0 and f["streak3_sum"] == -3 and f["streak3_same"] == -1 and f["last_outcome"] == -1
    assert -4 <= f["align_score"] <= 4 and f["align_abs"] == abs(f["align_score"])
    assert 0 <= f["rsi14"] <= 100 and 0 <= f["range_pos_15"] <= 1 and f["trending"] in (0.0, 1.0)
    # without ref/prior the cross-asset and streak dims are NaN, nothing else breaks
    g = features_at(df, t)
    assert math.isnan(g["ref_ret_5"]) and math.isnan(g["streak3_sum"]) and not math.isnan(g["ret_5"])


def test_uptrend_is_detected_across_timeframes():
    df = synth(drift_bp=8.0, seed=3)      # strong, persistent drift: all timeframes should align up
    t = int(df.ts.iloc[-1]) // 900 * 900
    f = features_at(df, t)
    assert f["align_score"] == 4 and f["ema_cross"] == 1 and f["rsi14"] > 50 and f["ret_60"] > 0


def test_resample_and_adx_shapes():
    df = synth(); t = int(df.ts.iloc[-1]) + 60
    b5 = _resample(df, 300, t); assert abs(len(b5) - len(df) // 5) <= 1 and (b5.n == 5).all()
    assert b5.high.max() <= df.high.max() and b5.low.min() >= df.low.min()
    adx = _adx(b5, 14); assert 0 <= adx <= 100
