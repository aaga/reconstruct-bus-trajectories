"""Before/after helper: s/mile by category for a set of segments in each measured window, with a borough control."""
from eng import *
def ordered(route, direction, pat):
    S, SH = reg(); best = []
    for k, v in SH.items():
        if v["route"] == route and v["dir"] == direction:
            seq = [s for s in v["seq"] if pat in (S[s]["name"] or "")]
            if len(seq) > len(best): best = seq
    return best
def table(segs, name="win", cats=True):
    ps = pd.read_parquet(HERE / f"ps_{name}.parquet"); Z = pd.read_parquet(HERE / "ref_zones.parquet"); pz = pd.read_parquet(HERE / f"pz_{name}.parquet").merge(Z[["zid", "seg_id", "cat"]], on="zid")
    zc = pz.pivot_table(index=["tag", "seg_id"], columns="cat", values="sec", aggfunc="sum").fillna(0).reset_index(); ps = ps.merge(zc, on=["tag", "seg_id"], how="left").fillna(0); S, _ = reg()
    ps["mi"] = ps.seg_id.map(lambda s: S[s]["len_m"]) / 1609.34; ps["stops"] = ps.get("stop", 0) + ps.get("nearside", 0); ps["lights"] = ps.get("signal", 0); ps["oth"] = ps.excess - ps.stops - ps.lights
    def agg(q):
        m = q.mi.sum(); return pd.Series(dict(segs=len(q), mi=m, n=q.n.median(), total=q.total.sum() / m, ff=q.ff.sum() / m, stops=q.stops.sum() / m, lights=q.lights.sum() / m, other=q.oth.sum() / m, mph=m / (q.total.sum() / 3600), minutes=q.total.sum() / 60))
    return ps[ps.seg_id.isin(set(segs))].groupby("tag").apply(agg), ps, agg
if __name__ == "__main__":
    import sys; S, _ = reg()
    for route, d, pat in [a.split(",") for a in sys.argv[1:]]:
        seq = ordered(route, d, pat); print(route, d, pat, len(seq))
        for i, s in enumerate(seq): print("  ", i, s, S[s]["label"].split(": ")[-1], round(S[s]["len_m"]))
