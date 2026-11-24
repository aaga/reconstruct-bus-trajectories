"""Citywide delay decomposition engine. Same rules as the corridor pages (floor = rolling 5th percentile, +-750 ft; zones by peak),
run over every route shape on a pooled per-segment profile."""
import json, ast, numpy as np, pandas as pd, duckdb
from pathlib import Path
from scipy.signal import find_peaks
from scipy.ndimage import percentile_filter, gaussian_filter1d
REPO = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists()); HERE = Path(__file__).parent
FT = 3.28084; BM = 10.0 / FT; STEP = 5.0
_R = {}
def L(v): return ast.literal_eval(v) if isinstance(v, str) else v
def reg():
    if not _R:
        r = json.load(open(REPO / "outputs/network/nyc/segment_registry.json")); ff = json.load(open(REPO / "outputs/network/nyc/freeflow.json"))["freeflow"]
        S = {}
        for k, s in r["segments"].items():
            S[k] = dict(len_m=s["len_m"], name=s["name"], label=s["label"], cls=s["road_class"], tff=ff.get(k, {}).get("t_ff_s", np.nan),
                        stops=[(st["off_m"], st["id"], st["name"], st.get("signal_side")) for st in L(s["stops_off"])],
                        peds=[c["off_m"] for c in L(s.get("crossings_off", "[]")) if c.get("type") == "ped_crossing_signal"])
        _R["S"] = S; _R["shapes"] = {k: dict(route=v["route_id"], dir=v["direction"], seq=L(v["seg_seq"])) for k, v in r["shapes"].items()}
    return _R["S"], _R["shapes"]
def load_profile(sql):
    """sql -> rows seg_id, bucket, n, sum_dt ; returns dict seg -> (dt_mean per bucket [k=0 at downstream end], n per bucket)"""
    q = duckdb.sql(sql).df().sort_values(["seg_id", "bucket"]); P = {}
    for sid, g in q.groupby("seg_id", sort=False):
        b = g.bucket.values; nb = int(b.max()) + 1; dt = np.full(nb, np.nan); n = np.zeros(nb); dt[b] = g.sum_dt.values / np.maximum(g.n.values, 1); n[b] = g.n.values; P[sid] = (dt, n)
    return P
def chain(seq, P):
    """curve along a shape on a regular 5 ft grid: e = seconds above free flow per 100 ft (smoothed), plus bookkeeping"""
    S, _ = reg(); x0 = 0.0; X = []; R = []; F = []; segx = []; sig = []; stops = []; tot = 0.0; fft = 0.0; nmed = []
    for sid in seq:
        s = S[sid]; Lm = s["len_m"]; nb = int(np.ceil(Lm / BM)); bl = np.full(nb, BM); bl[-1] = Lm - (nb - 1) * BM
        if sid in P:
            dt, n = P[sid]; d = np.full(nb, np.nan); m = min(nb, len(dt)); d[:m] = dt[:m]; nmed.append(np.nanmedian(n[:m]) if m else 0)
        else: d = np.full(nb, np.nan); nmed.append(0)
        ffb = (s["tff"] if np.isfinite(s["tff"]) else np.nansum(d)) * bl / Lm
        ex = np.where(np.isnan(d), 0.0, d - ffb)
        # bucket k spans [L - (k+1)BM, L - k BM] from the upstream end; flip to travel order
        X.append(x0 + (Lm - np.cumsum(bl[::1])[::-1] + 0) ); R.append(ex[::-1] / (bl[::-1] * FT) * 100); F.append(bl[::-1] * FT)
        end = (x0 + Lm) * FT; segx.append((sid, x0 * FT, end, float(np.nansum(d)), float(np.nansum(np.where(np.isnan(d), 0, ffb)))))
        sig.append((end, sid, "sig"))
        for o in s["peds"]: sig.append((end - o * FT, sid, "ped"))
        for o, i, nm, side in s["stops"]: stops.append((end - o * FT, i, nm, side, sid))
        x0 += Lm
    r = np.concatenate(R); w = np.concatenate(F); edges = np.r_[0, np.cumsum(w)]; cum = np.r_[0, np.cumsum(r * w / 100)]
    xs = np.arange(0, edges[-1], STEP); c = np.interp(np.r_[xs, xs[-1] + STEP], edges, cum); e = np.diff(c) / STEP * 100
    mass = e.sum(); e = gaussian_filter1d(e, 15.0 / STEP, mode="nearest")
    if e.sum() != 0: e *= mass / e.sum()
    sig.sort(); stops.sort()
    seen = set(); st2 = []
    for t in stops:
        if t[1] not in seen: seen.add(t[1]); st2.append(t)
    return dict(xs=xs + STEP / 2, e=e, segx=segx, sig=sig, stops=st2, n=np.array(nmed))
def floor(e, pct=5, half=750, sm=120.0):
    """baseline = rolling 5th percentile (+-750 ft), smoothed into a rolling hill, never above the curve. Everything under it is other traffic."""
    return np.minimum(gaussian_filter1d(percentile_filter(e, pct, size=int(2 * half / STEP) + 1, mode="nearest"), sm / STEP, mode="nearest"), e)
UP, DN = 1500.0, 750.0      # a territory never reaches further than this from its peak (only binds on highways and other featureless road)
def territories(e, pk, keep):
    """split the axis among the kept peaks, cutting at the lowest point of the curve between neighbours. Returns {peak index: (ia, ib)} inclusive."""
    kp = [i for i in range(len(pk)) if keep[i]]; out = {}; a = 0
    for j, i in enumerate(kp):
        if j < len(kp) - 1: cut = pk[i] + int(np.argmin(e[pk[i]:pk[kp[j + 1]] + 1])); b = max(cut - 1, a)
        else: cut = len(e); b = len(e) - 1
        out[i] = (int(max(a, pk[i] - int(UP / STEP))), int(min(b, pk[i] + int(DN / STEP)))); a = cut
    return out
def zones(C):
    """returns list of zone dicts with category and the features they belong to"""
    xs, e = C["xs"], C["e"]; bg = floor(e); C["bg"] = bg
    pk, _ = find_peaks(e, prominence=1.5, distance=int(30 / STEP)); sx = np.array([s[0] for s in C["sig"]]); lx = np.array([s[0] for s in C["stops"]]); Z = []
    for i, p in enumerate(pk):
        lo = pk[i - 1] + int(np.argmin(e[pk[i - 1]:p])) if i > 0 else 0
        hi = p + int(np.argmin(e[p:pk[i + 1]])) if i < len(pk) - 1 else len(e) - 1
        thr = bg[p] + max(1.0, 0.10 * (e[p] - bg[p])); a = b = p
        while a > lo and e[a] > thr and (p - a) * STEP < 600: a -= 1
        while b < hi and e[b] > thr and (b - p) * STEP < 300: b += 1
        Z.append([xs[p], e[p] - bg[p], a, b, p])
    if not Z: return []
    Z = np.array(Z); ap = Z[:, 0]; h = Z[:, 1]; st = np.full(len(Z), -1); sg = np.full(len(Z), -1)
    for i in range(len(Z)):
        if len(lx):
            ds = ap[i] - lx; c = np.where((ds >= -150) & (ds <= 100))[0]
            if len(c): st[i] = c[np.argmin(np.abs(ds[c]))]
        if len(sx):
            dg = sx - ap[i]; c = np.where((dg >= -30) & (dg <= 300))[0]
            if len(c): sg[i] = c[np.argmin(dg[c])]
    for j in range(len(lx)):
        if not (st == j).any():
            c = np.where((ap - lx[j] > 100) & (ap - lx[j] <= 200) & (h >= 10))[0]
            if len(c): st[c[np.argmax(h[c])]] = j
    out = []; cats = []; ss = []
    for i in range(len(Z)):
        s, g = int(st[i]), int(sg[i])
        if s >= 0 and h[i] < 3 and abs(ap[i] - lx[s]) > 60: s = -1
        if s >= 0 and g >= 0:
            gap = sx[g] - lx[s]
            cat = "nearside" if (0 <= gap <= 150 or (gap > 0 and sx[g] - ap[i] <= 150) or (-50 <= gap < 0 and sx[g] - ap[i] >= 0)) else ("stop" if (abs(ap[i] - lx[s]) <= 100 or ap[i] - lx[s] > 0) else "signal")
        elif s >= 0: cat = "stop"
        elif g >= 0: cat = "signal"
        else: cat = "other"
        cats.append(cat); ss.append(s)
    # every bit of the curve above the baseline belongs to a stop or a light: peaks with no feature fold into the neighbouring territory
    T = territories(e, pk, [c != "other" for c in cats])
    for i in range(len(Z)):
        if i not in T: continue
        s, g, cat = ss[i], int(sg[i]), cats[i]; a, b = T[i]
        out.append(dict(apex=ap[i], height=h[i], ia=a, ib=b, x0=xs[a], x1=xs[b], cat=cat, sec=float((e - bg)[a:b + 1].sum() * STEP / 100), full=float(e[a:b + 1].sum() * STEP / 100),
                        stop_id=C["stops"][s][1] if s >= 0 and cat != "signal" else None, stop_side=C["stops"][s][3] if s >= 0 and cat != "signal" else None,
                        sig_seg=C["sig"][g][1] if g >= 0 and cat != "stop" else None, sig_kind=C["sig"][g][2] if g >= 0 and cat != "stop" else None,
                        d_sig=float(sx[g] - ap[i]) if g >= 0 else np.nan, d_stop=float(ap[i] - lx[s]) if s >= 0 else np.nan))
    return out
def seg_of(C, x):
    ends = np.array([s[2] for s in C["segx"]]); return C["segx"][min(int(np.searchsorted(ends, x)), len(ends) - 1)][0]
