"""CityConfig period helpers."""

from __future__ import annotations

import pytest

from dataio.cities import get_city


def test_cta_periods_cover_every_hour():
    cta = get_city("cta")
    for h in range(24):
        assert cta.period_for_hour(h)  # raises if uncovered


def test_cta_period_boundaries():
    cta = get_city("cta")
    assert cta.period_for_hour(6) == "am_peak"
    assert cta.period_for_hour(9) == "am_peak"
    assert cta.period_for_hour(10) == "midday"
    assert cta.period_for_hour(15) == "pm_peak"
    assert cta.period_for_hour(19) == "evening"
    assert cta.period_for_hour(22) == "late_night"
    assert cta.period_for_hour(2) == "late_night"  # wraps midnight


def test_unknown_city_raises():
    with pytest.raises(KeyError):
        get_city("nyc")
