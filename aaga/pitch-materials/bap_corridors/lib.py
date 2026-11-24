"""Generic corridor delay analysis from the stored network outputs (traj_speed), any route / street.
Pools every trip of the given routes over every processed day. Same zone method as the B41 Flatbush page."""
import json, ast, glob, html, duckdb, numpy as np, pandas as pd
from pathlib import Path
from scipy.signal import find_peaks
from scipy.ndimage import percentile_filter, gaussian_filter1d
REPO = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists()); HERE = Path(__file__).parent
FT = 3.28084; BM = 10.0 / FT; STEP = 5.0
UP, DN = 1500.0, 750.0      # a territory never reaches further than this from its peak (only binds on long featureless road)
def owner(Z, n):
    """category that owns each point of the curve above the baseline ('' = nobody: counts as other traffic)"""
    o = np.full(n, "", dtype=object)
    for _, z in Z.iterrows(): o[int(z.ia):int(z.ib) + 1] = z["cat"]
    return o
def split(a, m):
    """seconds per bus above the baseline inside mask m, by owner"""
    ex = (a["e"] - a["bg"]); return {k: float(ex[m & (a["own"] == k)].sum() * STEP / 100) for k in ("stop", "nearside", "signal")}
def baseline(e):
    """rolling 5th percentile (+-750 ft) smoothed into a rolling hill, never above the curve: everything under it is other traffic"""
    return np.minimum(gaussian_filter1d(percentile_filter(e, 5, size=int(1500 / STEP) + 1, mode="nearest"), 120.0 / STEP, mode="nearest"), e)
def territories(Z, e):
    """every peak that belongs to a stop or light owns the curve (down to the baseline) out to the lowest point between it and the next such peak;
    peaks with no feature fold into a neighbour"""
    Z = Z[Z["cat"] != "other"].copy(); ip = Z.ip.astype(int).values; ia = []; ib = []; a = 0
    for j, p in enumerate(ip):
        if j < len(ip) - 1: cut = p + int(np.argmin(e[p:ip[j + 1] + 1])); b = max(cut - 1, a)
        else: cut = len(e); b = len(e) - 1
        ia.append(max(a, p - int(UP / STEP))); ib.append(min(b, p + int(DN / STEP))); a = cut
    Z["ia"] = ia; Z["ib"] = ib; return Z
_reg = _ff = None
def reg():
    global _reg, _ff
    if _reg is None:
        _reg = json.load(open(REPO / "outputs/network/nyc/segment_registry.json"))
        _ff = json.load(open(REPO / "outputs/network/nyc/freeflow.json"))["freeflow"]
    return _reg
def _lst(v): return ast.literal_eval(v) if isinstance(v, str) else v
def street_run(shape_id, names):
    """longest run of consecutive segments of a shape on the named street(s); gaps of up to 2 other segments are bridged"""
    r = reg(); seq = _lst(r["shapes"][shape_id]["seg_seq"]); on = [r["segments"][s]["name"] in names for s in seq]
    best = (0, 0, 0.0); i = 0
    while i < len(seq):
        if not on[i]: i += 1; continue
        j = i; gap = 0; last = i
        while j + 1 < len(seq) and (on[j + 1] or gap < 2):
            j += 1
            if on[j]: last = j; gap = 0
            else: gap += 1
        L = sum(r["segments"][s]["len_m"] for s in seq[i:last + 1])
        if L > best[2]: best = (i, last + 1, L)
        i = last + 1
    return seq[best[0]:best[1]], best[2]
def best_shape(route, direction, names):
    r = reg(); c = [(street_run(k, names)[1], k) for k, v in r["shapes"].items() if v["route_id"] == route and v["direction"] == direction]
    return max(c) if c else (0.0, None)
def corridor(route, direction, names):
    L, sh = best_shape(route, direction, names); r = reg(); segs = []; x = 0.0
    if sh is None: return []
    for sid in street_run(sh, names)[0]:
        s = r["segments"][sid]
        segs.append(dict(seg_id=sid, x0=x, len_m=s["len_m"], label=s["label"], name=s["name"], t_ff=_ff.get(sid, {}).get("t_ff_s", np.nan),
                         geom=s["geometry_lonlat"], stops=s["stops_off"], crossings=s.get("crossings_off", [])))
        x += s["len_m"]
    return segs
def profile(routes, segs, lo="0", hi="9"):
    """mean seconds per 10 ft bucket over all days (or lo <= date <= hi), all trips of the routes"""
    files = [f for rt in routes for f in glob.glob(str(REPO / f"outputs/network/nyc/traj_speed/service_date=*/route={rt}.parquet"))]
    files = [f for f in files if lo <= f.split("service_date=")[1][:10] <= hi]
    con = duckdb.connect(); con.register("C", pd.DataFrame(dict(seg_id=[s["seg_id"] for s in segs])))
    q = con.sql(f"select seg_id::varchar as seg_id, bucket, sum(n) as n, sum(sum_dt) as s from read_parquet({files!r}) join C using (seg_id) group by all").df()
    g = q.set_index(["seg_id", "bucket"]); out = []; have = set(q.seg_id)
    for s in segs:
        nb = int(np.ceil(s["len_m"] / BM)); k = np.arange(nb); bl = np.full(nb, BM); bl[-1] = s["len_m"] - (nb - 1) * BM
        if s["seg_id"] in have:
            qq = g.loc[s["seg_id"]]; dt = (qq.s / qq.n).reindex(k).values; n = qq.n.reindex(k).fillna(0).values
        else: dt = np.full(nb, np.nan); n = np.zeros(nb)
        x1 = s["x0"] + s["len_m"] - k * BM
        out.append(pd.DataFrame(dict(seg_id=s["seg_id"], bucket=k, x_ft=(x1 - bl / 2) * FT, len_ft=bl * FT, dt=dt, ff=s["t_ff"] * bl / s["len_m"], n=n)))
    return pd.concat(out).sort_values("x_ft").reset_index(drop=True), len(set(f.split("service_date=")[1][:10] for f in files))
def stop_name(n):
    p = [w.strip() for w in n.replace(" / ", "/").split("/")]
    return (p[-1] if len(p) > 1 else p[0]).title().replace(" Av", " Av").replace("Blvd", "Blvd")
def features(segs):
    sig = []; stops = []
    for s in segs:
        end = (s["x0"] + s["len_m"]) * FT; cross = s["label"].split("→")[-1].strip()
        sig.append(dict(x=end, name=cross))
        for c in s["crossings"]:
            if c["type"] == "ped_crossing_signal": sig.append(dict(x=end - c["off_m"] * FT, name="pedestrian signal"))
        for st in s["stops"]: stops.append(dict(x=end - st["off_m"] * FT, name=stop_name(st["name"]), id=st["id"]))
    return (pd.DataFrame(sig).sort_values("x").reset_index(drop=True),
            pd.DataFrame(stops, columns=["x", "name", "id"]).drop_duplicates("id").sort_values("x").reset_index(drop=True))
def smooth(x, y, sig=15.0, step=STEP):
    xs = np.arange(0, x.max() + step, step); ok = ~np.isnan(y); out = np.zeros(len(xs))
    for a in range(0, len(xs), 2000):
        w = np.exp(-.5 * ((xs[a:a + 2000, None] - x[None, ok]) / sig) ** 2) / (sig * np.sqrt(2 * np.pi)); out[a:a + 2000] = (w * y[ok][None, :]).sum(1) * 100
    return xs, out
def analyse(g, segs):
    """effect zones on the delay curve, peak by peak (identical rules to the B41 page, all stops)"""
    sig, ls = features(segs)
    y = (g.dt - g.ff).fillna(0); xs, e = smooth(g.x_ft.values, y.values)
    raw = float(y.sum()); e = e * raw / (e.sum() * STEP / 100)
    bg = baseline(e)
    pk, _ = find_peaks(e, prominence=1.5, distance=int(30 / STEP)); zones = []
    for i, p in enumerate(pk):
        lo = pk[i - 1] + int(np.argmin(e[pk[i - 1]:p])) if i > 0 else 0
        hi = p + int(np.argmin(e[p:pk[i + 1]])) if i < len(pk) - 1 else len(e) - 1
        thr = bg[p] + max(1.0, 0.10 * (e[p] - bg[p])); a = b = p
        while a > lo and e[a] > thr and (p - a) * STEP < 600: a -= 1
        while b < hi and e[b] > thr and (b - p) * STEP < 300: b += 1
        zones.append(dict(apex=xs[p], x0=xs[a], x1=xs[b], height=e[p] - bg[p], ia=a, ib=b, ip=p))
    Z = pd.DataFrame(zones); sx = sig.x.values; lx = ls.x.values; fst = []; fsg = []
    for _, z in Z.iterrows():
        ds = z.apex - lx; c = np.where((ds >= -150) & (ds <= 100))[0]; fst.append(int(c[np.argmin(np.abs(ds[c]))]) if len(c) else None)
        dg = sx - z.apex; c = np.where((dg >= -30) & (dg <= 300))[0]; fsg.append(int(c[np.argmin(dg[c])]) if len(c) else None)
    Z["st"] = fst; Z["sg"] = fsg
    for j, s in ls.iterrows():
        if not (Z.st == j).any():
            c = Z[(Z.apex - s.x > 100) & (Z.apex - s.x <= 200) & (Z.height >= 10)]
            if len(c): Z.loc[c.height.idxmax(), "st"] = j
    cat = []; name = []
    for _, z in Z.iterrows():
        st = None if pd.isna(z.st) else int(z.st); sg = None if pd.isna(z.sg) else int(z.sg)
        if st is not None and z.height < 3 and abs(z.apex - lx[st]) > 60: st = None
        if st is not None and sg is not None:
            gap = sx[sg] - lx[st]
            if 0 <= gap <= 150 or (gap > 0 and sx[sg] - z.apex <= 150) or (-50 <= gap < 0 and sx[sg] - z.apex >= 0): cat.append("nearside"); name.append(ls.name[st])
            elif abs(z.apex - lx[st]) <= 100 or z.apex - lx[st] > 0: cat.append("stop"); name.append(ls.name[st])
            else: cat.append("signal"); name.append(sig.name[sg])
        elif st is not None: cat.append("stop"); name.append(ls.name[st])
        elif sg is not None: cat.append("signal"); name.append(sig.name[sg])
        else: cat.append("other"); name.append("")
    Z["cat"] = cat; Z["name"] = name
    Z = territories(Z, e); Z["x0"] = xs[Z.ia.values]; Z["x1"] = xs[Z.ib.values]
    Z["sec"] = [float(((e - bg)[int(z.ia):int(z.ib) + 1]).sum() * STEP / 100) for _, z in Z.iterrows()]
    return dict(xs=xs, e=e, bg=bg, Z=Z, sig=sig, ls=ls, g=g, own=owner(Z, len(e)))
# ---------------------------------------------------------------- geometry: put both directions on one axis
def polyline(segs):
    """(x_ft along corridor, east_m, north_m) densely sampled"""
    P = []; lat0 = 40.7
    for s in segs:
        ll = np.array(s["geom"]); xy = np.c_[(ll[:, 0]) * 111320 * np.cos(np.radians(lat0)), ll[:, 1] * 110540]
        d = np.r_[0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        if d[-1] == 0: continue
        t = np.linspace(0, d[-1], max(2, int(d[-1] / 3)))
        P.append(np.c_[(s["x0"] + t / d[-1] * s["len_m"]) * FT, np.interp(t, d, xy[:, 0]), np.interp(t, d, xy[:, 1])])
    return np.vstack(P)
def u_map(segs_top, segs_ref):
    """function x_top -> u (distance along the reference direction), by nearest point; forced monotone"""
    A, B = polyline(segs_top), polyline(segs_ref); u = np.empty(len(A)); dist = np.empty(len(A))
    for a in range(0, len(A), 500):
        d2 = (A[a:a + 500, None, 1] - B[None, :, 1]) ** 2 + (A[a:a + 500, None, 2] - B[None, :, 2]) ** 2
        k = d2.argmin(1); u[a:a + 500] = B[k, 0]; dist[a:a + 500] = np.sqrt(d2.min(1))
    um = np.minimum.accumulate(u)                      # top direction runs against the reference
    return (lambda x: np.interp(x, A[:, 0], um)), A[:, 0], um, dist
def bearing(segs):
    a, b = segs[0]["geom"][0], segs[-1]["geom"][-1]; dx = (b[0] - a[0]) * np.cos(np.radians(a[1])); dy = b[1] - a[1]
    return ("East" if dx > 0 else "West") if abs(dx) > abs(dy) else ("North" if dy > 0 else "South")
