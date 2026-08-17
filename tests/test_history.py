"""Historical-era plumbing: feed selection and era→segment mapping."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.era_seg_bounds import _longest_chain  # noqa: E402
from analysis.network.gtfs_history import pick_for_date  # noqa: E402


def _v(sha, fetched, lo, hi):
    return {"sha1": sha, "fetched_at": f"{fetched}T00:00:00Z",
            "earliest_calendar_date": lo, "latest_calendar_date": hi}


def test_pick_for_date_takes_latest_live_feed():
    """Of the versions covering a date, the one CTA was publishing then."""
    vers = [
        _v("aaa", "2026-04-24", "2026-04-15", "2026-06-30"),
        _v("bbb", "2026-05-30", "2026-05-28", "2026-07-31"),
        # fetched AFTER the date: describes it, but wasn't live yet
        _v("ccc", "2026-07-10", "2026-05-01", "2026-09-30"),
    ]
    assert pick_for_date(vers, "2026-06-15")["sha1"] == "bbb"
    assert pick_for_date(vers, "2026-04-20")["sha1"] == "aaa"


def test_pick_for_date_falls_back_before_any_fetch():
    """A date preceding every covering fetch still resolves, not None."""
    vers = [_v("aaa", "2024-02-01", "2024-01-01", "2024-03-31")]
    assert pick_for_date(vers, "2024-01-10")["sha1"] == "aaa"
    assert pick_for_date(vers, "2030-01-01") is None


def test_longest_chain_rejects_cross_street_signal():
    """A node that forms no real segment with its neighbours is skipped.

    Proximity alone puts signals on the cross street inside the radius; the
    canonical segment set is what filters them out, so A__B survives even
    though the spurious N sits between A and B by distance.
    """
    pairs = {(1, 2): "SIG_1__SIG_2", (2, 3): "SIG_2__SIG_3"}
    nodes = [(1, 0.0), (99, 40.0), (2, 100.0), (3, 200.0)]
    chain = _longest_chain(nodes, pairs)
    assert [n for n, _ in chain] == [1, 2, 3]


def test_longest_chain_needs_two_linked_nodes():
    assert _longest_chain([(1, 0.0)], {}) == []
    assert _longest_chain([(1, 0.0), (7, 50.0)], {(1, 2): "x"}) == []


def test_longest_chain_skips_degenerate_spacing():
    """Back-to-back projections under MIN_SEG_M can't form a segment."""
    pairs = {(1, 2): "SIG_1__SIG_2"}
    assert _longest_chain([(1, 0.0), (2, 3.0)], pairs) == []
    got = _longest_chain([(1, 0.0), (2, 90.0)], pairs)
    assert [n for n, _ in got] == [1, 2]
