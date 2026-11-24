"""Fixed-zone measurement per period. Zones (bounds, category, features) come from the all-days pooled curve; each period's curve and its own floor
are integrated inside those fixed bounds. Output: one row per zone per period, and one row per segment per period."""
import sys, pickle, time; from eng import *
from multiprocessing import Pool
REF = HERE / "ref.pkl"
def build_ref():
    P = load_profile(f"select * from '{HERE}/cube/all.parquet'"); S, SH = reg()
    order = sorted(SH, key=lambda k: -sum(np.nanmedian(P[s][1]) if s in P else 0 for s in SH[k]["seq"])); done = set(); ref = {}; zid = 0
    for k in order:
        seq = SH[k]["seq"]
        if all(s in done for s in seq) or len(seq) < 3: continue
        C = chain(seq, P); Z = zones(C); ends = np.array([s[2] for s in C["segx"]]); own = [j for j, s in enumerate(C["segx"]) if s[0] not in done]; ownset = {C["segx"][j][0] for j in own}; zz = []
        for z in Z:
            sid = C["segx"][min(int(np.searchsorted(ends, z["apex"])), len(ends) - 1)][0]
            if sid in ownset: zz.append(dict(zid=zid, seg_id=sid, **z)); zid += 1
        ref[k] = dict(seq=seq, own=own, zones=zz); done.update(seq)
    pickle.dump(ref, open(REF, "wb")); return ref
def measure(arg):
    duckdb.execute("set enable_progress_bar=false"); tag, dates = arg; ref = pickle.load(open(REF, "rb")); months = sorted({d[:7] for d in dates}); fl = [f"{HERE}/cube/day_{m}.parquet" for m in months]
    P = load_profile(f"select seg_id, bucket, sum(n) n, sum(sum_dt) sum_dt from read_parquet({fl!r}) where cast(service_date as varchar) in {tuple(dates) + ('x',)!r} group by all")
    zr = []; sr = []
    for k, R in ref.items():
        C = chain(R["seq"], P); xs, e = C["xs"], C["e"]; bg = floor(e)
        for z in R["zones"]: zr.append((tag, z["zid"], float((e - bg)[z["ia"]:z["ib"] + 1].sum() * STEP / 100), float(e[z["ia"]:z["ib"] + 1].sum() * STEP / 100)))
        for j in R["own"]:
            sid, a, b, tot, ff = C["segx"][j]; m = (xs >= a) & (xs < b); sr.append((tag, sid, float(C["n"][j]), tot, ff, float(e[m].sum() * STEP / 100), float(bg[m].sum() * STEP / 100)))
    return pd.DataFrame(zr, columns=["tag", "zid", "sec", "full"]), pd.DataFrame(sr, columns=["tag", "seg_id", "n", "total", "ff", "excess", "floor"])
def run(jobs, name, procs=6):
    t0 = time.time()
    duckdb.execute("set enable_progress_bar=false")
    with Pool(procs) as p: out = p.map(measure, jobs)
    pd.concat([o[0] for o in out]).to_parquet(HERE / f"pz_{name}.parquet"); pd.concat([o[1] for o in out]).to_parquet(HERE / f"ps_{name}.parquet"); print(name, len(jobs), "periods", round(time.time() - t0), "s", flush=True)
if __name__ == "__main__":
    if not REF.exists() or "ref" in sys.argv:
        ref = build_ref(); Z = pd.DataFrame([dict(shape=k, **{q: v for q, v in z.items()}) for k, R in ref.items() for z in R["zones"]]); Z.to_parquet(HERE / "ref_zones.parquet"); print("ref zones", len(Z), Z.cat.value_counts().to_dict(), flush=True)
    DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; D = pd.DataFrame([dict(d=k, **v) for k, v in DA.items()]); D["dt"] = pd.to_datetime(D.d); D["wk"] = (D.dt - pd.to_timedelta(D.dt.dt.dayofweek, unit="D")).dt.strftime("%Y-%m-%d")
    wd = D[D.daytype == "weekday"]; jobs = [(w, list(g.d)) for w, g in wd.groupby("wk") if len(g) >= 3]; run(jobs, "week_wd")
    jobs = [("weekday", list(wd.d)), ("sat", list(D[D.daytype == "sat"].d)), ("sun", list(D[D.daytype.isin(["sun", "holiday"])].d)), ("dry_wd", list(wd[wd.weather == "dry"].d)), ("wet_wd", list(wd[wd.weather != "dry"].d))]
    print(D.weather.value_counts().to_dict()); run(jobs, "daytype", 5)

def daily():
    DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; run([(d, [d]) for d in sorted(DA)], "day", 7)
