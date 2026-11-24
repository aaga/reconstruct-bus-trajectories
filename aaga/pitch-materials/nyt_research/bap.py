"""Bus Action Plan priority corridors (our reading of the plan's schematic map, p.19) mapped onto street names, and a ranking of streets by bus delay."""
from eng import *; import re
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
X = pd.read_parquet(HERE / "X.parquet"); D = 208; X = X[X.n > 1000].copy(); X["nm"] = X.registry_name.fillna("")
BX, MN, QN, BK, SI = "Bronx", "Manhattan", "Queens", "Brooklyn", "Staten Island"
BAP = [(1, "E Gun Hill Rd", BX, ["East Gun Hill Road"]), (2, "E Kingsbridge Rd", BX, ["East Kingsbridge Road", "West Kingsbridge Road"]), (3, "E Fordham Rd", BX, ["East Fordham Road", "West Fordham Road", "Fordham Road"]), (4, "White Plains Rd", BX, ["White Plains Road"]),
       (5, "E Tremont Ave", BX, ["East Tremont Avenue", "West Tremont Avenue"]), (6, "Grand Concourse", BX, ["Grand Concourse"]), (7, "3rd Ave", BX, ["3rd Avenue", "Third Avenue"]), (8, "Southern Blvd", BX, ["Southern Boulevard"]),
       (9, "Tremont/Cross Bronx", BX, ["East 177th Street"]), (10, "South Bronx Crosstown", BX, ["East 149th Street", "East 161st Street", "East 163rd Street"]), (10, "South Bronx Crosstown", MN, ["West 145th Street"]),
       (11, "Upper Manhattan North-South", MN, [("Broadway", 40.811, 99), ("Amsterdam Avenue", 40.811, 99), "Saint Nicholas Avenue", "10th Avenue"]), (12, "125th St", MN, ["West 125th Street", "East 125th Street"]),
       (13, "Lexington Ave (95th-125th)", MN, [("Lexington Avenue", 40.784, 99)]), (14, "116th St", MN, ["West 116th Street", "East 116th Street"]), (15, "79th St", MN, ["West 79th Street", "East 79th Street"]),
       (16, "Midtown Crosstown", MN, ["West 42nd Street", "East 42nd Street", "West 49th Street", "East 49th Street", "West 50th Street", "East 50th Street"]), (17, "34th St Busway", MN, ["West 34th Street", "East 34th Street"]), (18, "9th Avenue", MN, ["9th Avenue"]),
       (19, "Lexington Ave (52nd-60th)", MN, [("Lexington Avenue", 40.757, 40.764)]), (20, "6th Ave", MN, ["6th Avenue", "Avenue of the Americas"]), (21, "Madison Ave", MN, ["Madison Avenue"]), (22, "Broadway", MN, [("Broadway", 0, 40.757)]),
       (23, "Northern Blvd", QN, ["Northern Boulevard"]), (24, "Jackson Heights Hub", QN, ["Roosevelt Avenue", "Broadway"]), (25, "Grand Ave/Grand St", BK, ["Grand Street"]), (25, "Grand Ave/Grand St", QN, ["Grand Avenue"]), (26, "108th St", QN, ["108th Street"]),
       (27, "Main St/Linden Pl", QN, ["Main Street", "Linden Place"]), (28, "Kissena/Union/Parsons", QN, ["Kissena Boulevard", "Union Street", "Parsons Boulevard"]), (29, "Ridgewood Busway", QN, ["Fresh Pond Road", "Palmetto Street"]),
       (30, "Woodhaven/Metropolitan", QN, ["Woodhaven Boulevard"]), (31, "Woodhaven", QN, ["Woodhaven Boulevard", "Cross Bay Boulevard"]), (32, "Jamaica Ave", QN, ["Jamaica Avenue"]), (33, "Merrick Blvd", QN, ["Merrick Boulevard"]),
       (34, "Sutphin/Guy R Brewer", QN, ["Sutphin Boulevard", "Guy R Brewer Boulevard", "Guy R. Brewer Boulevard"]), (35, "Bedford Ave", BK, ["Bedford Avenue"]), (36, "Marcy Ave", BK, ["Marcy Avenue"]), (37, "Livingston St", BK, ["Livingston Street"]),
       (38, "DeKalb/Lafayette", BK, ["DeKalb Avenue", "Dekalb Avenue", "Lafayette Avenue"]), (39, "Flatbush Ave", BK, ["Flatbush Avenue", "Flatbush Avenue Extension"]), (40, "Utica Ave", BK, ["Utica Avenue", "Malcolm X Boulevard"]), (41, "Church Ave", BK, ["Church Avenue"]),
       (42, "Kensington-JFK", BK, ["Church Avenue", "Linden Boulevard"]), (42, "Kensington-JFK", QN, ["Linden Boulevard", "North Conduit Avenue", "South Conduit Avenue"]), (43, "Linden Blvd", BK, ["Linden Boulevard"]), (44, "South Brooklyn Crosstown", BK, ["Kings Highway", "Flatlands Avenue"]),
       (45, "Avenue J", BK, ["Avenue J"]), (46, "Bay Parkway", BK, ["Bay Parkway"]), (47, "86th St", BK, ["86th Street"]), (48, "Bay St", SI, ["Bay Street"]), (49, "Victory Blvd", SI, ["Victory Boulevard"]), (50, "Richmond Ave", SI, ["Richmond Avenue"])]
X["bap"] = 0; X["bap_name"] = ""
for num, lab, bo, names in BAP:
    for nmx in names:
        nm, lo, hi = (nmx, -99, 99) if isinstance(nmx, str) else nmx; m = (X.borough == bo) & (X.nm == nm) & (X.mid_lat >= lo) & (X.mid_lat <= hi)
        if m.sum() == 0: print("NO MATCH", num, lab, nm)
        X.loc[m & (X.bap == 0), "bap_name"] = lab; X.loc[m & (X.bap == 0), "bap"] = num
X["bpd"] = X.n / D; X["dh"] = (X.total - X.ff) * X.bpd / 3600     # bus-hours of delay per day
X["oh"] = X.other * X.bpd / 3600; X["lh"] = X.signal * X.bpd / 3600; X["nh"] = X.nearside * X.bpd / 3600; X["sh"] = X.stop * X.bpd / 3600
X.to_parquet(HERE / "X_bap.parquet"); tot = X.dh.sum(); cov = X[X.bap > 0]
print(f"network: {X.mi.sum():.0f} directional miles, {tot:.0f} bus-hours of delay/day. BAP streets: {cov.mi.sum():.0f} mi ({100*cov.mi.sum()/X.mi.sum():.0f}%), {cov.dh.sum():.0f} h ({100*cov.dh.sum()/tot:.0f}%)")
g = X.groupby(["borough", "nm"]).agg(mi=("mi", "sum"), dh=("dh", "sum"), oh=("oh", "sum"), lh=("lh", "sum"), nh=("nh", "sum"), sh=("sh", "sum"), bap=("bap", "max"), bpd=("bpd", "median"), tt=("total", lambda s: 0)).reset_index()
t = X.assign(tw=X.total * X.n, mw=X.mi * X.n).groupby(["borough", "nm"])[["tw", "mw"]].sum(); g["mph"] = (t.mw / (t.tw / 3600)).values; g["dens"] = g.dh / g.mi; g = g[(g.mi >= 1.5) & (g.nm != "")]
g["cum"] = g.sort_values("dh", ascending=False).dh.cumsum() / tot; g.to_parquet(HERE / "streets.parquet")
top = g.sort_values("dh", ascending=False).head(60); print("top 60 streets by delay hours: BAP-covered", int((top.bap > 0).sum()), "; share of all delay in top 60:", round(top.dh.sum() / tot, 2))
u = g[g.bap == 0].sort_values("dh", ascending=False).head(45); print(u[["borough", "nm", "mi", "bpd", "mph", "dh", "dens", "oh", "nh", "lh", "sh"]].round(1).to_string())
print(g[g.bap > 0].sort_values("dh", ascending=False).head(25)[["borough", "nm", "mi", "bpd", "mph", "dh", "dens"]].round(1).to_string())
