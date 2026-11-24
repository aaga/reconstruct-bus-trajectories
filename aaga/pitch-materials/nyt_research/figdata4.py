"""(1) Headway CV along East Tremont Av before/after the busway. (2) 42nd St on World Cup match days vs comparable ordinary days, by delay type."""
import glob; from eng import *; from street import ordered
duckdb.execute("set enable_progress_bar=false"); S, SH = reg(); OUT = {}; B = str(REPO / "outputs/network/nyc")
DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; HOL = {"2026-05-25", "2026-06-19", "2026-07-03", "2026-07-04", "2026-09-07", "2026-10-12", "2026-11-03", "2026-11-11"}
wd = [d for d, v in DA.items() if v["daytype"] == "weekday" and d not in HOL]
# ---------------- 1. headway CV, BX36 on Tremont
def cvprof(route, ds):
    fs = [f"{B}/headway_cv/service_date={d}/route={route}.parquet" for d in ds]; fs = [f for f in fs if glob.glob(f)]
    q = duckdb.sql(f"select seg_id, bucket, sum(n) n, sum(sum_h) h, sum(sum_h2) h2 from read_parquet({fs!r}) group by all").df(); q["cv"] = np.sqrt(np.clip(q.h2 / q.n - (q.h / q.n) ** 2, 0, None)) / (q.h / q.n); q["mh"] = q.h / q.n / 60
    return {s: g.set_index("bucket") for s, g in q.groupby("seg_id")}
def along(seq, C):
    xs = []; cv = []; mh = []; x0 = 0.0
    for sid in seq:
        Lm = S[sid]["len_m"]; nb = int(np.ceil(Lm / BM))
        for k in range(nb - 1, -1, -1):
            xs.append((x0 + Lm - (k + .5) * BM) * FT); g = C.get(sid); ok = g is not None and k in g.index; cv.append(float(g.cv[k]) if ok else np.nan); mh.append(float(g.mh[k]) if ok else np.nan)
        x0 += Lm
    return np.array(xs), np.array(cv), np.array(mh)
pre = [d for d in wd if "2026-05-18" <= d <= "2026-06-26"]; post = [d for d in wd if "2026-09-28" <= d <= "2026-11-20"]; Cb, Ca = cvprof("BX36", pre), cvprof("BX36", post)
for d, i0, i1, b0, b1 in (("dir0", 10, 34, 18, 30), ("dir1", 10, 31, 15, 21)):
    full = ordered("BX36", d, "Tremont"); seq = full[i0:i1]; ends = np.cumsum([S[s]["len_m"] for s in seq]) * FT; starts = np.r_[0, ends[:-1]]; bw = (float(starts[b0 - i0]), float(ends[b1 - i0 - 1])); o = dict(busway=bw, ends=ends.tolist(), labels=[S[s]["label"].split(": ")[-1].split(" → ")[-1] for s in seq])
    for tag, C in (("before", Cb), ("after", Ca)):
        xs, cv, mh = along(seq, C); sm = pd.Series(cv).interpolate(limit_direction="both").rolling(31, center=True, min_periods=5).median().values
        a, b = np.nanmean(sm[(xs > bw[0] - 80) & (xs < bw[0] + 80)]), np.nanmean(sm[(xs > bw[1] - 80) & (xs < bw[1] + 80)])
        o[tag] = dict(cv=np.round(sm, 4).tolist(), cv_in=float(a), cv_out=float(b), headway=float(np.nanmedian(mh)), cv_first=float(np.nanmean(sm[:20])), cv_last=float(np.nanmean(sm[-20:])))
    o["xs"] = xs.tolist(); OUT["cv_" + d] = o; print(d, {t: {k: round(v, 3) for k, v in o[t].items() if k != "cv"} for t in ("before", "after")})
# ---------------- 2. 42nd Street, match days vs ordinary days of the same mix
MATCH = {"weekday": ["2026-06-16", "2026-06-25", "2026-06-30"], "sat": ["2026-06-13", "2026-06-27"], "sun": ["2026-07-05", "2026-07-19"]}; allm = {x for v in MATCH.values() for x in v} | {"2026-06-22"}
BASE = {k: [d for d, v in DA.items() if v["daytype"] == k and "2026-05-18" <= d <= "2026-07-31" and d not in allm and d not in HOL and d not in ("2026-06-18", "2026-05-23", "2026-05-24")] for k in MATCH}
def rprof(route, groups):
    """mean seconds per bucket, averaged over day types with the match-day weights (3 weekdays, 2 Saturdays, 2 Sundays)"""
    P = {}; W = {k: len(MATCH[k]) / 7 for k in MATCH}
    for k, ds in groups.items():
        fs = [f"{B}/traj_speed/service_date={d}/route={route}.parquet" for d in ds]; fs = [f for f in fs if glob.glob(f)]
        for sid, (dt, n) in load_profile(f"select seg_id, bucket, sum(n) n, sum(sum_dt) sum_dt from read_parquet({fs!r}) group by all").items():
            a = P.setdefault(sid, [np.zeros(len(dt)), np.zeros(len(dt)), np.zeros(len(dt))]); m = min(len(dt), len(a[0])); ok = ~np.isnan(dt[:m]); a[0][:m][ok] += W[k] * dt[:m][ok]; a[1][:m][ok] += W[k]; a[2][:m] += n[:m]
    return {s: (np.where(a[1] > 0, a[0] / np.maximum(a[1], 1e-9), np.nan), a[2]) for s, a in P.items()}
Pm, Pn = rprof("M42", MATCH), rprof("M42", BASE); print("baseline days", {k: len(v) for k, v in BASE.items()})
for d in ("dir0", "dir1"):
    seq = ordered("M42", d, "42nd Street"); Cb, Ca = chain(seq, Pn), chain(seq, Pm); Cm = dict(Cb); Cm["e"] = (Cb["e"] + Ca["e"]) / 2; Z = zones(Cm); xs = Cb["xs"]
    o = dict(xs=xs.tolist(), busway=(0.0, float(xs[-1])), labels=[S[s]["label"].split(": ")[-1].split(" → ")[-1] for s in seq], ends=[s[2] for s in Cb["segx"]], sig=[(x, k) for x, _, k in Cb["sig"]], stops=[(x, nm) for x, _, nm, *_ in Cb["stops"]],
             zones=[dict(ia=z["ia"], ib=z["ib"], cat=z["cat"], apex=z["apex"], inb=True) for z in Z], miles=float(xs[-1] / 5280), bearing="east" if L(json.load(open(REPO / "outputs/network/nyc/segment_registry.json"))["segments"][seq[0]]["geometry_lonlat"])[0][0] < L(json.load(open(REPO / "outputs/network/nyc/segment_registry.json"))["segments"][seq[-1]]["geometry_lonlat"])[-1][0] else "west")
    for tag, C in (("before", Cb), ("after", Ca)):
        bg = floor(C["e"]); zs = [float((C["e"] - bg)[z["ia"]:z["ib"] + 1].sum() * STEP / 100) for z in Z]; tot = float(C["e"].sum() * STEP / 100); c = {"stop": 0.0, "nearside": 0.0, "signal": 0.0}
        for z, s_ in zip(Z, zs):
            if z["cat"] in c: c[z["cat"]] += s_
        c["other"] = tot - sum(c.values()); o[tag] = dict(e=np.round(C["e"], 2).tolist(), bg=np.round(bg, 2).tolist(), zsec=zs, inside=c, inside_total=tot, minutes=sum(s[3] for s in C["segx"]) / 60, n=float(np.median(C["n"])))
    OUT["m42_" + d] = o; print(d, o["bearing"], round(o["miles"], 2), "mi", {t: ({k: round(v) for k, v in o[t]["inside"].items()}, round(o[t]["minutes"], 1), "min", int(o[t]["n"]), "buses") for t in ("before", "after")})
json.dump(OUT, open(HERE / "fig4.json", "w"))
