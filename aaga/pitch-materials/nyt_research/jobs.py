import sys, period, json; from eng import *
if __name__ == "__main__":
    DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; HOL = {"2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-10-12", "2026-11-03", "2026-11-11"}
    wd = [d for d, v in DA.items() if v["daytype"] == "weekday" and d not in HOL]
    if sys.argv[1] == "win":
        W = {"p1_apr27_may15": ("2026-04-27", "2026-05-15"), "p2_may18_jun26": ("2026-05-18", "2026-06-26"), "p3_jul06_aug28": ("2026-07-06", "2026-08-28"), "p4_sep28_nov20": ("2026-09-28", "2026-11-20"), "unga_sep21_25": ("2026-09-21", "2026-09-25"), "sep14_18": ("2026-09-14", "2026-09-18")}
        period.run([(k, [d for d in wd if a <= d <= b]) for k, (a, b) in W.items()], "win", 6)
    if sys.argv[1] == "day": period.run([(d, [d]) for d in sorted(DA)], "day", 7)
