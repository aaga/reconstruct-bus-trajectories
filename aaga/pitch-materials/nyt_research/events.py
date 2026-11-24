"""One-off events: bus speed on the affected streets on the event day(s) against comparable days. Uses seg x date x period travel time (all routes pooled)."""
from eng import *
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
duckdb.execute("set enable_progress_bar=false")
D = duckdb.sql("select seg_id, cast(service_date as varchar) d, period, n, sum_t from 'ext/city_seg_day.parquet'").df()
X = pd.read_parquet(HERE / "X.parquet")[["seg_id", "registry_name", "borough", "mid_lat", "mid_lon", "mi", "highway"]]; D = D.merge(X, on="seg_id"); D = D[~D.highway.isin(["motorway", "trunk"])]
DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; D["dow"] = pd.to_datetime(D.d).dt.dayofweek
HOL = {"2026-05-25", "2026-06-19", "2026-07-03", "2026-07-04", "2026-09-07", "2026-10-12", "2026-11-03", "2026-11-11", "2026-11-26"}
SPECIAL = HOL | {"2026-06-13", "2026-06-16", "2026-06-18", "2026-06-22", "2026-06-25", "2026-06-27", "2026-06-30", "2026-07-05", "2026-07-19", "2026-07-25", "2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-11-01", "2026-05-18", "2026-05-19", "2026-10-31"}
def mph(q): return (q.mi * q.n).sum() / (q.sum_t.sum() / 3600)
out = []
def ev(name, mask, days, periods, lo, hi, note=""):
    q = D[mask & D.period.isin(periods)]
    for d in days:
        dow = pd.Timestamp(d).dayofweek; a = q[q.d == d]; b = q[(q.dow == dow) & (q.d >= lo) & (q.d <= hi) & ~q.d.isin(SPECIAL) & (q.d != d)]
        if len(a) == 0 or b.d.nunique() < 3: print("skip", name, d); continue
        segs = set(a.seg_id) & set(b.seg_id); a = a[a.seg_id.isin(segs)]; bs = b[b.seg_id.isin(segs)].groupby("d").apply(mph)
        v = mph(a); out.append(dict(event=name, day=d, periods="+".join(periods), mph=v, base=bs.mean(), sd=bs.std(), n_base=len(bs), pct=100 * (v / bs.mean() - 1), z=(v - bs.mean()) / bs.std(), miles=a.groupby("seg_id").mi.first().sum(), note=note))
ALL = pd.Series(True, index=D.index); MN = D.borough == "Manhattan"; nm = D.registry_name.fillna("")
DAY = ["am_peak", "midday", "pm_peak"]
# Summer Streets, Manhattan (Park Av car-free 7am-3pm): parallel avenues and crosstown streets between 14th and 110th
aven = MN & nm.isin(["Lexington Avenue", "Madison Avenue", "3rd Avenue", "5th Avenue"]) & (D.mid_lat > 40.735) & (D.mid_lat < 40.795)
cross = MN & nm.str.contains(r"^(East|West) \d+(st|nd|rd|th) Street$") & (D.mid_lon > -73.985) & (D.mid_lon < -73.955) & (D.mid_lat > 40.735) & (D.mid_lat < 40.795)
for lab, m in (("Summer Streets: avenues beside Park Av", aven), ("Summer Streets: East Side crosstown streets", cross), ("Summer Streets: rest of Manhattan", MN & ~aven & ~cross)):
    ev(lab, m, ["2026-08-01", "2026-08-08", "2026-08-15"], ["am_peak", "midday"], "2026-06-27", "2026-09-05")
ev("Summer Streets: Grand Concourse", (D.borough == "Bronx") & (nm == "Grand Concourse"), ["2026-08-22"], ["am_peak", "midday"], "2026-06-27", "2026-09-26")
ev("Summer Streets: Eastern Pkwy crossings (Utica, Nostrand, Franklin, Bedford, Kingston Av)", (D.borough == "Brooklyn") & nm.isin(["Utica Avenue", "Nostrand Avenue", "Franklin Avenue", "Bedford Avenue", "Kingston Avenue", "Rogers Avenue", "New York Avenue"]) & (D.mid_lat > 40.660) & (D.mid_lat < 40.680), ["2026-08-22"], ["am_peak", "midday"], "2026-06-27", "2026-09-26")
# Knicks parade, Thu 18 June, lower Broadway
low = MN & (D.mid_lat < 40.722)
ev("Knicks parade: Manhattan below Canal St", low, ["2026-06-18"], ["midday"], "2026-05-01", "2026-07-31"); ev("Knicks parade: Manhattan below Canal St", low, ["2026-06-18"], ["pm_peak"], "2026-05-01", "2026-07-31"); ev("Knicks parade: rest of Manhattan", MN & ~low, ["2026-06-18"], ["midday"], "2026-05-01", "2026-07-31")
# LIRR strike: Mon 18 May all day, Tue 19 May morning
for b in ("Queens", "Brooklyn", "Manhattan", "Bronx"):
    ev(f"LIRR strike: {b}", D.borough == b, ["2026-05-18"], DAY, "2026-04-27", "2026-06-26"); 
ev("LIRR strike: Queens, Tue morning", D.borough == "Queens", ["2026-05-19"], ["am_peak"], "2026-04-27", "2026-06-26")
ev("LIRR strike: Jamaica (Archer, Sutphin, Jamaica Av, Hillside)", (D.borough == "Queens") & nm.isin(["Archer Avenue", "Sutphin Boulevard", "Jamaica Avenue", "Hillside Avenue", "Parsons Boulevard"]) & (D.mid_lon > -73.83) & (D.mid_lon < -73.77), ["2026-05-18"], DAY, "2026-04-27", "2026-06-26")
# Marathon, Sun 1 Nov
ev("Marathon: First Av, Manhattan", MN & (nm == "1st Avenue"), ["2026-11-01"], ["midday"], "2026-09-13", "2026-11-22"); ev("Marathon: 4th Av, Brooklyn", (D.borough == "Brooklyn") & (nm == "4th Avenue"), ["2026-11-01"], ["am_peak", "midday"], "2026-09-13", "2026-11-22")
for b in ("Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"): ev(f"Marathon: all of {b}", D.borough == b, ["2026-11-01"], ["midday"], "2026-09-13", "2026-11-22")
# School-related single days: Election Day (schools remote, offices open), Veterans Day, first day of school, Yom Kippur
for lab, d, lo, hi in (("Election Day, schools remote (Tue 3 Nov)", "2026-11-03", "2026-09-28", "2026-11-20"), ("Veterans Day (Wed 11 Nov)", "2026-11-11", "2026-09-28", "2026-11-20"), ("First day of school (Thu 10 Sept)", "2026-09-10", "2026-07-06", "2026-08-28"), ("Last day of school (Fri 26 June) vs May-June Fridays", "2026-06-26", "2026-05-01", "2026-06-25"),
                       ("Hottest day, 100F (Thu 2 July)", "2026-07-02", "2026-06-08", "2026-08-14"), ("Flood (Thu 21 May)", "2026-05-21", "2026-04-27", "2026-06-26"), ("Flood (Wed 20 May)", "2026-05-20", "2026-04-27", "2026-06-26"), ("Flood (Thu 20 Aug)", "2026-08-20", "2026-07-06", "2026-09-04"), ("Flood (Fri 21 Aug)", "2026-08-21", "2026-07-06", "2026-09-04"),
                       ("Gridlock alert day (Wed 18 Nov)", "2026-11-18", "2026-09-28", "2026-11-17"), ("Gridlock alert day (Thu 19 Nov)", "2026-11-19", "2026-09-28", "2026-11-17")):
    SPECIAL.discard(d); ev(lab + ": citywide", ALL, [d], DAY, lo, hi); ev(lab + ": Manhattan", MN, [d], DAY, lo, hi) if "Gridlock" in lab or "Election" in lab else None; SPECIAL.add(d)
# A train suspended Rockaway Blvd - Lefferts Blvd, 7-16 Nov: Liberty Av / Lefferts Blvd / Rockaway Blvd corridor
am = (D.borough == "Queens") & nm.isin(["Liberty Avenue", "Lefferts Boulevard", "Rockaway Boulevard"]) & (D.mid_lon > -73.85) & (D.mid_lon < -73.81)
ev("A train shutdown: Liberty Av, Lefferts Blvd, Rockaway Blvd", am, ["2026-11-09", "2026-11-10", "2026-11-12", "2026-11-13", "2026-11-16"], DAY, "2026-09-28", "2026-11-06")
ev("A train shutdown: rest of Queens", (D.borough == "Queens") & ~am, ["2026-11-09", "2026-11-10", "2026-11-12", "2026-11-13", "2026-11-16"], DAY, "2026-09-28", "2026-11-06")
R = pd.DataFrame(out); R.to_csv(HERE / "events.csv", index=False); print(R[["event", "day", "periods", "miles", "mph", "base", "sd", "n_base", "pct", "z"]].round(2).to_string())
