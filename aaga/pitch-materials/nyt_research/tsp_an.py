from eng import *
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 300)
Z = pd.read_parquet(HERE / "ref_zones.parquet"); I = pd.read_parquet(HERE / "tsp_seginfo.parquet"); pz = pd.read_parquet(HERE / "pz_tsp.parquet"); ps = pd.read_parquet(HERE / "ps_tsp.parquet")
pz[["d", "w"]] = pz.tag.str.split("_", expand=True); W = pz.pivot_table(index=["d", "zid"], columns="w", values="sec").reset_index(); W["dz"] = W.post - W.pre
ps[["d", "w"]] = ps.tag.str.split("_", expand=True); n = ps.pivot_table(index=["d", "seg_id"], columns="w", values="n").min(1).rename("nmin").reset_index()
fl = ps.pivot_table(index=["d", "seg_id"], columns="w", values="floor"); fl = (fl.post - fl.pre).rename("dfloor").reset_index(); ex = ps.pivot_table(index=["d", "seg_id"], columns="w", values="excess"); ex = (ex.post - ex.pre).rename("dexc").reset_index()
W = W.merge(Z[["zid", "seg_id", "cat", "sig_seg", "sig_kind", "d_sig", "sec", "height"]].rename(columns={"sec": "sec_all"}), on="zid").merge(I, on="seg_id", suffixes=("", "_act")).merge(n, on=["d", "seg_id"]).merge(fl, on=["d", "seg_id"]).merge(ex, on=["d", "seg_id"])
W = W[(W.nmin >= 600) & W.cat.isin(["signal", "nearside"]) & (W.sig_kind == "sig") & (W.sig_seg == W.seg_id)]
W["role"] = np.where(W.kind == "ctrl", "ctrl", np.where((W.kind == "tsp") & (W.d == W.d_act), "tsp", "x")); W = W[W.role != "x"]; W.to_parquet(HERE / "tsp_dz.parquet")
g = W.groupby(["d", "boro", "role", "cat"]).agg(n=("dz", "size"), pre=("pre", "mean"), dz=("dz", "mean"), med=("dz", "median"), se=("dz", lambda x: x.std() / np.sqrt(len(x))), dfloor=("dfloor", "mean")).round(2)
t = g.reset_index(); t = t[t.role == "tsp"].merge(t[t.role == "ctrl"], on=["d", "boro", "cat"], suffixes=("", "_c")); t["did"] = t.dz - t.dz_c; t["did_floor"] = t.dfloor - t.dfloor_c
print(t[["d", "boro", "cat", "n", "pre", "dz", "dz_c", "did", "se", "dfloor", "dfloor_c", "did_floor", "n_c"]].to_string())
for cat in ("signal", "nearside"):
    q = t[(t.cat == cat) & (t.n >= 3)]; print(cat, "pooled DiD (n-weighted):", round(np.average(q.did, weights=q.n), 2), "s on", int(q.n.sum()), "zones; pre level", round(np.average(q.pre, weights=q.n), 1), " floor DiD:", round(np.average(q.did_floor, weights=q.n), 2))
# individual standouts: z-score vs controls in same date+boro+cat
c = W[W.role == "ctrl"].groupby(["d", "boro", "cat"]).dz.agg(["mean", "std"]).reset_index(); T = W[W.role == "tsp"].merge(c, on=["d", "boro", "cat"]); T["did"] = T.dz - T["mean"]; T["zs"] = T.did / T["std"]; T["rel"] = T.did / T.pre
S, _ = reg(); T["label"] = T.seg_id.map(lambda s: S[s]["label"])
print(T.sort_values("did")[["d", "nm", "label", "cat", "pre", "post", "did", "zs", "rel", "nmin", "dfloor"]].head(25).round(2).to_string()); print(T.sort_values("did")[["d", "nm", "cat", "pre", "post", "did", "zs", "nmin"]].tail(8).round(2).to_string())
print("share improved vs ctrl:", T.groupby("cat").apply(lambda q: pd.Series(dict(n=len(q), share=(q.did < 0).mean(), mean=q.did.mean(), med=q.did.median(), big=(q.zs < -2).mean(), bigpos=(q.zs > 2).mean()))).round(3))
T.to_parquet(HERE / "tsp_ind.parquet")
