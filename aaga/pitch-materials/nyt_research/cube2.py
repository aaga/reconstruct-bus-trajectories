"""Hour-of-day cubes. ev_hour: located slow/stopped events (< 5 mph) by segment, 5 m position bin, hour, weekday flag. trav_hour: bus passes and travel time by segment, hour, weekday flag."""
import duckdb, glob, json, os
B = str(next(p for p in __import__("pathlib").Path(__file__).resolve().parents if (p / ".git").exists()) / "outputs/network/nyc") + "/"; OUT = str(__import__("pathlib").Path(__file__).resolve().parent / "cube") + "/"
con = duckdb.connect(); con.execute("set threads=8; set memory_limit='16GB'; set preserve_insertion_order=false; set enable_progress_bar=false")
DA = json.load(open(B + "date_attrs.json"))["days"]; import pandas as pd
con.register("dd", pd.DataFrame([dict(sd=k, wd=v["daytype"] == "weekday") for k, v in DA.items()]))
con.execute(f"""copy (select seg_id, hour_local h, wd, (floor(off_down_m/5)*5)::int off5, count(*) n, sum(dur_s)::float dur
  from read_parquet('{B}events/*/*.parquet', hive_partitioning=true, union_by_name=true) e join dd on cast(e.service_date as varchar)=dd.sd group by all) to '{OUT}ev_hour.parquet' (compression zstd)""")
print("ev done", flush=True)
con.execute(f"""copy (select seg_id, hour_local h, wd, count(*) n, sum(t_obs_s)::float t, sum(t_obs_s*t_obs_s)::double t2
  from read_parquet('{B}traversals/*/*.parquet', hive_partitioning=true, union_by_name=true) e join dd on cast(e.service_date as varchar)=dd.sd where flags=0 group by all) to '{OUT}trav_hour.parquet' (compression zstd)""")
print("trav done", flush=True)
