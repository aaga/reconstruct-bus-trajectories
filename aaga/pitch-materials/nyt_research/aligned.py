"""Delay curve around every signal approach, aligned at the light: -500 ft .. +300 ft, 5 ft grid. One row per owned approach."""
import pickle; from eng import *
ref = pickle.load(open(HERE / "ref.pkl", "rb")); S, SH = reg(); P = load_profile(f"select * from '{HERE}/cube/all.parquet'"); off = np.arange(-500, 301, 5.0); M = []; meta = []
for k, R in ref.items():
    seq = R["seq"]; C = chain(seq, P); xs, e = C["xs"], C["e"]; bg = floor(e); ends = np.cumsum([S[s]["len_m"] for s in seq]) * FT; own = set(R["own"])
    for j in R["own"]:
        if j + 1 >= len(seq) or (j + 1) not in own or j == 0: continue
        x = ends[j]
        if x - 500 < xs[0] or x + 300 > xs[-1]: continue
        M.append(np.r_[np.interp(x + off, xs, e), np.interp(x + off, xs, bg)]); meta.append(dict(seg_id=seq[j], nxt=seq[j + 1], prev=seq[j - 1], shape=k))
np.save(HERE / "aligned.npy", np.array(M, dtype=np.float32)); pd.DataFrame(meta).to_parquet(HERE / "aligned_meta.parquet"); print(len(M))
