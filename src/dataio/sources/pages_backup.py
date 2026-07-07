"""Record-a-Ride Pages backup API → canonical trace (network).

The recording app backs each trip's raw pings up to a Cloudflare Pages function
(``GET {base_url}/api/trips/{key}/pings.csv``) in the same column shape the phone
export uses. This adapter fetches that CSV and parses it like a phone trace.

Because it needs the live Pages endpoint (and, for private trips, a token), it is
not exercised by the offline test suite; ``analysis.comparison`` is the rich
source-provider that drives it in production.

spec: {"kind": "pages_backup", "base_url", "key", "pattern_id"?, "token"?}
"""

from __future__ import annotations

import io
import urllib.request

import pandas as pd

from dataio.gtfs import load_avl_csv


def _fetch_csv(url: str, token: str | None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "reconstruct-bus-trajectories"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted Pages URL)
        return resp.read().decode("utf-8")


def load(spec: dict) -> pd.DataFrame:
    base = str(spec["base_url"]).rstrip("/")
    url = f"{base}/api/trips/{spec['key']}/pings.csv"
    text = _fetch_csv(url, spec.get("token"))
    df = load_avl_csv(io.StringIO(text), drop_deadhead=spec.get("drop_deadhead", True))
    if "pattern_id" not in df.columns and spec.get("pattern_id"):
        df["pattern_id"] = str(spec["pattern_id"])
    return df
