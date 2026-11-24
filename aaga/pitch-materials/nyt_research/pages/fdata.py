"""Corridor data for the final pitch, through the corridor-page machinery (bap_corridors): East Tremont Av before / after the busway (weekdays,
zones fixed on the mean of the two curves) and all of Flatbush Avenue."""
import sys, importlib.util, pickle, copy, glob, json, numpy as np, pandas as pd, duckdb
from pathlib import Path
HERE = Path(__file__).parent; FIG = HERE.parent.parent; REPO = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists())
def mod(name, path):
    sys.path.insert(0, str(path.parent)); sp = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m
bap = mod("bapbuild", FIG / "bap_corridors" / "build.py"); lib = sys.modules["lib"]
DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; HOL = {"2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-10-12", "2026-11-03", "2026-11-11"}
WD = [d for d, v in DA.items() if v["daytype"] == "weekday" and d not in HOL]
def wd_profile(routes, segs, lo, hi):
    """lib.profile restricted to weekdays in [lo, hi]"""
    ds = {d for d in WD if lo <= d <= hi}; orig = lib.glob.glob
    lib.glob.glob = lambda p: [f for f in orig(p) if f.split("service_date=")[1][:10] in ds]
    try: return lib.profile(routes, segs)
    finally: lib.glob.glob = orig
def tremont():
    cache = HERE / "f_tremont.pkl"
    if cache.exists(): return pickle.load(open(cache, "rb"))
    c = next(c for c in bap.CORRIDORS if c["key"] == "tremont"); P = {}
    for d in ("dir0", "dir1"):
        segs = lib.corridor(c["routes"][0], d, c["streets"]); gb, nb = wd_profile(c["routes"], segs, "2026-05-18", "2026-06-26"); ga, na = wd_profile(c["routes"], segs, "2026-09-28", "2026-11-20")
        gm = gb.copy(); gm["dt"] = (gb.dt + ga.dt) / 2; am = lib.analyse(gm, segs); out = {}
        for tag, g in (("before", gb), ("after", ga)):
            a = lib.analyse(g, segs); Z = am["Z"].copy(); Z["sec"] = [float(((a["e"] - a["bg"])[int(z.ia):int(z.ib) + 1]).sum() * lib.STEP / 100) for _, z in Z.iterrows()]
            Z["height"] = [float(a["e"][int(z.ip)] - a["bg"][int(z.ip)]) for _, z in Z.iterrows()]; a["Z"] = Z; a["own"] = am["own"]; out[tag] = a
        P[d] = dict(segs=segs, bearing=lib.bearing(segs), days=(nb, na), **out)
    pickle.dump(P, open(cache, "wb")); return P
def tremont_D(P, tag):
    c = copy.deepcopy(next(c for c in bap.CORRIDORS if c["key"] == "tremont")); D = {d: dict(segs=P[d]["segs"], a=P[d][tag], days=P[d]["days"][0 if tag == "before" else 1], bearing=P[d]["bearing"]) for d in ("dir0", "dir1")}
    D["key"] = "tr" + tag[0]; ua, ub = bap.section(c, D); return c, D, ua, ub
def flatbush():
    c = dict(key="flatbush_full", title="Flatbush Avenue", borough="Brooklyn", routes=["B41"], streets=["Flatbush Avenue", "Flatbush Avenue Extension"], status="", text="")
    D = bap.load(c); D["key"] = "fb"; c["u"] = (0.0, float(D["dir1"]["a"]["xs"].max())); bap.section(c, D); return c, D
if __name__ == "__main__":
    P = tremont(); c, D, ua, ub = tremont_D(P, "before"); lim = c["before_after"]["limits"]; print("window", ua, ub, {d: P[d]["bearing"] for d in ("dir0", "dir1")}, {d: P[d]["days"] for d in ("dir0", "dir1")})
    for tag in ("before", "after"):
        c, D, ua, ub = tremont_D(P, tag)
        for d in ("dir0", "dir1"): s = bap.stats(D, d, *lim[d]); print(tag, d, P[d]["bearing"], {k: round(v, 1) if k == "miles" else round(v) for k, v in s.items()})
    c, D = flatbush(); print("flatbush", {d: (D[d]["bearing"], round(float(D[d]["a"]["xs"].max())), len(D[d]["a"]["Z"]), D[d]["days"]) for d in ("dir0", "dir1")}); M = bap.merged_signals(D, -1, 1e9); print(len(M), [m["name"] for m in M][:8], [m["name"] for m in M][-5:])
