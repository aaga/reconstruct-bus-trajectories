"""Hourly gauge-corrected rain (NOAA MRMS) cropped to a city's network.

Source: MRMS ``MultiSensor_QPE_01H_Pass2`` on the NOAA open-data bucket —
1-hour accumulations (mm) on a 0.01° grid (~1.1 x 0.8 km at Boston), radar
pattern bias-corrected to rain gauges, ~2 h after the hour. A file stamped
``YYYYMMDD-HH0000`` is valid AT that time and covers the hour ENDING then,
i.e. (HH-1:00, HH:00] UTC.

Checked 2026-10-05: 24 files/day across MBTA's archive window; the 24 hourly
values at the Logan cell for 2026-07-07 sum to 40.4 mm vs 39.9 mm on the Logan
gauge (GHCN-D) — timing, units and cell lookup line up (Logan's gauge likely
feeds the correction, so this is a sanity check, not independent accuracy).

Output: caches/rain/<city>/mrms_qpe01h_pass2.npz
    lat (ny,), lon (nx,)            cell centres, lon in [-180, 180)
    valid_utc (nh,) int64           hour-ending times, epoch seconds
    mm (nh, ny, nx) float32         NaN = no coverage / missing file

Usage:
    PYTHONPATH=src uv run python analysis/network/rain_mrms.py --city mbta
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402

BUCKET = "https://noaa-mrms-pds.s3.amazonaws.com/CONUS/MultiSensor_QPE_01H_Pass2_00.00"
MARGIN_DEG = 0.03  # pad the network bbox by ~3 km


def _url(t: pd.Timestamp) -> str:
    return (f"{BUCKET}/{t:%Y%m%d}/"
            f"MRMS_MultiSensor_QPE_01H_Pass2_00.00_{t:%Y%m%d-%H}0000.grib2.gz")


def _bbox(city) -> tuple[float, float, float, float]:
    reg = json.loads((REPO / "outputs" / "network" / city.city_id
                      / "segment_registry.json").read_text())
    pts = np.array([p for s in reg["segments"].values()
                    for p in s.get("geometry_lonlat", [])])
    lon0, lat0 = pts.min(axis=0) - MARGIN_DEG
    lon1, lat1 = pts.max(axis=0) + MARGIN_DEG
    return float(lat0), float(lat1), float(lon0), float(lon1)


def _download(t: pd.Timestamp, raw_dir: Path) -> Path | None:
    dst = raw_dir / f"{t:%Y%m%d-%H}.grib2.gz"
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    for attempt in range(3):
        try:
            with urllib.request.urlopen(_url(t), timeout=120) as r:
                data = r.read()
            dst.write_bytes(data)
            return dst
        except Exception:  # noqa: BLE001 — retry, then report missing
            time.sleep(2 * (attempt + 1))
    return None


def _crop(args) -> tuple[int, np.ndarray | None]:
    """Decode one GRIB2 file and return the bbox crop (ny, nx) or None."""
    i, path, bbox = args
    import eccodes

    lat0, lat1, lon0, lon1 = bbox
    try:
        raw = gzip.decompress(Path(path).read_bytes())
    except Exception:  # noqa: BLE001
        return i, None
    h = eccodes.codes_new_from_message(raw)
    try:
        ni = eccodes.codes_get(h, "Ni")
        nj = eccodes.codes_get(h, "Nj")
        la1 = eccodes.codes_get(h, "latitudeOfFirstGridPointInDegrees")
        lo1 = eccodes.codes_get(h, "longitudeOfFirstGridPointInDegrees")
        di = eccodes.codes_get(h, "iDirectionIncrementInDegrees")
        dj = eccodes.codes_get(h, "jDirectionIncrementInDegrees")
        jpos = eccodes.codes_get(h, "jScansPositively")
        vals = eccodes.codes_get_values(h).astype(np.float32).reshape(nj, ni)
    finally:
        eccodes.codes_release(h)
    lats = la1 + (np.arange(nj) * dj if jpos else -np.arange(nj) * dj)
    lons = ((lo1 + np.arange(ni) * di) + 180.0) % 360.0 - 180.0
    rows = np.flatnonzero((lats >= lat0) & (lats <= lat1))
    cols = np.flatnonzero((lons >= lon0) & (lons <= lon1))
    crop = vals[rows.min():rows.max() + 1, cols.min():cols.max() + 1].copy()
    crop[crop < 0] = np.nan  # MRMS flags no-coverage with negatives
    return i, crop


def _grid(bbox) -> tuple[np.ndarray, np.ndarray]:
    """Cell-centre coordinates of the crop (MRMS CONUS grid, 0.01°)."""
    lat0, lat1, lon0, lon1 = bbox
    la = 54.995 - np.arange(3500) * 0.01
    lo = (230.005 + np.arange(7000) * 0.01 + 180.0) % 360.0 - 180.0
    return (la[(la >= lat0) & (la <= lat1)],
            lo[(lo >= lon0) & (lo <= lon1)])


def build(city_id: str, workers: int = 8, delete_raw: bool = False) -> Path:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    days = sorted(json.loads((base / "date_attrs.json").read_text())["days"])
    # hour-ending stamps covering every service date (local 03:00 cutover, so
    # pad a day on both sides in UTC)
    t0 = pd.Timestamp(days[0], tz=city.tz).tz_convert("UTC").floor("D") - pd.Timedelta(hours=0)
    t1 = (pd.Timestamp(days[-1], tz=city.tz) + pd.Timedelta(days=2)).tz_convert("UTC").ceil("D")
    hours = pd.date_range(t0, t1, freq="h", inclusive="left")
    bbox = _bbox(city)
    out_dir = REPO / "caches" / "rain" / city.city_id
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    print(f"{city.city_id}: {len(hours):,} hours {hours[0]:%Y-%m-%d %H}Z..{hours[-1]:%Y-%m-%d %H}Z, "
          f"bbox lat {bbox[0]:.3f}..{bbox[1]:.3f} lon {bbox[2]:.3f}..{bbox[3]:.3f}", flush=True)

    t = time.time()
    with ThreadPoolExecutor(16) as ex:
        paths = list(ex.map(lambda h: _download(h, raw_dir), hours))
    n_miss = sum(p is None for p in paths)
    print(f"downloaded {len(hours) - n_miss:,} files ({n_miss} missing) in {time.time() - t:.0f}s", flush=True)

    lat, lon = _grid(bbox)
    mm = np.full((len(hours), len(lat), len(lon)), np.nan, dtype=np.float32)
    jobs = [(i, str(p), bbox) for i, p in enumerate(paths) if p is not None]
    t = time.time()
    with ProcessPoolExecutor(workers) as ex:
        for i, crop in ex.map(_crop, jobs, chunksize=16):
            if crop is not None and crop.shape == mm.shape[1:]:
                mm[i] = crop
    print(f"decoded+cropped in {time.time() - t:.0f}s; grid {len(lat)}x{len(lon)}", flush=True)

    out = out_dir / "mrms_qpe01h_pass2.npz"
    np.savez_compressed(out, lat=lat, lon=lon,
                        valid_utc=(hours.asi8 // 10**9).astype(np.int64), mm=mm)
    cover = np.isfinite(mm).all(axis=(1, 2)).mean()
    if delete_raw:  # the crop is the product; raw files are re-downloadable
        import shutil
        shutil.rmtree(raw_dir, ignore_errors=True)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB); hours fully covered {cover:.1%}; "
          f"hours with any rain >0.1 mm {np.nanmax(mm, axis=(1, 2)).__gt__(0.1).mean():.1%}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--delete-raw", action="store_true",
                    help="remove the downloaded GRIB files once cropped")
    a = ap.parse_args()
    build(a.city, a.workers, a.delete_raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
