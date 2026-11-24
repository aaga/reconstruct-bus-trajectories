"""One row per signal approach (segment end): delay credited to the light, to a near-side stop+light, and to a far-side stop just after it."""
import pickle, sys; from eng import *
ref = pickle.load(open(HERE / "ref.pkl", "rb")); S, SH = reg(); P = load_profile(f"select * from '{HERE}/cube/all.parquet'"); A = pd.read_parquet(HERE / "city_seg_all.parquet").set_index("seg_id")
MV = json.load(open(REPO / "outputs/network/nyc/movements.json")); rows = []
for k, R in ref.items():
    seq = R["seq"]; ends = np.cumsum([S[s]["len_m"] for s in seq]) * FT; own = set(R["own"]); Z = R["zones"]; za = np.array([z["apex"] for z in Z]) if Z else np.array([])
    C = chain(seq, P); cx = C["xs"]; ce = C["e"]; cb = floor(ce)
    for j in R["own"]:
        if j + 1 >= len(seq) or (j + 1) not in own: continue
        sid, nxt = seq[j], seq[j + 1]; xs_ = ends[j]; s = S[sid]
        near = [o * FT for o, *_ in s["stops"] if o * FT <= 150]; far = [(S[nxt]["len_m"] - o) * FT for o, *_ in S[nxt]["stops"] if (S[nxt]["len_m"] - o) * FT <= 200]
        mid = [o * FT for o, *_ in s["stops"] if 150 < o * FT <= 400]
        ap = [z for z in Z if -300 <= z["apex"] - xs_ <= 30 and z["cat"] in ("signal", "nearside")]
        fz = [z for z in Z if 0 < z["apex"] - xs_ <= 300 and z["cat"] == "stop"]
        mv = MV.get(sid, {}); t = mv.get(k) or (max(set(mv.values()), key=list(mv.values()).count) if mv else None)
        rows.append(dict(seg_id=sid, nxt=nxt, shape=k, n=A.n[sid], cls=s["cls"], name=s["name"], label=s["label"], len_ft=s["len_m"] * FT, n_near=len(near), n_far=len(far), n_mid=len(mid), near_ft=min(near) if near else np.nan, far_ft=min(far) if far else np.nan,
                         appr=sum(z["sec"] for z in ap), appr_full=sum(z["full"] for z in ap), ns=sum(z["sec"] for z in ap if z["cat"] == "nearside"), farstop=sum(z["sec"] for z in fz), farstop_full=sum(z["full"] for z in fz), turn=t, win=float(ce[(cx >= xs_ - 250) & (cx < xs_ + 250)].sum() * STEP / 100), win_floor=float(cb[(cx >= xs_ - 250) & (cx < xs_ + 250)].sum() * STEP / 100), win_up=float(ce[(cx >= xs_ - 250) & (cx < xs_)].sum() * STEP / 100),
                         seg_other=A.other[sid], seg_excess=A.excess[sid], seg_ff=A.ff[sid], seg_total=A.total[sid], nxt_excess=A.excess[nxt], nxt_ff=A.ff[nxt], nxt_len_ft=S[nxt]["len_m"] * FT))
B = pd.DataFrame(rows); B["cfg"] = np.select([(B.n_near > 0) & (B.n_far == 0), (B.n_near == 0) & (B.n_far > 0), (B.n_near == 0) & (B.n_far == 0)], ["near", "far", "none"], "both"); B["bundle"] = B.appr + B.farstop
B.to_parquet(HERE / "bundle.parquet"); pd.set_option("display.width", 220); B = B[B.n > 500]
def wm(g, c): return np.average(g[c], weights=g.n)
print(B.cfg.value_counts().to_dict())
T = B[(B.turn == "T") & (B.len_ft >= 250) & (B.nxt_len_ft >= 250)]
f = lambda g: pd.Series(dict(n=len(g), appr=wm(g, "appr"), farstop=wm(g, "farstop"), bundle=wm(g, "bundle"), win=wm(g, "win"), win_med=g.win.median(), win_abovefloor=wm(g.assign(x=g.win - g.win_floor), "x"), buses=g.n.median() / 208))
print(T.groupby("cfg").apply(f).round(1))
print(T[T.cls.isin(["primary", "secondary", "tertiary", "residential"])].groupby(["cls", "cfg"]).apply(f)[["n", "bundle", "win"]].round(1).unstack())
N = B[(B.cfg == "none") & (B.len_ft >= 250) & (B.nxt_len_ft >= 250)]
print("turn:", N.groupby("turn").apply(lambda g: pd.Series(dict(n=len(g), appr=wm(g, "appr"), win=wm(g, "win"), win_med=g.win.median()))).round(1))
