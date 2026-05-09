"""Interactive HTML visualizer for reconstructed trajectories.

Emits a self-contained HTML file with a Plotly time-space chart. Supports
pan, scroll-zoom, box-zoom, hover-readout of (time, mile, trip_id, bus_id),
and click-to-toggle each trip line. Loads plotly.js from CDN by default to
keep the file small (~tens of KB); pass ``embed_js=True`` for fully offline.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import plotly.graph_objects as go

from .pipeline import TripReconstruction

_M_PER_MI = 1609.344


def make_interactive_html(
    recons: dict[str, TripReconstruction],
    out_path: str | Path,
    title: str = "Reconstructed bus trajectories",
    n_eval: int = 1500,
    show_raw: bool = True,
    embed_js: bool = False,
) -> Path:
    """Render an interactive time-space chart for a set of reconstructions."""
    fig = go.Figure()

    items = sorted(recons.items(), key=lambda kv: kv[1].meta.first_ping)
    palette = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    ]

    for i, (trip_id, r) in enumerate(items):
        c = palette[i % len(palette)]
        f = r.smoothed.f

        # Smoothed line.
        ts = np.linspace(r.t[0], r.t[-1], n_eval)
        xs_mi = f(ts) / _M_PER_MI
        clock = r.meta.first_ping + np.array(
            [np.timedelta64(int(s * 1000), "ms") for s in ts]
        )
        v_mph = f.derivative()(ts) * 2.23694
        fig.add_trace(
            go.Scatter(
                x=clock,
                y=xs_mi,
                mode="lines",
                line={"color": c, "width": 1.6},
                name=f"{trip_id} bus {r.meta.bus_id}",
                legendgroup=trip_id,
                customdata=np.stack([np.full_like(v_mph, int(trip_id), dtype=object), v_mph], axis=1),
                hovertemplate=(
                    "<b>trip %{customdata[0]}</b><br>"
                    "time: %{x|%H:%M:%S}<br>"
                    "mile: %{y:.3f}<br>"
                    "speed: %{customdata[1]:.1f} mph"
                    "<extra></extra>"
                ),
            )
        )

        # Raw on-route pings as a toggleable scatter.
        if show_raw:
            raw_clock = r.meta.first_ping + np.array(
                [np.timedelta64(int(s * 1000), "ms") for s in r.t]
            )
            fig.add_trace(
                go.Scatter(
                    x=raw_clock,
                    y=r.d / _M_PER_MI,
                    mode="markers",
                    marker={"color": c, "size": 4, "opacity": 0.45},
                    name=f"{trip_id} raw",
                    legendgroup=trip_id,
                    showlegend=False,
                    hovertemplate=(
                        "<b>raw %{x|%H:%M:%S}</b><br>"
                        "mile: %{y:.3f}"
                        "<extra></extra>"
                    ),
                )
            )

    fig.update_layout(
        title=title,
        xaxis_title="Time of day",
        yaxis_title="Distance along route (mi)",
        hovermode="closest",
        legend={"groupclick": "togglegroup"},
        margin={"l": 60, "r": 30, "t": 60, "b": 50},
        template="plotly_white",
        height=720,
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(0,0,0,0.08)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(0,0,0,0.08)")

    out_path = Path(out_path)
    fig.write_html(
        out_path,
        include_plotlyjs=True if embed_js else "cdn",
        full_html=True,
        config={"scrollZoom": True, "displaylogo": False},
    )
    return out_path
