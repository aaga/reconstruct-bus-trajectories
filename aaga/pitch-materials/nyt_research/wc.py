"""World Cup match days: 42nd St closed to traffic except buses (First to Twelfth Av) from 6 h before kickoff to 3 h after the match."""
from eng import *; import glob
pd.set_option("display.width", 250)
B = str(REPO / "outputs/network/nyc/traversals"); S, SH = reg()
MATCH = {"2026-06-13": 18, "2026-06-16": 15, "2026-06-22": 20, "2026-06-25": 16, "2026-06-27": 17, "2026-06-30": 17, "2026-07-05": 16, "2026-07-19": 15}
def street(route, pat):
    fs = glob.glob(f"{B}/service_date=2026-0[67]-*/route={route}.parquet") + glob.glob(f"{B}/service_date=2026-05-*/route={route}.parquet")
    q = duckdb.sql(f"select seg_id, service_date::varchar d, hour_local h, count(*) n, avg(t_obs_s) t from read_parquet({fs!r}) where flags=0 group by all").df()
    q["name"] = q.seg_id.map(lambda s: S[s]["name"]); q = q[q.name.str.contains(pat, na=False)]; q["len"] = q.seg_id.map(lambda s: S[s]["len_m"])
    # keep segments seen on most day-hours; speed = sum len / sum mean t over those segments
    k = q.groupby("seg_id").size(); keep = k[k >= k.max() * 0.6].index; q = q[q.seg_id.isin(keep)]
    g = q.groupby(["d", "h"]).agg(t=("t", "sum"), L=("len", "sum"), ns=("seg_id", "nunique"), n=("n", "median")).reset_index(); g = g[g.ns >= len(keep) * 0.9]
    g["mph"] = g.L / g.t * 2.23694; g["dow"] = pd.to_datetime(g.d).dt.dayofweek; return g, len(keep), q.len.groupby(q.seg_id).first().sum() / 1609.34
for route, pat in (("M42", "42nd Street"), ("M34+", "34th Street"), ("M50", "50th Street|49th Street"), ("M57", "57th Street"), ("M23+", "23rd Street"), ("M5", "5th Avenue"), ("M7", "6th Avenue|Avenue of the Americas")):
    g, ns, mi = street(route, pat); rows = []
    for d, ko in MATCH.items():
        hrs = list(range(ko - 6, min(ko + 5, 24))); dow = pd.Timestamp(d).dayofweek
        a = g[(g.d == d) & g.h.isin(hrs)]; base = g[(g.dow == dow) & ~g.d.isin(MATCH) & g.h.isin(hrs) & (g.d >= "2026-05-25") & (g.d <= "2026-07-31")]
        if len(a) < 4: continue
        b = base.groupby("d").mph.mean(); rows.append(dict(d=d, ko=ko, match=a.mph.mean(), base=b.mean(), sd=b.std(), nb=len(b), diff=a.mph.mean() - b.mean()))
    R = pd.DataFrame(rows); print(route, pat, "segs", ns, "miles", round(mi, 2)); print(R.round(2).to_string()) if len(R) else None
    g.to_parquet(HERE / f"wc_{route.replace('+','p')}.parquet")
