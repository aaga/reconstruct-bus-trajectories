"""Same outputs as bap.py + recs.py, but on the prior report's basis: weekday rush-hour lost time, directions merged for the 'red' set, plan corridors clipped to their limits."""
from eng import *; import pickle
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 100)
X = pd.read_parquet(HERE / "X.parquet"); T = pd.read_parquet(HERE / "rush_seg.parquet")[["seg_id", "n", "t", "hr", "key"]].rename(columns={"n": "rn", "t": "rt"}); mem = pd.read_parquet(HERE / "bap_member.parquet")
X = X.merge(T, on="seg_id").merge(mem, on="seg_id", how="left"); X["bap"] = X.bap.fillna(0).astype(int); X["nm"] = X.registry_name.fillna(""); X["dh"] = X.hr; X["bpd"] = X.n / 208
for k, c in (("oh", "other"), ("lh", "signal"), ("nh", "nearside"), ("sh", "stop")): X[k] = X.hr * X[c] / (X.total - X.ff).clip(lower=1e-6)    # rush hours split by the all-day delay mix of the block
X.to_parquet(HERE / "X_bap.parquet"); tot = X.hr.sum()
B = pd.read_parquet(HERE / "rush_block.parquet").sort_values("per_mi", ascending=False); B["cum"] = B.hr.cumsum() / B.hr.sum(); redk = set(B[B.cum <= 0.5].key); hot = set(X[X.key.isin(redk)].seg_id)
bk = X.groupby("key").agg(mi=("mi", "max"), hr=("hr", "sum"), bap=("bap", "max")).reset_index(); rk = bk[bk.key.isin(redk)]
FACT = dict(total_hr=float(tot), dir_mi=float(X.mi.sum()), street_mi=float(bk.mi.sum()), red_mi=float(rk.mi.sum()), red_min=float(B[B.cum <= 0.5].per_mi.min()), plan_dir_mi=float(X[X.bap > 0].mi.sum()), plan_hr=float(X[X.bap > 0].hr.sum()),
            red_plan_mi=float(rk[rk.bap > 0].mi.sum()), red_out_mi=float(rk[rk.bap == 0].mi.sum()), red_plan_hr=float(rk[rk.bap > 0].hr.sum()), red_out_hr=float(rk[rk.bap == 0].hr.sum()))
print({k: round(v, 1) for k, v in FACT.items()}); print("plan share of lost time %.1f%%, of dir miles %.1f%%; of red miles %.0f%%" % (100 * FACT["plan_hr"] / tot, 100 * FACT["plan_dir_mi"] / FACT["dir_mi"], 100 * FACT["red_plan_mi"] / FACT["red_mi"]))
g = X.groupby(["borough", "nm"]).apply(lambda q: pd.Series(dict(mi=q.mi.sum(), dh=q.hr.sum(), cov=q.hr[q.bap > 0].sum() / max(q.hr.sum(), 1e-9), mph=q.mi.mul(q.rn).sum() / (q.rt.sum() / 3600), bpd=q.bpd.median(), oh=q.oh.sum(), lh=q.lh.sum(), nh=q.nh.sum(), sh=q.sh.sum()))).reset_index()
g = g[(g.mi >= 1.5) & (g.nm != "")].sort_values("dh", ascending=False).reset_index(drop=True); g["rank"] = np.arange(1, len(g) + 1); g.to_parquet(HERE / "streets.parquet")
print(g.head(30)[["rank", "borough", "nm", "mi", "dh", "cov", "mph"]].round(2).to_string())
C = pd.read_pickle(HERE / "corridors_all.pkl"); Bn = pd.read_parquet(HERE / "bundle.parquet").drop_duplicates("seg_id").set_index("seg_id"); S, _ = reg(); Xi = X.set_index("seg_id"); out = []
skip = {"I 278 East", "I 278 West", "Horace Harding Expressway", "Grand Central Parkway", "Long Island Expressway"}
for _, r in g[(g["cov"] < 0.10) & ~g.nm.isin(skip)].head(16).iterrows():
    q = Xi[(Xi.nm == r.nm) & (Xi.borough == r.borough)]; w = q.n; m = (q.mi * w).sum()
    per = {k: float((q[c] * w).sum() / m) for k, c in (("other", "other"), ("near", "nearside"), ("lights", "signal"), ("stop", "stop"), ("ff", "ff"), ("total", "total"))}; d = per["other"] + per["near"] + per["lights"] + per["stop"]; sh = {k: per[k] / d for k in ("other", "near", "lights", "stop")}
    nn = Bn.reindex(q.index); n_near = int((nn.cfg == "near").sum()); best = None
    for _, c in C[(C.name == r.nm) & (C.boro == r.borough)].iterrows():
        segs = [s for s in c.segs if s in Xi.index]; mi = np.array([Xi.mi[s] for s in segs]); dh = np.array([Xi.hr[s] for s in segs]); cm = np.r_[0, np.cumsum(mi)]
        for i in range(len(segs)):
            j = min(int(np.searchsorted(cm, cm[i] + 1.0)), len(segs))
            if cm[j] - cm[i] < 0.8: break
            dd = dh[i:j].sum() / (cm[j] - cm[i])
            if best is None or dd > best[0]: qq = Xi.loc[segs[i:j]]; best = (dd, S[segs[i]]["label"].split(": ")[-1].split(" → ")[0], S[segs[j - 1]]["label"].split(" → ")[-1], float(qq.mi.mul(qq.rn).sum() / (qq.rt.sum() / 3600)), float(qq.bpd.mean()))
    rec = []
    # thresholds sit just under the citywide share of each kind of delay (other 33%, stop before a light 27%, lights 25%)
    if sh["other"] >= 0.33 or per["other"] >= 130: rec.append("bus lane")
    if sh["near"] >= 0.27 and n_near >= 4: rec.append("boarding islands / far-side stops")
    if sh["lights"] >= 0.24 or per["lights"] >= 88: rec.append("signal priority")
    if not rec: rec.append("signal priority")
    out.append(dict(street=r.nm, borough=r.borough, mi=float(r.mi), bpd=float(r.bpd), mph=float(r.mph), dh=float(r.dh), dens=float(r.dh / r.mi), rank=int(r["rank"]), per=per, share=sh, buslane=float(np.average(q.bus_lane_share, weights=q.mi)), n_near=n_near, n_sig=len(q), rec=rec, sav={},
                    worst=dict(dens=best[0], frm=best[1], to=best[2], mph=best[3], bpd=best[4]) if best else None))
print(pd.DataFrame(out).assign(rec=lambda d: d.rec.map(" + ".join))[["rank", "street", "borough", "mi", "mph", "dh", "rec"]].round(1).to_string())
json.dump(out, open(HERE / "recs.json", "w")); json.dump(FACT, open(HERE / "rush_fact.json", "w"))
r = json.load(open(REPO / "outputs/network/nyc/segment_registry.json"))["segments"]; pickle.dump(dict(geo={s: L(r[s]["geometry_lonlat"]) for s in X.seg_id}, hot=hot), open(HERE / "mapgeo.pkl", "wb"))
