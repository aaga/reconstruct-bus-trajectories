"""weather_effect: METAR snow parsing + segment midpoint."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.weather_effect import is_snow, midpoint  # noqa: E402


def test_is_snow_counts_falling_snow():
    for wx in ("-SN", "SN FZFG", "+SN", "-SN BR", "-RASN BR", "-FZDZSN BR", "SHSN", "-SG", "VCSHSN"):
        assert is_snow(wx), wx


def test_is_snow_rejects_non_snow():
    for wx in ("+TSGRRA BR", "-RA", "BR", "BLSN", "DRSN", "TSRA", None, "M"):
        assert not is_snow(wx), wx


def test_blowing_snow_with_falling_snow_still_counts():
    assert is_snow("-SN BLSN")


def test_midpoint():
    assert midpoint([[0.0, 0.0], [0.0, 2.0]]) == (0.0, 1.0)
