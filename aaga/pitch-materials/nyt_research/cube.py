"""Daily cube pooled over routes: seg_id x bucket x service_date -> n, sum_dt. One parquet per month. Also the all-days total and a weekday/weekend split."""
import duckdb, glob, sys
B = str(next(p for p in __import__("pathlib").Path(__file__).resolve().parents if (p / ".git").exists()) / "outputs/network/nyc") + "/"; OUT = str(__import__("pathlib").Path(__file__).resolve().parent / "cube") + "/"
import os; os.makedirs(OUT, exist_ok=True)
con = duckdb.connect(); con.execute("set threads=9; set memory_limit='18GB'; set preserve_insertion_order=false")
for m in ("2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"):
    if os.path.exists(OUT + f"day_{m}.parquet"): continue
    fs = glob.glob(B + f"traj_speed/service_date={m}-*/*.parquet")
    con.execute(f"copy (select seg_id, bucket::usmallint bucket, service_date, sum(n)::int n, sum(sum_dt)::float sum_dt from read_parquet({fs!r}) group by all) to '{OUT}day_{m}.parquet' (compression zstd)")
    print(m, len(fs), flush=True)
con.execute(f"copy (select seg_id, bucket, sum(n) n, sum(sum_dt) sum_dt from '{OUT}day_*.parquet' group by all) to '{OUT}all.parquet'")
print("DONE", con.sql(f"select count(*), sum(n) from '{OUT}all.parquet'").fetchone(), flush=True)
