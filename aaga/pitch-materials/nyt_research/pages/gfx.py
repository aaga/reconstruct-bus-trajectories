"""SVG graphics for the pitch pages. Every function returns an SVG string; colours are CSS tokens defined in style.css."""
import json, html, pickle, numpy as np, pandas as pd
from pathlib import Path
R = Path(__file__).parent.parent; F = json.load(open(R / "fig.json")); TR = json.load(open(R / "fig_tremont.json")); RECS = json.load(open(R / "recs.json"))
esc = html.escape
COL = dict(ff="var(--c-ff)", stop="var(--c-stop)", near="url(#ns)", lights="var(--c-sig)", other="var(--c-oth)")
LAB = dict(ff="Moving", stop="Bus stops", near="Stop + red light", lights="Red lights", other="Other traffic")
def tt(h, cat, big, rows=(), sub=""):
    k = dict(ff="Moving at free flow", stop="Bus stop", near="Near-side stop (stop + light)", lights="Traffic light", other="Other traffic").get(cat, cat)
    c = dict(ff="freeflow", near="nearside", lights="signal").get(cat, cat)
    return f' class="hz" data-h="{esc(h)}" data-d="{esc(sub)}" data-c="{c}" data-k="{esc(k)}" data-v="{esc(big)}" data-r="{esc("|".join(f"{a}={b}" for a, b in rows))}"'
def svg(w, h, body, label, cls=""): return f'<svg class="{cls}" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">{body}</svg>'
def P(xs, ys): return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))

# ------------------------------------------------------------------ 1. the hour
def budget():
    W = 1000; g = []; c = F["budget"]["city"]; parts = [("ff", c["ff"]), ("stop", c["stop"]), ("near", c["nearside"]), ("lights", c["signal"]), ("other", c["other"])]
    x = 0; y = 34; h = 84
    g.append(f'<text class="g-lab" x="0" y="20">Citywide, an average hour on a bus · {c["mph"]:.1f} mph</text>')
    for k, v in parts:
        w = v * W; m = v * 60
        g.append(f'<rect x="{x + 1.5:.1f}" y="{y}" width="{w - 3:.1f}" height="{h}" rx="3" fill="{COL[k]}"{tt(LAB[k], k, f"{m:.0f} minutes of every hour", [("Share of bus time", f"{100 * v:.0f}%"), ("Bus-hours per day", f"{v * c["hours"]:,.0f}")], "All five boroughs")}/>')
        dk = " dk" if k == "other" else ""
        g.append(f'<text class="g-big{dk}" x="{x + w / 2:.1f}" y="{y + 44}" text-anchor="middle">{m:.0f}</text><text class="g-in{dk}" x="{x + w / 2:.1f}" y="{y + 64}" text-anchor="middle">min</text>')
        g.append(f'<text class="g-cap" x="{x + 3:.1f}" y="{y + h + 18}">{LAB[k]}</text>'); x += w
    y += h + 58; g.append(f'<text class="g-lab" x="0" y="{y - 12}">By borough</text>')
    for b in ("Manhattan", "Bronx", "Brooklyn", "Queens", "Staten Island"):
        q = F["budget"][b]; x = 150; ww = W - 150 - 86
        g.append(f'<text class="g-row" x="0" y="{y + 16}">{b}</text><text class="g-num" x="{W}" y="{y + 16}" text-anchor="end">{q["mph"]:.1f} mph</text>')
        for k, kk in (("ff", "ff"), ("stop", "stop"), ("near", "nearside"), ("lights", "signal"), ("other", "other")):
            w = q[kk] * ww; g.append(f'<rect x="{x + 1:.1f}" y="{y}" width="{max(w - 2, 1):.1f}" height="22" rx="2" fill="{COL[k]}"{tt(LAB[k], k, f"{q[kk] * 60:.0f} minutes of every hour", [("Share of bus time", f"{100 * q[kk]:.0f}%")], b)}/>')
            if w > 34: g.append(f'<text class="g-in{" dk" if k == "other" else ""}" x="{x + w / 2:.1f}" y="{y + 15.5}" text-anchor="middle">{q[kk] * 60:.0f}</text>')
            x += w
        y += 31
    return svg(W, y + 2, "".join(g), "Where an hour on a New York City bus goes, citywide and by borough")

# ------------------------------------------------------------------ 2. the map
def citymap(n_lab=12):
    M = pickle.load(open(R / "mapgeo.pkl", "rb")); X = pd.read_parquet(R / "X_bap.parquet").set_index("seg_id"); geo, hot = M["geo"], M["hot"]
    W = 1000; lon0, lon1, lat0, lat1 = -74.262, -73.695, 40.493, 40.917; k = W / ((lon1 - lon0) * np.cos(np.radians(40.7))); H = (lat1 - lat0) * k
    X_ = lambda lo: (lo - lon0) * np.cos(np.radians(40.7)) * k; Y_ = lambda la: (lat1 - la) * k
    lay = {"base": [], "plan": [], "planhot": [], "miss": []}
    for s, pts in geo.items():
        if X.highway[s] in ("motorway", "trunk", "motorway_link", "trunk_link") or pts[0][0] < -74.26: continue
        d = "M" + "L".join(f"{X_(a):.1f},{Y_(b):.1f}" for a, b in pts); h = s in hot; b = X.bap[s] > 0
        lay["planhot" if h and b else "miss" if h else "plan" if b else "base"].append(d)
    g = [f'<path class="m-base" d="{"".join(lay["base"])}"/><path class="m-plan" d="{"".join(lay["plan"])}"/><path class="m-planhot" d="{"".join(lay["planhot"])}"/><path class="m-miss" d="{"".join(lay["miss"])}"/>']
    for nm, x, y in (("MANHATTAN", -73.992, 40.80), ("THE BRONX", -73.87, 40.875), ("QUEENS", -73.80, 40.745), ("BROOKLYN", -73.955, 40.625), ("STATEN ISLAND", -74.15, 40.60)):
        g.append(f'<text class="m-boro" x="{X_(x):.0f}" y="{Y_(y):.0f}" text-anchor="middle">{nm}</text>')
    placed = []
    for i, r in enumerate(RECS[:n_lab]):
        q = X[(X.nm == r["street"]) & (X.borough == r["borough"])]; qh = q[q.index.isin(hot)]; qh = qh if len(qh) else q; w = qh.dh.values
        xs_, ys_ = qh.mid_lon.map(X_).values, qh.mid_lat.map(Y_).values; mx, my = np.average(xs_, weights=w), np.average(ys_, weights=w)
        dmin = np.array([min([np.hypot(a - px_, b - py_) for px_, py_ in placed] or [999]) for a, b in zip(xs_, ys_)])
        sc_ = np.minimum(dmin, 34) * 4 - np.hypot(xs_ - mx, ys_ - my) * .15; j = int(np.argmax(sc_)); cx, cy = xs_[j], ys_[j]; placed.append((cx, cy))
        g.append(f'<g{tt(r["street"], "other", f"{r["dh"]:.0f} bus-hours lost each weekday rush", [("Average speed", f"{r["mph"]:.1f} mph"), ("Suggested", ", ".join(SHORT["busway or protected lane" if (c == "bus lane" and (r["street"], r["borough"]) in EXIST) else c] for c in r["rec"]))], r["borough"] + " · not in the plan")}><circle class="m-dot" cx="{cx:.1f}" cy="{cy:.1f}" r="11"/><text class="m-num" x="{cx:.1f}" y="{cy + 4:.1f}" text-anchor="middle">{i + 1}</text></g>')
    g.append('<text class="g-lab" x="0" y="150">Most lost time outside the plan</text>')
    nice = lambda r: r["street"].replace("5th Avenue", "Fifth Avenue").replace("3rd Avenue", "Third Avenue").replace("1st Avenue", "First Avenue").replace("2nd Avenue", "Second Avenue") + (", Bronx" if r["street"] == "Broadway" else "")
    for i, r in enumerate(RECS[:n_lab]): g.append(f'<circle class="m-dot" cx="10" cy="{176 + i * 24}" r="9"/><text class="m-num" x="10" y="{180 + i * 24}" text-anchor="middle">{i + 1}</text><text class="g-row" x="27" y="{181 + i * 24}">{esc(nice(r))}</text><text class="g-ax" x="215" y="{181 + i * 24}" text-anchor="end">{r["dh"]:.0f} h</text>')
    g.append(f'<text class="g-ax" x="0" y="{176 + n_lab * 24 + 6}">bus-hours lost per weekday, rush hours</text>')
    return svg(W, int(H), "".join(g), "Map of New York City bus streets: the worst-delay miles inside and outside the Bus Action Plan corridors", "map")

# ------------------------------------------------------------------ 3. corridor cards (HTML)
ICON = {"bus lane": '<svg viewBox="0 0 22 14"><rect x="0" y="3" width="22" height="8" rx="1.5" fill="var(--c-lane)"/><text x="11" y="9.6" text-anchor="middle" font-size="5.4" font-weight="700" fill="#fff" font-family="Libre Franklin,sans-serif">BUS</text></svg>',
        "busway or protected lane": '<svg viewBox="0 0 22 14"><rect x="0" y="1" width="22" height="12" rx="1.5" fill="var(--c-lane)"/><text x="11" y="9.2" text-anchor="middle" font-size="5.4" font-weight="700" fill="#fff" font-family="Libre Franklin,sans-serif">ONLY</text></svg>',
        "boarding islands / far-side stops": '<svg viewBox="0 0 22 14"><rect x="1" y="3.5" width="20" height="7" rx="3.5" fill="var(--c-island)" stroke="var(--c-stop)" stroke-width="1.6"/></svg>',
        "signal priority": '<svg viewBox="0 0 9 21" style="width:8px;height:18px"><rect x=".5" y=".5" width="8" height="20" rx="3" fill="#1d1d1b" stroke="#fff"/><circle cx="4.5" cy="4.6" r="2" fill="#ff5a52"/><circle cx="4.5" cy="10.5" r="2" fill="#ffc233"/><circle cx="4.5" cy="16.4" r="2" fill="#35c96b"/></svg>'}
SHORT = {"bus lane": "Protected bus lane", "busway or protected lane": "Busway or physical protection", "boarding islands / far-side stops": "Boarding islands, far-side stops", "signal priority": "Signal priority"}
EXIST = {("5th Avenue", "Manhattan"): "Two painted bus lanes, 61st to 34th St, since 2018", ("Hillside Avenue", "Queens"): "Painted bus lanes, finished October 2025", ("3rd Avenue", "Manhattan"): "Painted bus lane on much of its length",
         ("Hylan Boulevard", "Staten Island"): "Rush-hour kerbside bus lanes", ("Nostrand Avenue", "Brooklyn"): "Select Bus Service lanes, part-time on some blocks", ("Archer Avenue", "Queens"): "Busway eastbound, 150th to 160th St",
         ("1st Avenue", "Manhattan"): "Select Bus Service lanes", ("2nd Avenue", "Manhattan"): "Select Bus Service lanes"}
def cards(n=12):
    out = []
    for i, r in enumerate(RECS[:n]):
        p = r["per"]; d = p["other"] + p["near"] + p["lights"] + p["stop"]; W = 300; x = 0; bar = []
        for k, kk in (("other", "other"), ("near", "near"), ("lights", "lights"), ("stop", "stop")):
            w = p[kk] / d * W; bar.append(f'<rect x="{x + 1:.1f}" y="0" width="{max(w - 2, 1):.1f}" height="16" rx="2" fill="{COL[k]}"{tt(LAB[k], k, f"{p[kk] / 60:.1f} min per mile, per bus", [("Share of the delay", f"{100 * p[kk] / d:.0f}%")], r["street"])}/>'); x += w
        w_ = r["worst"]; worst = f'Worst mile: {esc(w_["frm"])} to {esc(w_["to"])}, {w_["mph"]:.1f} mph' if w_ and w_["frm"] != w_["to"] and "mid-block" not in (w_["frm"] + w_["to"]) and w_["frm"] != r["street"] and w_["to"] != r["street"] else ""
        note = EXIST.get((r["street"], r["borough"]), ""); recs = ["busway or protected lane" if (c == "bus lane" and note) else c for c in r["rec"]]
        if note and not any(c.startswith("bus") for c in recs) and r["share"]["other"] >= 0.33: recs.insert(0, "busway or protected lane")
        chips = "".join(f'<span class="chip">{ICON[c]}{SHORT[c]}</span>' for c in recs)
        out.append(f'<article class="card"><div class="card-n">{i + 1}</div><h4>{esc(r["street"].replace("5th Avenue", "Fifth Avenue").replace("3rd Avenue", "Third Avenue").replace("1st Avenue", "First Avenue").replace("2nd Avenue", "Second Avenue"))}</h4><p class="card-b">{esc(r["borough"])} · {r["mph"]:.1f} mph at rush hour · {r["bpd"]:.0f} buses a day per direction</p>'
                   f'<p class="card-big"><b>{r["dh"]:.0f}</b> bus-hours lost each weekday, in rush hours</p><svg viewBox="0 0 {W} 16" preserveAspectRatio="none" class="card-bar" role="img" aria-label="Delay mix">{"".join(bar)}</svg><p class="card-m">{note + ". " if note else ""}{worst}</p><p class="card-t">Suggested treatment</p><div class="chips">{chips}</div></article>')
    return '<div class="cards">' + "".join(out) + "</div>"

# ------------------------------------------------------------------ 4. Tremont before / after
def tremont():
    W = 1000; ml = 44; ph = 150; g = []; y0 = 30
    for d, title, flip in (("dir1", "Westbound", True), ("dir0", "Eastbound", False)):
        T = TR["tremont_" + d]; xs = np.array(T["xs"]); L = xs.max(); eb, ea = np.array(T["before"]["e"]), np.array(T["after"]["e"]); ymax = 50
        X = (lambda x: ml + (L - x) / L * (W - ml - 6)) if flip else (lambda x: ml + x / L * (W - ml - 6)); Y = lambda v: y0 + ph - min(v, ymax) / ymax * ph
        b0, b1 = T["busway"]; tb, ta = T["before"], T["after"]
        m = (xs >= b0) & (xs <= b1); sb, sa = eb[m].sum() * 5 / 100, ea[m].sum() * 5 / 100
        g.append(f'<rect x="{min(X(b0), X(b1)):.1f}" y="{y0 - 6}" width="{abs(X(b1) - X(b0)):.1f}" height="{ph + 6}" class="t-band"/>')
        for t in (0, 20, 40): g.append(f'<line class="grid" x1="{ml}" x2="{W - 6}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/><text class="g-ax" x="{ml - 6}" y="{Y(t) + 3.5:.1f}" text-anchor="end">{t}</text>')
        px = [X(v) for v in xs]
        g.append(f'<path class="t-before" d="{P(px, [Y(v) for v in eb])} L{px[-1]:.1f},{Y(0):.1f} L{px[0]:.1f},{Y(0):.1f} Z"{tt("Before the busway", "other", f"{sb / 60:.1f} min of delay per bus inside the busway", [("Period", "18 May to 26 June"), ("End to end", f"{tb["total"] / 60:.1f} min")], title)}/>')
        g.append(f'<path class="t-after" d="{P(px, [Y(v) for v in ea])} L{px[-1]:.1f},{Y(0):.1f} L{px[0]:.1f},{Y(0):.1f} Z"{tt("With the busway", "lane", f"{sa / 60:.1f} min of delay per bus inside the busway", [("Period", "28 Sept to 20 Nov"), ("End to end", f"{ta["total"] / 60:.1f} min")], title)}/><path class="t-afterl" d="{P(px, [Y(v) for v in ea])}"/><path class="t-beforel" d="{P(px, [Y(v) for v in eb])}"/>')
        ys = y0 + ph + 4; g.append(f'<rect x="{ml}" y="{ys}" width="{W - ml - 6}" height="13" fill="var(--c-road)"/><rect x="{min(X(b0), X(b1)):.1f}" y="{ys + 2}" width="{abs(X(b1) - X(b0)):.1f}" height="9" fill="var(--c-lane)"/><text class="t-lanetx" x="{(X(b0) + X(b1)) / 2:.1f}" y="{ys + 9.6}" text-anchor="middle">BUSWAY</text>')
        for x, kd in T["sig"]:
            if kd == "sig": g.append(f'<rect x="{X(x) - 1:.1f}" y="{ys}" width="2" height="13" fill="var(--c-xwalk)"/>')
        seen = -99
        for x, lab in zip(T["ends"], T["labels"]):
            if any(k in lab for k in ("Webster", "Third", "Southern", "Belmont", "Crotona Avenue", "Park Avenue", "Boston", "Arthur")) and abs(X(x) - seen) > 70: g.append(f'<text class="g-ax" x="{X(x):.1f}" y="{ys + 27}" text-anchor="middle">{esc(lab.replace(" Avenue", " Av").replace(" Boulevard", " Blvd"))}</text>'); seen = X(x)
        arrow = "←" if flip else "→"; g.append(f'<text class="g-lab" x="{ml}" y="{y0 - 12}">{title} {arrow}</text>')
        xm = (X(b0) + X(b1)) / 2; g.append(f'<text class="t-call" x="{xm:.1f}" y="{y0 + 12}" text-anchor="middle">{100 * (sa - sb) / sb:+.0f}% delay</text><text class="g-ax" x="{xm:.1f}" y="{y0 + 27}" text-anchor="middle">{sb / 60:.1f} → {sa / 60:.1f} min per bus</text>')
        y0 += ph + 78
    g.append(f'<text class="g-ax" x="{ml}" y="{y0 - 22}">Seconds of delay per 100 feet of street. Dashed line: before (18 May to 26 June). Filled: after (28 Sept to 20 Nov).</text>')
    return svg(W, y0 - 10, "".join(g), "Delay along East Tremont Avenue before and after the busway, both directions")

# ------------------------------------------------------------------ 5. 42nd Street on match days
def worldcup():
    W = 1000; H = 380; ml, mr, mt, mb = 44, 150, 26, 40; w = F["wc"]; hs = [h for h in w["h"] if 6 <= h <= 22]; ix = [w["h"].index(h) for h in hs]
    X = lambda h: ml + (h - 6) / 16 * (W - ml - mr); Y = lambda v: mt + (6.6 - v) / (6.6 - 3.6) * (H - mt - mb); g = []
    g.append(f'<rect x="{X(11):.1f}" y="{mt}" width="{X(20) - X(11):.1f}" height="{H - mt - mb}" class="t-band"/><text class="g-cap" x="{(X(11) + X(20)) / 2:.1f}" y="{mt + 16}" text-anchor="middle">42nd Street closed to cars on all three match days</text>')
    for t in (4, 5, 6): g.append(f'<line class="grid" x1="{ml}" x2="{W - mr}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/><text class="g-ax" x="{ml - 6}" y="{Y(t) + 3.5:.1f}" text-anchor="end">{t} mph</text>')
    for h in range(6, 23, 2): g.append(f'<text class="g-ax" x="{X(h):.1f}" y="{H - mb + 16}" text-anchor="middle">{(h - 1) % 12 + 1}{"am" if h < 12 else "pm"}</text>')
    mu = np.array([w["mean"][i] for i in ix]); sd = np.array([w["sd"][i] for i in ix]); px = [X(h) for h in hs]
    g.append(f'<path class="w-band" d="{P(px, [Y(v) for v in mu + sd])} L{" L".join(f"{x:.1f},{Y(v):.1f}" for x, v in zip(px[::-1], (mu - sd)[::-1]))} Z"/><path class="w-base" d="{P(px, [Y(v) for v in mu])}"/>')
    D = np.array([[w["days"][d][i] for i in ix] for d in w["days"]], dtype=float); mm = np.nanmean(D, 0)
    for row in D: g.append(f'<path class="w-day" d="{P(px, [Y(v) for v in row])}"/>')
    g.append(f'<path class="w-match" d="{P(px, [Y(v) for v in mm])}"/>')
    for h, a, b in zip(hs, mm, mu): g.append(f'<circle cx="{X(h):.1f}" cy="{Y(a):.1f}" r="9" fill="transparent"{tt(f"{(h - 1) % 12 + 1}{"am" if h < 12 else "pm"}", "lane", f"{a:.1f} mph on match days", [("Ordinary weekday", f"{b:.1f} mph"), ("Difference", f"{100 * (a / b - 1):+.0f}%")], "M42 on 42nd Street")}/>')
    g.append(f'<text class="w-lm" x="{W - mr + 8}" y="{Y(mm[-1]) - 6:.1f}">Match days</text><text class="g-ax" x="{W - mr + 8}" y="{Y(mm[-1]) + 8:.1f}">16, 25, 30 June</text><text class="w-lb" x="{W - mr + 8}" y="{Y(mu[-1]) + 22:.1f}">Ordinary weekdays</text><text class="g-ax" x="{W - mr + 8}" y="{Y(mu[-1]) + 36:.1f}">average of {w["nbase"]} days</text>')
    return svg(W, H, "".join(g), "M42 bus speed by hour on World Cup match days compared with ordinary weekdays")

# ------------------------------------------------------------------ 6. crosstown streets
def crosstown():
    rows = sorted(F["crosstown"], key=lambda r: r["ff"] + r["stops"] + r["lights"] + r["other"]); W = 1000; ml = 205; mr = 70; y = 30; g = []; mx = 14.0; sc = (W - ml - mr) / mx
    note = {14: "busway since 2019", 34: "bus lanes", 42: "bus lanes, part", 23: "bus lanes", 125: "bus lanes, part"}
    for r in rows:
        x = ml; nm = f'{r["st"]}th St'.replace("23th", "23rd").replace("42th", "42nd").replace("72th", "72nd")
        g.append(f'<text class="g-row{" b" if r["st"] == 14 else ""}" x="0" y="{y + 16}">{nm}</text><text class="g-ax" x="76" y="{y + 16}">{note.get(r["st"], "")}</text>')
        for k, kk in (("ff", "ff"), ("stop", "stops"), ("lights", "lights"), ("other", "other")):
            v = r[kk] / 60; w = v * sc; lab = "Bus stops (incl. stop + light)" if k == "stop" else LAB[k]
            g.append(f'<rect x="{x + 1:.1f}" y="{y}" width="{w - 2:.1f}" height="24" rx="2" fill="{COL[k]}"{tt(lab, k, f"{v:.1f} min per mile", [("Bus speed", f"{r["mph"]:.1f} mph")], nm)}/>')
            if k == "other": g.append(f'<text class="g-in dk" x="{x + w / 2:.1f}" y="{y + 16.5}" text-anchor="middle">{v:.1f}</text>')
            x += w
        g.append(f'<text class="g-num" x="{x + 8:.1f}" y="{y + 16.5}">{r["mph"]:.1f} mph</text>'); y += 33
    for t in range(0, 15, 2): g.append(f'<text class="g-ax" x="{ml + t * sc:.1f}" y="{y + 10}" text-anchor="middle">{t}{" minutes per mile" if t == 0 else ""}</text>')
    return svg(W, y + 16, "".join(g), "Minutes per mile on Manhattan crosstown streets, split into moving, stops, lights and other traffic")

# ------------------------------------------------------------------ 7. scoreboard
def scoreboard(wc_in=None, merged=None):
    """merged: travel-time change for the Tremont busway with both directions combined; also renames the 42nd St row to 'A temporary busway'"""
    if wc_in is None: wc_in = json.load(open(R / "fig_wcall.json"))["closure"]["time_pct"]
    rows = [("A busway", "East Tremont Av, westbound", -25.0, None, "lane"), ("A busway", "East Tremont Av, eastbound", -11.7, None, "lane"), ("Cars banned for a day", "42nd St on World Cup match days, noon to 8pm", wc_in, None, "lane"),
            ("A centre bus lane", "Broadway, Queens, eastbound (Q70), from early June", -11.0, None, "lane"),
            ("A second bus lane", "Madison Av, 25th to 42nd St", -4.7, None, "lane"), ("Lane moved off the kerb", "Lexington Av, 60th to 52nd St", -3.0, None, "lane"), ("Signal priority", "212 lights switched on, May to November", F["tsp_tt"]["mean"], 1.96 * F["tsp_tt"]["se"], "signal"),
            ("Camera fines", "22 routes, staggered start dates", 0.36, 0.25, "other")]
    if merged is not None: rows = [("A busway", "East Tremont Av, the Bronx, both directions", merged, None, "lane"), ("A temporary busway", "42nd St on World Cup match days, noon to 8pm", wc_in, None, "lane")] + rows[3:]
    W = 1000; ml = 400; mr = 60; y = 34; g = []; X = lambda v: ml + (v + 28) / 32 * (W - ml - mr)
    for t in (-25, -20, -15, -10, -5, 0): g.append(f'<line class="grid" x1="{X(t):.1f}" x2="{X(t):.1f}" y1="14" y2="{34 + 46 * len(rows) - 8}"/><text class="g-ax" x="{X(t):.1f}" y="10" text-anchor="middle">{t}%</text>')
    for a, b, v, ci, c in rows:
        g.append(f'<text class="g-row b" x="0" y="{y + 12}">{a}</text><text class="g-ax" x="0" y="{y + 28}">{b}</text>')
        col = {"lane": "var(--c-lane)", "signal": "var(--c-sig)", "other": "var(--fg2)"}[c]; x0, x1 = sorted((X(0), X(v)))
        g.append(f'<rect x="{x0:.1f}" y="{y + 4}" width="{max(x1 - x0, 2):.1f}" height="22" rx="2" fill="{col}"{tt(a, c if c != "other" else "x", f"{v:+.1f}% bus travel time", [("Where", b)] + ([("95% range", f"{v - ci:+.1f}% to {v + ci:+.1f}%")] if ci else []), "Compared with similar streets")}/>')
        if ci: g.append(f'<line class="s-ci" x1="{X(v - ci):.1f}" x2="{X(v + ci):.1f}" y1="{y + 15}" y2="{y + 15}"/>')
        g.append(f'<text class="g-num" x="{(X(v) - 8) if v < -1 else (X(max(v, 0) + (ci or 0)) + 8):.1f}" y="{y + 20}" text-anchor="{"end" if v < -1 else "start"}">{v:+.1f}%</text>'); y += 46
    g.append(f'<line class="s-zero" x1="{X(0):.1f}" x2="{X(0):.1f}" y1="14" y2="{y - 8}"/>')
    return svg(W, y, "".join(g), "Change in bus travel time after seven kinds of intervention")

# ------------------------------------------------------------------ 8. where the stop sits
def placement():
    A = F["align"]; off = np.array(A["off"]); W = 1000; pw = 312; gap = 32; ph = 170; mt = 58; g = []; ymax = 22
    panels = [("none", "No bus stop", "A light on its own", "lights"), ("far", "Stop after the light", "Far-side stop", "stop"), ("near", "Stop before the light", "Near-side stop", "near")]
    base = np.array(A["none"]["y"])
    for i, (k, t1, t2, cat) in enumerate(panels):
        x0 = i * (pw + gap); X = lambda o: x0 + (o + 300) / 550 * pw; Y = lambda v: mt + ph - min(v, ymax) / ymax * ph; y = np.array(A[k]["y"]); px = [X(o) for o in off]
        g.append(f'<text class="g-row b" x="{x0}" y="16">{t1}</text><text class="g-ax" x="{x0}" y="32">{A[k]["n"]:,} intersections</text>')
        for t in (0, 10, 20): g.append(f'<line class="grid" x1="{x0}" x2="{x0 + pw}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>' + (f'<text class="g-ax" x="{x0 + 2}" y="{Y(t) - 3:.1f}">{t}{" seconds per 100 ft" if t == 20 else ""}</text>' if i == 0 and t else ""))
        if k != "none":
            g.append(f'<path d="{P(px, [Y(v) for v in y])} L{" L".join(f"{a:.1f},{Y(v):.1f}" for a, v in zip(px[::-1], np.minimum(base, y)[::-1]))} Z" fill="{COL[cat]}"{tt(t2, cat, f"{A[k]["area"] - A["none"]["area"]:.0f} seconds more than a light alone", [("Total within 300 ft", f"{A[k]["area"]:.0f} s per bus")], "Average of all such intersections")}/>')
        g.append(f'<path d="{P(px, [Y(v) for v in np.minimum(base, y)])} L{px[-1]:.1f},{Y(0):.1f} L{px[0]:.1f},{Y(0):.1f} Z" fill="var(--c-sig)" opacity="{1 if k == "none" else .55}"{tt("The light itself", "lights", f"{A["none"]["area"]:.0f} seconds per bus", [], "Average with no stop nearby")}/><path class="a-line" d="{P(px, [Y(v) for v in y])}"/>')
        ys = mt + ph + 6; g.append(f'<rect x="{x0}" y="{ys}" width="{pw}" height="14" fill="var(--c-road)"/><rect x="{X(0) - 1.5:.1f}" y="{ys}" width="3" height="14" fill="var(--c-xwalk)"/>')
        g.append(f'<rect x="{X(0) - 3.4:.1f}" y="{ys - 3}" width="6.8" height="20" rx="2.6" fill="#1d1d1b" stroke="#fff" stroke-width=".8"/><circle cx="{X(0):.1f}" cy="{ys + 1.5}" r="1.8" fill="#ff5a52"/><circle cx="{X(0):.1f}" cy="{ys + 7}" r="1.8" fill="#ffc233"/><circle cx="{X(0):.1f}" cy="{ys + 12.5}" r="1.8" fill="#35c96b"/>')
        if k == "far": g.append(f'<rect x="{X(60):.1f}" y="{ys + 2.5}" width="{X(140) - X(60):.1f}" height="9" rx="4.5" fill="var(--c-island)" stroke="var(--c-stop)" stroke-width="1.5"/>')
        if k == "near": g.append(f'<rect x="{X(-110):.1f}" y="{ys + 2.5}" width="{X(-30) - X(-110):.1f}" height="9" rx="4.5" fill="var(--c-island)" stroke="var(--c-stop)" stroke-width="1.5"/>')
        g.append(f'<text class="g-ax" x="{x0}" y="{ys + 30}">300 ft before</text><text class="g-ax" x="{x0 + pw}" y="{ys + 30}" text-anchor="end">250 ft after →</text>')
        g.append(f'<text class="a-big" x="{x0 + pw}" y="22" text-anchor="end">{A[k]["area"]:.0f} s</text><text class="g-ax" x="{x0 + pw}" y="38" text-anchor="end">lost per bus</text>')
    return svg(W, mt + ph + 44, "".join(g), "Average delay around an intersection with no stop, a far-side stop and a near-side stop")

# ------------------------------------------------------------------ 9. signal priority
def tsp():
    W = 1000; g = []; pw = 312; gap = 32; ph = 130; mt = 46; S = [F["tsp_series"][i] for i in (0, 1, 2)]
    nice = {"Jamaica Av & Springfield Blvd": "Springfield Blvd at Jamaica Av, Queens", "Metcalf Avenue & Soundview Avenue & Watson Avenue": "Watson Av at Metcalf Av, Bronx", "Forest Av & Fairview Av": "Forest Av at Fairview Av, Queens"}
    for i, s in enumerate(S):
        x0 = i * (pw + gap); wk = pd.to_datetime(s["weeks"]); t = (wk - pd.Timestamp("2026-04-27")).days.values; sec = np.array(s["sec"]); ymax = np.ceil(sec.max() / 10) * 10 + 12
        X = lambda d: x0 + d / 203 * pw; Y = lambda v: mt + ph - v / ymax * ph; da = (pd.Timestamp(s["d"]) - pd.Timestamp("2026-04-27")).days
        g.append(f'<text class="g-row b" x="{x0}" y="14">{esc(nice.get(s["nm"], s["nm"]))}</text><text class="g-ax" x="{x0}" y="30">seconds lost at the light, per bus</text>')
        for v in (0, 20, 40, 60):
            if v <= ymax: g.append(f'<line class="grid" x1="{x0}" x2="{x0 + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/><text class="g-ax" x="{x0 + 2}" y="{Y(v) - 3:.1f}">{v}</text>')
        g.append(f'<line class="p-act" x1="{X(da):.1f}" x2="{X(da):.1f}" y1="{mt}" y2="{mt + ph}"/><text class="g-cap" x="{X(da) - 5:.1f}" y="{mt + ph - 6}" text-anchor="end">priority on, {pd.Timestamp(s["d"]).strftime("%-d %b")}</text>')
        pre, post = sec[t < da - 3].mean(), sec[t > da + 9].mean()
        g.append(f'<line class="p-mean" x1="{X(0):.1f}" x2="{X(da):.1f}" y1="{Y(pre):.1f}" y2="{Y(pre):.1f}"/><line class="p-mean" x1="{X(da + 10):.1f}" x2="{X(203):.1f}" y1="{Y(post):.1f}" y2="{Y(post):.1f}"/>')
        g.append(f'<path class="p-line" d="{P([X(d) for d in t], [Y(v) for v in sec])}"/>')
        for d, v, w_ in zip(t, sec, wk): g.append(f'<circle class="p-dot" cx="{X(d):.1f}" cy="{Y(v):.1f}" r="2.6"{tt("Week of " + w_.strftime("%-d %B"), "lights", f"{v:.0f} s per bus", [], nice.get(s["nm"], s["nm"]))}/>')
        g.append(f'<text class="a-big sm" x="{x0 + pw}" y="{Y(post) - 8:.1f}" text-anchor="end">{100 * (post / pre - 1):+.0f}%</text>')
        for m_, lab in ((4, "May"), (65, "July"), (127, "Sept"), (188, "Nov")): g.append(f'<text class="g-ax" x="{X(m_):.1f}" y="{mt + ph + 15}">{lab}</text>')
    # all lights: strip of changes
    y0 = mt + ph + 58; h = F["tsp_hist"]; g.append(f'<text class="g-row b" x="0" y="{y0}">And every other light: change in delay after priority was switched on, compared with nearby lights</text>')
    X = lambda v: 60 + (np.clip(v, -12, 12) + 12) / 24 * (W - 120); rng = np.random.default_rng(3)
    for j, (k, lab, cat) in enumerate((("signal", "No bus stop at the light", "lights"), ("nearside", "Bus stop just before the light", "near"))):
        yy = y0 + 44 + j * 56; v = np.array(h[k]); g.append(f'<text class="g-ax" x="0" y="{yy - 18}">{lab} · {len(v)} lights · average {v.mean():+.1f} s per bus · {100 * (v < 0).mean():.0f}% improved</text>')
        for a in v: g.append(f'<circle cx="{X(a):.1f}" cy="{yy + rng.uniform(-9, 9):.1f}" r="3.2" fill="{COL[cat] if cat != "near" else "var(--c-stop)"}" opacity=".55"/>')
        g.append(f'<line class="s-zero" x1="{X(0):.1f}" x2="{X(0):.1f}" y1="{yy - 13}" y2="{yy + 13}"/><line class="p-avg" x1="{X(v.mean()):.1f}" x2="{X(v.mean()):.1f}" y1="{yy - 13}" y2="{yy + 13}"/>')
    yy += 30
    for t_ in (-12, -8, -4, 0, 4, 8, 12): g.append(f'<text class="g-ax" x="{X(t_):.1f}" y="{yy}" text-anchor="middle">{"−" if t_ < 0 else "+" if t_ > 0 else ""}{abs(t_)}{" s" if t_ == 0 else ""}{" or more, faster" if t_ == -12 else " or more, slower" if t_ == 12 else ""}</text>')
    return svg(W, yy + 8, "".join(g), "Delay at three intersections before and after signal priority, and the change at every other light")

# ------------------------------------------------------------------ 10. twin streets
def twins():
    W = 1000; g = []; y0 = 26; ph = 96; ymax = 60; k = (W - 8) / (4.8 * 5280)
    for key in ("ocean", "coney"):
        T = F["twin_" + key]; xs = np.array(T["xs"]); e = np.array(T["e"]); bg = np.array(T["bg"]); X = lambda x: 4 + x * k; Y = lambda v: y0 + ph - min(v, ymax) / ymax * ph; px = [X(v) for v in xs]
        g.append(f'<text class="g-row b" x="0" y="{y0 - 8}">{T["name"]}</text><text class="g-ax" x="{len(T["name"]) * 8.3 + 10:.0f}" y="{y0 - 8}">{T["route"]} · {T["mph"]:.1f} mph · {T["sig_mi"]:.0f} lights and {T["stop_mi"]:.0f} stops per mile · {T["lanes"]:.0f} lanes each way · {T["bpd"]:.0f} buses a day</text>')
        zz = [z for z in T["zones"] if z["cat"] in ZC and xs[z["ib"]] - xs[z["ia"]] >= 15]; draw_soft(g, xs, e, bg, [(xs[z["ia"]], xs[z["ib"]], z["cat"]) for z in zz], X, Y, ymax, line=False)
        g.append(f'<path class="a-line thin" d="{P(px, [Y(v) for v in e])}"/><line class="grid" x1="4" x2="{X(xs[-1]):.1f}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>')
        g.append(f'<text class="g-ax" x="{X(xs[-1]) + 8:.1f}" y="{y0 + ph - 30}">per mile: {T["lights"] / 60:.1f} min at lights,</text><text class="g-ax" x="{X(xs[-1]) + 8:.1f}" y="{y0 + ph - 15}">{T["other"] / 60:.1f} in traffic, {T["stops"] / 60:.1f} at stops</text>' if key == "ocean" else "")
        if key == "coney": g.append(f'<text class="g-ax" x="{X(xs[-1]) - 4:.1f}" y="{y0 - 8}" text-anchor="end">per mile: {T["lights"] / 60:.1f} min at lights, {T["other"] / 60:.1f} in traffic, {T["stops"] / 60:.1f} at stops</text>')
        y0 += ph + 50
    for m in range(0, 5): g.append(f'<text class="g-ax" x="{4 + m * 5280 * k:.1f}" y="{y0 - 34}">{m} mi</text>')
    return svg(W, y0 - 26, "".join(g), "Delay curves along Ocean Avenue and Coney Island Avenue in Brooklyn")

# ------------------------------------------------------------------ 11. weekday vs Sunday
def sunday():
    W = 1000; ml = 150; g = []; y = 8; d = F["daytype"]; sc = (W - ml - 90) / 480
    for k, lab in (("weekday", "Weekdays"), ("sun", "Sundays")):
        r = d[k]; x = ml; g.append(f'<text class="g-row b" x="0" y="{y + 18}">{lab}</text>')
        for c, kk in (("ff", "ff"), ("stop", "stops"), ("lights", "lights"), ("other", "other")):
            w = r[kk] * sc; g.append(f'<rect x="{x + 1:.1f}" y="{y}" width="{w - 2:.1f}" height="28" rx="2" fill="{COL[c]}"{tt(LAB[c] if c != "stop" else "Bus stops (incl. stop + light)", c, f"{r[kk] / 60:.1f} min per mile", [], lab)}/><text class="g-in{" dk" if c == "other" else ""}" x="{x + w / 2:.1f}" y="{y + 18.5}" text-anchor="middle">{r[kk] / 60:.1f}</text>'); x += w
        g.append(f'<text class="g-num" x="{x + 8:.1f}" y="{y + 18.5}">{r["mph"]:.1f} mph</text>'); y += 38
    g.append(f'<text class="g-ax" x="{ml}" y="{y + 8}">minutes per mile</text>')
    return svg(W, y + 14, "".join(g), "Minutes per mile on weekdays and Sundays")
def legend(keys=("ff", "stop", "near", "lights", "other")):
    if "near" in keys:      # the blend comes after the two colours it is made of
        keys = [k for k in keys if k != "near"]; keys.insert(max([keys.index(k) for k in ("stop", "lights") if k in keys], default=-1) + 1, "near")
    return '<div class="legend">' + "".join(f'<span><svg width="14" height="11"><rect width="14" height="11" rx="2" fill="{COL[k]}"/></svg>{LAB[k]}</span>' for k in keys) + "</div>"

# ------------------------------------------------------------------ 12. Fifth Avenue strip
def fifth():
    T = json.load(open(R / "fig_fifth.json")); W = 1000; ml = 40; ph = 130; y0 = 44; xs = np.array(T["xs"]); e = np.array(T["e"]); bg = np.array(T["bg"]); ymax = 30
    X = lambda x: ml + x / xs[-1] * (W - ml - 4); Y = lambda v: y0 + ph - min(v, ymax) / ymax * ph; px = [X(v) for v in xs]; g = []; p = T["per"]
    for t in (0, 10, 20, 30): g.append(f'<line class="grid" x1="{ml}" x2="{W - 4}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/><text class="g-ax" x="{ml - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{t}</text>')
    oth = e.sum() * 5 / 100 - sum(z["sec"] for z in T["zones"] if z["cat"] != "other")
    zz = [z for z in T["zones"] if z["cat"] in ZC and xs[z["ib"]] - xs[z["ia"]] >= 15]
    draw_soft(g, xs, e, bg, [(xs[z["ia"]], xs[z["ib"]], z["cat"]) for z in zz], X, Y, ymax, tips=[tt(LAB[ZC[z["cat"]]], ZC[z["cat"]], f"{z["sec"]:.0f} seconds per bus", [], "Fifth Avenue") for z in zz], line=False)
    g.append(f'<rect x="{ml}" y="{y0}" width="{W - ml - 4}" height="{ph}" fill="transparent"{tt("Other traffic", "other", f"{oth / 60:.1f} minutes per bus", [("Share of the delay", f"{100 * oth / (e.sum() * 5 / 100):.0f}%")], "Fifth Avenue, 61st to 34th Street")} style="pointer-events:none"/>')
    g.append(f'<path class="a-line thin" d="{P(px, [Y(v) for v in e])}"/>'); ys = y0 + ph + 5
    g.append(f'<rect x="{ml}" y="{ys}" width="{W - ml - 4}" height="13" fill="var(--c-road)"/>')
    for x, lab in T["ends"]:
        g.append(f'<rect x="{X(x) - 1:.1f}" y="{ys}" width="2" height="13" fill="var(--c-xwalk)"/>')
        n = "".join(ch for ch in lab if ch.isdigit())
        if n in ("59", "57", "50", "42", "34", "47", "53", "38"): g.append(f'<text class="g-ax" x="{min(X(x), W - 4):.1f}" y="{ys + 28}" text-anchor="{"end" if X(x) > W - 30 else "middle"}">{n}th St</text>'.replace("42th", "42nd").replace("53th", "53rd"))
    g.append(f'<text class="g-lab" x="{ml}" y="16">Southbound →</text><text class="g-ax" x="{ml}" y="32">seconds of delay per 100 feet</text>')
    return svg(W, ys + 36, "".join(g), "Delay along Fifth Avenue from 61st to 34th Street")

# ------------------------------------------------------------------ 13. the five other match days, aligned on the start of the closure
def worldcup5():
    D5 = json.load(open(R / "fig_wc5.json")); W = 1000; H = 360; ml, mr, mt, mb = 52, 150, 26, 44; lo, hi = -6, 13
    X = lambda t: ml + (t - lo) / (hi - lo) * (W - ml - mr); Y = lambda v: mt + (45 - v) / 70 * (H - mt - mb); g = []
    g.append(f'<rect x="{X(0):.1f}" y="{mt}" width="{X(10) - X(0):.1f}" height="{H - mt - mb}" class="t-band"/><text class="g-cap" x="{(X(0) + X(10)) / 2:.1f}" y="{mt + 16}" text-anchor="middle">42nd Street closed to cars</text>')
    for v in (-20, 0, 20, 40): g.append(f'<line class="{"s-zero" if v == 0 else "grid"}" x1="{ml}" x2="{W - mr}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/><text class="g-ax" x="{ml - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:+d}%</text>'.replace("+0%", "usual"))
    for t in range(-6, 13, 3): g.append(f'<text class="g-ax" x="{X(t):.1f}" y="{H - mb + 16}" text-anchor="middle">{"closure starts" if t == 0 else f"{abs(t)} h {"before" if t < 0 else "in"}"}</text>')
    acc = {}; names = {"2026-06-13": "Sat 13 June", "2026-06-22": "Mon 22 June", "2026-06-27": "Sat 27 June", "2026-07-05": "Sun 5 July", "2026-07-19": "Sun 19 July"}
    for d, o in D5.items():
        pts = [(h - o["start"], r, m, b) for h, r, m, b in zip(o["h"], o["rel"], o["mph"], o["base"]) if lo <= h - o["start"] <= 12 and h <= o["end"]]
        g.append(f'<path class="w-day" d="{P([X(t) for t, *_ in pts], [Y(max(min(r, 45), -25)) for _, r, *_ in pts])}"/>')
        for t, r, m, b in pts: acc.setdefault(t, []).append(r)
    ts = sorted(t for t in acc if len(acc[t]) >= 3); mm = [float(np.mean(acc[t])) for t in ts]
    g.append(f'<path class="w-match" d="{P([X(t) for t in ts], [Y(v) for v in mm])}"/>')
    for t, v in zip(ts, mm): g.append(f'<circle cx="{X(t):.1f}" cy="{Y(v):.1f}" r="9" fill="transparent"{tt(("closure starts" if t == 0 else f"{abs(t)} h {"before" if t < 0 else "into"} the closure"), "lane", f"{v:+.0f}% against the usual speed", [("Days averaged", str(len(acc[t])))], "M42, five match days")}/>')
    g.append(f'<text class="w-lm" x="{W - mr + 8}" y="{Y(mm[-1]) - 4:.1f}">Average of</text><text class="w-lm" x="{W - mr + 8}" y="{Y(mm[-1]) + 12:.1f}">five match days</text><text class="g-ax" x="{W - mr + 8}" y="{Y(mm[-1]) + 28:.1f}">thin lines: each day</text>')
    return svg(W, H, "".join(g), "M42 speed relative to the usual speed for that weekday and hour, on five World Cup match days, aligned on the start of the street closure")

# ================================================================== revised graphics (blob disaggregation)
F3 = json.load(open(R / "fig3.json")); ZC = {"signal": "lights", "nearside": "near", "stop": "stop"}
def _strip(g, xs, e, bg, zones, zsec, X, Y, ymax, sub):
    px = [X(v) for v in xs]; ee = np.minimum(e, ymax)
    g.append(f'<path d="{P(px, [Y(v) for v in ee])} L{px[-1]:.1f},{Y(0):.1f} L{px[0]:.1f},{Y(0):.1f} Z" fill="var(--c-oth)" class="soft"/>')
    for z, s_ in zip(zones, zsec):
        if z["cat"] not in ZC: continue
        a, b = z["ia"], z["ib"]; c = ZC[z["cat"]]
        g.append(f'<path d="{P(px[a:b + 1], [Y(v) for v in ee[a:b + 1]])} L{" L".join(f"{x:.1f},{Y(min(v, ymax)):.1f}" for x, v in zip(px[a:b + 1][::-1], bg[a:b + 1][::-1]))} Z" fill="{COL[c]}"{tt(LAB[c], c, f"{s_:.0f} seconds per bus", [], sub)}/>')
    g.append(f'<path class="a-line thin" d="{P(px, [Y(v) for v in ee])}"/>')
def tremont2():
    W = 1000; ml = 44; ph = 96; g = []; y0 = 34; ymax = 50
    for d, title, flip in (("dir1", "Westbound", True), ("dir0", "Eastbound", False)):
        T = F3["tremont_" + d]; xs = np.array(T["xs"]); L_ = xs.max(); b0, b1 = T["busway"]
        X = (lambda x: ml + (L_ - x) / L_ * (W - ml - 6)) if flip else (lambda x: ml + x / L_ * (W - ml - 6))
        g.append(f'<text class="g-lab" x="{ml}" y="{y0 - 14}">{title} {"←" if flip else "→"}</text>')
        for tag, lab in (("before", "Before"), ("after", "With the busway")):
            Y = lambda v, y0=y0: y0 + ph - v / ymax * ph; e = np.array(T[tag]["e"]); bg = np.array(T[tag]["bg"])
            g.append(f'<rect x="{min(X(b0), X(b1)):.1f}" y="{y0}" width="{abs(X(b1) - X(b0)):.1f}" height="{ph}" class="t-band"/>')
            for t in (0, 20, 40): g.append(f'<line class="grid" x1="{ml}" x2="{W - 6}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/><text class="g-ax" x="{ml - 6}" y="{Y(t) + 3.5:.1f}" text-anchor="end">{t}</text>')
            _strip(g, xs, e, bg, T["zones"], T[tag]["zsec"], X, Y, ymax, f"{title}, {lab.lower()}"); g.append(f'<text class="g-cap" x="{ml + 6}" y="{y0 + 14}">{lab}</text>'); y0 += ph + 10
        ys = y0 - 4; g.append(f'<rect x="{ml}" y="{ys}" width="{W - ml - 6}" height="13" fill="var(--c-road)"/><rect x="{min(X(b0), X(b1)):.1f}" y="{ys + 2}" width="{abs(X(b1) - X(b0)):.1f}" height="9" fill="var(--c-lane)"/><text class="t-lanetx" x="{(X(b0) + X(b1)) / 2:.1f}" y="{ys + 9.6}" text-anchor="middle">BUSWAY</text>')
        for x, kd in T["sig"]:
            if kd == "sig": g.append(f'<rect x="{X(x) - 1:.1f}" y="{ys}" width="2" height="13" fill="var(--c-xwalk)"/>')
        seen = -99
        for x, lab in zip(T["ends"], T["labels"]):
            if any(k in lab for k in ("Webster", "Third", "Southern", "Belmont", "Crotona Avenue", "Park Avenue", "Boston", "Arthur")) and abs(X(x) - seen) > 70: g.append(f'<text class="g-ax" x="{X(x):.1f}" y="{ys + 27}" text-anchor="middle">{esc(lab.replace(" Avenue", " Av").replace(" Boulevard", " Blvd"))}</text>'); seen = X(x)
        # bars: seconds per bus inside the busway, by type
        y0 = ys + 52; g.append(f'<text class="g-cap" x="{ml}" y="{y0 - 6}">Inside the busway, seconds of delay per bus</text>'); sc = (W - ml - 220) / 380
        for tag, lab in (("before", "Before"), ("after", "With the busway")):
            c = T[tag]["inside"]; x = ml + 140; g.append(f'<text class="g-row" x="{ml}" y="{y0 + 15}">{lab}</text>')
            for k, kk in (("other", "other"), ("near", "nearside"), ("stop", "stop"), ("lights", "signal")):
                w = c[kk] * sc; ch = 100 * (T["after"]["inside"][kk] / T["before"]["inside"][kk] - 1)
                g.append(f'<rect x="{x + 1:.1f}" y="{y0}" width="{max(w - 2, 1):.1f}" height="22" rx="2" fill="{COL[k]}"{tt(LAB[k], k, f"{c[kk]:.0f} seconds per bus", [("Before to after", f"{ch:+.0f}%")], f"{title}, {lab.lower()}")}/>')
                if w > 30: g.append(f'<text class="g-in{" dk" if k == "other" else ""}" x="{x + w / 2:.1f}" y="{y0 + 15.5}" text-anchor="middle">{c[kk]:.0f}</text>')
                x += w
            g.append(f'<text class="g-num" x="{x + 8:.1f}" y="{y0 + 15.5}">{T[tag]["inside_total"]:.0f} s</text>'); y0 += 28
        b, a = T["before"]["inside"], T["after"]["inside"]
        g.append(f'<text class="g-ax" x="{ml + 140}" y="{y0 + 10}">Other traffic {100 * (a["other"] / b["other"] - 1):+.0f}% · stop + red light {100 * (a["nearside"] / b["nearside"] - 1):+.0f}% · bus stops {100 * (a["stop"] / b["stop"] - 1):+.0f}% · red lights {100 * (a["signal"] / b["signal"] - 1):+.0f}%</text>'); y0 += 64
    return svg(W, y0 - 40, "".join(g), "Delay along East Tremont Avenue before and after the busway, split into other traffic, stops and lights")
def tsp5():
    W = 1000; n = 5; gap = 18; pw = (W - gap * (n - 1)) / n; ph = 150; mt = 96; g = []
    nice = {"Jamaica Av & Springfield Blvd": ("Springfield Blvd", "at Jamaica Av, Queens"), "Forest Avenue & Green Avenue": ("Forest Av", "at Greene Av, Queens"), "Metcalf Avenue & Soundview Avenue & Watson Avenue": ("Watson Av", "at Metcalf Av, Bronx"),
            "Forest Av & Fairview Av": ("Forest Av", "at Fairview Av, Queens"), "Lafayette Ave & Rosedale Ave": ("Lafayette Av", "at Rosedale Av, Bronx")}
    for i, s in enumerate(F3["tsp5"]):
        x0 = i * (pw + gap); off = np.array(s["off"]); m = (off >= -450) & (off <= 150); o = off[m]; eb = np.array(s["before"]["e"])[m]; ea = np.array(s["after"]["e"])[m]; ba = np.array(s["after"]["bg"])[m]
        ymax = max(20, np.ceil(max(eb.max(), ea.max()) / 10) * 10); X = lambda v: x0 + (v + 450) / 600 * pw; Y = lambda v: mt + ph - min(v, ymax) / ymax * ph; px = [X(v) for v in o]
        zs = [z for z in s["zones"] if z["cat"] in ("signal", "nearside") and -300 <= z["apex"] <= 30]; bsec, asec = sum(z["before"] for z in zs), sum(z["after"] for z in zs); t1, t2 = nice.get(s["nm"], (s["nm"], ""))
        g.append(f'<text class="g-row b" x="{x0}" y="14">{esc(t1)}</text><text class="g-ax" x="{x0}" y="29">{esc(t2)}</text><text class="a-big sm" x="{x0}" y="54">{bsec:.0f} → {asec:.0f} s</text><text class="g-ax" x="{x0}" y="69">at the light, per bus</text>')
        for t in (0, ymax / 2, ymax): g.append(f'<line class="grid" x1="{x0}" x2="{x0 + pw}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>' + (f'<text class="g-ax" x="{x0 + 2}" y="{Y(t) - 3:.1f}">{t:.0f}</text>' if t else ""))
        g.append(f'<path d="{P(px, [Y(v) for v in ea])} L{px[-1]:.1f},{Y(0):.1f} L{px[0]:.1f},{Y(0):.1f} Z" fill="var(--c-oth)" class="soft"/>')
        for z in s["zones"]:
            if z["cat"] not in ZC: continue
            k = (o >= z["a"]) & (o <= z["b"])
            if k.sum() < 2: continue
            g.append(f'<path d="{P(np.array(px)[k], [Y(v) for v in ea[k]])} L{" L".join(f"{x:.1f},{Y(v):.1f}" for x, v in zip(np.array(px)[k][::-1], ba[k][::-1]))} Z" fill="{COL[ZC[z["cat"]]]}"{tt(LAB[ZC[z["cat"]]], ZC[z["cat"]], f"{z["after"]:.0f} seconds per bus after", [("Before", f"{z["before"]:.0f} s")], t1 + " " + t2)}/>')
        g.append(f'<path class="a-line thin" d="{P(px, [Y(v) for v in ea])}"/><path class="t-beforel" d="{P(px, [Y(v) for v in eb])}"/>')
        ys = mt + ph + 5; g.append(f'<rect x="{x0}" y="{ys}" width="{pw}" height="12" fill="var(--c-road)"/>')
        for v in s["sig"]:
            if -450 <= v <= 150: g.append(f'<rect x="{X(v) - 1:.1f}" y="{ys}" width="2" height="12" fill="var(--c-xwalk)"/>')
        for v, _ in s["stops"]:
            if -430 <= v <= 130: g.append(f'<rect x="{X(v) - 9:.1f}" y="{ys + 2}" width="18" height="8" rx="4" fill="var(--c-island)" stroke="var(--c-stop)" stroke-width="1.3"/>')
        g.append(f'<rect x="{X(0) - 3.2:.1f}" y="{ys - 3}" width="6.4" height="18" rx="2.4" fill="#1d1d1b" stroke="#fff" stroke-width=".8"/><circle cx="{X(0):.1f}" cy="{ys + 1}" r="1.6" fill="#ff5a52"/><circle cx="{X(0):.1f}" cy="{ys + 6}" r="1.6" fill="#ffc233"/><circle cx="{X(0):.1f}" cy="{ys + 11}" r="1.6" fill="#35c96b"/>')
        g.append(f'<text class="g-ax" x="{x0}" y="{ys + 28}">450 ft before</text><text class="g-ax" x="{x0 + pw}" y="{ys + 28}" text-anchor="end">→</text><text class="g-ax" x="{x0}" y="{ys + 44}">on {pd.Timestamp(s["d"]).strftime("%-d %b")}</text>')
    return svg(W, mt + ph + 54, "".join(g), "Delay curve approaching five traffic lights, four weeks before and four weeks after signal priority was switched on")
def placement2():
    A = F3["place"]; off = np.array(A["off"]); W = 1000; pw = 312; gap = 32; ph = 170; mt = 58; g = []; ymax = 20; sl = (off >= -300) & (off <= 250)
    panels = [("none", "No bus stop", ["signal", "nearside", "stop"]), ("far", "Stop after the light", ["signal", "nearside", "stop"]), ("near", "Stop before the light", ["signal", "nearside", "stop"])]
    for i, (k, t1, cats) in enumerate(panels):
        x0 = i * (pw + gap); X = lambda o: x0 + (o + 300) / 550 * pw; Y = lambda v: mt + ph - min(v, ymax) / ymax * ph; px = [X(o) for o in off[sl]]; tot = sum(A[k][c + "_s"] for c in cats)
        g.append(f'<text class="g-row b" x="{x0}" y="16">{t1}</text><text class="g-ax" x="{x0}" y="32">{A[k]["n"]:,} intersections</text><text class="a-big" x="{x0 + pw}" y="22" text-anchor="end">{tot:.0f} s</text><text class="g-ax" x="{x0 + pw}" y="38" text-anchor="end">lost per bus</text>')
        for t in (0, 5, 10, 15, 20): g.append(f'<line class="grid" x1="{x0}" x2="{x0 + pw}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>' + (f'<text class="g-ax" x="{x0 + 2}" y="{Y(t) - 3:.1f}">{t}</text>' if i == 0 and t else ""))
        base = np.zeros(sl.sum())
        for c in cats:
            y = np.array(A[k][c])[sl]; top = base + y; cc = ZC[c]; lab = {"signal": "The light", "stop": "The stop", "nearside": "Stop and light together"}[c]
            if y.max() > 0.05: g.append(f'<path d="{P(px, [Y(v) for v in top])} L{" L".join(f"{a:.1f},{Y(v):.1f}" for a, v in zip(px[::-1], base[::-1]))} Z" fill="{COL[cc]}"{tt(lab, cc, f"{A[k][c + "_s"]:.0f} seconds per bus", [], t1)}/>')
            base = top
        g.append(f'<path class="a-line" d="{P(px, [Y(v) for v in base])}"/>')
        ys = mt + ph + 6; g.append(f'<rect x="{x0}" y="{ys}" width="{pw}" height="14" fill="var(--c-road)"/><rect x="{X(0) - 1.5:.1f}" y="{ys}" width="3" height="14" fill="var(--c-xwalk)"/>')
        g.append(f'<rect x="{X(0) - 3.4:.1f}" y="{ys - 3}" width="6.8" height="20" rx="2.6" fill="#1d1d1b" stroke="#fff" stroke-width=".8"/><circle cx="{X(0):.1f}" cy="{ys + 1.5}" r="1.8" fill="#ff5a52"/><circle cx="{X(0):.1f}" cy="{ys + 7}" r="1.8" fill="#ffc233"/><circle cx="{X(0):.1f}" cy="{ys + 12.5}" r="1.8" fill="#35c96b"/>')
        if k == "far": g.append(f'<rect x="{X(60):.1f}" y="{ys + 2.5}" width="{X(140) - X(60):.1f}" height="9" rx="4.5" fill="var(--c-island)" stroke="var(--c-stop)" stroke-width="1.5"/>')
        if k == "near": g.append(f'<rect x="{X(-110):.1f}" y="{ys + 2.5}" width="{X(-30) - X(-110):.1f}" height="9" rx="4.5" fill="var(--c-island)" stroke="var(--c-stop)" stroke-width="1.5"/>')
        g.append(f'<text class="g-ax" x="{x0}" y="{ys + 30}">300 ft before</text><text class="g-ax" x="{x0 + pw}" y="{ys + 30}" text-anchor="end">250 ft after →</text>')
        parts = " + ".join(f'{A[k][c + "_s"]:.0f} s {"light" if c == "signal" else "stop" if c == "stop" else "stop and light"}' for c in cats if A[k][c + "_s"] >= 0.5)
        g.append(f'<text class="g-ax" x="{x0}" y="48">{parts}</text>')
    return svg(W, mt + ph + 44, "".join(g), "Average delay credited to the light and the stop around an intersection, with other traffic removed")

# ================================================================== smooth, indicative rendering of delay types
from scipy.ndimage import gaussian_filter1d as _gf
def _sstep(t): t = np.clip(t, 0, 1); return t * t * (3 - 2 * t)
def soft_layers(xs, e, bg, zones, taper=45.0, sig=120.0):
    """smooth floor + one lens-shaped blob per zone: the blob's lower edge curves up to meet the curve at its ends, so there are no vertical walls.
    zones: list of (x_start, x_end, cat). Returns floor, [(cat, mask, lower_edge)]"""
    step = float(np.median(np.diff(xs))); f = np.minimum(np.asarray(bg, float), e); out = []      # the baseline is already a smooth hill (eng.floor)
    zs = sorted([z for z in zones if z[2] in ZC and z[1] - z[0] >= 3 * step]); 
    for i, (a, b, cat) in enumerate(zs):
        # taper only the free ends; where two blobs touch they meet along a short vertical at the trough between them
        la = i > 0 and a - zs[i - 1][1] <= 2 * step + 1; rb = i < len(zs) - 1 and zs[i + 1][0] - b <= 2 * step + 1
        T = min(taper, (b - a) / 2.2); w = np.minimum(np.ones(len(xs)) if la else _sstep((xs - a) / T), np.ones(len(xs)) if rb else _sstep((b - xs) / T)); m = (xs >= a) & (xs <= (zs[i + 1][0] if rb else b))      # touching blobs share their boundary point
        out.append((cat, m, e - w * (e - f)))
    return f, out
def draw_soft(g, xs, e, bg, zones, X, Y, ymax, tips=None, line=True, flip=False):
    """orange under the whole curve, lens blobs on top. Y maps a value to a pixel (may point down for a mirrored panel)."""
    e = np.minimum(np.asarray(e, float), ymax); px = np.array([X(v) for v in xs]); f, L = soft_layers(xs, e, bg, zones)
    g.append(f'<path d="{P(px, [Y(v) for v in e])} L{px[-1]:.1f},{Y(0):.1f} L{px[0]:.1f},{Y(0):.1f} Z" fill="var(--c-oth)" class="soft"/>')
    order = sorted(range(len(zones)), key=lambda i: zones[i]) if tips else None
    for i, (cat, m, lower) in enumerate(L):
        t = tips[order[i]] if tips and i < len(order) else ""
        g.append(f'<path d="{P(px[m], [Y(v) for v in e[m]])} L{" L".join(f"{x:.1f},{Y(v):.1f}" for x, v in zip(px[m][::-1], lower[m][::-1]))} Z" fill="{"url(#nsr)" if cat == "nearside" and px[-1] < px[0] else COL[ZC[cat]]}"{t}/>')
    if line: g.append(f'<path class="a-line thin" d="{P(px, [Y(v) for v in e])}"/>')

def tremont3():
    """before above the street, after mirrored below it, with the 'before' outline ghosted onto the 'after' side"""
    W = 1000; ml = 44; ph = 118; g = []; y0 = 36; ymax = 50
    for d, title, flip in (("dir1", "Westbound", True), ("dir0", "Eastbound", False)):
        T = F3["tremont_" + d]; xs = np.array(T["xs"]); L_ = xs.max(); b0, b1 = T["busway"]
        X = (lambda x: ml + (L_ - x) / L_ * (W - ml - 6)) if flip else (lambda x: ml + x / L_ * (W - ml - 6)); zl = [(xs[z["ia"]], xs[z["ib"]], z["cat"]) for z in T["zones"]]
        yu = y0 + ph; ys = yu + 4; yd = ys + 17; Yu = lambda v: yu - min(v, ymax) / ymax * ph; Yd = lambda v: yd + min(v, ymax) / ymax * ph
        g.append(f'<text class="g-lab" x="{ml}" y="{y0 - 16}">{title} {"←" if flip else "→"}</text>')
        g.append(f'<rect x="{min(X(b0), X(b1)):.1f}" y="{y0}" width="{abs(X(b1) - X(b0)):.1f}" height="{2 * ph + 21}" class="t-band"/>')
        for t in (20, 40):
            for Yf in (Yu, Yd): g.append(f'<line class="grid" x1="{ml}" x2="{W - 6}" y1="{Yf(t):.1f}" y2="{Yf(t):.1f}"/><text class="g-ax" x="{ml - 6}" y="{Yf(t) + 3.5:.1f}" text-anchor="end">{t}</text>')
        for tag, Yf, lab in (("before", Yu, "Before"), ("after", Yd, "With the busway")):
            zz = [z for z in zl if z[2] in ZC and z[1] - z[0] >= 15]; secs = [s_ for z, s_ in zip(T["zones"], T[tag]["zsec"]) if z["cat"] in ZC and xs[z["ib"]] - xs[z["ia"]] >= 15]
            draw_soft(g, xs, np.array(T[tag]["e"]), np.array(T[tag]["bg"]), zz, X, Yf, ymax, tips=[tt(LAB[ZC[z[2]]], ZC[z[2]], f"{s_:.0f} seconds per bus", [], f"{title}, {lab.lower()}") for z, s_ in zip(zz, secs)])
        px = [X(v) for v in xs]; g.append(f'<path class="t-beforel" d="{P(px, [Yd(v) for v in np.minimum(np.array(T["before"]["e"]), ymax)])}"/>')
        g.append(f'<text class="g-cap" x="{ml + 6}" y="{y0 + 14}">Before</text><text class="g-cap" x="{ml + 6}" y="{yd + ph - 6}">With the busway (dashed line: before)</text>')
        g.append(f'<rect x="{ml}" y="{ys}" width="{W - ml - 6}" height="13" fill="var(--c-road)"/><rect x="{min(X(b0), X(b1)):.1f}" y="{ys + 2}" width="{abs(X(b1) - X(b0)):.1f}" height="9" fill="var(--c-lane)"/><text class="t-lanetx" x="{(X(b0) + X(b1)) / 2:.1f}" y="{ys + 9.6}" text-anchor="middle">BUSWAY</text>')
        for x, kd in T["sig"]:
            if kd == "sig": g.append(f'<rect x="{X(x) - 1:.1f}" y="{ys}" width="2" height="13" fill="var(--c-xwalk)"/>')
        seen = -99; yl = yd + ph + 16
        for x, lab in zip(T["ends"], T["labels"]):
            if any(k in lab for k in ("Webster", "Third", "Southern", "Belmont", "Crotona Avenue", "Park Avenue", "Boston", "Arthur")) and abs(X(x) - seen) > 70: g.append(f'<text class="g-ax" x="{X(x):.1f}" y="{yl}" text-anchor="middle">{esc(lab.replace(" Avenue", " Av").replace(" Boulevard", " Blvd"))}</text>'); seen = X(x)
        y0 = yl + 30; g.append(f'<text class="g-cap" x="{ml}" y="{y0 - 6}">Inside the busway, seconds of delay per bus</text>'); sc = (W - ml - 220) / 380
        for tag, lab in (("before", "Before"), ("after", "With the busway")):
            c = T[tag]["inside"]; x = ml + 140; g.append(f'<text class="g-row" x="{ml}" y="{y0 + 15}">{lab}</text>')
            for k, kk in (("other", "other"), ("near", "nearside"), ("stop", "stop"), ("lights", "signal")):
                w = c[kk] * sc; ch = 100 * (T["after"]["inside"][kk] / T["before"]["inside"][kk] - 1)
                g.append(f'<rect x="{x + 1:.1f}" y="{y0}" width="{max(w - 2, 1):.1f}" height="22" rx="2" fill="{COL[k]}"{tt(LAB[k], k, f"{c[kk]:.0f} seconds per bus", [("Before to after", f"{ch:+.0f}%")], f"{title}, {lab.lower()}")}/>')
                if w > 30: g.append(f'<text class="g-in{" dk" if k == "other" else ""}" x="{x + w / 2:.1f}" y="{y0 + 15.5}" text-anchor="middle">{c[kk]:.0f}</text>')
                x += w
            g.append(f'<text class="g-num" x="{x + 8:.1f}" y="{y0 + 15.5}">{T[tag]["inside_total"]:.0f} s</text>'); y0 += 28
        b, a = T["before"]["inside"], T["after"]["inside"]
        g.append(f'<text class="g-ax" x="{ml + 140}" y="{y0 + 10}">Other traffic {100 * (a["other"] / b["other"] - 1):+.0f}% · stop + red light {100 * (a["nearside"] / b["nearside"] - 1):+.0f}% · bus stops {100 * (a["stop"] / b["stop"] - 1):+.0f}% · red lights {100 * (a["signal"] / b["signal"] - 1):+.0f}%</text>'); y0 += 70
    return svg(W, y0 - 46, "".join(g), "Delay along East Tremont Avenue: before the busway above the street, with the busway mirrored below, split by cause")

def tsp5s():
    W = 1000; n = 5; gap = 18; pw = (W - gap * (n - 1)) / n; ph = 150; mt = 96; g = []
    nice = {"Jamaica Av & Springfield Blvd": ("Springfield Blvd", "at Jamaica Av, Queens"), "Forest Avenue & Green Avenue": ("Forest Av", "at Greene Av, Queens"), "Metcalf Avenue & Soundview Avenue & Watson Avenue": ("Watson Av", "at Metcalf Av, Bronx"),
            "Forest Av & Fairview Av": ("Forest Av", "at Fairview Av, Queens"), "Lafayette Ave & Rosedale Ave": ("Lafayette Av", "at Rosedale Av, Bronx")}
    for i, s in enumerate(F3["tsp5"]):
        x0 = i * (pw + gap); off = np.array(s["off"]); m = (off >= -450) & (off <= 150); o = off[m]; eb = np.array(s["before"]["e"])[m]; ea = np.array(s["after"]["e"])[m]; ba = np.array(s["after"]["bg"])[m]
        ymax = max(20, np.ceil(max(eb.max(), ea.max()) / 10) * 10); X = lambda v: x0 + (v + 450) / 600 * pw; Y = lambda v: mt + ph - min(v, ymax) / ymax * ph
        zs = [z for z in s["zones"] if z["cat"] in ("signal", "nearside") and -300 <= z["apex"] <= 30]; bsec, asec = sum(z["before"] for z in zs), sum(z["after"] for z in zs); t1, t2 = nice.get(s["nm"], (s["nm"], ""))
        g.append(f'<text class="g-row b" x="{x0}" y="14">{esc(t1)}</text><text class="g-ax" x="{x0}" y="29">{esc(t2)}</text><text class="a-big sm" x="{x0}" y="54">{bsec:.0f} → {asec:.0f} s</text><text class="g-ax" x="{x0}" y="69">at the light, per bus</text>')
        for t in (0, ymax / 2, ymax): g.append(f'<line class="grid" x1="{x0}" x2="{x0 + pw}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>' + (f'<text class="g-ax" x="{x0 + 2}" y="{Y(t) - 3:.1f}">{t:.0f}</text>' if t else ""))
        zz = [z for z in s["zones"] if z["cat"] in ZC and min(z["b"], 150) - max(z["a"], -450) >= 15]
        draw_soft(g, o, ea, ba, [(max(z["a"], -450), min(z["b"], 150), z["cat"]) for z in zz], X, Y, ymax, tips=[tt(LAB[ZC[z["cat"]]], ZC[z["cat"]], f"{z["after"]:.0f} seconds per bus after", [("Before", f"{z["before"]:.0f} s")], t1 + " " + t2) for z in zz])
        g.append(f'<path class="t-beforel" d="{P([X(v) for v in o], [Y(v) for v in eb])}"/>')
        ys = mt + ph + 5; g.append(f'<rect x="{x0}" y="{ys}" width="{pw}" height="12" fill="var(--c-road)"/>')
        for v in s["sig"]:
            if -450 <= v <= 150: g.append(f'<rect x="{X(v) - 1:.1f}" y="{ys}" width="2" height="12" fill="var(--c-xwalk)"/>')
        for v, _ in s["stops"]:
            if -430 <= v <= 130: g.append(f'<rect x="{X(v) - 9:.1f}" y="{ys + 2}" width="18" height="8" rx="4" fill="var(--c-island)" stroke="var(--c-stop)" stroke-width="1.3"/>')
        g.append(f'<rect x="{X(0) - 3.2:.1f}" y="{ys - 3}" width="6.4" height="18" rx="2.4" fill="#1d1d1b" stroke="#fff" stroke-width=".8"/><circle cx="{X(0):.1f}" cy="{ys + 1}" r="1.6" fill="#ff5a52"/><circle cx="{X(0):.1f}" cy="{ys + 6}" r="1.6" fill="#ffc233"/><circle cx="{X(0):.1f}" cy="{ys + 11}" r="1.6" fill="#35c96b"/>')
        g.append(f'<text class="g-ax" x="{x0}" y="{ys + 28}">450 ft before</text><text class="g-ax" x="{x0 + pw}" y="{ys + 28}" text-anchor="end">→</text><text class="g-ax" x="{x0}" y="{ys + 44}">on {pd.Timestamp(s["d"]).strftime("%-d %b")}</text>')
    return svg(W, mt + ph + 54, "".join(g), "Delay curve approaching five traffic lights, four weeks before and four weeks after signal priority was switched on")

def worldcup_we():
    W = 1000; H = 380; ml, mr, mt, mb = 44, 150, 26, 40; w = json.load(open(R / "fig_wcwe.json")); hs = w["h"]
    X = lambda h: ml + (h - 6) / 16 * (W - ml - mr); Y = lambda v: mt + (7.7 - v) / (7.7 - 3.4) * (H - mt - mb); g = []
    g.append(f'<rect x="{X(12):.1f}" y="{mt}" width="{X(21) - X(12):.1f}" height="{H - mt - mb}" class="t-band"/><text class="g-cap" x="{(X(12) + X(21)) / 2:.1f}" y="{mt + 16}" text-anchor="middle">42nd Street closed to cars on all four match days</text>')
    for t in (4, 5, 6, 7): g.append(f'<line class="grid" x1="{ml}" x2="{W - mr}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/><text class="g-ax" x="{ml - 6}" y="{Y(t) + 3.5:.1f}" text-anchor="end">{t} mph</text>')
    for h in range(6, 23, 2): g.append(f'<text class="g-ax" x="{X(h):.1f}" y="{H - mb + 16}" text-anchor="middle">{(h - 1) % 12 + 1}{"am" if h < 12 else "pm"}</text>')
    mu = np.array(w["mean"]); sd = np.array(w["sd"]); px = [X(h) for h in hs]
    g.append(f'<path class="w-band" d="{P(px, [Y(v) for v in mu + sd])} L{" L".join(f"{x:.1f},{Y(v):.1f}" for x, v in zip(px[::-1], (mu - sd)[::-1]))} Z"/><path class="w-base" d="{P(px, [Y(v) for v in mu])}"/>')
    D = np.array([w["days"][d] for d in w["days"]], dtype=float); mm = np.nanmean(D, 0)
    for row in D:
        ok = ~np.isnan(row); g.append(f'<path class="w-day" d="{P(np.array(px)[ok], [Y(v) for v in row[ok]])}"/>')
    g.append(f'<path class="w-match" d="{P(px, [Y(v) for v in mm])}"/>')
    for h, a, b in zip(hs, mm, mu): g.append(f'<circle cx="{X(h):.1f}" cy="{Y(a):.1f}" r="9" fill="transparent"{tt(f"{(h - 1) % 12 + 1}{"am" if h < 12 else "pm"}", "lane", f"{a:.1f} mph on match days", [("Ordinary weekend day", f"{b:.1f} mph"), ("Difference", f"{100 * (a / b - 1):+.0f}%")], "M42 on 42nd Street")}/>')
    g.append(f'<text class="w-lm" x="{W - mr + 8}" y="{Y(mm[-1]) - 6:.1f}">Match days</text><text class="g-ax" x="{W - mr + 8}" y="{Y(mm[-1]) + 8:.1f}">13, 27 June; 5, 19 July</text><text class="w-lb" x="{W - mr + 8}" y="{Y(mu[-1]) + 22:.1f}">Ordinary weekends</text><text class="g-ax" x="{W - mr + 8}" y="{Y(mu[-1]) + 36:.1f}">average of {w["nbase"]} days</text>')
    return svg(W, H, "".join(g), "M42 bus speed by hour on four weekend World Cup match days compared with ordinary weekend days")

def spline(xs, ys, t=0.5):
    """smooth curve through every point (Catmull-Rom, as cubic Beziers)"""
    p = list(zip(xs, ys)); d = f"M{p[0][0]:.1f},{p[0][1]:.1f}"
    for i in range(len(p) - 1):
        p0, p1, p2, p3 = p[max(i - 1, 0)], p[i], p[i + 1], p[min(i + 2, len(p) - 1)]
        d += f" C{p1[0] + (p2[0] - p0[0]) * t / 3:.1f},{p1[1] + (p2[1] - p0[1]) * t / 3:.1f} {p2[0] - (p3[0] - p1[0]) * t / 3:.1f},{p2[1] - (p3[1] - p1[1]) * t / 3:.1f} {p2[0]:.1f},{p2[1]:.1f}"
    return d
def worldcup_all():
    """seven match days (weekdays and weekends together) against ordinary days in the same mix; individual days are drawn faint and heavily smoothed"""
    W = 1000; H = 400; ml, mr, mt, mb = 44, 150, 26, 40; w = json.load(open(R / "fig_wcall.json")); hs = w["h"]
    X = lambda h: ml + (h - 6) / 16 * (W - ml - mr); Y = lambda v: mt + (7.7 - v) / (7.7 - 3.4) * (H - mt - mb); g = []
    g.append(f'<rect x="{X(8):.1f}" y="{mt}" width="{X(12) - X(8):.1f}" height="{H - mt - mb}" class="t-band" style="opacity:.04"/><rect x="{X(12):.1f}" y="{mt}" width="{X(20) - X(12):.1f}" height="{H - mt - mb}" class="t-band"/>')
    g.append(f'<text class="g-cap" x="{(X(12) + X(20)) / 2:.1f}" y="{mt + 16}" text-anchor="middle">Closed to cars on all seven days</text><text class="g-cap" x="{(X(8) + X(12)) / 2:.1f}" y="{mt + 16}" text-anchor="middle">Closures begin, 8am to noon</text>')
    for t in (4, 5, 6, 7): g.append(f'<line class="grid" x1="{ml}" x2="{W - mr}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/><text class="g-ax" x="{ml - 6}" y="{Y(t) + 3.5:.1f}" text-anchor="end">{t} mph</text>')
    for h in range(6, 23, 2): g.append(f'<text class="g-ax" x="{X(h):.1f}" y="{H - mb + 16}" text-anchor="middle">{(h - 1) % 12 + 1}{"am" if h < 12 else "pm"}</text>')
    mu = np.array(w["mean"]); sd = np.array(w["sd"]); mm = np.array(w["match"]); px = np.array([X(h) for h in hs])
    g.append(f'<path class="w-band" d="{spline(px, [Y(v) for v in mu + sd])} L{spline(px[::-1], [Y(v) for v in (mu - sd)[::-1]])[1:]} Z"/><path class="w-base" d="{spline(px, [Y(v) for v in mu])}"/>')
    for row in w["days"].values():
        r = _gf(pd.Series(row, dtype=float).interpolate(limit_direction="both").values, 1.1, mode="nearest"); g.append(f'<path class="w-day" style="opacity:.22" d="{spline(px, [Y(v) for v in r])}"/>')
    g.append(f'<path class="w-match" d="{spline(px, [Y(v) for v in mm])}"/>')
    for h, a, b in zip(hs, mm, mu): g.append(f'<circle cx="{X(h):.1f}" cy="{Y(a):.1f}" r="9" fill="transparent"{tt(f"{(h - 1) % 12 + 1}{"am" if h < 12 else "pm"}", "lane", f"{a:.1f} mph on match days", [("Ordinary days", f"{b:.1f} mph"), ("Difference", f"{100 * (a / b - 1):+.0f}%")], "M42 on 42nd Street")}/>')
    g.append(f'<text class="w-lm" x="{W - mr + 8}" y="{Y(mm[-1]) - 8:.1f}">Match days</text><text class="g-ax" x="{W - mr + 8}" y="{Y(mm[-1]) + 6:.1f}">average of seven</text><text class="w-lb" x="{W - mr + 8}" y="{Y(mu[-1]) + 22:.1f}">Ordinary days</text><text class="g-ax" x="{W - mr + 8}" y="{Y(mu[-1]) + 36:.1f}">{w["nbase"]} days, same mix</text>')
    return svg(W, H, "".join(g), "M42 bus speed by hour on seven World Cup match days compared with ordinary days")

def placement3():
    A = F3["place"]; off = np.array(A["off"]); W = 1000; pw = 312; gap = 32; ph = 170; mt = 58; g = []; ymax = 20; sl = (off >= -300) & (off <= 250); o = off[sl]
    # far-side: everything before the light is the light, everything after it is the stop, cross-faded over about 50 ft at the light
    far = np.array(A["far"]["signal"])[sl] + np.array(A["far"]["stop"])[sl]; s = _sstep((o + 10) / 50); far_l, far_s = far * (1 - s), far * s
    panels = [("none", "No bus stop", [("signal", np.array(A["none"]["signal"])[sl], "The light")]), ("far", "Stop after the light", [("signal", far_l, "The light"), ("stop", far_s, "The stop")]),
              ("near", "Stop before the light", [("nearside", np.array(A["near"]["nearside"])[sl] + np.array(A["near"]["signal"])[sl], "Stop and light together")])]
    for i, (k, t1, layers) in enumerate(panels):
        x0 = i * (pw + gap); X = lambda v: x0 + (v + 300) / 550 * pw; Y = lambda v: mt + ph - min(v, ymax) / ymax * ph; px = [X(v) for v in o]; secs = [float(y.sum() * 5 / 100) for _, y, _ in layers]; tot = sum(secs)
        g.append(f'<text class="g-row b" x="{x0}" y="16">{t1}</text><text class="g-ax" x="{x0}" y="32">{A[k]["n"]:,} intersections</text><text class="a-big" x="{x0 + pw}" y="22" text-anchor="end">{tot:.0f} s</text><text class="g-ax" x="{x0 + pw}" y="38" text-anchor="end">lost per bus</text>')
        for t in (0, 5, 10, 15, 20): g.append(f'<line class="grid" x1="{x0}" x2="{x0 + pw}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>' + (f'<text class="g-ax" x="{x0 + 2}" y="{Y(t) - 3:.1f}">{t}</text>' if i == 0 and 0 < t < 20 else ""))
        base = np.zeros(len(o))
        for (c, y, lab), sc_ in zip(layers, secs):
            top = base + y; g.append(f'<path d="{P(px, [Y(v) for v in top])} L{" L".join(f"{a:.1f},{Y(v):.1f}" for a, v in zip(px[::-1], base[::-1]))} Z" fill="{COL[ZC[c]]}"{tt(lab, ZC[c], f"{sc_:.0f} seconds per bus", [], t1)}/>'); base = top
        g.append(f'<path class="a-line" d="{P(px, [Y(v) for v in base])}"/>')
        ys = mt + ph + 6; g.append(f'<rect x="{x0}" y="{ys}" width="{pw}" height="14" fill="var(--c-road)"/><rect x="{X(0) - 1.5:.1f}" y="{ys}" width="3" height="14" fill="var(--c-xwalk)"/>')
        g.append(f'<rect x="{X(0) - 3.4:.1f}" y="{ys - 3}" width="6.8" height="20" rx="2.6" fill="#1d1d1b" stroke="#fff" stroke-width=".8"/><circle cx="{X(0):.1f}" cy="{ys + 1.5}" r="1.8" fill="#ff5a52"/><circle cx="{X(0):.1f}" cy="{ys + 7}" r="1.8" fill="#ffc233"/><circle cx="{X(0):.1f}" cy="{ys + 12.5}" r="1.8" fill="#35c96b"/>')
        if k == "far": g.append(f'<rect x="{X(60):.1f}" y="{ys + 2.5}" width="{X(140) - X(60):.1f}" height="9" rx="4.5" fill="var(--c-island)" stroke="var(--c-stop)" stroke-width="1.5"/>')
        if k == "near": g.append(f'<rect x="{X(-110):.1f}" y="{ys + 2.5}" width="{X(-30) - X(-110):.1f}" height="9" rx="4.5" fill="var(--c-island)" stroke="var(--c-stop)" stroke-width="1.5"/>')
        g.append(f'<text class="g-ax" x="{x0}" y="{ys + 30}">300 ft before</text><text class="g-ax" x="{x0 + pw}" y="{ys + 30}" text-anchor="end">250 ft after →</text>')
        g.append(f'<text class="g-ax" x="{x0}" y="48">{" + ".join(f"{sc_:.0f} s {lab.lower().replace("the ", "")}" for (_, _, lab), sc_ in zip(layers, secs))}</text>')
    return svg(W, mt + ph + 44, "".join(g), "Average delay credited to the light and the stop around an intersection, with other traffic removed")

# ================================================================== generic mirrored before/after, 42nd Street, headway CV
F4 = json.load(open(R / "fig4.json"))
def mirror(panels, top, bot, band, keep, barlab, ymax=50, flipkey=None, sub=""):
    W = 1000; ml = 44; ph = 118; g = []; y0 = 36
    for T, title, flip in panels:
        xs = np.array(T["xs"]); L_ = xs.max(); b0, b1 = T["busway"]
        X = (lambda x: ml + (L_ - x) / L_ * (W - ml - 6)) if flip else (lambda x: ml + x / L_ * (W - ml - 6)); zl = [(xs[z["ia"]], xs[z["ib"]], z["cat"]) for z in T["zones"]]
        yu = y0 + ph; ys = yu + 4; yd = ys + 17; Yu = lambda v: yu - min(v, ymax) / ymax * ph; Yd = lambda v: yd + min(v, ymax) / ymax * ph
        g.append(f'<text class="g-lab" x="{ml}" y="{y0 - 16}">{title} {"←" if flip else "→"}</text>')
        for t in (20, 40):
            for Yf in (Yu, Yd): g.append(f'<line class="grid" x1="{ml}" x2="{W - 6}" y1="{Yf(t):.1f}" y2="{Yf(t):.1f}"/><text class="g-ax" x="{ml - 6}" y="{Yf(t) + 3.5:.1f}" text-anchor="end">{t}</text>')
        for tag, Yf, lab in (("before", Yu, top), ("after", Yd, bot)):
            zz = [z for z in zl if z[2] in ZC and z[1] - z[0] >= 15]; secs = [s_ for z, s_ in zip(T["zones"], T[tag]["zsec"]) if z["cat"] in ZC and xs[z["ib"]] - xs[z["ia"]] >= 15]
            draw_soft(g, xs, np.array(T[tag]["e"]), np.array(T[tag]["bg"]), zz, X, Yf, ymax, tips=[tt(LAB[ZC[z[2]]], ZC[z[2]], f"{s_:.0f} seconds per bus", [], f"{title}, {lab.lower()}") for z, s_ in zip(zz, secs)])
        px = [X(v) for v in xs]; g.append(f'<path class="t-beforel" d="{P(px, [Yd(v) for v in np.minimum(np.array(T["before"]["e"]), ymax)])}"/>')
        g.append(f'<text class="g-cap" x="{ml + 6}" y="{y0 + 14}">{top}</text><text class="g-cap" x="{ml + 6}" y="{yd + ph - 6}">{bot} (dashed line: {top.lower()})</text>')
        g.append(f'<rect x="{ml}" y="{ys}" width="{W - ml - 6}" height="13" fill="var(--c-road)"/><rect x="{min(X(b0), X(b1)):.1f}" y="{ys + 2}" width="{abs(X(b1) - X(b0)):.1f}" height="9" fill="var(--c-lane)"/>')
        for u in np.linspace(b0, b1, 7)[1:-1]: g.append(f'<text class="t-lanetx" x="{X(u):.1f}" y="{ys + 9.6}" text-anchor="middle">{band}</text>')
        for x, kd in T["sig"]:
            if kd == "sig": g.append(f'<rect x="{X(x) - 1:.1f}" y="{ys}" width="2" height="13" fill="var(--c-xwalk)"/>')
        seen = -99; yl = yd + ph + 16
        for x, lab in sorted(zip(T["ends"], T["labels"]), key=lambda t: X(t[0])):
            if any(k in lab for k in keep) and abs(X(x) - seen) > 64: g.append(f'<text class="g-ax" x="{X(x):.1f}" y="{yl}" text-anchor="middle">{esc(lab.replace(" Avenue", " Av").replace(" Boulevard", " Blvd").replace("Avenue of the Americas", "6th Av"))}</text>'); seen = X(x)
        y0 = yl + 30; g.append(f'<text class="g-cap" x="{ml}" y="{y0 - 6}">{barlab}</text>'); mx = max(T["before"]["inside_total"], T["after"]["inside_total"]); sc = (W - ml - 230) / mx
        for tag, lab in (("before", top), ("after", bot)):
            c = T[tag]["inside"]; x = ml + 150; g.append(f'<text class="g-row" x="{ml}" y="{y0 + 15}">{lab}</text>')
            for k, kk in (("other", "other"), ("near", "nearside"), ("stop", "stop"), ("lights", "signal")):
                w = c[kk] * sc; ch = 100 * (T["after"]["inside"][kk] / T["before"]["inside"][kk] - 1)
                g.append(f'<rect x="{x + 1:.1f}" y="{y0}" width="{max(w - 2, 1):.1f}" height="22" rx="2" fill="{COL[k]}"{tt(LAB[k], k, f"{c[kk] / 60:.1f} minutes per bus", [("Change", f"{ch:+.0f}%")], f"{title}, {lab.lower()}")}/>')
                if w > 34: g.append(f'<text class="g-in{" dk" if k == "other" else ""}" x="{x + w / 2:.1f}" y="{y0 + 15.5}" text-anchor="middle">{c[kk] / 60:.1f}</text>')
                x += w
            g.append(f'<text class="g-num" x="{x + 8:.1f}" y="{y0 + 15.5}">{T[tag]["inside_total"] / 60:.1f} min</text>'); y0 += 28
        b, a = T["before"]["inside"], T["after"]["inside"]
        g.append(f'<text class="g-ax" x="{ml + 150}" y="{y0 + 10}">Other traffic {100 * (a["other"] / b["other"] - 1):+.0f}% · stop + red light {100 * (a["nearside"] / b["nearside"] - 1):+.0f}% · bus stops {100 * (a["stop"] / b["stop"] - 1):+.0f}% · red lights {100 * (a["signal"] / b["signal"] - 1):+.0f}%</text>'); y0 += 70
    return svg(W, y0 - 46, "".join(g), sub)
def m42():
    keep = ("1st Avenue", "2nd Avenue", "3rd Avenue", "Lexington", "Madison", "5th Avenue", "6th Avenue", "Americas", "7th Avenue", "8th Avenue", "9th Avenue", "10th Avenue", "11th Avenue", "12th Avenue", "Broadway", "Park Avenue")
    panels = [(F4["m42_" + d], ("Eastbound" if F4["m42_" + d]["bearing"] == "east" else "Westbound"), F4["m42_" + d]["bearing"] == "west") for d in ("dir0", "dir1")]
    return mirror(panels, "Ordinary days", "Match days", "BUSES ONLY", keep, "Whole of 42nd Street, minutes of delay per bus", sub="Delay along 42nd Street on ordinary days above the street and on World Cup match days mirrored below, split by cause")
def cvfig():
    W = 1000; pw = 484; gap = 32; ph = 190; mt = 64; g = []
    for i, (d, title) in enumerate((("dir0", "Eastbound, in direction of travel →"), ("dir1", "Westbound, in direction of travel →"))):
        T = F4["cv_" + d]; xs = np.array(T["xs"]); x0 = i * (pw + gap); X = lambda v: x0 + 36 + v / xs.max() * (pw - 40); lo, hi = (0.92, 1.16) if d == "dir0" else (0.74, 0.98); Y = lambda v: mt + ph - (v - lo) / (hi - lo) * ph; b0, b1 = T["busway"]
        g.append(f'<text class="g-lab" x="{x0}" y="16">{title}</text>')
        g.append(f'<rect x="{X(b0):.1f}" y="{mt}" width="{X(b1) - X(b0):.1f}" height="{ph}" class="t-band"/><text class="g-cap" x="{(X(b0) + X(b1)) / 2:.1f}" y="{mt + 15}" text-anchor="middle">busway</text>')
        for t in np.arange(lo + .04, hi, .04): g.append(f'<line class="grid" x1="{x0 + 36}" x2="{x0 + pw}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/><text class="g-ax" x="{x0 + 30}" y="{Y(t) + 4:.1f}" text-anchor="end">{t:.2f}</text>')
        for tag, cls in (("before", "w-base"), ("after", "w-match")):
            cv = np.array(T[tag]["cv"]); g.append(f'<path class="{cls}" style="stroke-width:2.4" d="{P([X(v) for v in xs], [Y(v) for v in cv])}"/>')
        bf, af = T["before"], T["after"]
        g.append(f'<text class="g-ax" x="{x0}" y="34">Growth across the busway: <tspan font-weight="700">{bf["cv_out"] - bf["cv_in"]:+.3f} before, {af["cv_out"] - af["cv_in"]:+.3f} with the busway</tspan></text><text class="g-ax" x="{x0}" y="50">Average gap between buses: {bf["headway"]:.1f} min before, {af["headway"]:.1f} after</text>')
        seen = -99
        for x, lab in zip(T["ends"], T["labels"]):
            if any(k in lab for k in ("Webster", "Third", "Southern", "Belmont", "Park Avenue")) and abs(X(x) - seen) > 70: g.append(f'<text class="g-ax" x="{X(x):.1f}" y="{mt + ph + 16}" text-anchor="middle">{esc(lab.replace(" Avenue", " Av").replace(" Boulevard", " Blvd"))}</text>'); seen = X(x)
        if i == 0: g.append(f'<text class="w-lb" x="{X(xs.max() * .02):.1f}" y="{Y(bf["cv"][10]) - 8:.1f}" style="font-size:12px">Before</text><text class="w-lm" x="{X(xs.max() * .02):.1f}" y="{Y(af["cv"][10]) + 16:.1f}" style="font-size:12px">With the busway</text>')
    return svg(W, mt + ph + 26, "".join(g), "Variation in the gap between buses along East Tremont Avenue, before and with the busway")

def m42check():
    """left: every ordinary day and the seven match days on 42nd St; right: express routes that use 42nd St, on it vs elsewhere in Manhattan"""
    D = json.load(open(R / "fig_m42_sections.json"))["42nd Street (closed to cars)"]; O = pd.read_csv(R / "m42_other_routes.csv"); W = 1000; g = []; rng = np.random.default_rng(5)
    pw = 520; X = lambda v: 20 + (v + 20) / 32 * (pw - 40); y = 96
    g.append(f'<text class="g-lab" x="0" y="16">M42 travel time on 42nd Street, each day</text><text class="g-ax" x="0" y="34">against the average for that kind of day (weekday, Saturday or Sunday)</text>')
    for t in (-15, -10, -5, 0, 5, 10): g.append(f'<line class="{"s-zero" if t == 0 else "grid"}" x1="{X(t):.1f}" x2="{X(t):.1f}" y1="56" y2="150"/><text class="g-ax" x="{X(t):.1f}" y="166" text-anchor="middle">{"usual" if t == 0 else f"{t:+d}%"}</text>')
    for v in D["ord"]: g.append(f'<circle cx="{X(max(min(v, 11.5), -19.5)):.1f}" cy="{y + rng.uniform(-22, 22):.1f}" r="4" fill="var(--fg2)" opacity=".45"/>')
    for d_, v in D["days"].items(): g.append(f'<circle cx="{X(v):.1f}" cy="{y + rng.uniform(-22, 22):.1f}" r="6" fill="var(--c-lane)" stroke="var(--bg)" stroke-width="1.5"{tt(pd.Timestamp(d_).strftime("%a %-d %B"), "lane", f"{v:+.0f}% travel time", [], "Match day")}/>')
    g.append(f'<text class="w-lm" x="{X(-17):.1f}" y="52" style="font-size:12px">7 match days</text><text class="w-lb" x="{X(2):.1f}" y="52" style="font-size:12px">{len(D["ord"])} ordinary days</text>')
    x0 = pw + 40; X2 = lambda v: x0 + 150 + (v + 25) / 37 * (W - x0 - 170)
    g.append(f'<text class="g-lab" x="{x0}" y="16">Express routes that use 42nd Street</text><text class="g-ax" x="{x0}" y="34">same buses, same days: on 42nd Street and on their other Manhattan streets</text>')
    for t in (-20, -10, 0, 10): g.append(f'<line class="{"s-zero" if t == 0 else "grid"}" x1="{X2(t):.1f}" x2="{X2(t):.1f}" y1="48" y2="150"/><text class="g-ax" x="{X2(t):.1f}" y="166" text-anchor="middle">{"usual" if t == 0 else f"{t:+d}%"}</text>')
    for i, rt in enumerate(sorted(O.route.unique())):
        yy = 62 + i * 19; a = float(O[(O.route == rt) & (O.sec == "on 42nd St")].pct.iloc[0]); b = float(O[(O.route == rt) & (O.sec == "elsewhere in Manhattan")].pct.iloc[0])
        g.append(f'<text class="g-row" x="{x0}" y="{yy + 4}">{rt}</text><line x1="{X2(a):.1f}" x2="{X2(b):.1f}" y1="{yy}" y2="{yy}" stroke="var(--rule)" stroke-width="2"/><circle cx="{X2(b):.1f}" cy="{yy}" r="5" fill="var(--fg2)"{tt(rt, "x", f"{b:+.0f}% travel time", [], "Elsewhere in Manhattan")}/><circle cx="{X2(a):.1f}" cy="{yy}" r="6" fill="var(--c-lane)"{tt(rt, "lane", f"{a:+.0f}% travel time", [], "On 42nd Street")}/>')
    g.append(f'<circle cx="{x0 + 6}" cy="186" r="6" fill="var(--c-lane)"/><text class="g-ax" x="{x0 + 18}" y="190">on 42nd Street</text><circle cx="{x0 + 126}" cy="186" r="5" fill="var(--fg2)"/><text class="g-ax" x="{x0 + 137}" y="190">elsewhere in Manhattan</text>')
    return svg(W, 198, "".join(g), "Checks on the 42nd Street result: every day's M42 travel time, and express routes on and off 42nd Street")
