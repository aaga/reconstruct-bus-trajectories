"""42nd Street, M42: time per bus by cause in the closure hours (noon to 8pm), ordinary days against the seven match days.
The 10 ft delay curves exist per day, not per hour. So: the total for the hours comes from segment travel times; each cause's whole-day delay is
scaled by how much slow-moving time (located <5 mph events) fell in that cause's zones in those hours against the whole day; other traffic is the remainder."""
import glob; from eng import *; from street import ordered
duckdb.execute("set enable_progress_bar=false"); S, SH = reg(); B = str(REPO / "outputs/network/nyc"); F4 = json.load(open(HERE / "fig4.json")); OUT = {}
DA = json.load(open(REPO / "outputs/network/nyc/date_attrs.json"))["days"]; HOL = {"2026-05-25", "2026-06-19", "2026-07-03"}
MATCH = {"weekday": ["2026-06-16", "2026-06-25", "2026-06-30"], "sat": ["2026-06-13", "2026-06-27"], "sun": ["2026-07-05", "2026-07-19"]}; allm = {x for v in MATCH.values() for x in v} | {"2026-06-22"}
BASE = {k: [d for d, v in DA.items() if v["daytype"] == k and "2026-05-18" <= d <= "2026-07-31" and d not in allm and d not in HOL and d not in ("2026-06-18", "2026-05-23", "2026-05-24")] for k in MATCH}
W = {k: len(MATCH[k]) / 7 for k in MATCH}; HRS = list(range(12, 20))
def files(kind, ds): return [f for d in ds for f in glob.glob(f"{B}/{kind}/service_date={d}/route=M42.parquet")]
for d in ("dir0", "dir1"):
    T = F4["m42_" + d]; seq = ordered("M42", d, "42nd Street"); xs = np.array(T["xs"]); ends = dict(zip(seq, T["ends"])); own = np.full(len(xs), "", dtype=object)
    for z in T["zones"]: own[z["ia"]:z["ib"] + 1] = z["cat"]
    ff = sum(S[s]["tff"] for s in seq); res = {}
    for tag, groups in (("ordinary", BASE), ("match", MATCH)):
        acc = {w: dict(t=0.0, ev={"stop": 0.0, "nearside": 0.0, "signal": 0.0, "": 0.0}) for w in ("day", "hours")}; nb = {}
        for k, ds in groups.items():
            tr = duckdb.sql(f"select seg_id, hour_local h, t_obs_s t from read_parquet({files('traversals', ds)!r}) where flags = 0 and seg_id in {tuple(seq)!r}").df()
            ev = duckdb.sql(f"select seg_id, hour_local h, (off_down_m + off_start_m) / 2 as offm, dur_s from read_parquet({files('events', ds)!r}) where seg_id in {tuple(seq)!r}").df()
            ev["x"] = ev.seg_id.map(ends) - ev.offm * FT; ev["cat"] = own[np.clip(np.searchsorted(xs, ev.x.values), 0, len(xs) - 1)]
            for w, m_t, m_e in (("day", np.ones(len(tr), bool), np.ones(len(ev), bool)), ("hours", tr.h.isin(HRS).values, ev.h.isin(HRS).values)):
                n = tr[m_t].groupby("seg_id").size(); acc[w]["t"] += W[k] * float(tr[m_t].groupby("seg_id").t.mean().reindex(seq).sum())
                e = ev[m_e].groupby(["seg_id", "cat"]).dur_s.sum().reset_index(); e["per"] = e.dur_s / e.seg_id.map(n)
                for c, v in e.groupby("cat").per.sum().items(): acc[w]["ev"][c] += W[k] * float(v)
                nb[(w, k)] = float(n.median())
        whole = T["before" if tag == "ordinary" else "after"]["inside"]; o = {}
        for w in ("day", "hours"):
            c = {k: whole[k] * (acc[w]["ev"][k] / acc["day"]["ev"][k]) for k in ("stop", "nearside", "signal")}; delay = acc[w]["t"] - ff; c["other"] = delay - sum(c.values()); o[w] = dict(total=acc[w]["t"], ff=ff, **c, events=acc[w]["ev"])
        o["chain_delay_day"] = T["before" if tag == "ordinary" else "after"]["inside_total"]; o["n"] = {f"{w}_{k}": v for (w, k), v in nb.items()}; res[tag] = o
        print(d, T["bearing"], tag, {w: {k: round(v) for k, v in o[w].items() if k != "events"} for w in ("day", "hours")}, "| chain delay whole day", round(o["chain_delay_day"]), "| slow-event s", {w: {k or "none": round(v) for k, v in o[w]["events"].items()} for w in ("day", "hours")})
    OUT[d] = dict(bearing=T["bearing"], miles=T["miles"], **res)
json.dump(OUT, open(HERE / "fig5_m42hours.json", "w"))
