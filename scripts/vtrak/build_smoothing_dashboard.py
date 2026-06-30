"""Interactive multi-bandwidth viewer for the VTRAK time-space trajectories.

Renders one self-contained HTML page (Plotly via CDN) where you can:
  * pick one of the 3 vehicles (radio),
  * toggle the raw VTRAK pings and any of bw=5,10,15,20,25,30,40,50 (checkbox),
  * pan / scroll-zoom the time-space diagram,
  * see every bus stop on that route as a horizontal line.

Output: figures/smoothing_dashboard.html
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from build_vtrak_smooth import (VEH, ROOT, GTFS, load_r2, load_rocket,
                                pick_trip, best_shape)
from bus_trajectories.io import load_route_stops
from bus_trajectories.smooth import locreg_pchip, locreg_mqsi

BANDWIDTHS = [5, 10, 15, 20, 25, 30, 40, 50]
N_DENSE = 2500
OUT = ROOT / "figures" / "smoothing_dashboard.html"


def build_data():
    r2, rocket = load_r2(), load_rocket()
    out = {"order": list(VEH.keys()), "bandwidths": BANDWIDTHS, "vehicles": {}}
    for veh, (route, shapes) in VEH.items():
        g = r2[r2.vehicle_id == veh].copy()
        rk = rocket[rocket.VEH_ID.astype(str) == veh].copy()
        window = (rk.ts_utc.min(), rk.ts_utc.max())
        trip = pick_trip(g, window)
        sid, _, matcher = best_shape(trip, shapes)
        t0, t1 = trip.timestamp.min(), trip.timestamp.max()

        rk_win = rk[(rk.ts_utc >= t0) & (rk.ts_utc <= t1)].copy()
        # Drop the VTRAK 1 Hz "doublet" artifact: the feed publishes each ~0.5 Hz
        # GPS fix twice (the repeat carries an identical lat/lon 1 s later).
        # Collapse runs of consecutive identical (lat, lon) to their first row;
        # the time gap to the next distinct fix still encodes any dwell.
        n_before = len(rk_win)
        keep = ((rk_win.LATITUDE != rk_win.LATITUDE.shift())
                | (rk_win.LONGITUDE != rk_win.LONGITUDE.shift()))
        rk_win = rk_win[keep].copy()
        print(f"  dedup: {n_before} -> {len(rk_win)} pings "
              f"({n_before - len(rk_win)} consecutive-duplicate fixes dropped)")
        res = matcher.match(rk_win.LATITUDE.to_numpy(), rk_win.LONGITUDE.to_numpy())
        on = res.on_route
        tt = rk_win.ts_utc.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        t_sec = (tt - tt[0]).astype("timedelta64[ms]").astype(float) / 1000.0
        ts, ds = t_sec[on], res.dist_along_m[on]

        bw_curves = {}
        tdense = np.linspace(ts.min(), ts.max(), N_DENSE)
        tmin = np.round(tdense / 60.0, 4).tolist()
        for bw in BANDWIDTHS:
            sm = locreg_pchip(ts, ds, bandwidth=bw, degree=3)
            bw_curves[str(bw)] = {
                "t": tmin,
                "d": np.round(sm.f(tdense) / 1000.0, 5).tolist(),
            }
        # LOCREG-MQSI (monotone C^2 quintic) at bw=10 for the headline comparison.
        mq = locreg_mqsi(ts, ds, bandwidth=10, degree=3)
        mqsi_curve = {"t": tmin,
                      "d": np.round(mq.f(tdense) / 1000.0, 5).tolist()}

        stops = load_route_stops(GTFS, sid)
        stops_km = [{"name": s["name"], "d": round(s["dist_along_m"] / 1000.0, 5)}
                    for s in stops]

        c0 = pd.Timestamp(t0).tz_convert("America/Chicago")
        c1 = pd.Timestamp(t1).tz_convert("America/Chicago")
        label = f"{c0:%Y-%m-%d %H:%M}–{c1:%H:%M} America/Chicago"

        out["vehicles"][veh] = {
            "route": route,
            "trip": str(trip.trip_id.iloc[0]),
            "shape": sid,
            "label": label,
            "raw": {"t": np.round(ts / 60.0, 4).tolist(),
                    "d": np.round(ds / 1000.0, 5).tolist()},
            "bw": bw_curves,
            "mqsi10": mqsi_curve,
            "stops": stops_km,
        }
        print(f"VEH {veh} r{route} shape {sid}: {on.sum()} raw pings, "
              f"{len(stops_km)} stops")
    return out


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>VTRAK smoothing viewer</title>
<script src="https://cdn.plot.ly/plotly-3.5.0.min.js"></script>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         display: grid; grid-template-columns: 250px 1fr; height: 100vh; }}
  #controls {{ overflow-y: auto; padding: 16px; border-right: 1px solid #ddd;
               background: #fafafa; }}
  #controls h2 {{ font-size: 15px; margin: 0 0 6px; }}
  #controls h3 {{ font-size: 12px; text-transform: uppercase; color: #666;
                  margin: 18px 0 6px; letter-spacing: .04em; }}
  .row {{ display: flex; align-items: center; gap: 8px; margin: 3px 0;
          font-size: 14px; }}
  .swatch {{ width: 22px; height: 3px; border-radius: 2px; flex: none; }}
  #meta {{ font-size: 12px; color: #555; margin-top: 14px; line-height: 1.5; }}
  #plot {{ width: 100%; height: 100vh; }}
  label {{ cursor: pointer; }}
</style></head>
<body>
<div id="controls">
  <h2>Vehicle</h2>
  <div id="veh"></div>
  <h3>Series</h3>
  <div id="series"></div>
  <h3>Overlay</h3>
  <div class="row"><input type="checkbox" id="stops" checked>
    <label for="stops">bus stops (horizontal lines)</label></div>
  <div id="meta"></div>
</div>
<div id="plot"></div>
<script>
const DATA = {data_json};
const COLORS = {{
  raw:'#999999', '5':'#1f77b4','10':'#ff7f0e','15':'#2ca02c','20':'#d62728',
  '25':'#9467bd','30':'#8c564b','40':'#e377c2','50':'#17becf', mqsi:'#000000'
}};
let current = DATA.order[0];

// vehicle radios
const vehDiv = document.getElementById('veh');
DATA.order.forEach((v,i) => {{
  const r = document.createElement('div'); r.className='row';
  r.innerHTML = `<input type="radio" name="veh" id="veh_${{v}}" value="${{v}}" ${{i===0?'checked':''}}>`
    + `<label for="veh_${{v}}">${{v}} &mdash; Route ${{DATA.vehicles[v].route}}</label>`;
  vehDiv.appendChild(r);
}});

// series checkboxes (raw + each bandwidth)
const seriesDiv = document.getElementById('series');
function addCheck(id, labelText, color, checked) {{
  const row = document.createElement('div'); row.className='row';
  row.innerHTML = `<input type="checkbox" id="s_${{id}}" ${{checked?'checked':''}}>`
    + `<span class="swatch" style="background:${{color}}"></span>`
    + `<label for="s_${{id}}">${{labelText}}</label>`;
  seriesDiv.appendChild(row);
}}
addCheck('raw','raw VTRAK pings',COLORS.raw,true);
addCheck('mqsi10','LOCREG-MQSI bw=10 (C²)',COLORS.mqsi,true);
DATA.bandwidths.forEach(bw => addCheck('bw'+bw,'LOCREG-PCHIP bw='+bw,COLORS[String(bw)], bw===10));

function activeSeries() {{
  const s = [];
  if (document.getElementById('s_raw').checked) s.push('raw');
  DATA.bandwidths.forEach(bw => {{ if (document.getElementById('s_bw'+bw).checked) s.push(String(bw)); }});
  return s;
}}

function draw(preserve) {{
  const gd = document.getElementById('plot');
  let xr = null, yr = null;
  if (preserve && gd && gd.layout && gd.layout.xaxis) {{
    xr = gd.layout.xaxis.range ? gd.layout.xaxis.range.slice() : null;
    yr = gd.layout.yaxis.range ? gd.layout.yaxis.range.slice() : null;
  }}
  const v = DATA.vehicles[current];
  const traces = [];
  const sel = activeSeries();
  if (sel.includes('raw'))
    traces.push({{x:v.raw.t, y:v.raw.d, mode:'markers', type:'scattergl',
      name:'raw pings', marker:{{size:4, color:COLORS.raw, opacity:0.5}}}});
  DATA.bandwidths.forEach(bw => {{
    if (sel.includes(String(bw)))
      traces.push({{x:v.bw[bw].t, y:v.bw[bw].d, mode:'lines', type:'scattergl',
        name:'PCHIP bw='+bw, line:{{color:COLORS[String(bw)], width:2}}}});
  }});
  if (document.getElementById('s_mqsi10').checked)
    traces.push({{x:v.mqsi10.t, y:v.mqsi10.d, mode:'lines', type:'scattergl',
      name:'MQSI bw=10', line:{{color:COLORS.mqsi, width:2}}}});

  const shapes = [];
  if (document.getElementById('stops').checked)
    v.stops.forEach(s => shapes.push({{type:'line', xref:'paper', x0:0, x1:1,
      yref:'y', y0:s.d, y1:s.d, line:{{color:'rgba(120,120,120,0.35)', width:1}},
      layer:'below'}}));

  const layout = {{
    title: {{text:`Vehicle ${{current}} &mdash; Route ${{v.route}} &mdash; trip ${{v.trip}} `
      +`(shape ${{v.shape}})<br><sub>${{v.label}}</sub>`, font:{{size:15}}}},
    xaxis:{{title:'minutes since trip start', range: xr, autorange: xr ? false : true}},
    yaxis:{{title:'distance along route shape (km)', range: yr, autorange: yr ? false : true}},
    dragmode:'pan', hovermode:'closest', template:'plotly_white',
    margin:{{l:60,r:20,t:60,b:50}}, shapes:shapes, showlegend:true,
    legend:{{orientation:'h', y:-0.12}}
  }};
  Plotly.react('plot', traces, layout, {{scrollZoom:true, responsive:true,
    displaylogo:false}});
  document.getElementById('meta').innerHTML =
    `<b>Trip:</b> ${{v.trip}}<br><b>Shape:</b> ${{v.shape}}<br>`
    +`<b>Stops:</b> ${{v.stops.length}}<br><b>Raw pings:</b> ${{v.raw.t.length}}`;
}}

document.querySelectorAll('input').forEach(el => el.addEventListener('change', e => {{
  const vehChange = e.target.name === 'veh';
  if (vehChange) current = e.target.value;       // new trip -> reset view
  draw(!vehChange);                              // checkbox toggle -> keep view
}}));
draw(false);
</script>
</body></html>
"""


def main():
    data = build_data()
    html = HTML.format(data_json=json.dumps(data))
    OUT.write_text(html)
    size_mb = OUT.stat().st_size / 1e6
    print(f"-> wrote {OUT} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
