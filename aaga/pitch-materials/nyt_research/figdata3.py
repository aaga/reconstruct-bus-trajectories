"""Data for: Tremont before/after by delay type; top signal-priority intersections' curves before/after; stop placement with traffic removed (zone blobs only)."""
import pickle, sys; from eng import *; from street import ordered
sys.path.insert(0, str(HERE)); import tsp as tspm
duckdb.execute("set enable_progress_bar=false"); S, SH = reg(); OUT = {}
DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; HOL = {"2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-10-12", "2026-11-03", "2026-11-11"}
wd = [d for d, v in DA.items() if v["daytype"] == "weekday" and d not in HOL]
def prof(ds):
    fl = [f"{HERE}/cube/day_{m}.parquet" for m in sorted({d[:7] for d in ds})]
    return load_profile(f"select seg_id, bucket, sum(n) n, sum(sum_dt) sum_dt from read_parquet({fl!r}) where cast(service_date as varchar) in {tuple(ds) + ('x',)!r} group by all")
def two(seq, Pb, Pa):
    """zones fixed on the mean of the two curves; each period measured with its own floor"""
    Cb, Ca = chain(seq, Pb), chain(seq, Pa); Cm = dict(Cb); Cm["e"] = (Cb["e"] + Ca["e"]) / 2; Z = zones(Cm); res = {}
    for tag, C in (("before", Cb), ("after", Ca)):
        bg = floor(C["e"]); res[tag] = dict(e=np.round(C["e"], 2).tolist(), bg=np.round(bg, 2).tolist(), zsec=[float((C["e"] - bg)[z["ia"]:z["ib"] + 1].sum() * STEP / 100) for z in Z])
    return Cb, Z, res
# ---------------- Tremont
Pb = prof([d for d in wd if "2026-05-18" <= d <= "2026-06-26"]); Pa = prof([d for d in wd if "2026-09-28" <= d <= "2026-11-20"])
for d, i0, i1, b0, b1 in (("dir0", 10, 34, 18, 30), ("dir1", 10, 31, 15, 21)):
    seq = ordered("BX36", d, "Tremont")[i0:i1]; C, Z, res = two(seq, Pb, Pa); xs = C["xs"]; bw = (C["segx"][b0 - i0][1], C["segx"][b1 - i0 - 1][2]); inb = (xs >= bw[0]) & (xs <= bw[1])
    o = dict(xs=xs.tolist(), busway=bw, labels=[S[s]["label"].split(": ")[-1].split(" → ")[-1] for s in seq], ends=[s[2] for s in C["segx"]], sig=[(x, k) for x, _, k in C["sig"]], stops=[(x, nm) for x, _, nm, *_ in C["stops"]],
             zones=[dict(ia=z["ia"], ib=z["ib"], cat=z["cat"], apex=z["apex"], inb=bool(bw[0] <= z["apex"] <= bw[1])) for z in Z], **res)
    for tag in ("before", "after"):
        e = np.array(o[tag]["e"]); tot = float(e[inb].sum() * STEP / 100); c = {"stop": 0.0, "nearside": 0.0, "signal": 0.0}
        for z, s_ in zip(o["zones"], o[tag]["zsec"]):
            if z["inb"] and z["cat"] in c: c[z["cat"]] += s_
        c["other"] = tot - sum(c.values()); o[tag]["inside"] = c; o[tag]["inside_total"] = tot
    OUT["tremont_" + d] = o; print(d, {t: {k: round(v) for k, v in o[t]["inside"].items()} for t in ("before", "after")})
# ---------------- signal priority: top 5 approaches, curve before / after
ref = pickle.load(open(HERE / "ref.pkl", "rb")); owner = {R["seq"][j]: k for k, R in ref.items() for j in R["own"]}
ind = pd.read_parquet(HERE / "tsp_ind.parquet"); G = ind.groupby(["d", "seg_id", "nm", "label", "boro"]).agg(pre=("pre", "sum"), post=("post", "sum"), ctrl=("mean", "sum"), nmin=("nmin", "first")).reset_index(); G["did"] = G.post - G.pre - G.ctrl
G = G[~G.nm.str.contains("Dyer") & (G.nmin >= 1000)].sort_values("did").head(5); OUT["tsp5"] = []
for _, g in G.iterrows():
    pre, post = tspm.windows(g.d); k = owner[g.seg_id]; seq = SH[k]["seq"]; j = seq.index(g.seg_id); sub = seq[max(0, j - 4):j + 4]; C, Z, res = two(sub, prof(pre), prof(post)); xs = C["xs"]
    xsig = [s[2] for s in C["segx"] if s[0] == g.seg_id][0]; m = (xs >= xsig - 600) & (xs <= xsig + 250)
    zz = [dict(a=float(xs[z["ia"]] - xsig), b=float(xs[z["ib"]] - xsig), cat=z["cat"], apex=float(z["apex"] - xsig), before=res["before"]["zsec"][i], after=res["after"]["zsec"][i]) for i, z in enumerate(Z) if xsig - 600 <= z["apex"] <= xsig + 250]
    OUT["tsp5"].append(dict(nm=g.nm, label=g.label, d=g.d, boro=g.boro, did=float(g.did), pre=float(g.pre), post=float(g.post), off=(xs[m] - xsig).tolist(), before=dict(e=np.array(res["before"]["e"])[m].tolist(), bg=np.array(res["before"]["bg"])[m].tolist()), after=dict(e=np.array(res["after"]["e"])[m].tolist(), bg=np.array(res["after"]["bg"])[m].tolist()),
                            zones=zz, sig=[float(x - xsig) for x, _, kd in C["sig"] if kd == "sig" and -600 <= x - xsig <= 250], stops=[(float(x - xsig), nm) for x, _, nm, *_ in C["stops"] if -600 <= x - xsig <= 250], n_pre=len(pre), n_post=len(post)))
    print(g.nm, g.d, round(g.pre, 1), "->", round(g.post, 1), [(z["cat"], round(z["before"]), round(z["after"])) for z in zz])
# ---------------- stop placement: blobs only (curve minus floor inside each zone), aligned at the light
P = load_profile(f"select * from '{HERE}/cube/all.parquet'"); B = pd.read_parquet(HERE / "bundle.parquet").drop_duplicates("seg_id").set_index("seg_id"); off = np.arange(-300, 301, 5.0); acc = {}
for k, R in ref.items():
    seq = R["seq"]; C = chain(seq, P); xs, e = C["xs"], C["e"]; bg = floor(e); ex = e - bg; ends = np.cumsum([S[s]["len_m"] for s in seq]) * FT; own = set(R["own"])
    for j in R["own"]:
        sid = seq[j]
        if j + 1 >= len(seq) or (j + 1) not in own or sid not in B.index: continue
        b = B.loc[sid]
        if not (b.n > 2000 and b.len_ft >= 350 and b.nxt_len_ft >= 300 and b.n_mid == 0 and b.turn == "T" and b.cfg in ("none", "far", "near")): continue
        x = ends[j]
        if x - 300 < xs[0] or x + 300 > xs[-1]: continue
        # only the blobs that belong to this light and to a stop just after it; a far-side stop's blob cannot start before the light
        lay = {c: np.zeros(len(xs)) for c in ("signal", "stop", "nearside")}; ix = int(np.searchsorted(xs, x))
        for z in R["zones"]:
            a_, b_ = z["ia"], z["ib"] + 1
            if z["cat"] in ("signal", "nearside") and z.get("sig_seg") == sid and z.get("sig_kind") == "sig": lay[z["cat"]][a_:b_] = ex[a_:b_]
            elif z["cat"] == "stop" and 0 < z["apex"] - x <= 300:
                lay["stop"][max(a_, ix):b_] = ex[max(a_, ix):b_]
                if a_ < ix: lay["signal"][a_:ix] = ex[a_:ix]
        if b.cfg == "far" and b.far_ft == b.far_ft:      # past the far-side stop's approach, anything still credited to the light belongs to the stop
            ic = int(np.searchsorted(xs, x + max(b.far_ft - 60, 20))); lay["stop"][ic:] += lay["signal"][ic:]; lay["signal"][ic:] = 0
        a = acc.setdefault(b.cfg, dict(w=0.0, n=0, **{c: np.zeros(len(off)) for c in lay}))
        for c in lay: a[c] += b.n * np.interp(x + off, xs, lay[c])
        a["w"] += b.n; a["n"] += 1
OUT["place"] = dict(off=off.tolist(), **{cfg: dict(n=a["n"], **{c: np.round(a[c] / a["w"], 3).tolist() for c in ("signal", "stop", "nearside")}, **{c + "_s": float((a[c] / a["w"]).sum() * 5 / 100) for c in ("signal", "stop", "nearside")}) for cfg, a in acc.items()})
print({cfg: (v["n"], {c: round(v[c + "_s"], 1) for c in ("signal", "stop", "nearside")}) for cfg, v in OUT["place"].items() if cfg != "off"})
json.dump(OUT, open(HERE / "fig3.json", "w"))
