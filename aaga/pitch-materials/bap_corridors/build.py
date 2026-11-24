"""Mirrored delay graphics for one-mile corridor sections. Config in corridors.py."""
import sys, pickle; from lib import *
from corridors import CORRIDORS
esc = html.escape
CATS = [("freeflow", "Moving time (free flow)"), ("stop", "Bus stop"), ("nearside", "Near-side bus stop (stop + light)"), ("signal", "Traffic light"), ("other", "Other traffic")]
CATLAB = dict(CATS); CATLAB.update(lane="Bus lane", island="Bus stop improvement")
FILL = {"freeflow": "var(--c-ff)", "stop": "var(--c-stop)", "nearside": "url(#ns)", "signal": "var(--c-sig)", "other": "var(--c-oth)"}
def tt(h, cat, big, rows, sub="", cls="hz"):
    return (f' class="{cls}" data-h="{esc(h)}" data-d="{esc(sub)}" data-c="{cat}" data-k="{esc(CATLAB.get(cat, cat))}" data-v="{esc(big)}"'
            f' data-r="{esc("|".join(f"{k}={v}" for k, v in rows))}"')
def path(xs, ys, X, Y): return "M" + " L".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in zip(xs, ys))
def zone_shape(a, ia, ib, X, Y):
    xs = a["xs"][ia:ib + 1]
    return ("M" + " L".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in zip(xs, a["e"][ia:ib + 1])) + " L"
            + " L".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in zip(xs[::-1], a["bg"][ia:ib + 1][::-1])) + " Z")
def under(xs, e, X, Y, extra=""):
    return f'<path d="{path(xs, e, X, Y)} L{X(xs[-1]):.1f},{Y(0):.1f} L{X(xs[0]):.1f},{Y(0):.1f} Z" fill="var(--c-oth)"{extra}/>'
def tlight(x, y):
    return (f'<rect class="tl" x="{x - 4.2:.1f}" y="{y - 10:.1f}" width="8.4" height="20" rx="3"/>'
            f'<circle cx="{x:.1f}" cy="{y - 5.9:.1f}" r="2.1" fill="#ff5a52"/><circle cx="{x:.1f}" cy="{y:.1f}" r="2.1" fill="#ffc233"/><circle cx="{x:.1f}" cy="{y + 5.9:.1f}" r="2.1" fill="#35c96b"/>')
def sign(x, y, name):
    w = 6.3 * len(name) + 12
    return f'<rect class="sgn" x="{x - w / 2:.1f}" y="{y - 10.5:.1f}" width="{w:.1f}" height="14" rx="3"/><text class="t-x" x="{x:.1f}" y="{y:.1f}" text-anchor="middle">{esc(name.upper())}</text>'
def load(c):
    """both directions analysed; top direction mapped onto the reference (bottom) axis u"""
    cache = HERE / "cache" / (c["key"] + ".pkl")
    if cache.exists():
        out = pickle.load(open(cache, "rb"))
        for d in ("dir0", "dir1"): out[d]["a"] = analyse(out[d]["a"]["g"], out[d]["segs"])     # floor settings may have changed since the cache was written
        return out
    out = {}
    for d in ("dir0", "dir1"):
        segs = corridor(c["routes"][0], d, c["streets"]); g, nd = profile(c["routes"], segs)
        out[d] = dict(segs=segs, a=analyse(g, segs), days=nd, bearing=bearing(segs))
    cache.parent.mkdir(exist_ok=True); pickle.dump(out, open(cache, "wb")); return out
def section(c, D):
    """reference direction = dir1 (drawn underneath, left to right). Returns u-window and x<->u maps."""
    ref, top = "dir1", "dir0"; f, ax, au, dist = u_map(D[top]["segs"], D[ref]["segs"]); D["fu"] = f
    def exc(d, lo, hi):
        g = D[d]["a"]["g"]; u = g.x_ft.values if d == ref else f(g.x_ft.values); m = (u >= lo) & (u < hi); return float((g.dt - g.ff).fillna(0)[m].sum())
    L = float(D[ref]["a"]["xs"].max())
    if c.get("u"): return c["u"]
    else:
        lo_all, hi_all = c.get("within", (0, L)); cand = np.arange(lo_all, max(lo_all + 1, hi_all - 5280), 100.0)
        ua = float(max(cand, key=lambda u0: exc(ref, u0, u0 + 5280) + exc(top, u0, u0 + 5280)))
    return ua, min(ua + 5280, L)
def merged_signals(D, U0, U1):
    f = D["fu"]; S = {"dir1": [(i, s.x, s["name"]) for i, s in D["dir1"]["a"]["sig"].iterrows()], "dir0": [(i, float(f(s.x)), s["name"]) for i, s in D["dir0"]["a"]["sig"].iterrows()]}
    used = set(); out = []
    for i, u, nm in S["dir0"]:
        cnd = sorted(((n2 != nm, abs(u - u2), j, u2, n2) for j, u2, n2 in S["dir1"] if j not in used and ((n2 == nm and abs(u - u2) <= 200) or abs(u - u2) <= 130)))
        if cnd:
            _, _, j, u2, n2 = cnd[0]; used.add(j); out.append(dict(u=(u + u2) / 2, name=nm if n2 == nm else f"{nm} / {n2}", dir0=i, dir1=j))
        else: out.append(dict(u=u, name=nm, dir0=i, dir1=None))
    out += [dict(u=u, name=nm, dir0=None, dir1=j) for j, u, nm in S["dir1"] if j not in used]
    out = sorted([m for m in out if U0 < m["u"] < U1], key=lambda m: m["u"])
    for k, m in enumerate(out): m["id"] = f"{D['key']}m{k}"
    return out
def short(n): return n.replace(" Avenue", " Av").replace(" Street", " St").replace(" Boulevard", " Blvd").replace(" Road", " Rd").replace(" Place", " Pl").replace(" Parkway", " Pkwy").replace("East ", "E ").replace("West ", "W ")
def hero(c, D, ua, ub):
    W, ml, mr = 1180, 40, 12; pad = 380.0; U0, U1 = ua - pad, ub + pad; ch = 140; RH = 78; f = D["fu"]; key = D["key"]
    X = lambda u: ml + (u - U0) / (U1 - U0) * (W - ml - mr)
    Ud = {"dir1": (lambda x: x), "dir0": f}; DN = {d: D[d]["bearing"] + "bound" for d in ("dir0", "dir1")}
    M = merged_signals(D, U0, U1); sid = {(d, m[d]): m["id"] for m in M for d in ("dir0", "dir1") if m[d] is not None}
    def inwin(d): a = D[d]["a"]; u = Ud[d](a["xs"]); return (u >= ua) & (u <= ub)
    emax = max(float(D[d]["a"]["e"][inwin(d)].max()) for d in ("dir0", "dir1")); ymax = float(np.ceil(emax / 20) * 20)
    yN = 46 + ch; r0y = yN + c.get("gap", 26); r1y = r0y + RH; yS = r1y + 8; H = yS + ch + 36
    Yn = lambda v: yN - min(v, ymax) / ymax * ch; Ys = lambda v: yS + min(v, ymax) / ymax * ch
    g = []; ax = []; tot = {m["id"]: {"dir0": [0.0, 0], "dir1": [0.0, 0]} for m in M}
    step = 20 if ymax <= 100 else 40
    for t in np.arange(0, ymax + 1, step):
        for Y in (Yn, Ys):
            g.append(f'<line class="grid" x1="{X(U0):.1f}" x2="{W - mr}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>')
            ax.append(f'<text class="t-ax" x="{ml - 6}" y="{Y(t) + 3:.1f}" text-anchor="end">{t:.0f}</text>')
    ax.append(f'<text class="t-dir" x="{ml}" y="{Yn(ymax) - 22:.1f}">← {DN["dir0"]}</text><text class="t-dir" x="{ml}" y="{Ys(ymax) + 30:.1f}">{DN["dir1"]} →</text>'
              f'<text class="t-ax" x="{W - mr}" y="{Yn(ymax) - 22:.1f}" text-anchor="end">delay, seconds per 100 ft</text>')
    for d, Y in (("dir0", Yn), ("dir1", Ys)):
        a = D[d]["a"]; xs = a["xs"]; uu = Ud[d](xs); Xd = lambda x, d=d: X(float(Ud[d](x)))
        idx = np.where((uu >= U0) & (uu <= U1))[0]; i0, i1 = int(idx.min()), int(idx.max()); total = float(np.nansum(a["g"].dt))
        cm = (uu >= ua) & (uu < ub); exc = float(a["e"][cm].sum() * STEP / 100); zu = Ud[d](a["Z"].apex.values)
        oth = exc - sum(split(a, cm).values())
        g.append(under(xs[i0:i1 + 1], np.minimum(a["e"][i0:i1 + 1], ymax), Xd, Y, tt("Other traffic", "other", f"{oth:.0f} s per bus", [("Counted over", "the section shown in full"), ("Share of the delay there", f"{100 * oth / max(exc, 1e-9):.0f}%")], DN[d])))
        for _, q in a["Z"].iterrows():
            ia, ib = max(int(q.ia), i0), min(int(q.ib), i1)
            if q["cat"] == "other" or ib <= ia: continue
            rows = [("Zone length", f"{q.x1 - q.x0:.0f} ft"), ("Peak height", f"{q.height:.0f} s per 100 ft")]
            if q["cat"] == "nearside": rows.insert(0, ("Light just after", a["sig"].name[int(q.sg)]))
            link = ""
            if q["cat"] in ("signal", "nearside") and (d, int(q.sg)) in sid:
                k = sid[(d, int(q.sg))]; tot[k][d][0] += q.sec; tot[k][d][1] += 1
                link = f' data-sig="{k}" data-ax="{Xd(q.apex):.1f}" data-ay="{Y(a["bg"][int(q.ip)]):.1f}"'
            g.append(f'<path d="{zone_shape(a, ia, min(ib + 1, i1), Xd, Y)}" fill="{"url(#nsr)" if q["cat"] == "nearside" and d == "dir0" else FILL[q["cat"]]}"{tt(q["name"], q["cat"], f"{q.sec:.0f} s per bus", rows, DN[d])}{link}/>')
        m = slice(i0, i1 + 1)
        g.append(f'<path class="curve thin" d="{path(xs[m], np.minimum(a["e"][m], ymax), Xd, Y)}"/>')
        last = None
        for _, s in a["ls"].assign(u=Ud[d](a["ls"].x.values)).sort_values("u").iterrows():
            if U0 < s.u < U1:
                ya, yb = (Y(ymax), Y(0)) if d == "dir0" else (Y(0), Y(ymax)); px = X(s.u)
                ye = ya if d == "dir0" else yb
                g.append(f'<line class="m-stop" x1="{px:.1f}" x2="{px:.1f}" y1="{ya:.1f}" y2="{yb:.1f}"/><rect class="d-stop" x="{px - 3.5:.1f}" y="{ye - 3.5:.1f}" width="7" height="7" rx="1.5"/>')
                if last is None or abs(px - last) > 66:
                    g.append(f'<text class="t-stop" x="{px:.1f}" y="{(ya - 8) if d == "dir0" else (yb + 15):.1f}" text-anchor="middle">{esc(s["name"])}</text>'); last = px
    # ---- street plan. rows per direction from the kerb to the centre: kerb, mid, centre (13 px each)
    row = {("dir0", "kerb"): r0y, ("dir0", "mid"): r0y + 13, ("dir0", "centre"): r0y + 26, ("dir1", "centre"): r0y + 39, ("dir1", "mid"): r0y + 52, ("dir1", "kerb"): r0y + 65}
    lanes = c.get("lanes", []); has_centre = any(l["type"] == "centre" for l in lanes); arow = "kerb" if has_centre else "centre"
    g.append(f'<rect x="{X(U0):.1f}" y="{r0y}" width="{X(U1) - X(U0):.1f}" height="{RH}" fill="var(--c-road)"/>')
    for m in M: g.append(f'<rect x="{X(m["u"]) - 1.5:.1f}" y="{r0y}" width="3" height="{RH}" fill="var(--c-xwalk)"/>')
    for l in lanes:
        for d in l.get("dirs", ("dir0", "dir1")):
            u0, u1 = max(l["u0"], U0), min(l["u1"], U1); y = row[(d, {"centre": "centre", "offset": "mid", "curb": "kerb"}[l["type"]])]
            if u1 <= u0: continue
            planned = l.get("status", "planned") != "open"
            g.append(f'<rect x="{X(u0):.1f}" y="{y + .5}" width="{X(u1) - X(u0):.1f}" height="12" fill="var(--c-lane)"{" fill-opacity=\".55\" stroke=\"var(--c-lanestroke)\" stroke-dasharray=\"5 3\"" if planned else ""}'
                     f'{tt(l["label"], "lane", l.get("when", ""), l.get("rows", []), DN[d])}/>')
            for u in np.arange(u0 + 330, u1 - 250, 700 if planned else 620):
                g.append(f'<text class="t-lane" x="{X(u):.1f}" y="{y + 9.3}" text-anchor="middle">{("PLANNED " if planned else "") + l.get("text", "BUS LANE")}</text>')
    g.append(f'<line x1="{X(U0):.1f}" x2="{X(U1):.1f}" y1="{r0y + 39}" y2="{r0y + 39}" stroke="var(--c-centre)" stroke-width="1.4"/>')
    edges = [U0] + [m["u"] for m in M] + [U1]
    mids = [(a_ + b_) / 2 for a_, b_ in zip(edges[:-1], edges[1:]) if b_ - a_ >= 150]; lastu = -1e9
    for um in mids:                                   # one arrow about every eighth of a mile, at a mid-block
        if um - lastu < 560: continue
        lastu = um
        for _ in (0,):
            x = X(um); y0, y1 = row[("dir0", arow)] + 6.5, row[("dir1", arow)] + 6.5
            g.append(f'<path class="arr" d="M{x + 12:.1f},{y0 - 1.5} h-14 v-3 l-9,4.5 l9,4.5 v-3 h14 z"/><path class="arr" d="M{x - 12:.1f},{y1 - 1.5} h14 v-3 l9,4.5 l-9,4.5 v-3 h-14 z"/>')
    for s in c.get("stops", []):
        y = row[(s["dir"], s.get("row", "mid" if has_centre else "kerb"))]
        g.append(f'<rect x="{X(s["u0"]):.1f}" y="{y + 1.5}" width="{X(s["u1"]) - X(s["u0"]):.1f}" height="10" rx="5" fill="var(--c-island)" stroke="var(--c-stop)" stroke-width="1.6"'
                 f'{tt(s["name"], "island", s.get("kind", "Boarding island"), s.get("rows", []), DN[s["dir"]])}/>')
    for m in M:
        x = X(m["u"]); t = tot[m["id"]]; both = t["dir0"][0] + t["dir1"][0]; yc = r0y + 39
        rows = [(DN[d], (f"{t[d][0]:.0f} s" if t[d][1] else "no peak found") if m[d] is not None else "no light in this direction") for d in ("dir0", "dir1")]
        g.append(f'<g id="{m["id"]}" data-x="{x:.1f}" data-y="{yc}"{tt(short(m["name"]), "signal", f"{both:.0f} s per bus" if both else "No delay peak", rows, "Traffic light, both directions", "hz sigdot")}>'
                 f'<rect x="{x - 6:.1f}" y="{r0y}" width="12" height="{RH}" fill="transparent"/>{tlight(x, yc)}</g>')
    last = -1e9
    for m in M:
        nm = short(m["name"].split(" / ")[0]); w = 6.3 * len(nm) + 12
        if m["name"] != "pedestrian signal" and X(m["u"]) - w / 2 - last > 8 and X(m["u"]) < W - 60 and X(m["u"]) > ml + 20:
            g.append(sign(X(m["u"]), r0y - 7, nm)); last = X(m["u"]) + w / 2
    o = lambda u: (X(u) - X(U0)) / (X(U1) - X(U0))
    fade = (f'<defs><linearGradient id="hf{key}" gradientUnits="userSpaceOnUse" x1="{X(U0):.1f}" x2="{X(U1):.1f}" y1="0" y2="0">'
            f'<stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="{o(ua - 60):.3f}" stop-color="#fff"/><stop offset="{o(ub + 60):.3f}" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
            f'<mask id="hm{key}" maskUnits="userSpaceOnUse" x="0" y="0" width="{W}" height="{H}"><rect width="{W}" height="{H}" fill="url(#hf{key})"/></mask></defs>')
    return (f'<div class="scroll"><svg class="hero" viewBox="0 0 {W} {H}" role="img" aria-label="Delay curves in both directions, mirrored around a plan of {esc(c["title"])}">'
            f'{fade}<g mask="url(#hm{key})">{"".join(g)}</g>{"".join(ax)}<g class="hlinks"></g></svg></div>')
def stats(D, d, ua, ub):
    a = D[d]["a"]; g = a["g"]; f = (lambda x: x) if d == "dir1" else D["fu"]; u = f(g.x_ft.values); m = (u >= ua) & (u < ub); b = g[m]
    uz = f(a["xs"]); z = split(a, (uz >= ua) & (uz < ub))
    o = dict(total=float(np.nansum(b.dt)), freeflow=float(b.ff.sum()), miles=float(b.len_ft.sum() / 5280), stop=float(z.get("stop", 0)), nearside=float(z.get("nearside", 0)), signal=float(z.get("signal", 0)), n=int(np.median(b.n)))
    o["other"] = o["total"] - o["freeflow"] - o["stop"] - o["nearside"] - o["signal"]; return o
def bars(D, ua, ub):
    W, L, bh = 920, 8, 30; rows = []; y = 24; S = {d: stats(D, d, ua, ub) for d in ("dir0", "dir1")}; mx = max(s["total"] for s in S.values())
    for d in ("dir0", "dir1"):
        r = S[d]; x = L; sc = (W - 2 * L) / mx
        rows.append(f'<text class="t-lab" x="{L}" y="{y - 7}">{D[d]["bearing"]}bound · {r["total"]/60:.1f} min for {r["miles"]:.2f} miles · {r["miles"]/(r["total"]/3600):.1f} mph</text>')
        for k, lab in CATS:
            w = r[k] * sc
            rows.append(f'<rect x="{x + 1:.1f}" y="{y}" width="{max(w - 2, 1):.1f}" height="{bh}" rx="3" fill="{FILL[k]}"{tt(lab, k, f"{r[k]/60:.1f} min per bus", [("Share of the section", f"{100*r[k]/r["total"]:.0f}%")], D[d]["bearing"] + "bound")}/>')
            if w > 40: rows.append(f'<text class="t-in{" dk" if k == "other" else ""}" x="{x + w/2:.1f}" y="{y + bh/2 + 4.5}" text-anchor="middle">{r[k]/60:.1f}</text>')
            x += w
        y += bh + 34
    return f'<svg viewBox="0 0 {W} {y - 34 + 6}" role="img" aria-label="Time breakdown per direction, minutes per bus">{"".join(rows)}</svg>', S
def sections():
    secs = []; summ = []
    for c in CORRIDORS:
        D = load(c); D["key"] = c["key"]; ua, ub = section(c, D)
        if c.get("before_after"):
            ba = []
            for d, (u0, u1) in c["before_after"]["limits"].items():
                f = (lambda x: x) if d == "dir1" else D["fu"]; t = []
                for lo, hi in c["before_after"]["periods"]:
                    g, nd = profile(c["routes"], D[d]["segs"], lo, hi); u = f(g.x_ft.values); m = (u >= u0) & (u < u1); t.append((float(np.nansum(g.dt[m])) / 60, nd, int(np.median(g.n[m]))))
                ba.append(f"{D[d]['bearing'].lower()}bound {t[0][0]:.1f} minutes before and {t[1][0]:.1f} after"); print("before/after", c["key"], d, t)
            c["text"] = c["text"].replace("{BA}", " and ".join(ba))
        b, S = bars(D, ua, ub); print(c["key"], round(ua), round(ub), {d: {k: round(v / 60, 1) for k, v in S[d].items() if k not in ("miles", "n")} for d in S}, flush=True)
        ex = sum(S[d]["total"] - S[d]["freeflow"] for d in S) / 120
        secs.append(f'''  <section class="corr" id="{c["key"]}">
    <h2>{esc(c["title"])}</h2>
    <p><span class="tag">{esc(c["borough"])}</span><span class="tag">{esc(" · ".join(c["routes"]).replace("+", "-SBS"))}</span><span class="tag">{esc(c["status"])}</span></p>
    <p>{c["text"]}</p>
    <div class="chart">{hero(c, D, ua, ub)}</div>
    <div class="chart">{b}</div>
    <p class="meta">{c.get("note", "")} Bars are minutes per bus inside the one-mile window; {S["dir0"]["n"] + S["dir1"]["n"]:,} bus trips over {D["dir1"]["days"]} days.</p>
  </section>''')
        summ.append(f'<tr><td><a href="#{c["key"]}">{esc(c["title"])}</a></td><td>{esc(c["borough"])}</td><td>{esc(", ".join(c["routes"]).replace("+", "-SBS"))}</td>' + "".join(f'<td class="n">{S[d]["miles"]/(S[d]["total"]/3600):.1f}</td>' for d in ("dir0", "dir1")) + f'<td class="n b">{ex:.1f}</td><td>{esc(c["status"])}</td></tr>')
    return secs, summ
if __name__ == "__main__":
    T = open(HERE.parent / "b41_ltd" / "template.html").read()
    head = T[T.index("<meta name"):T.index('<div class="wrap">')]; tail = T[T.index('<div id="tip"'):]
    head = head.replace("</style>", ".t-in { font-size: 12px; } .tag { display: inline-block; font-size: 12px; font-weight: 600; letter-spacing: .03em; text-transform: uppercase; color: var(--fg2); border: 1px solid var(--rule); border-radius: 999px; padding: 2px 10px; margin-right: 6px; }\n.corr { border-top: 1px solid var(--rule); padding-top: 32px; } .k-plan { width: 18px; height: 8px; background: var(--c-lane); opacity: .55; outline: 1px dashed var(--c-lanestroke); border-radius: 2px; }\n</style>", 1)
    head = head.replace("--c-road: #5d5c58;", "--c-lanestroke: #a23a2c; --c-road: #5d5c58;").replace("--c-road: #3b3b38;", "--c-lanestroke: #e9a79c; --c-road: #3b3b38;")
    secs, summ = sections()
    page = open(HERE / "page.html").read().replace("{{SECTIONS}}", "\n".join(secs)).replace("{{SUMMARY}}", "".join(summ)).replace("{{LEGEND}}", "".join(f'<span class="lg"><svg width="16" height="12"><rect width="16" height="12" rx="2" fill="{FILL[k]}"/></svg>{esc(l)}</span>' for k, l in CATS))
    open(HERE / "bus_corridors.html", "w").write('<title>Bus Corridor Delay</title>\n' + head + page + tail); print("written", (len(head) + len(page) + len(tail)) // 1024, "KB")
