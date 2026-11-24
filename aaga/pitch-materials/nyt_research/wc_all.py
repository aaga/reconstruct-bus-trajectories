"""M42 on 42nd Street: seven World Cup match days (22 June left out: its closure began at 2pm) against ordinary days weighted to the same mix of
weekdays, Saturdays and Sundays. Also the same comparison for neighbouring crosstown streets. -> fig_wcall.json"""
import json, numpy as np, pandas as pd; from pathlib import Path
HERE = Path(__file__).parent
M = {"2026-06-13": 18, "2026-06-16": 15, "2026-06-25": 16, "2026-06-27": 17, "2026-06-30": 17, "2026-07-05": 16, "2026-07-19": 15}; SKIP = {"2026-06-22"}; HOL = {"2026-05-25", "2026-06-19", "2026-07-03"}; hs = list(range(6, 23))
def load(r):
    g = pd.read_parquet(HERE / f"wc_{r}.parquet"); g = g[(g.d >= "2026-05-18") & (g.d <= "2026-07-31") & g.h.between(6, 22) & ~g.d.isin(HOL | SKIP)].copy(); g["typ"] = np.where(g.dow < 5, "wd", np.where(g.dow == 5, "sat", "sun")); return g
def comp(g):
    b = g[~g.d.isin(M)]; wt = pd.Series({d: g[g.d == d].typ.iloc[0] for d in M if (g.d == d).any()}).value_counts(normalize=True)
    mu = sum(wt[t] * b[b.typ == t].groupby("h").mph.mean().reindex(hs) for t in wt.index).values; sd = sum(wt[t] * b[b.typ == t].groupby("h").mph.std().reindex(hs) for t in wt.index).values
    D = np.array([g[g.d == d].set_index("h").mph.reindex(hs).values for d in M]); return mu, sd, D, b.d.nunique()
g = load("M42"); mu, sd, D, nb = comp(g); mm = np.nanmean(D, 0); c = (np.array(hs) >= 12) & (np.array(hs) <= 19); e = np.array(hs) <= 7
out = dict(h=hs, mean=np.round(mu, 3).tolist(), sd=np.round(sd, 3).tolist(), match=np.round(mm, 3).tolist(), days={d: [None if np.isnan(v) else round(float(v), 3) for v in row] for d, row in zip(M, D)}, nbase=int(nb),
           closure=dict(match=float(mm[c].mean()), base=float(mu[c].mean()), speed_pct=float(100 * (mm[c].mean() / mu[c].mean() - 1)), time_pct=float(100 * (mu[c].mean() / mm[c].mean() - 1))), early=dict(match=float(mm[e].mean()), base=float(mu[e].mean())), other={})
for r, nm in (("M34p", "34th"), ("M50", "50th"), ("M57", "57th"), ("M23p", "23rd")):
    m2, _, D2, _ = comp(load(r)); a = np.nanmean(D2, 0); out["other"][nm] = float(100 * (np.nanmean(a[c]) / np.nanmean(m2[c]) - 1))
print({k: v for k, v in out.items() if k in ("closure", "early", "other", "nbase")}); json.dump(out, open(HERE / "fig_wcall.json", "w"))
