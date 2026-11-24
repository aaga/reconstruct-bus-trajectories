"""Bus-camera enforcement (ACE) event study. Treated = segments used by a batch's routes; control = other segments in the same borough not on any 2026 batch route.
Outcomes per week (weekdays): seconds above free flow per mile, split into other traffic (floor), stop zones, light zones."""
from eng import *; import re
pd.set_option("display.width", 250)
BATCH = {"B1 15May": (["Q60", "B12"], "2026-03-16", "2026-05-15"), "B2 12Jun": (["B15", "M31"], "2026-04-13", "2026-06-12"), "B3 10Jul": (["M7", "Q10", "Q80"], "2026-05-11", "2026-07-10"),
         "B4 11Sep": (["B38", "B103", "BX11", "BX17"], "2026-07-13", "2026-09-11"), "B5 16Oct": (["BX21", "B8", "B9", "Q101"], "2026-08-17", "2026-10-16"), "B6 13Nov": (["Q25", "Q63", "Q66"], "2026-09-14", "2026-11-13"), "B7 Dec": (["Q65", "B1", "B6"], "2026-10-12", "2026-12-11")}
r = json.load(open(REPO / "outputs/network/nyc/segment_registry.json"))["segments"]; segr = {s: {q["route_id"] for q in L(v["routes"])} for s, v in r.items()}
def boro(rs):
    b = [m.group(1) for q in rs for m in [re.match(r"(BX|B|M|Q|S)\d", q)] if m]; return max(set(b), key=b.count) if b else "X"
allr = {x for v in BATCH.values() for x in v[0]}; print("routes present:", {x: sum(x in v for v in segr.values()) for x in sorted(allr)})
ps = pd.read_parquet(HERE / "ps_week_wd.parquet"); Z = pd.read_parquet(HERE / "ref_zones.parquet"); pz = pd.read_parquet(HERE / "pz_week_wd.parquet").merge(Z[["zid", "seg_id", "cat"]], on="zid")
zc = pz.pivot_table(index=["tag", "seg_id"], columns="cat", values="sec", aggfunc="sum").fillna(0).reset_index(); ps = ps.merge(zc, on=["tag", "seg_id"], how="left").fillna(0)
S, _ = reg(); ps["mi"] = ps.seg_id.map(lambda s: S[s]["len_m"]) / 1609.34; ps["boro"] = ps.seg_id.map(lambda s: boro(segr[s])); ps["stops"] = ps.get("stop", 0) + ps.get("nearside", 0); ps["lights"] = ps.get("signal", 0); ps["oth"] = ps.excess - ps.stops - ps.lights
ps = ps[ps.n >= 150]
def agg(q):   # bus-weighted seconds per mile
    w = q.n; m = (q.mi * w).sum(); return pd.Series(dict(exc=(q.excess * w).sum() / m, oth=(q.oth * w).sum() / m, flo=(q.floor * w).sum() / m, stops=(q.stops * w).sum() / m, lights=(q.lights * w).sum() / m, tot=(q.total * w).sum() / m, segs=len(q)))
out = []
ctrl_all = ps[~ps.seg_id.map(lambda s: bool(segr[s] & allr))]
C = ctrl_all.groupby(["boro", "tag"]).apply(agg).reset_index()
for b, (routes, warn, fine) in BATCH.items():
    T = ps[ps.seg_id.map(lambda s: bool(segr[s] & set(routes)))]
    # keep a balanced set: segments present in >= 28 of 30 weeks
    k = T.groupby("seg_id").tag.nunique(); T = T[T.seg_id.isin(k[k >= 28].index)]
    for bo, Tb in T.groupby("boro"):
        if Tb.seg_id.nunique() < 20: continue
        a = Tb.groupby("tag").apply(agg).reset_index().merge(C[C.boro == bo], on="tag", suffixes=("", "_c")); a["batch"] = b; a["boro"] = bo; a["warn"] = warn; a["fine"] = fine; out.append(a)
E = pd.concat(out); E.to_parquet(HERE / "ace_weekly.parquet")
def ph(r): return "pre" if r.tag < r.warn else ("warn" if r.tag < r.fine else "fine")
E["ph"] = E.apply(ph, axis=1)
for v in ("exc", "oth", "stops", "lights", "tot"): E["d_" + v] = E[v] - E[v + "_c"]
R = E.groupby(["batch", "boro", "ph"]).agg(wk=("tag", "size"), segs=("segs", "mean"), tot=("tot", "mean"), d_tot=("d_tot", "mean"), d_exc=("d_exc", "mean"), d_oth=("d_oth", "mean"), d_stops=("d_stops", "mean"), d_lights=("d_lights", "mean")).round(1)
print(R.to_string())
