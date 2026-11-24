"""Refresh the decomposition columns of X.parquet from city_seg_all (OSM columns kept), rebuild city_daily and the Fifth Avenue figure data."""
import sys; from eng import *
duckdb.execute("set enable_progress_bar=false")
A = pd.read_parquet(HERE / "city_seg_all.parquet"); X0 = pd.read_parquet(HERE / "v_old/X.parquet"); keep = [c for c in X0.columns if c not in A.columns or c == "seg_id"]
X = A.merge(X0[keep], on="seg_id"); X["stops_"] = X.stop + X.nearside; X = X[list(X0.columns)]; assert len(X) == len(X0); X.to_parquet(HERE / "X.parquet")
S, SH = reg()
if "daily" in sys.argv:
    ps = pd.read_parquet(HERE / "ps_day.parquet"); Z = pd.read_parquet(HERE / "ref_zones.parquet"); pz = pd.read_parquet(HERE / "pz_day.parquet").merge(Z[["zid", "seg_id", "cat"]], on="zid")
    zc = pz.groupby(["tag", "seg_id", "cat"]).sec.sum().unstack().fillna(0).reset_index(); ps = ps.merge(zc, on=["tag", "seg_id"], how="left").fillna(0); ps["mi"] = ps.seg_id.map(lambda s: S[s]["len_m"]) / 1609.34
    def agg(q):
        w = q.n; m = (q.mi * w).sum(); return pd.Series(dict(bus_mi=m, mph=m / ((q.total * w).sum() / 3600), ff=(q.ff * w).sum() / m, stops=((q.stop + q.nearside) * w).sum() / m, lights=(q.signal * w).sum() / m, other=((q.excess - q.stop - q.nearside - q.signal) * w).sum() / m, total=(q.total * w).sum() / m))
    D = ps.groupby("tag").apply(agg).reset_index(); DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; D["daytype"] = D.tag.map(lambda d: DA[d]["daytype"]); D["weather"] = D.tag.map(lambda d: DA[d]["weather"]); D.to_parquet(HERE / "city_daily.parquet")
    O = pd.read_parquet(HERE / "v_old/city_daily.parquet"); print("daily old", O.groupby("daytype")[["mph", "stops", "lights", "other"]].mean().round(1)); print("daily new", D.groupby("daytype")[["mph", "stops", "lights", "other"]].mean().round(1))
C = pd.read_pickle(HERE / "corridors_all.pkl"); c = C[(C.name == "5th Avenue") & (C.boro == "Manhattan")].sort_values("mi").iloc[-1]
labs = [S[s]["label"].split(" → ")[-1] for s in c.segs]; P = load_profile(f"select * from '{HERE}/cube/all.parquet'")
i0 = next(i for i, l in enumerate(labs) if "60th" in l or "59th" in l); i1 = next(i for i, l in enumerate(labs) if "34th" in l)
segs = c.segs[i0:i1 + 1]; Cc = chain(segs, P); Z = zones(Cc); Xs = X.set_index("seg_id").loc[segs]; w = Xs.n; m = (Xs.mi * w).sum()
out = dict(frm=labs[i0 - 1] if i0 else c.frm, to=labs[i1], mi=float(Xs.mi.sum()), mph=float(m / ((Xs.total * w).sum() / 3600)), bpd=float(Xs.n.mean() / 208), xs=Cc["xs"].tolist(), e=np.round(Cc["e"], 2).tolist(), bg=np.round(Cc["bg"], 2).tolist(), zones=[dict(ia=z["ia"], ib=z["ib"], cat=z["cat"], sec=z["sec"]) for z in Z],
           ends=[(s[2], labs[i0 + j]) for j, s in enumerate(Cc["segx"])], per={k: float((Xs[k2] * w).sum() / m) for k, k2 in (("ff", "ff"), ("stops", "stops_"), ("lights", "signal"), ("other", "other"), ("total", "total"))}, minutes=float(Xs.total.sum() / 60), ffmin=float(Xs.ff.sum() / 60))
old = json.load(open(HERE / "v_old/fig_fifth.json")); print("fifth old", {k: round(v, 1) for k, v in old["per"].items()}); print("fifth new", {k: round(v, 1) for k, v in out["per"].items()}); json.dump(out, open(HERE / "fig_fifth.json", "w"))
