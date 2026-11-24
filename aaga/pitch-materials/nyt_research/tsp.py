import sys, re; EXT = str(__import__("pathlib").Path(__file__).resolve().parent / "ext")
import period; from eng import *
import datetime as dt
DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; HOL = {"2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-10-12"}
def windows(d, weeks=4):
    D = dt.date.fromisoformat(d); M = D - dt.timedelta(days=D.weekday())
    def wk(a, b): return [x.isoformat() for x in (M + dt.timedelta(days=i) for i in range(a, b)) if x.weekday() < 5 and x.isoformat() in DA and DA[x.isoformat()].get("daytype") == "weekday" and x.isoformat() not in HOL]
    return wk(-7 * weeks, 0), wk(7, 7 + 7 * weeks)
def seginfo():
    r = json.load(open(REPO / "outputs/network/nyc/segment_registry.json"))["segments"]; tsp = pd.read_csv(EXT + "/tsp_nodes.csv")
    norm = lambda x: re.sub(r"^([A-Z]+)0+", r"\1", x.strip().upper().replace(" LOCAL", "").replace("-SBS", "+")); nd = {}
    for _, t in tsp.iterrows(): nd[t.node] = (t.d, {norm(x) for x in re.split("[;,]", str(t["Associated Routes"]))}, t["Main Street"], t["Cross Street"])
    out = {}
    for s, v in r.items():
        rts = L(v["routes"]); b = [m.group(1) for q in rts for m in [re.match(r"(BX|B|M|Q|S)\d", q["route_id"])] if m]; k = "ctrl"; d = None; nm = None
        if v["down_node"] in nd:
            d, tr, ms, cs = nd[v["down_node"]]; k = "tsp" if {norm(q["route_id"]) for q in rts} & tr else "cross"; nm = f"{ms.title()} & {cs.title()}"
        elif v["up_node"] in nd: k = "after"; d = nd[v["up_node"]][0]
        out[s] = dict(kind=k, d=d, boro=max(set(b), key=b.count) if b else "X", nm=nm)
    return pd.DataFrame.from_dict(out, orient="index").rename_axis("seg_id").reset_index()
if __name__ == "__main__":
    dates = sorted(pd.read_csv(EXT + "/tsp_nodes.csv").d.unique()); jobs = []
    for d in dates:
        pre, post = windows(d); print(d, len(pre), len(post))
        if len(pre) >= 10 and len(post) >= 8: jobs += [(d + "_pre", pre), (d + "_post", post)]
    period.run(jobs, "tsp", 6); seginfo().to_parquet(HERE / "tsp_seginfo.parquet")
