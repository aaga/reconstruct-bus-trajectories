"""Citywide decomposition on the all-days pooled profile. Outputs seg-level and zone-level tables."""
import sys, time; from eng import *
def run(P, tag, ref=None):
    S, SH = reg(); order = sorted(SH, key=lambda k: -sum(np.nanmedian(P[s][1]) if s in P else 0 for s in SH[k]["seq"]))
    done = set(); segs = []; zs = []; t0 = time.time()
    for k in order:
        seq = SH[k]["seq"]
        if all(s in done for s in seq) or len(seq) < 3: continue
        C = chain(seq, P); Z = zones(C); xs, e, bg = C["xs"], C["e"], C["bg"]; ends = np.array([s[2] for s in C["segx"]])
        zseg = [C["segx"][min(int(np.searchsorted(ends, z["apex"])), len(ends) - 1)][0] for z in Z]
        for j, (sid, a, b, tot, ff) in enumerate(C["segx"]):
            if sid in done: continue
            m = (xs >= a) & (xs < b); exc = float(e[m].sum() * STEP / 100); r = dict(seg_id=sid, shape=k, route=SH[k]["route"], dir=SH[k]["dir"], n=float(C["n"][j]), total=tot, ff=ff, excess=exc, floor=float(bg[m].sum() * STEP / 100), stop=0.0, nearside=0.0, signal=0.0, otherpk=0.0)
            for z, zsid in zip(Z, zseg):
                if zsid == sid:
                    r[{"other": "otherpk"}.get(z["cat"], z["cat"])] += z["sec"]; zs.append(dict(seg_id=sid, shape=k, n=float(C["n"][j]), off_ft=b - z["apex"], **{q: z[q] for q in ("cat", "sec", "full", "height", "x0", "x1", "apex", "stop_id", "stop_side", "sig_seg", "sig_kind", "d_sig", "d_stop")}))
            segs.append(r)
        done.update(seq)
    A = pd.DataFrame(segs); A["other"] = A.excess - A.stop - A.nearside - A.signal; A.to_parquet(HERE / f"city_seg_{tag}.parquet"); pd.DataFrame(zs).to_parquet(HERE / f"city_zone_{tag}.parquet")
    print(tag, len(A), "segs", len(zs), "zones", round(time.time() - t0), "s", flush=True); return A
if __name__ == "__main__":
    P = load_profile(f"select * from '{HERE}/cube/all.parquet'"); print("profile", len(P), flush=True)
    A = run(P, "all"); w = A.n
    tot = (A.total * w).sum(); print({k: round(float((A[k] * w).sum() / tot), 3) for k in ("ff", "stop", "nearside", "signal", "other")}, "excess/total", round(float((A.excess * w).sum() / tot), 3), "ff+excess vs total", round(float(((A.ff + A.excess) * w).sum() / tot), 3))
    b = A[A.route == "B41"]; print("B41 segs", len(b), b.groupby("dir")[["total", "ff", "stop", "nearside", "signal", "other"]].sum().div(60).round(1))
