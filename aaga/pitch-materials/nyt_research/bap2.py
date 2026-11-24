"""Plan corridor extents on our segments, following the prior report's from/to limits (24 printed in the plan, the rest read from its map). Validated against that report's blocks, miles and hours per corridor."""
from eng import *; import re
pd.set_option("display.width", 260); pd.set_option("display.max_rows", 200)
PR = json.load(open(HERE / "ext/spdata.json"))["viz"]["plan"]
T = pd.read_parquet(HERE / "rush_seg.parquet"); S, SH = reg(); r = json.load(open(REPO / "outputs/network/nyc/segment_registry.json"))["segments"]
T["lab"] = T.seg_id.map(lambda s: S[s]["label"]); T["a"] = T.lab.str.split(": ").str[-1].str.split(" → ").str[0]; T["b"] = T.lab.str.split(" → ").str[-1]
geo = {s: np.array(L(r[s]["geometry_lonlat"])) for s in T.seg_id}; T["x0"] = T.seg_id.map(lambda s: geo[s][0][0]); T["y0"] = T.seg_id.map(lambda s: geo[s][0][1]); T["x1"] = T.seg_id.map(lambda s: geo[s][-1][0]); T["y1"] = T.seg_id.map(lambda s: geo[s][-1][1])
BO = {"BX": ["Bronx"], "MN": ["Manhattan"], "QN": ["Queens"], "BK": ["Brooklyn"], "SI": ["Staten Island"], "BK–QN": ["Brooklyn", "Queens"]}
CAND = {1: ["East Gun Hill Road", "West Gun Hill Road"], 2: ["East Kingsbridge Road|West Kingsbridge Road"], 3: ["East Fordham Road|West Fordham Road|Fordham Road"], 4: ["White Plains Road"], 5: ["East Tremont Avenue"], 6: ["Grand Concourse"], 7: ["Third Avenue", "3rd Avenue"], 8: ["Southern Boulevard"],
        9: ["West 181st Street", "East Tremont Avenue|West Tremont Avenue", "East 177th Street", "Cross Bronx Expressway", "Bruckner Boulevard", "Westchester Avenue"], 10: ["West 145th Street|East 149th Street|145th Street Bridge", "East 149th Street", "West 145th Street"], 11: ["Broadway", "Amsterdam Avenue"],
        12: ["West 125th Street|East 125th Street"], 13: ["Lexington Avenue"], 14: ["West 116th Street|East 116th Street", "Manhattan Avenue", "Morningside Avenue", "Pleasant Avenue"], 15: ["West 79th Street|East 79th Street|79th Street Transverse"], 16: ["East 50th Street|West 50th Street|East 49th Street|West 49th Street"],
        17: ["West 34th Street|East 34th Street"], 18: ["9th Avenue"], 19: ["Lexington Avenue"], 20: ["6th Avenue"], 21: ["Madison Avenue"], 22: ["Broadway"], 23: ["Northern Boulevard"], 24: ["Broadway", "Roosevelt Avenue"], 25: ["Grand Avenue|Grand Street"], 26: ["108th Street"],
        27: ["Main Street", "Linden Place", "Union Street"], 28: ["Kissena Boulevard", "Union Street", "Parsons Boulevard"], 30: ["Woodhaven Boulevard"], 31: ["Woodhaven Boulevard|Cross Bay Boulevard"], 32: ["Jamaica Avenue"], 33: ["Merrick Boulevard"], 34: ["Guy R. Brewer Boulevard", "Sutphin Boulevard", "Guy R. Brewer Boulevard|Sutphin Boulevard"],
        35: ["Bedford Avenue"], 36: ["Marcy Avenue"], 37: ["Livingston Street"], 38: ["DeKalb Avenue", "Lafayette Avenue"], 39: ["Flatbush Avenue|Flatbush Avenue Extension", "Flatbush Avenue|Marine Parkway Bridge|Flatbush Avenue Extension"], 40: ["Utica Avenue", "Malcolm X Boulevard", "Broadway"],
        41: ["Church Avenue"], 42: ["Church Avenue", "Linden Boulevard|North Conduit Avenue|South Conduit Avenue|North Conduit Boulevard", "Linden Boulevard", "North Conduit Avenue|South Conduit Avenue", "New Lots Avenue"], 43: ["Linden Boulevard", "Linden Boulevard|North Conduit Avenue|South Conduit Avenue"], 45: ["Avenue J"], 46: ["Bay Parkway", "Cropsey Avenue"], 47: ["86th Street"], 48: ["Bay Street"], 49: ["Victory Boulevard"], 50: ["Richmond Avenue"]}
ROUTE = {10: "BX6+", 44: "B82+"}
PARTCAND = {42: [["Church Avenue"], ["Linden Boulevard|North Conduit Avenue|South Conduit Avenue|North Conduit Boulevard", "Linden Boulevard"], ["New Lots Avenue"]],
            14: [["West 116th Street|East 116th Street"], ["Manhattan Avenue|Morningside Avenue", "Manhattan Avenue", "Morningside Avenue"], ["Pleasant Avenue"]]}; X = pd.read_parquet(HERE / "X.parquet").set_index("seg_id"); T["boro"] = T.seg_id.map(X.borough); T["nm"] = T.seg_id.map(lambda s: S[s]["name"])
def interval(q, A, B, target):
    """candidate block sets on street set q: between cross streets A and B, or `target` blocks counted from either one, by projection on the street's main axis"""
    P0 = np.c_[(q.x0 + q.x1) / 2 * np.cos(np.radians(40.7)), (q.y0 + q.y1) / 2]; c = P0.mean(0); u = np.linalg.svd(P0 - c, full_matrices=False)[2][0]
    pr = lambda x, y: (np.c_[x * np.cos(np.radians(40.7)), y] - c) @ u; mid = (P0 - c) @ u
    def pos(N):
        v = np.r_[pr(q.x1[q.b == N], q.y1[q.b == N]), pr(q.x0[q.a == N], q.y0[q.a == N])]; return [float(np.median(v))] + [float(v.min()), float(v.max())] if len(v) else []
    pa, pb = pos(A), pos(B); opts = []
    for a_ in pa:
        for b_ in pb:
            if abs(a_ - b_) > 1e-6: opts.append(q[(mid >= min(a_, b_) - 1e-5) & (mid <= max(a_, b_) + 1e-5)])
    for p0 in pa + pb:
        for sgn in (1, -1):
            o = np.argsort(sgn * mid); o = o[sgn * mid[o] >= sgn * p0 - 1e-5][:target]; opts.append(q.iloc[o])
    if not pa and not pb: opts.append(q)
    return [o for o in opts if len(o)]
rows = []; member = {}
import itertools
for c in PR["corridors"]:
    num = c["num"]; per_part = []
    for p in c["parts"]:
        if p["basis"] == "route":
            segs = {s for k, v in SH.items() if v["route"] == ROUTE[num] for s in v["seq"]}; per_part.append([(T[T.seg_id.isin(segs)], "route " + ROUTE[num])]); continue
        opts = []
        for cand in (PARTCAND[num][c["parts"].index(p)] if num in PARTCAND else CAND[num]):
            q = T[T.nm.isin(cand.split("|")) & T.boro.isin(BO[c["boro"]] + (["Manhattan"] if num in (9, 10) else []) + (["Bronx"] if num == 11 else []))]
            if len(q) < 2: continue
            for o in interval(q, p["from"], p["to"], p["blocks"]):
                if abs(len(o) - p["blocks"]) <= max(3, (0.6 if num in PARTCAND else 0.25) * p["blocks"]): opts.append((o, cand))
        if not opts: print("NO OPTION", num, c["name"], p); opts = [(T.iloc[:0], "none")]
        # keep the distinct options closest in block count
        seen = {}; 
        for o, cand in opts: seen.setdefault((cand, frozenset(o.seg_id)), (o, cand))
        opts = sorted(seen.values(), key=lambda t: abs(len(t[0]) - p["blocks"]))[:14]; per_part.append(opts)
    best = None
    for combo in itertools.islice(itertools.product(*per_part), 4000):
        segs = set().union(*[set(g.seg_id) for g, _ in combo]) if combo else set(); q = T[T.seg_id.isin(segs)]
        err = abs(q.mi.sum() - c["dir_mi"]) / max(c["dir_mi"], .1) + abs(q.hr.sum() - c["hr"]) / max(c["hr"], 1) + 0.2 * sum(abs(len(g) - p["blocks"]) / p["blocks"] for (g, _), p in zip(combo, c["parts"]))
        if best is None or err < best[0]: best = (err, combo, q)
    if best is None: best = (0, (), T.iloc[:0])
    _, combo, q = best
    for s_ in q.seg_id: member.setdefault(s_, num)
    rows.append(dict(num=num, name=c["name"], blocks=len(q), blocks_ref=sum(p["blocks"] for p in c["parts"]), mi=q.mi.sum(), mi_ref=c["dir_mi"], hr=q.hr.sum(), hr_ref=c["hr"], parts=" ; ".join(f"{nm.split('|')[0]} {len(g)}/{p['blocks']}" for (g, nm), p in zip(combo, c["parts"]))))
R = pd.DataFrame(rows); R["d_mi"] = (R.mi / R.mi_ref.replace(0, np.nan) - 1).round(2); R["d_hr"] = (R.hr / R.hr_ref.replace(0, np.nan) - 1).round(2)
print(R[["num", "name", "blocks", "blocks_ref", "mi", "mi_ref", "hr", "hr_ref", "d_mi", "d_hr", "parts"]].round(1).to_string())
M = T[T.seg_id.isin(member)]; print(f"plan total: {len(M)} blocks (ref 3203), {M.mi.sum():.0f} dir mi (ref 291), {M.hr.sum():.0f} h (ref 3352), {100*M.hr.sum()/T.hr.sum():.1f}% of rush lost time (ref 24.9)")
pd.Series(member).rename("bap").rename_axis("seg_id").reset_index().to_parquet(HERE / "bap_member.parquet")
