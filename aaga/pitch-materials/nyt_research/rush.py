"""Replicate the prior report's metric: weekday rush-hour (6-10, 15-19) lost time per block, directions merged into street blocks, ranked by loss per mile."""
from eng import *
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 100)
S, _ = reg(); r = json.load(open(REPO / "outputs/network/nyc/segment_registry.json"))["segments"]
DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; nwd = sum(v["daytype"] == "weekday" for v in DA.values()); print("weekdays in data:", nwd)
t = pd.read_parquet(HERE / "cube/trav_hour.parquet"); t = t[t.wd & t.h.isin([6, 7, 8, 9, 15, 16, 17, 18])].groupby("seg_id").agg(n=("n", "sum"), t=("t", "sum")).reset_index()
t = t[t.seg_id.isin(S)]; t["ff"] = t.seg_id.map(lambda s: S[s]["tff"]); t["mi"] = t.seg_id.map(lambda s: S[s]["len_m"]) / 1609.34; t = t[t.n >= 100].dropna(subset=["ff"])
t["hr"] = (t.t - t.n * t.ff).clip(lower=None) / 3600 / 146; print("blocks", len(t), "dir miles", round(t.mi.sum()), "lost bus-hours per weekday rush:", round(t.hr.sum()), "(prior report: 18,723 blocks, 2,723 mi, 13,441 h)")
t["rev"] = t.seg_id.map(lambda s: r[s].get("rev_seg_id")); t["key"] = [min(a, b) if isinstance(b, str) and b in set(t.seg_id) else a for a, b in zip(t.seg_id, t.rev)]
X = pd.read_parquet(HERE / "X.parquet")[["seg_id", "registry_name", "borough"]]; t = t.merge(X, on="seg_id", how="left")
g = t.groupby("key").agg(hr=("hr", "sum"), mi=("mi", "max"), nm=("registry_name", "first"), boro=("borough", "first")).reset_index(); g["per_mi"] = g.hr / g.mi
g = g.sort_values("per_mi", ascending=False); g["cum"] = g.hr.cumsum() / g.hr.sum(); red = g[g.cum <= 0.5]
print("street blocks", len(g), "street miles", round(g.mi.sum()), "| red blocks", len(red), "red miles", round(red.mi.sum()), "min per-mile", round(red.per_mi.min(), 1), "(prior: 11,415 / 1,924 mi / 2,927 red / 215 mi / 17)")
print(red.groupby("boro").mi.sum().round(0).to_dict(), "(prior: Manhattan 58, Queens 57, Brooklyn 51, Bronx 39, SI 9)")
st = t.groupby(["boro" if "boro" in t else "borough", "registry_name"]).agg(hr=("hr", "sum"), mi=("mi", "sum")).reset_index().sort_values("hr", ascending=False); print(st.head(26).round(1).to_string())
t.to_parquet(HERE / "rush_seg.parquet"); g.to_parquet(HERE / "rush_block.parquet")
# compare with my all-day directional hot set
import pickle; hot = pickle.load(open(HERE / "mapgeo.pkl", "rb"))["hot"]; redsegs = set(t[t.key.isin(set(red.key))].seg_id)
a = t[t.seg_id.isin(hot)].mi.sum(); b = t[t.seg_id.isin(redsegs)].mi.sum(); ab = t[t.seg_id.isin(hot & redsegs)].mi.sum(); print(f"my all-day hot set: {a:.0f} dir mi; rush red set: {b:.0f} dir mi; overlap {ab:.0f} mi = {100*ab/a:.0f}% of mine, {100*ab/b:.0f}% of theirs")
