# Bus Trip Observation Tool

A mobile web app for collecting ground-truth field data on CTA bus trips, to
validate and enrich the trajectory-reconstruction pipeline in this repo. A
rider opens the app on their phone, picks the bus they're boarding from live
CTA Bus Tracker arrivals, and the app records:

- **GPS pings** at ~1 Hz (≈30× denser than the CTA AVL feed),
- **accelerometer + gyro** at ~30–60 Hz,
- **annotated delay events** — bus stop dwell, waiting to exit a stop, red
  light, passenger disruption, driver change/holding, right/left turn delay,
  other — each with precise start/end timestamps and GPS, plus signal-color
  marks (red↔green transitions observed while stopped).

Everything is captured into IndexedDB on the phone (no connectivity needed
beyond the CTA lookups) and exported as CSVs at trip end. With server backup
enabled, the trip also autosaves to Cloudflare R2 every 60 s.

`pings_<trip_id>.csv` loads directly through
`src/bus_trajectories/io.py:load_avl_csv` — columns and the
`YYYY-MM-DD HH:MM:SS.ffffff` time format match the AVL archive CSVs, and the
CTA selection fills `trip_id` / `bus_id` (vehicle #) / `route_id` /
`pattern_id` so reconstruction works without modification.

## Layout

```
index.html / style.css      mobile UI: Setup → Recording → Summary
app.js                      controller, event state machine, rehydrate-on-load
storage.js                  IndexedDB (tripMeta, pings, motion batches, events)
sensors.js                  watchPosition, devicemotion (+ iOS permission), WakeLock
cta.js                      proxy client + offline nearby-stop search
export.js                   the one CSV/JSON builder (app, sync, desktop all use it)
sync.js                     60 s autosave; motion uploads as append-only chunks
trips.html / trips.js       desktop browser for saved trips (list + download)
functions/api/cta/…         Pages Function: CTA Bus Tracker proxy (key injection)
functions/api/trips/…       Pages Functions: R2 upload/download/list (token-gated)
data/cta_bus_stops.json     all CTA bus stops (offline nearby-stop search)
data/cta_pattern_stops.json per-pattern ordered stops + near-side flags
scripts/build_stops.py      rebuilds both data bundles from GTFS
scripts/build_all_intersections.py   all-CTA signalized-intersection dataset
wrangler.toml               Pages + R2 binding config
```

## Setup

### 1. CTA Bus Tracker API key

Apply at https://www.ctabustracker.com/home (free, 10k requests/day). The key
stays server-side; the phone only ever talks to the proxy.

### 2. Deploy to Cloudflare Pages

```bash
cd observation_tool
npx wrangler pages deploy .            # first run creates the project
```

Then in the Cloudflare dashboard (or via `wrangler pages secret put`):

- set **CTA_KEY** = your Bus Tracker key
- set **SYNC_TOKEN** = any long random string (gates trip upload/download)
- create an R2 bucket named **cta-observation-trips** and confirm the
  `TRIPS` binding from `wrangler.toml` is attached

Local dev: put both vars in `observation_tool/.dev.vars` (never commit it)
and run `npx wrangler pages dev .` — Miniflare simulates R2 locally.
Geolocation works on `localhost`; devicemotion is empty on desktop.

### 3. On the phone

Open the deployed HTTPS URL. Grant location when asked; iOS asks for motion
access on the **Start recording** tap. Keep the app open and the screen on
during the trip (a wake lock is held; the OS suspends all sensors if the tab
is backgrounded — if that happens, the recording resumes seamlessly when you
return, with a logged gap).

Enter the sync token once under "Server backup settings" to enable autosave.
Saved trips are browsable at `/trips.html` on a desktop.

## Rebuilding the data bundles

```bash
# stop bundles (downloads cta_gtfs.zip to the repo root if missing)
uv run python observation_tool/scripts/build_stops.py
```

Near-side flags (a stop with a signalized intersection ≤90 ft downstream —
where dwell vs. signal delay is ambiguous) come from the intersections
dataset. Until the all-CTA build below has run, only route 22's seven
patterns have flags; all other stops show "near-side?" (unknown) in the app.

### All-CTA signalized intersections (one-time, long)

Builds `cta_intersections_all.json` at the repo root — the same schema as
`intersections_route22.json`, consumable by `delay_decomposition` — for every
CTA bus shape (~763). Needs a Valhalla instance for map-matching (stage 1)
and Overpass (stage 2). Both stages checkpoint and **resume**: re-run the
same command after any failure and it picks up where it left off.

```bash
# machinery check on a couple of shapes first:
uv run python observation_tool/scripts/build_all_intersections.py \
    --valhalla http://localhost:8002 --shape-ids 67803936,67803939

# full run (hours; re-run freely to resume):
uv run python observation_tool/scripts/build_all_intersections.py \
    --valhalla http://localhost:8002

# then refresh the per-pattern near-side flags the app bundles:
uv run python observation_tool/scripts/build_stops.py
```

Notes:
- Overpass is queried in way-id chunks (default 300) with retry/backoff.
  `--transport auto` (default) falls back to a curl subprocess when
  overpass-api.de's TLS-fingerprint filter drops Python's client — observed
  on macOS; curl from the same machine is accepted.
- Stage-2 output for route 22 was verified to reproduce
  `intersections_route22.json` exactly (145/145 control points, identical
  near-side stop set).

## Data formats

- `pings_<trip_id>.csv` — `trip_id, bus_id, route_id, pattern_id,
  avl_event_time, latitude, longitude, accuracy_m, heading, speed_mps`
- `events_<trip_id>.csv` — `event_id, trip_id, type, note, parent_id,
  start_time, end_time, start_lat, start_lon, end_lat, end_lon, duration_s`.
  `type` ∈ bus_stop, exit_wait (child of a bus_stop via `parent_id`),
  red_light, passenger, driver_change, right_turn, left_turn, other.
- `signal_marks_<trip_id>.csv` — `event_id, trip_id, time, lat, lon, color`;
  the first mark per event is the color observed on stopping, each subsequent
  mark is an observed red↔green transition.
- `motion_<trip_id>.csv` — `timestamp, ax, ay, az, gx, gy, gz, interval_ms`.
- `trip_meta_<trip_id>.json` — CTA selection, observer, device, start/end,
  background gaps, counts.

In R2 a trip lives at `trips/<key>/…` where `<key>` is
`<start-epoch-ms>_<trip_id>`; snapshots are overwritten each autosave while
motion accumulates as `motion/0001.csv, 0002.csv, …` (header only in the
first chunk, so concatenation — or the trips.html "combined motion CSV"
button — yields one valid file).

## Known limitations

- Browsers cap GPS at ~1 Hz; there is no reliable background collection —
  the screen must stay on (wake lock held) with the app foregrounded.
- The near-side badge is only authoritative for patterns present in the
  intersections dataset; others show "near-side?".
- iOS re-requires a tap to re-enable devicemotion after a page reload; the
  app shows a one-tap pill when it detects motion isn't flowing.
