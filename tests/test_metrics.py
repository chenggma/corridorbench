import math
import pytest
from corridorbench import metrics


def test_geh_zero_when_equal():
    assert metrics.geh(1000, 1000) == 0.0


def test_geh_both_zero():
    assert metrics.geh(0, 0) == 0.0


def test_geh_known_values():
    # sqrt(2*(M-C)^2/(M+C)); WisDOT TEOpS 16-20 asymmetry example:
    # target 250: modeled 325 -> ~4.4; modeled 175 -> ~5.1 (asymmetric).
    assert metrics.geh(325, 250) == pytest.approx(4.423, abs=1e-3)
    assert metrics.geh(175, 250) == pytest.approx(5.145, abs=1e-3)
    assert metrics.geh(1100, 1000) == pytest.approx(
        math.sqrt(2 * 100 ** 2 / 2100), abs=1e-9)


def test_geh_negative_raises():
    with pytest.raises(ValueError):
        metrics.geh(-1, 5)


def test_rnse_refuses():
    with pytest.raises(NotImplementedError):
        metrics.rnse()


def test_station_hour_geh_partial_hours_scale():
    # 6 of 12 bins observed at 100 veh/5min, sim 110 -> hourly like-for-like
    obs = {("s1", (5 - 4) * 3600 - 1800 + i * 300): 100.0 for i in range(6)}
    sim = {("s1", (5 - 4) * 3600 - 1800 + i * 300): 110.0 for i in range(6)}
    g = metrics.station_hour_geh(sim, obs, ["s1"], hours=[5])
    # scaled: M=110*6*2=1320, C=1200 -> geh(1320,1200)
    assert g[("s1", 5)] == pytest.approx(metrics.geh(1320, 1200), abs=1e-9)


def test_coverage():
    cov, n = metrics.coverage({("a", 5): 3.0, ("a", 6): 7.0})
    assert n == 2 and cov == 0.5
    assert metrics.coverage({}) == (None, 0)


def test_fhwa_sum_checks():
    t0 = (5 - 4) * 3600 - 1800
    obs = {("s1", t0 + i * 300): 100.0 for i in range(12)}
    sim = {("s1", t0 + i * 300): 100.0 for i in range(12)}
    g = metrics.station_hour_geh(sim, obs, ["s1"], hours=[5])
    out = metrics.fhwa_2004_criteria(g, sim, obs, ["s1"], hours=[5])
    assert out["A_geh5_85pct"][0] is True
    assert out["E_sum_within5pct"][0] is True
    assert out["F_sum_geh4"][0] is True
