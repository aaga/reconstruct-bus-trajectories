"""Collect the data behind the report graphics into fig.json."""
import pickle; from eng import *; from street import ordered
duckdb.execute("set enable_progress_bar=false")
DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; HOL = {"2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-10-12", "2026-11-03", "2026-11-11"}
wd = [d for d, v in DA.items() if v["daytype"] == "weekday" and d not in HOL]
def prof(a, b):
    ds = [d for d in wd if a <= d <= b]; fl = [f"{HERE}/cube/day_{m}.parquet" for m in sorted({d[:7] for d in ds})]
    return load_profile(f"select seg_id, bucket, sum(n) n, sum(sum_dt) sum_dt from read_parquet({fl!r}) where cast(service_date as varchar) in {tuple(ds)!r} group by all")
F = {}; S, SH = reg()
# ---- Tremont before / after curves
Pb, Pa = prof("2026-05-18", "2026-06-26"), prof("2026-09-28", "2026-11-20")
for d, i0, i1, b0, b1 in (("dir0", 10, 34, 18, 30), ("dir1", 10, 31, 15, 21)):
    seq = ordered("BX36", d, "Tremont")[i0:i1]; out = dict(labels=[S[s]["label"].split(": ")[-1].split(" → ")[-1] for s in seq])
    for tag, P in (("before", Pb), ("after", Pa)):
        C = chain(seq, P); bg = floor(C["e"]); out[tag] = dict(e=np.round(C["e"], 2).tolist(), bg=np.round(bg, 2).tolist(), total=sum(s[3] for s in C["segx"]), ff=sum(s[4] for s in C["segx"]))
    out["xs"] = C["xs"].tolist(); out["sig"] = [(x, k) for x, _, k in C["sig"]]; out["stops"] = [(x, nm) for x, _, nm, *_ in C["stops"]]; ends = [s[2] for s in C["segx"]]; out["ends"] = ends
    out["busway"] = (C["segx"][b0 - i0][1], C["segx"][b1 - i0 - 1][2]); F["tremont_" + d] = out
json.dump(F, open(HERE / "fig_tremont.json", "w")); print({k: (round(v["before"]["total"] / 60, 1), round(v["after"]["total"] / 60, 1), v["busway"]) for k, v in F.items()})
