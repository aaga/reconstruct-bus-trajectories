"""Corridor units = runs of >= 1 mile on one street within a route shape (deduplicated by segment set). Features from OSM, outcomes from the decomposition.
Then nearest-neighbour 'twins': similar street, different delay."""
import pickle; from eng import *
from sklearn.preprocessing import StandardScaler
pd.set_option("display.width", 260); pd.set_option("display.max_rows", 200)
ref = pickle.load(open(HERE / "ref.pkl", "rb")); S, SH = reg(); X = pd.read_parquet(HERE / "X.parquet").set_index("seg_id"); used = set(); rows = []
for k, v in sorted(SH.items(), key=lambda kv: -len(kv[1]["seq"])):
    seq = v["seq"]; i = 0
    while i < len(seq):
        nm = S[seq[i]]["name"]; j = i
        while j + 1 < len(seq) and S[seq[j + 1]]["name"] == nm: j += 1
        run = [s for s in seq[i:j + 1] if s in X.index]; Lmi = sum(S[s]["len_m"] for s in run) / 1609.34
        if nm and Lmi >= 1.0 and len(run) >= 6 and len(set(run) & used) < 0.3 * len(run):
            q = X.loc[run]; w = q.mi; m = w.sum(); used.update(run)
            rows.append(dict(name=nm, route=v["route"], dir=v["dir"], shape=k, segs=run, mi=m, boro=q.borough.mode()[0], cls=q.highway.mode()[0], lanes=np.nansum(q.lanes_dir * w) / w[q.lanes_dir.notna()].sum() if q.lanes_dir.notna().any() else np.nan,
                             oneway=np.average(q.oneway.astype(float), weights=w), buslane=np.average(q.bus_lane_share, weights=w), sig_mi=len(run) / m, stop_mi=q.n_stops.sum() / m, bpd=np.average(q.n, weights=w) / 208,
                             frm=S[run[0]]["label"].split(": ")[-1].split(" → ")[0], to=S[run[-1]]["label"].split(" → ")[-1],
                             **{c: (q[c2] * q.n).sum() / (q.mi * q.n).sum() for c, c2 in (("ff", "ff"), ("stops", "stops_"), ("lights", "signal"), ("other", "other"), ("total", "total"))}))
        i = j + 1
C = pd.DataFrame(rows); C["mph"] = 3600 / C.total; C["delay"] = C.total - C.ff; C.to_pickle(HERE / "corridors_all.pkl"); print(len(C), "corridors", round(C.mi.sum()), "miles")
print(C.groupby("boro")[["mph", "stops", "lights", "other", "sig_mi", "stop_mi", "lanes"]].median().round(1))
D = C.dropna(subset=["lanes"]).reset_index(drop=True); F = D[["lanes", "oneway", "sig_mi", "stop_mi", "buslane"]].copy(); F["lbpd"] = np.log(D.bpd); Zs = StandardScaler().fit_transform(F)
pairs = []
for i in range(len(D)):
    for j in range(i + 1, len(D)):
        if D.boro[i] != D.boro[j] or D.cls[i] != D.cls[j] or D.name[i] == D.name[j]: continue
        d = np.linalg.norm(Zs[i] - Zs[j])
        if d < 0.6: pairs.append((abs(D.delay[i] - D.delay[j]), d, i, j))
pairs.sort(reverse=True); seen = set()
cols = ["name", "frm", "to", "route", "boro", "cls", "mi", "lanes", "sig_mi", "stop_mi", "buslane", "bpd", "mph", "stops", "lights", "other", "total"]
for gap, d, i, j in pairs:
    if i in seen or j in seen: continue
    seen.update((i, j)); a, b = (i, j) if D.delay[i] < D.delay[j] else (j, i)
    print(f"--- gap {gap:.0f} s/mile  (distance {d:.2f})"); print(D.loc[[a, b], cols].round(1).to_string(header=len(seen) == 2))
    if len(seen) >= 24: break
# what explains corridor delay? simple regression
import statsmodels.formula.api as smf
R = D.assign(lbpd=np.log(D.bpd)); 
for y in ("other", "lights", "stops", "delay"):
    m = smf.wls(f"{y} ~ C(boro) + lanes + oneway + sig_mi + stop_mi + buslane + lbpd", R, weights=R.mi).fit(); print(y, "R2", round(m.rsquared, 2), {k: f"{v:.1f} ({m.bse[k]:.1f})" for k, v in m.params.items() if not k.startswith("C(") and k != "Intercept"})
