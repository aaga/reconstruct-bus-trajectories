"""Untreated high-delay streets: worst mile, delay mix, recommended treatment, rough savings. Also map geometry classes."""
from eng import *
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
X = pd.read_parquet(HERE / "X_bap.parquet").set_index("seg_id"); g = pd.read_parquet(HERE / "streets.parquet"); C = pd.read_pickle(HERE / "corridors_all.pkl"); B = pd.read_parquet(HERE / "bundle.parquet").drop_duplicates("seg_id").set_index("seg_id"); S, _ = reg()
skip = {"I 278 East", "I 278 West", "Horace Harding Expressway", "Grand Central Parkway", "Long Island Expressway"}
u = g[(g.bap == 0) & ~g.nm.isin(skip)].copy(); u["score"] = u.dh; u = u.sort_values("dh", ascending=False).head(24); out = []
for _, r in u.iterrows():
    q = X[(X.nm == r.nm) & (X.borough == r.borough)]; w = q.n; m = (q.mi * w).sum()
    per = {k: float((q[c] * w).sum() / m) for k, c in (("other", "other"), ("near", "nearside"), ("lights", "signal"), ("stop", "stop"), ("ff", "ff"), ("total", "total"))}
    delay = per["other"] + per["near"] + per["lights"] + per["stop"]; sh = {k: per[k] / delay for k in ("other", "near", "lights", "stop")}; bl = float(np.average(q.bus_lane_share, weights=q.mi))
    nn = B.reindex(q.index); n_near = int((nn.cfg == "near").sum()); near_pass = float(nn[nn.cfg == "near"].n.sum() / 208); n_sig = len(q)
    # worst mile from ordered runs
    best = None
    for _, c in C[(C.name == r.nm) & (C.boro == r.borough)].iterrows():
        segs = [s for s in c.segs if s in X.index]; mi = np.array([X.mi[s] for s in segs]); dh = np.array([X.dh[s] for s in segs]); cm = np.r_[0, np.cumsum(mi)]
        for i in range(len(segs)):
            j = int(np.searchsorted(cm, cm[i] + 1.0)); j = min(j, len(segs))
            if cm[j] - cm[i] < 0.8: break
            d = dh[i:j].sum() / (cm[j] - cm[i])
            if best is None or d > best[0]:
                qq = X.loc[segs[i:j]]; best = (d, S[segs[i]]["label"].split(": ")[-1].split(" → ")[0], S[segs[j - 1]]["label"].split(" → ")[-1], float((qq.mi * qq.n).sum() / ((qq.total * qq.n).sum() / 3600)), c.dir, float(qq.bpd.mean()))
    rec = []
    if sh["other"] >= 0.38 or per["other"] >= 150: rec.append("busway or protected lane" if bl >= 0.5 else "bus lane")
    if sh["near"] >= 0.24 and n_near >= 4: rec.append("boarding islands / far-side stops")
    if sh["lights"] >= 0.22 or per["lights"] >= 80: rec.append("signal priority")
    if not rec: rec.append("signal priority")
    # rough savings, bus-hours per day: lane -30% of other traffic; far-side move 4-7 s per near-side stop pass; TSP -4% of light delay (as deployed) 
    sav = dict(lane=0.30 * r.oh if rec[0].startswith("bus") else 0.0, stops=near_pass * 5.5 / 3600 if "boarding" in " ".join(rec) else 0.0, tsp=0.04 * (r.lh + r.nh) if "signal priority" in rec else 0.0)
    out.append(dict(street=r.nm, borough=r.borough, mi=float(r.mi), bpd=float(r.bpd), mph=float(r.mph), dh=float(r.dh), dens=float(r.dens), per=per, share=sh, buslane=bl, n_near=n_near, n_sig=n_sig, rec=rec, sav=sav,
                    worst=dict(dens=best[0], frm=best[1], to=best[2], mph=best[3], bpd=best[5]) if best else None))
R = pd.DataFrame(out); print(R.assign(o=R.share.map(lambda s: round(s["other"], 2)), nr=R.share.map(lambda s: round(s["near"], 2)), l=R.share.map(lambda s: round(s["lights"], 2)), w=R.worst.map(lambda w: f"{w['frm']} → {w['to']} {w['mph']:.1f} mph" if w else ""), rec=R.rec.map(" + ".join), s=R.sav.map(lambda s: round(sum(s.values()))))[["street", "borough", "mi", "mph", "dh", "dens", "buslane", "o", "nr", "l", "n_near", "rec", "s", "w"]].round(1).to_string())
json.dump(out, open(HERE / "recs.json", "w"))
# ---- map classes: delay density per segment (bus-hours of delay per mile per day)
X["dens"] = X.dh / X.mi; thr = np.sort(X.dens.values)[::-1]; cs = np.cumsum(np.sort(X.dh.values)[::-1]) ; 
order = X.sort_values("dens", ascending=False); cum = order.dh.cumsum() / X.dh.sum(); hot = set(order.index[cum <= 0.5]); print("hot segments (densest, holding half of all delay):", len(hot), "of", len(X), "| miles", round(X.loc[list(hot)].mi.sum()), "of", round(X.mi.sum()), "| min density", round(order.dens[cum <= 0.5].min(), 1))
H = X.loc[list(hot)]; print("hot miles in BAP:", round(H[H.bap > 0].mi.sum()), "outside:", round(H[H.bap == 0].mi.sum()), "| hot delay in BAP:", round(H[H.bap > 0].dh.sum()), "outside:", round(H[H.bap == 0].dh.sum()))
r = json.load(open(REPO / "outputs/network/nyc/segment_registry.json"))["segments"]; geo = {}
for s in X.index: geo[s] = L(r[s]["geometry_lonlat"])
import pickle; pickle.dump(dict(geo=geo, hot=hot), open(HERE / "mapgeo.pkl", "wb"))
