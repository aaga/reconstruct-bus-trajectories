"""Final pitch page: Tremont case study -> how the split is found -> busway before/after -> 42nd St -> scoreboard -> city -> plan -> lights -> Flatbush."""
import json, pickle, numpy as np, pandas as pd
import gfx, shell, fdata; from shell import fig; from gfx import tt, svg, COL, LAB, P, esc
G = gfx; bap = fdata.bap; R = gfx.R
# ------------------------------------------------------------------ data
TP = fdata.tremont(); cB, DB, ua, ub = fdata.tremont_D(TP, "before"); cA, DA_, _, _ = fdata.tremont_D(TP, "after"); LIM = cB["before_after"]["limits"]
ST = {tag: {d: bap.stats(D, d, *LIM[d]) for d in ("dir0", "dir1")} for tag, D in (("before", DB), ("after", DA_))}
DIRN = {d: TP[d]["bearing"] + "bound" for d in ("dir0", "dir1")}
def parts(s): return dict(ff=s["freeflow"], stop=s["stop"], near=s["nearside"], lights=s["signal"], other=s["other"])
MERGED = (-11.7 * ST["before"]["dir0"]["total"] + -25.0 * ST["before"]["dir1"]["total"]) / (ST["before"]["dir0"]["total"] + ST["before"]["dir1"]["total"])
H5 = json.load(open(R / "fig5_m42hours.json"))
# ------------------------------------------------------------------ generic stacked time bars
def timebars(groups, label, unit=60.0, compare=True):
    """groups: [(group title, [(row label, parts dict in seconds, tooltip sub)])] drawn to one scale"""
    W = 1000; ml = 190; mr = 96; mx = max(sum(p.values()) for _, rows in groups for _, p, _ in rows); sc = (W - ml - mr) / mx; y = 8; g = []
    for title, rows in groups:
        if title: g.append(f'<text class="g-lab" x="0" y="{y + 14}">{esc(title)}</text>'); y += 26
        base = sum(rows[0][1].values())
        for i, (lab, p, sub) in enumerate(rows):
            x = ml; tot = sum(p.values()); g.append(f'<text class="g-row" x="0" y="{y + 20}">{esc(lab)}</text>')
            for k in ("ff", "stop", "near", "lights", "other"):
                w = p[k] * sc; rr = [("Share of the trip", f"{100 * p[k] / tot:.0f}%")] + ([("Change", f"{100 * (p[k] / rows[0][1][k] - 1):+.0f}%")] if compare and i and rows[0][1][k] > 0 else [])
                g.append(f'<rect x="{x + 1:.1f}" y="{y}" width="{max(w - 2, 1):.1f}" height="30" rx="2.5" fill="{COL[k]}"{tt(LAB[k], k, f"{p[k] / unit:.1f} minutes per bus", rr, sub)}/>')
                if w > 36: g.append(f'<text class="g-in{" dk" if k == "other" else ""}" x="{x + w / 2:.1f}" y="{y + 19.5}" text-anchor="middle">{p[k] / unit:.1f}</text>')
                x += w
            g.append(f'<text class="g-num" x="{x + 8:.1f}" y="{y + 19.5}">{tot / unit:.1f} min' + (f' <tspan class="chg">{100 * (tot / base - 1):+.0f}%</tspan>' if compare and i else "") + '</text>'); y += 38
        y += 12
    return svg(W, y - 10, "".join(g), label)
# ------------------------------------------------------------------ 1. inset map
def inset():
    geo = pickle.load(open(R / "mapgeo.pkl", "rb"))["geo"]; x0, x1, y0, y1 = -73.918, -73.868, 40.828, 40.866; W = 330; kx = np.cos(np.radians(40.85)); sc = W / ((x1 - x0) * kx); H = (y1 - y0) * sc
    X = lambda lo: (lo - x0) * kx * sc; Y = lambda la: (y1 - la) * sc; pth = lambda ll: "M" + " L".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in ll)
    base = " ".join(pth(ll) for ll in geo.values() if x0 < ll[0][0] < x1 and y0 < ll[0][1] < y1)
    f = DB["fu"]; g = [f'<rect width="{W}" height="{H:.0f}" fill="var(--panel)" rx="6"/><path class="m-base" style="stroke-width:1" d="{base}"/>']
    whole = " ".join(pth(s["geom"]) for s in TP["dir1"]["segs"]); g.append(f'<path d="{whole}" fill="none" stroke="var(--fg2)" stroke-width="2.2"/>')
    bw = [s for s in TP["dir1"]["segs"] if LIM["dir0"][0] - 30 <= s["x0"] * 3.28084 and (s["x0"] + s["len_m"]) * 3.28084 <= LIM["dir0"][1] + 30]
    g.append(f'<path d="{" ".join(pth(s["geom"]) for s in bw)}" fill="none" stroke="var(--c-lane)" stroke-width="6" stroke-linecap="round"/>')
    mid = bw[len(bw) // 2]["geom"][0]; g.append(f'<text class="g-lab" x="{X(mid[0]):.0f}" y="{Y(mid[1]) - 14:.0f}" text-anchor="middle" style="fill:var(--c-lane);paint-order:stroke;stroke:var(--panel);stroke-width:4px">Busway</text>')
    e = TP["dir1"]["segs"][0]["geom"][0]; w_ = TP["dir1"]["segs"][-1]["geom"][-1]; lab = min((e, w_), key=lambda p: p[0])
    g.append(f'<text class="g-ax" x="{X(mid[0]) + 14:.0f}" y="{Y(mid[1]) + 22:.0f}" style="paint-order:stroke;stroke:var(--panel);stroke-width:4px">East Tremont Av</text>')
    g.append(f'<text class="m-boro" x="{W - 12}" y="22" text-anchor="end" style="stroke:var(--panel)">THE BRONX</text>')
    return svg(W, round(H), "".join(g), "Map of the central Bronx with the East Tremont Avenue busway highlighted")
# ------------------------------------------------------------------ 5b. M42 each day
def m42days():
    D = json.load(open(R / "fig_m42_sections.json"))["42nd Street (closed to cars)"]; W = 1000; g = []; rng = np.random.default_rng(5); X = lambda v: 40 + (v + 20) / 32 * (W - 80); y = 78
    for t in (-15, -10, -5, 0, 5, 10): g.append(f'<line class="{"s-zero" if t == 0 else "grid"}" x1="{X(t):.1f}" x2="{X(t):.1f}" y1="34" y2="124"/><text class="g-ax" x="{X(t):.1f}" y="142" text-anchor="middle">{"usual" if t == 0 else f"{t:+d}%"}</text>')
    for v in D["ord"]: g.append(f'<circle cx="{X(max(min(v, 11.5), -19.5)):.1f}" cy="{y + rng.uniform(-30, 30):.1f}" r="5" fill="var(--fg2)" opacity=".4"/>')
    for d_, v in D["days"].items(): g.append(f'<circle cx="{X(v):.1f}" cy="{y + rng.uniform(-26, 26):.1f}" r="8" fill="var(--c-lane)" stroke="var(--bg)" stroke-width="1.5"{tt(pd.Timestamp(d_).strftime("%a %-d %B"), "lane", f"{v:+.0f}% travel time", [], "Match day")}/>')
    g.append(f'<text class="w-lm" x="{X(-17):.1f}" y="22">7 match days</text><text class="w-lb" x="{X(1.5):.1f}" y="22">{len(D["ord"])} ordinary days</text><text class="g-ax" x="{X(-20):.1f}" y="160">← faster</text><text class="g-ax" x="{X(12):.1f}" y="160" text-anchor="end">slower →</text>')
    return svg(W, 166, "".join(g), "M42 travel time on 42nd Street on each day, against the usual for that kind of day")
# ------------------------------------------------------------------ 11. Flatbush, top to bottom
def vflat():
    c, D = fdata.flatbush(); k = 0.14; L = float(D["dir1"]["a"]["xs"].max()); ntop = D["dir1"]["bearing"] == "South"; nb = "dir0" if D["dir0"]["bearing"] == "North" else "dir1"
    W = 1000; Cx = 500; RH = 20; gut = 6; ymax = 60; sc = 7.6; mt = 64; H = mt + L * k + 40; Ud = {"dir1": (lambda x: np.asarray(x, float)), "dir0": D["fu"]}
    Y = lambda u: mt + (np.asarray(u, float) if ntop else L - np.asarray(u, float)) * k
    XS = {nb: (lambda v: Cx + RH + gut + np.minimum(v, ymax) * sc), ("dir1" if nb == "dir0" else "dir0"): (lambda v: Cx - RH - 6 - np.minimum(v, ymax) * sc)}
    M = bap.merged_signals(D, -1e9, 1e9); sid = {(d, m[d]): m["id"] for m in M for d in ("dir0", "dir1") if m[d] is not None}; tot = {m["id"]: {"dir0": [0.0, 0], "dir1": [0.0, 0]} for m in M}
    DN = {d: D[d]["bearing"] + "bound" for d in ("dir0", "dir1")}; g = []; ax = []
    for d in ("dir0", "dir1"):
        Xs = XS[d]; out = 1 if d == nb else -1
        for t in (0, 20, 40, 60):
            g.append(f'<line class="grid" x1="{Xs(t):.1f}" x2="{Xs(t):.1f}" y1="{mt - 6}" y2="{H - 34:.0f}"/>')
            for yy in (mt - 12, H - 20): ax.append(f'<text class="t-ax" x="{Xs(t):.1f}" y="{yy:.0f}" text-anchor="middle">{t}</text>')
        a = D[d]["a"]; xs = a["xs"]; ys = Y(Ud[d](xs)); e = a["e"]; bg = a["bg"]; n = len(xs)
        def pts(i0, i1, vals, rev=False):
            ii = list(range(i0, i1 + 1, 2)); ii = ii + ([i1] if ii[-1] != i1 else []); ii = ii[::-1] if rev else ii; return " L".join(f"{Xs(vals[i]):.1f},{ys[i]:.1f}" for i in ii)
        exc = float(e.sum() * bap.STEP / 100); oth = exc - sum(bap.split(a, np.ones(n, bool)).values())
        # other traffic in chunks: a new blob starts at each clear dip in the baseline (a valley at least 0.8 s per 100 ft deep)
        from scipy.signal import find_peaks
        vl, _ = find_peaks(-bg, prominence=0.8, distance=int(600 / bap.STEP)); cuts = [0] + [int(v) for v in vl if 80 < v < n - 80] + [n - 1]
        um = Ud[d](xs); sgu = sorted((m["u"], bap.short(m["name"].split(" / ")[0])) for m in M if "pedestrian" not in m["name"] and "mid-block" not in m["name"])
        near = lambda u: min(sgu, key=lambda t: abs(t[0] - u))[1]
        for a_, b_ in zip(cuts[:-1], cuts[1:]):
            sec_ = float(bg[a_:b_ + 1].sum() * bap.STEP / 100); ends_ = sorted((um[a_], um[b_])); nmA, nmB = near(ends_[0]), near(ends_[1])
            g.append(f'<path class="hz othc" d="M{pts(a_, b_, e)} L{Xs(0):.1f},{ys[b_]:.1f} L{Xs(0):.1f},{ys[a_]:.1f} Z" fill="var(--c-oth)"{bap.tt("Other traffic", "other", f"{sec_:.0f} s per bus", [("Stretch", f"{nmA} to {nmB}" if nmA != nmB else f"near {nmA}"), ("Length", f"{abs(um[b_] - um[a_]) / 5280:.2f} miles"), ("Share of this direction's other traffic", f"{100 * sec_ / oth:.0f}%")], DN[d]).replace(' class="hz"', "", 1)}/>')
        for _, q in a["Z"].iterrows():
            ia, ib = int(q.ia), min(int(q.ib) + 1, n - 1)
            if ib <= ia: continue
            rows = [("Zone length", f"{q.x1 - q.x0:.0f} ft"), ("Peak height", f"{q.height:.0f} s per 100 ft")]
            if q["cat"] == "nearside": rows.insert(0, ("Light just after", a["sig"].name[int(q.sg)]))
            link = ""
            if q["cat"] in ("signal", "nearside") and (d, int(q.sg)) in sid:
                kk = sid[(d, int(q.sg))]; tot[kk][d][0] += q.sec; tot[kk][d][1] += 1; link = f' data-sig="{kk}" data-ax="{Xs(bg[int(q.ip)]):.1f}" data-ay="{ys[int(q.ip)]:.1f}"'
            fill = ("url(#nsu)" if (d == nb) == ntop else "url(#nsd)") if q["cat"] == "nearside" else bap.FILL[q["cat"]]
            g.append(f'<path d="M{pts(ia, ib, e)} L{pts(ia, ib, bg, True)} Z" fill="{fill}"{bap.tt(q["name"], q["cat"], f"{q.sec:.0f} s per bus", rows, DN[d])}{link}/>')
        g.append(f'<path class="curve thin" d="M{pts(0, n - 1, e)}"/>')
        last = -99
        for _, s in a["ls"].assign(y=Y(Ud[d](a["ls"].x.values))).sort_values("y").iterrows():
            g.append(f'<line class="m-stop" x1="{Xs(0):.1f}" x2="{Xs(ymax):.1f}" y1="{s.y:.1f}" y2="{s.y:.1f}"/><rect class="d-stop" x="{Xs(ymax) - 3.5:.1f}" y="{s.y - 3.5:.1f}" width="7" height="7" rx="1.5"/>')
            if s.y - last > 13: g.append(f'<text class="t-stop" x="{Xs(ymax) - out * 8:.1f}" y="{s.y - 5:.1f}" text-anchor="{"end" if out > 0 else "start"}">{esc(s["name"])}</text>'); last = s.y
    # ---- the street
    g.append(f'<rect x="{Cx - RH}" y="{Y(0 if ntop else L):.1f}" width="{2 * RH}" height="{L * k:.1f}" fill="var(--c-road)"/>')
    for m in M: g.append(f'<rect x="{Cx - RH}" y="{Y(m["u"]) - 1.5:.1f}" width="{2 * RH}" height="3" fill="var(--c-xwalk)"/>')
    def uof(*names):
        for m in M:
            if any(nm in m["name"] for nm in names): return m["u"]
    side = {d: (1 if d == nb else -1) for d in ("dir0", "dir1")}; sbd = "dir1" if nb == "dir0" else "dir0"
    for d, ua_, ub_, sub in ((sbd, 0.0, uof("8th Avenue"), "Southbound, Livingston St to 8 Av"), (nb, uof("Lafayette Avenue"), uof("Plaza Street", "Grand Army Plaza", "Sterling Place"), "Northbound, Grand Army Plaza to Lafayette Av")):
        if ua_ is None or ub_ is None: print("lane limit not found", sub); continue
        ya, yb = sorted((float(Y(ua_)), float(Y(ub_)))); x = Cx + (1 if side[d] > 0 else -8)
        g.append(f'<rect x="{x}" y="{ya:.1f}" width="7" height="{yb - ya:.1f}" fill="var(--c-lane)"{bap.tt("Centre bus lane", "lane", f"{abs(ub_ - ua_) / 5280:.2f} miles", [("Opened", "17 November 2026"), ("Hours", "24 hours, buses only")], sub)}/>')
    g.append(f'<line x1="{Cx}" x2="{Cx}" y1="{Y(0 if ntop else L):.1f}" y2="{Y(L if ntop else 0):.1f}" stroke="var(--c-centre)" stroke-width="1.2"/>')
    edges = sorted([0.0] + [m["u"] for m in M] + [L]); lastu = -1e9
    for a_, b_ in zip(edges[:-1], edges[1:]):
        um = (a_ + b_) / 2
        if b_ - a_ < 260 or um - lastu < 1800: continue
        lastu = um; y = float(Y(um)); up, dn = "l-4.5,9 h3 v9 h3 v-9 h3 z", "l-4.5,-9 h3 v-9 h3 v9 h3 z"
        g.append(f'<path class="arr" d="M{Cx + side[nb] * 7:.1f},{y - 9:.1f} {up if ntop else dn}"/><path class="arr" d="M{Cx - side[nb] * 7:.1f},{y + 9:.1f} {dn if ntop else up}"/>')
    last = -99
    for m in M:
        y = float(Y(m["u"])); t = tot[m["id"]]; both = t["dir0"][0] + t["dir1"][0]
        rows = [(DN[d], (f"{t[d][0]:.0f} s" if t[d][1] else "no peak found") if m[d] is not None else "no light in this direction") for d in ("dir0", "dir1")]
        g.append(f'<g id="{m["id"]}" data-x="{Cx}" data-y="{y:.1f}"{bap.tt(bap.short(m["name"]), "signal", f"{both:.0f} s per bus" if both else "No delay peak", rows, "Traffic light, both directions", "hz sigdot")}>'
                 f'<rect x="{Cx - RH}" y="{y - 7:.1f}" width="{2 * RH}" height="14" fill="transparent"/><g transform="translate({Cx},{y:.1f}) scale(.82) translate({-Cx},{-y:.1f})">{bap.tlight(Cx, y)}</g></g>')
        nm = bap.short(m["name"].split(" / ")[0])
    ax.append(f'<text class="t-dir" x="{XS[sbd](0):.1f}" y="18" text-anchor="end">Southbound, to Kings Plaza ↓</text><text class="t-dir" x="{XS[nb](0):.1f}" y="18">↑ Northbound, to Downtown Brooklyn</text>'
              f'<text class="t-ax" x="{XS[sbd](ymax):.1f}" y="34">← more delay · seconds per 100 ft</text><text class="t-ax" x="{XS[nb](ymax):.1f}" y="34" text-anchor="end">seconds per 100 ft · more delay →</text>')
    return f'<svg class="hero vflat" viewBox="0 0 {W} {H:.0f}" role="img" aria-label="Delay along all of Flatbush Avenue from north to south, southbound on the left and northbound on the right">{"".join(g)}{"".join(ax)}<g class="hlinks"></g></svg>', D, tot, M
# ------------------------------------------------------------------ page
EXTRA = """<style>
:root { --c-centre: #e7c64a; --c-lanestroke: #a23a2c; --c-loc: #a9a79f; }
.secn { font: 700 11px var(--sans); letter-spacing: .14em; color: var(--fg2); margin: 56px 0 6px; display: flex; align-items: center; gap: 10px; } .secn::after { content: ""; flex: 1; border-top: 1px solid var(--rule); } .secn + h2, .secn + figure, .secn + .lede { margin-top: 0; } figure .secn { margin-top: 0; }
.lede { display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 32px; align-items: start; max-width: 1024px; margin-inline: auto; } .lede .txt { max-width: 660px; } .lede figure { margin: 6px 0 0; } @media (max-width: 860px) { .lede { grid-template-columns: 1fr; } .lede figure { max-width: 330px; } }
.chg { fill: var(--c-ff); font-weight: 700; } .chart { background: var(--panel); border-radius: 8px; padding: 14px; min-width: 0; } .chart svg.hero { min-width: 820px; }
.curve { fill: none; stroke: var(--fg); stroke-width: 2; stroke-linejoin: round; } .curve.thin { stroke-width: 1.3; }
.m-stop { stroke: var(--c-stop); stroke-width: 1.2; stroke-dasharray: 5 3; } .d-stop { fill: var(--c-stop); } .t-ax { font: 400 11px var(--sans); fill: var(--fg2); font-variant-numeric: tabular-nums; } .t-stop { font: 600 11px var(--sans); fill: var(--c-stop); }
.hlinks { pointer-events: none; } .hl { stroke: var(--fg); stroke-width: 1.5; stroke-dasharray: .1 4.5; stroke-linecap: round; } .hl-halo { stroke: var(--bg); stroke-width: 4; opacity: .55; stroke-linecap: round; }
.arr { fill: #fff; opacity: .92; pointer-events: none; } .sigdot { cursor: pointer; } .t-dir { font: 700 14px var(--sans); fill: var(--fg); } .t-lane { font: 700 7.5px var(--sans); fill: #fff; letter-spacing: .08em; opacity: .92; pointer-events: none; }
path[fill="var(--c-oth)"] { fill-opacity: .55; } .othc { stroke: var(--panel); stroke-width: 2.5; paint-order: stroke; } rect[fill="var(--c-oth)"] { fill-opacity: 1; } path.hz[fill="var(--c-oth)"]:hover, path.hz[fill="var(--c-oth)"].on { filter: none; fill-opacity: .7; }
.tl { fill: #1d1d1b; stroke: #fff; stroke-width: 1; } .sgn { fill: var(--bg); stroke: var(--rule); stroke-width: 1; } .t-x { font: 600 9px var(--sans); letter-spacing: .05em; fill: var(--fg2); pointer-events: none; }
.vwrap { background: var(--panel); border-radius: 8px; padding: 14px; } .vwrap svg { min-width: 720px; }
.key { display: flex; flex-wrap: wrap; gap: 6px 18px; font: 500 13px var(--sans); color: var(--fg2); margin: 0 0 12px; align-items: center; } .key span { display: inline-flex; align-items: center; gap: 6px; } .key svg { width: auto; height: auto; flex: none; }
.k-stopname { font-style: normal; font-weight: 600; font-size: 11px; color: var(--c-stop); } .k-stopname::before { content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 1.5px; background: var(--c-stop); margin-right: 4px; } .k-sgn { font-style: normal; font-weight: 600; font-size: 9px; letter-spacing: .05em; color: var(--fg2); background: var(--bg); border: 1px solid var(--rule); border-radius: 3px; padding: 1px 5px; }
</style>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs><linearGradient id="nsu" x1="0" x2="0" y1="1" y2="0"><stop offset="0" style="stop-color:var(--c-stop)"/><stop offset=".4" style="stop-color:var(--c-stop)"/><stop offset="1" style="stop-color:var(--c-sig)"/></linearGradient><linearGradient id="nsd" x1="0" x2="0" y1="0" y2="1"><stop offset="0" style="stop-color:var(--c-stop)"/><stop offset=".4" style="stop-color:var(--c-stop)"/><stop offset="1" style="stop-color:var(--c-sig)"/></linearGradient></defs></svg>"""
def sec(n): return f'<p class="secn">{n}</p>'
KEY = ('<div class="key"><span><svg width="26" height="4"><rect width="26" height="2" y="1" fill="var(--fg)"/></svg>delay curve</span>'
       '<span><svg width="9" height="20" viewBox="0 0 9 21"><rect class="tl" x=".5" y=".5" width="8" height="20" rx="3"/><circle cx="4.5" cy="4.6" r="2" fill="#ff5a52"/><circle cx="4.5" cy="10.5" r="2" fill="#ffc233"/><circle cx="4.5" cy="16.4" r="2" fill="#35c96b"/></svg>traffic light</span>'
       '<span><i class="k-stopname">Arthur Av</i>bus stop</span><span><i class="k-sgn">ARTHUR AV</i>cross street</span></div>')
b0, b1, a0, a1 = ST["before"]["dir0"], ST["before"]["dir1"], ST["after"]["dir0"], ST["after"]["dir1"]
def add(p, q): return {k: p[k] + q[k] for k in p}
PB, PA = add(parts(b0), parts(b1)), add(parts(a0), parts(a1)); TB, TA = sum(PB.values()), sum(PA.values())
mn = lambda s: f"{s / 60:.1f}"; pct = lambda a, b: f"{abs(100 * (a / b - 1)):.0f}"
hero_before = bap.hero({**cB, "gap": 8, "lanes": [dict(l, status="planned", text="BUSWAY") for l in cB["lanes"][:2]]}, DB, ua, ub).replace("PLANNED BUSWAY", "BUSWAY")
import re; hero_before = re.sub(r'<rect class="sgn"[^>]*/><text class="t-x"[^>]*>[^<]*</text>', "", hero_before)
vsvg, FD, ftot, FM = vflat(); fs = {d: bap.stats(FD, d, 0, 1e9) for d in ("dir0", "dir1")}; fnb = "dir0" if FD["dir0"]["bearing"] == "North" else "dir1"; fsb = "dir1" if fnb == "dir0" else "dir0"
hb = {d: {t: parts(dict(freeflow=H5[d][t]["hours"]["ff"], **{k: H5[d][t]["hours"][k] for k in ("stop", "nearside", "signal", "other")})) for t in ("ordinary", "match")} for d in H5}; he = next(d for d in H5 if H5[d]["bearing"] == "east"); hw = next(d for d in H5 if H5[d]["bearing"] == "west")
HO, HM = add(hb[he]["ordinary"], hb[hw]["ordinary"]), add(hb[he]["match"], hb[hw]["match"])
c = G.F["budget"]["city"]; maplegend = '<div class="legend"><span><i style="border-color:var(--c-miss)"></i>Worst-delay miles outside the plan</span><span><i style="border-color:var(--c-plan)"></i>Worst-delay miles on a plan corridor</span><span><i style="border-color:var(--c-plan);opacity:.4"></i>Rest of the plan corridors</span><span><i style="border-color:var(--m-base);border-top-width:2px"></i>Other bus streets</span></div>'
LEG = G.legend(("ff", "stop", "lights", "near", "other"))
body = EXTRA + f'''
<div class="col">
<p class="kicker">Opinion pitch · mock-up</p>
<h1>Half a Mile in the Bronx Shows How to Fix New York’s Buses</h1>
<p class="dek">We followed every city bus, block by block, for seven months. One short street, closed to through traffic in September, explains where the time goes and what gets it back.</p>
<p class="byline">Mock-up for a pitch · Analysis of bus position reports, 27 April to 21 November 2026 · Draft text, real data</p>
</div>
{sec(1)}
<div class="lede"><div class="txt">
<p>East Tremont Avenue crosses the middle of the Bronx, and the Bx36 crawls along it. Last spring, between Third Avenue and Southern Boulevard, a bus needed {mn(b0["total"])} minutes to go six-tenths of a mile eastbound. That is {b0["miles"] / (b0["total"] / 3600):.1f} miles an hour, about the pace of a brisk walk.</p>
<p>On 19 September the city turned that stretch into a busway. From 6am to 8pm, cars must turn off at the next corner. It is the first busway in the Bronx, and it gives a clean before-and-after test of an old argument: is it traffic that slows the buses, or everything else?</p>
</div>
<figure>{inset()}<figcaption>The busway runs eastbound from Third Avenue to Southern Boulevard, and westbound from Southern Boulevard to Belmont Avenue.</figcaption></figure></div>
{sec(2)}
{fig("Before the busway: about a quarter of the trip was spent moving", "Minutes per bus inside the future busway, both directions added together, on weekdays from 18 May to 26 June.", timebars([("", [("Before the busway", PB, "Both directions, before the busway")])], "Time per bus inside the future busway before it opened", compare=False), f"Eastbound and westbound added together: {b0['miles']:.2f} miles eastbound and {b1['miles']:.2f} westbound, where the busway is shorter. Moving is the time the trip would take with green lights, no stops and no traffic. Stop + red light is a bus stop placed just before a light, where the two delays cannot be told apart.", legend=LEG)}
<div class="col">
<p>Add the two directions together and a bus spent {mn(PB["ff"])} of its {mn(TB)} minutes actually moving. Bus stops and red lights took {mn(PB["stop"] + PB["near"] + PB["lights"])} minutes. The last {mn(PB["other"])} went to other traffic: queues, double-parked cars, turning vehicles.</p>
<p>That split is the heart of this piece, so here is how it is made.</p>
</div>
{sec(3)}
<figure class="wide"><p class="fig-h">How the time is split: every peak belongs to a stop or a light</p><p class="fig-s">Delay along East Tremont Avenue before the busway. The higher the curve, the longer buses spend on that spot beyond a clear run. Hover over any peak, or any traffic light on the street.</p>{G.legend(("stop", "lights", "near", "other"))}{KEY.replace('<span><i class="k-sgn">ARTHUR AV</i>cross street</span>', "")}<div class="chart">{hero_before}</div>
<figcaption>Weekdays, 18 May to 26 June. {DIRN["dir0"]} is on top and runs right to left; {DIRN["dir1"].lower()} is underneath, drawn upside down, so each traffic light lines up in all three bands. The dashed band on the street marks where the busway later went in. Buses report their position about every 30 seconds, so a short stop is spread over a few hundred feet of curve.</figcaption></figure>
<div class="col">
<p>For every 10 feet of street we average the time buses spend there and subtract the time a bus takes late at night with nothing in the way. What is left is delay, and it comes in peaks. A peak at a bus stop is blue. A peak at a traffic light is red. Where the stop sits just before the light, the two cannot be separated, and the peak fades from blue to red.</p>
<p>Underneath the peaks is a level the curve never drops below, even in the middle of a block. That is the orange: time lost to other traffic, wherever the bus happens to be.</p>
</div>
{sec(4)}
{fig("With the busway, the orange shrank the most", "Minutes per bus inside the busway, both directions added together, before it opened and with it in place.", timebars([("", [("Before", PB, "Both directions, before"), ("With the busway", PA, "Both directions, with the busway")])], "Time per bus inside the busway before and after it opened"), "Before: weekdays 18 May to 26 June. With the busway: weekdays 28 September to 20 November. Whole day, although the busway is in force from 6am to 8pm. The two periods are different seasons; on other Bronx bus streets travel time did not change between them.", legend=LEG)}
<div class="col">
<p>The two trips through the busway fell from {mn(TB)} minutes to {mn(TA)}. The biggest change is the orange: other traffic cost {pct(PA["other"], PB["other"])} percent less. Time at bus stops changed far less. Westbound, where the busway is shorter, the trip is a quarter quicker; eastbound, a tenth. Of 256 half-mile stretches of Bronx bus street, this was the single most improved.</p>
<p>A busway did what a busway should. It took out traffic, and left the stops and most of the lights as they were.</p>
</div>
{sec(5)}
<div class="col">
<h2>The accidental busway</h2>
<p>Manhattan ran the same experiment by accident. On eight days in June and July, for World Cup matches across the river, the city closed 42nd Street to everything but buses from six hours before kickoff. Nobody called it a transit project. The M42, named the city’s slowest route again this month, ran about 20 percent faster while the street was closed. Before the barriers went up each morning, it ran exactly as slowly as usual.</p>
</div>
{fig("With cars off 42nd Street, the M42 sped up, and only then", "Average speed of the M42 along 42nd Street, by hour, on seven World Cup match days and on ordinary days.", G.worldcup_all(), "Match days: three weekdays, two Saturdays and two Sundays. The eighth, a Monday with an 8pm kickoff, is left out because its closure began only at 2pm. Ordinary days: 64 days from 18 May to 31 July, with weekdays, Saturdays and Sundays averaged in the same three-two-two mix. Grey band: the usual day-to-day range (one standard deviation). Faint lines are the seven match days, smoothed.")}
{fig("M42 travel time on 42nd Street, each day", "Each dot is one day’s travel time along 42nd Street, against the average for that kind of day (weekday, Saturday or Sunday).", m42days(), "Whole-day travel time per mile, 18 May to 31 July, so the hours before each closure are included. All seven match days were faster than usual, and five were faster than almost every ordinary day.")}
{sec(6)}
{fig("On 42nd Street too, the time that disappeared was traffic", "Minutes per bus along the length of 42nd Street and back, between noon and 8pm: ordinary days, and the seven match days when the street was closed to cars.", timebars([("", [("Ordinary days", HO, "Both directions, ordinary days, noon to 8pm"), ("Days with no cars", HM, "Both directions, match days, noon to 8pm")])], "Time per M42 bus along 42nd Street from noon to 8pm on ordinary days and on match days"), "Eastbound and westbound added together. The total for these hours is measured directly. The split by cause is an estimate: our delay curves are built per day, not per hour, so each cause’s share for noon to 8pm is scaled from the whole-day curve by how much stopped and crawling time fell at stops and lights in those hours. Ordinary days are weighted to the same mix of weekdays, Saturdays and Sundays as the match days. An MTA alert shows the stops at First and Second Avenues were closed during the World Cup.", legend=LEG)}
<div class="col">
<p>The pattern is the one from the Bronx. With the cars gone, the trip there and back took {mn(sum(HM.values()))} minutes where it usually takes {mn(sum(HO.values()))}, and other traffic cost {pct(HM["other"], HO["other"])} percent less. Red lights cost less as well, because a bus no longer waits behind a queue to reach them. Bus stops took about as long as ever.</p>
</div>
{sec(7)}
<div class="col">
<h2>Everything else the city tried</h2>
<p>The busway and the match days were not the only experiments of 2026. The city also painted new bus lanes, gave buses priority at more than 280 traffic lights and began fining drivers from cameras on 22 more routes. Each started on a known date, so each can be measured the same way.</p>
</div>
{fig("What each change did to bus travel time", "Change on the streets where it happened, compared with similar streets over the same weeks. Bars to the left are faster buses.", G.scoreboard(merged=MERGED), "Busway: weekdays 18 May to 26 June against 28 September to 20 November, inside the busway limits, both directions combined, net of the change on other Bronx streets. Temporary busway: noon to 8pm on seven match days against 64 ordinary days in the same mix of weekdays, Saturdays and Sundays. Broadway, Queens: the five weeks before the lane went in (week of 1 June) against June to November, Q70 buses eastbound from 69th Street to Roosevelt Avenue; signal priority was added at the same time. Madison and Lexington: against neighbouring blocks of the same avenue; Madison’s “before” is a construction period. Signal priority and cameras: four weeks before and after each start date, against other streets in the same borough; lines show the 95 percent range.")}
<div class="col">
<p class="pull">The measures that are easy to announce barely register. The one that takes road space from cars works everywhere it has been tried.</p>
</div>
{sec(8)}
<div class="col">
<h2>The whole city, split the same way</h2>
<p>East Tremont Avenue is not unusual. Apply the same method to every route in the city and the average bus, at {c["mph"]:.1f} miles an hour, looks much like the Bx36 did last spring.</p>
</div>
{fig("Out of every hour on a bus, 21 minutes are spent moving", "Where the time goes on the average New York City bus, all routes, all days.", G.budget(), "Moving is the time the trip would take with green lights, no stops and no traffic. Our citywide speed, 8.3 mph, matches the MTA’s own figure for 2026. Source: analysis of MTA bus position reports, 27 April to 21 November 2026.", legend=G.legend())}
<div class="col">
<p>Twenty-one minutes of the hour go to red lights and to stops that sit beside them. Thirteen go to other traffic, the part a busway removes. Five are plain bus stops. Manhattan loses the most to traffic; Staten Island, with its long open roads, the least.</p>
</div>
{sec(9)}
<div class="col">
<h2>The plan, and what it leaves out</h2>
<p>In July the city and the state published a plan to speed up buses on 50 priority corridors by 20 percent. It is the most serious bus plan New York has had, and its corridors are well chosen: they lose more than twice as much time per mile as the average bus street. But it is small. We laid it over our map of delay.</p>
</div>
{fig("Half of the rush-hour delay is on these streets. Two-thirds of them are not in the plan.", "Thick lines are the 220 miles of street where buses lose the most time per mile; together they hold half of all rush-hour lost time. Blue is inside one of the plan’s 50 corridors; orange is outside. Numbers mark the twelve streets that lose the most time and are not in the plan.", G.citymap(), "Weekdays, 6 to 10am and 3 to 7pm; both directions of a street counted together. Lost time is time beyond a clear run. Corridors are from “Next Stop: Fast Buses, Better Service” (July 2026), which prints start and end points for 24 of its 50 corridors; the rest are read from its map, good to a few blocks.", scroll=False, legend=maplegend)}
<div class="col">
<p>Of the 220 worst miles, 77 are on a plan corridor and 143 are not. The street that loses the most bus time in New York is Fifth Avenue: 263 bus-hours every weekday in rush hours alone. It is not on the list. Nor are Queens Boulevard, Hillside Avenue, Hylan Boulevard or Third Avenue.</p>
</div>
<figure class="wide"><p class="fig-h">The streets with the most bus delay outside the plan</p><p class="fig-s">Bars show what the delay on each street is made of. Suggested treatments follow from the mix.</p>{G.legend(("stop", "lights", "near", "other"))}{G.cards()}<figcaption>Bus-hours lost are per weekday in rush hours, both directions, beyond a clear run; a street counts as outside the plan when under a tenth of its lost time is on a plan corridor. The bars use the all-day split of each street’s delay. Eight of the twelve already have painted bus lanes or a part-time busway on some of their length; for those the suggestion is physical protection or busway hours.</figcaption></figure>
{sec(10)}
<div class="col">
<h2>The lights</h2>
<p>A busway fixes the orange. It does not fix the red. For that there is signal priority: a light that sees the bus coming and holds its green. The city turned it on at more than 280 intersections during these months. At the average light it saved a bus 0.7 seconds.</p>
</div>
{fig("Signal priority: three lights where it worked, and all the others", "Seconds a bus loses at the light, week by week, and the change at every light where priority was switched on.", G.tsp(), "Top: weekly averages on weekdays; horizontal lines are the averages before and after. Bottom: each dot is one approach to a light, four weeks after against four weeks before, net of the change at other lights in the same borough. The black line is the average.")}
<div class="col">
<p>A few intersections show what the technology can do: about a fifth of the wait, gone in a week and gone for good. The cloud of dots shows what it usually does. Where a bus stop sits just before the light, priority did nothing at all, because a bus that is loading passengers cannot use a green.</p>
</div>
{sec(11)}
<div class="col">
<h2>Looking ahead: Flatbush Avenue</h2>
<p>The next test is already under way. Flatbush Avenue is Brooklyn’s spine. The city plans to rebuild it for bus rapid transit, and the first piece opened on 17 November: centre-running bus lanes with boarding islands, from Livingston Street to Grand Army Plaza.</p>
<p>Below is the whole avenue as it was during our seven months, almost entirely before those lanes. A northbound bus takes {mn(fs[fnb]["total"])} minutes end to end and spends {mn(fs[fnb]["freeflow"])} of them moving. Scroll from Downtown Brooklyn to Kings Plaza; every peak is a stop or a light with a name, and every one is a place where the new design either works or does not.</p>
</div>
<figure class="wide"><p class="fig-h">All of Flatbush Avenue, top to bottom</p><p class="fig-s">Delay along {fs[fnb]["miles"]:.1f} miles of Flatbush Avenue, north at the top. Southbound buses are on the left, northbound on the right, with the street between them. Hover over any peak, any traffic light, or the orange band, which is cut into stretches at each dip in other traffic.</p>{G.legend(("stop", "lights", "near", "other"))}{KEY.replace("Arthur Av", "Church Av").replace('<span><i class="k-sgn">ARTHUR AV</i>cross street</span>', "")}<div class="scroll vwrap">{vsvg}</div>
<figcaption>All B41 trips, local and Limited, 27 April to 21 November 2026. Red strips on the street are the centre bus lanes that opened on 17 November; only four days of data come after them. Peaks taller than 60 seconds per 100 feet are cut off at the edge.</figcaption></figure>
<div class="col">
<div class="box"><h3>How we did this</h3>
<p>Every MTA bus reports its position about every 30 seconds. We rebuilt each trip’s path along its route, for 209 days and about 31 billion 10-foot steps, and averaged the time spent on each step. Subtracting the time a bus takes late at night with nothing in the way leaves the delay. Peaks in that delay are credited to the bus stop or traffic light they sit at; the smooth level underneath the peaks is other traffic.</p>
<p><b>Limits.</b> None of these was a randomised trial. Positions every 30 seconds blur short stops. There is no door data, so stop delay includes pulling in and out. On streets with a light every block, some light and stop delay is counted as other traffic. The busway comparison spans spring to autumn. Match days bring their own crowds. Bus-hours count buses, not riders.</p></div>
</div>
'''
tail_fix = "line(layer, dot.dataset.x, +dot.dataset.y + (+z.dataset.ay < +dot.dataset.y ? -11 : 11), z.dataset.ax, z.dataset.ay);"
assert tail_fix in shell.TAIL
html = shell.page("How to Fix the Buses", body).replace(tail_fix, "if (svg.classList.contains('vflat')) line(layer, +dot.dataset.x + (+z.dataset.ax < +dot.dataset.x ? -13 : 13), dot.dataset.y, z.dataset.ax, z.dataset.ay); else " + tail_fix)
html = html.replace("centre", "center").replace("Centre", "Center"); assert "banned" not in html.lower()
open(shell.HERE / "pitch_f.html", "w").write(html); print(len(html) // 1024, "KB", "merged tremont", round(MERGED, 1))
print({t: {d: {k: round(v) for k, v in parts(ST[t][d]).items()} for d in ST[t]} for t in ST}); print("flatbush", {d: (FD[d]["bearing"], {k: round(v / 60, 1) for k, v in fs[d].items() if k not in ("miles", "n")}) for d in fs}); print([m["name"] for m in FM][:30])
