import pickle, re; from eng import *; from street import ordered, table
duckdb.execute("set enable_progress_bar=false"); F = {}; S, SH = reg(); X = pd.read_parquet(HERE / "X.parquet"); D = 208
# ---- G1 citywide budget
A = X[X.n > 0]
def bud(q):
    w = q.n; t = (q.total * w).sum(); return dict(hours=t / 3600 / D, mph=(q.mi * w).sum() / (t / 3600), ff=(q.ff * w).sum() / t, stop=(q.stop * w).sum() / t, nearside=(q.nearside * w).sum() / t, signal=(q.signal * w).sum() / t, other=(q.other * w).sum() / t, ffmph=(q.mi * w).sum() / ((q.ff * w).sum() / 3600))
F["budget"] = dict(city=bud(A), **{b: bud(g) for b, g in A.groupby("borough")})
cd = pd.read_parquet(HERE / "city_daily.parquet"); F["daytype"] = cd.groupby("daytype")[["mph", "ff", "stops", "lights", "other", "total"]].mean().round(2).to_dict("index")
F["daily"] = cd[["tag", "mph", "other", "stops", "lights", "daytype"]].round(3).values.tolist(); F["hourly"] = pd.read_parquet(HERE / "hourly.parquet").round(3).reset_index().to_dict("list")
# ---- crosstown
M = X[(X.borough == "Manhattan") & (X.n > 2000)]
def ct(n):
    m = re.match(r"^(East|West) (\d+)(st|nd|rd|th) Street$", str(n)); return int(m.group(2)) if m else -1
M = M.assign(st=M.registry_name.map(ct)); rows = []
for st, q in M[M.st > 0].groupby("st"):
    if q.mi.sum() < 2: continue
    w = q.n; m = (q.mi * w).sum(); rows.append(dict(st=int(st), mi=q.mi.sum(), bpd=np.average(q.n, weights=q.mi) / D, mph=m / ((q.total * w).sum() / 3600), ff=(q.ff * w).sum() / m, stops=(q.stops_ * w).sum() / m, lights=(q.signal * w).sum() / m, other=(q.other * w).sum() / m, buslane=np.average(q.bus_lane_share, weights=q.mi), kinds="|".join(sorted(set("|".join(q.bus_lane_kind.dropna()).split("|")) - {""}))))
F["crosstown"] = rows; print(pd.DataFrame(rows)[["st", "mph", "other", "buslane", "kinds"]].round(2).to_string())
# ---- stop placement / turn curves (clean blocks: >= 350 ft before, >= 300 ft after)
Mx = np.load(HERE / "aligned.npy"); me = pd.read_parquet(HERE / "aligned_meta2.parquet"); off = np.arange(-500, 301, 5.0); E = Mx[:, :161]; ok = (me.n > 2000) & (me.len_ft >= 350) & (me.nxt_len_ft >= 300) & (me.n_mid == 0); sl = (off >= -300) & (off <= 250)
def curve(m):
    w = me.n[m].values; c = np.average(E[m.values], axis=0, weights=w); return dict(n=int(m.sum()), y=np.round(c[sl], 2).tolist(), area=float(c[sl].sum() * 5 / 100))
F["align"] = dict(off=off[sl].tolist(), none=curve(ok & (me.turn == "T") & (me.cfg == "none")), far=curve(ok & (me.turn == "T") & (me.cfg == "far")), near=curve(ok & (me.turn == "T") & (me.cfg == "near")),
                  left=curve(ok & (me.turn == "L") & (me.cfg == "none")), right=curve(ok & (me.turn == "R") & (me.cfg == "none")))
print({k: (v["n"], round(v["area"], 1)) for k, v in F["align"].items() if k != "off"})
# ---- twins: Ocean Av vs Coney Island Av
P = load_profile(f"select * from '{HERE}/cube/all.parquet'"); C = pd.read_pickle(HERE / "corridors_all.pkl")
for key, nm, rt in (("ocean", "Ocean Avenue", "B49"), ("coney", "Coney Island Avenue", "B68")):
    c = C[(C.name == nm) & (C.route == rt)].sort_values("mi").iloc[-1]; Cc = chain(c.segs, P); Z = zones(Cc)
    F["twin_" + key] = dict(name=nm, route=rt, frm=c.frm, to=c.to, mi=c.mi, lanes=c.lanes, sig_mi=c.sig_mi, stop_mi=c.stop_mi, bpd=c.bpd, mph=c.mph, stops=c.stops, lights=c.lights, other=c.other, total=c.total, ff=c.ff, xs=Cc["xs"].tolist(), e=np.round(Cc["e"], 2).tolist(), bg=np.round(Cc["bg"], 2).tolist(),
                             zones=[dict(ia=z["ia"], ib=z["ib"], cat=z["cat"], sec=z["sec"]) for z in Z], sig=[x for x, *_ in Cc["sig"]], stops_x=[x for x, *_ in Cc["stops"]])
    print(key, c.frm, c.to, round(c.mi, 1), c.dir, round(c.mph, 1))
# ---- TSP: travel time on the approach segment, and standouts weekly
I = pd.read_parquet(HERE / "tsp_seginfo.parquet"); ps = pd.read_parquet(HERE / "ps_tsp.parquet"); ps[["d", "w"]] = ps.tag.str.split("_", expand=True); t = ps.pivot_table(index=["d", "seg_id"], columns="w", values="total").reset_index().merge(ps.pivot_table(index=["d", "seg_id"], columns="w", values="n").min(1).rename("nmin").reset_index()).merge(I, on="seg_id", suffixes=("", "_act"))
t = t[t.nmin >= 600]; t["lr"] = np.log(t.post / t.pre); c = t[t.kind == "ctrl"].groupby(["d", "boro"]).lr.mean().rename("c").reset_index(); T = t[(t.kind == "tsp") & (t.d == t.d_act)].merge(c, on=["d", "boro"]); T["did"] = 100 * (T.lr - T.c)
F["tsp_tt"] = dict(mean=float(T.did.mean()), se=float(T.did.std() / np.sqrt(len(T))), n=int(len(T))); print("TSP approach-segment travel time % vs control:", F["tsp_tt"])
ind = pd.read_parquet(HERE / "tsp_ind.parquet"); F["tsp_hist"] = dict(signal=ind[ind.cat == "signal"].did.round(2).tolist(), nearside=ind[ind.cat == "nearside"].did.round(2).tolist())
Z = pd.read_parquet(HERE / "ref_zones.parquet"); wk = pd.read_parquet(HERE / "pz_week_wd.parquet").merge(Z[["zid", "seg_id", "cat"]], on="zid"); wk = wk[wk.cat.isin(["signal", "nearside"])]; sw = pd.read_parquet(HERE / "ps_week_wd.parquet")
G = ind.groupby(["d", "seg_id", "nm", "label", "boro"]).agg(pre=("pre", "sum"), post=("post", "sum"), ctrl=("mean", "sum"), nmin=("nmin", "first")).reset_index(); G["did"] = G.post - G.pre - G.ctrl
ctrlw = wk[wk.seg_id.isin(set(I[I.kind == "ctrl"].seg_id))].merge(I[["seg_id", "boro"]], on="seg_id").groupby(["boro", "tag"]).sec.mean().rename("c").reset_index()
F["tsp_series"] = []
for nm_pat in ("Jamaica Av & Springfield", "Metcalf", "Forest Av & Fairview", "Springfield Boulevard & Braddock", "Lafayette Ave & Rosedale", "Forest Avenue & Green"):
    g = G[G.nm.str.contains(nm_pat)].sort_values("did").iloc[0]; s = wk[wk.seg_id == g.seg_id].groupby("tag").sec.sum(); n = sw[sw.seg_id == g.seg_id].set_index("tag").n
    F["tsp_series"].append(dict(nm=g.nm, label=g.label, d=g.d, boro=g.boro, weeks=s.index.tolist(), sec=s.round(2).tolist(), n=n.reindex(s.index).tolist(), did=float(g.did), pre=float(g.pre), post=float(g.post)))
F["tsp_ctrl"] = {b: dict(weeks=q.tag.tolist(), sec=q.c.round(3).tolist()) for b, q in ctrlw.groupby("boro")}
# ---- 42nd St hourly
g = pd.read_parquet(HERE / "wc_M42.parquet"); ex = ["2026-06-13", "2026-06-16", "2026-06-22", "2026-06-25", "2026-06-27", "2026-06-30", "2026-07-05", "2026-07-19", "2026-06-18", "2026-06-19", "2026-07-03"]
base = g[(g.dow < 5) & ~g.d.isin(ex) & (g.d >= "2026-05-26") & (g.d <= "2026-07-31")]; b = base.groupby("h").mph.agg(["mean", "std", "size"])
F["wc"] = dict(h=b.index.tolist(), mean=b["mean"].round(3).tolist(), sd=b["std"].round(3).tolist(), nbase=int(base.d.nunique()), days={d: g[g.d == d].set_index("h").mph.reindex(b.index).round(3).tolist() for d in ("2026-06-16", "2026-06-25", "2026-06-30")}, ko={"2026-06-16": 15, "2026-06-25": 16, "2026-06-30": 17})
json.dump(F, open(HERE / "fig.json", "w"), default=lambda o: None if (isinstance(o, float) and np.isnan(o)) else float(o)); print("saved", list(F))
