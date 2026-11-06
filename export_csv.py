#!/usr/bin/env python3
"""Export jobs to CSV.

  py export_csv.py              all jobs  -> jobs_export.csv
  py export_csv.py --interested only jobs you marked Interested, with full descriptions -> interested_export.csv
                                (upload this file to Claude for a batch review)
"""
import csv
import sqlite3
import sys
from pathlib import Path

base = Path(__file__).parent
conn = sqlite3.connect(base / "jobs.db")
have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}

if "--interested" in sys.argv:
    cols = [c for c in ["score", "fast_lane", "company", "title", "location", "loc_class", "posted_at", "first_seen",
                        "flags", "reasons", "url", "description"] if c in have]
    rows = conn.execute(f"SELECT {','.join(cols)} FROM jobs WHERE status='interested' ORDER BY score DESC").fetchall()
    out = "interested_export.csv"
    di = cols.index("description")
    rows = [tuple((v[:8000] if i == di and isinstance(v, str) else v) for i, v in enumerate(r)) for r in rows]
else:
    cols = [c for c in ["score", "fast_lane", "company", "title", "location", "loc_class", "posted_at", "first_seen", "active",
                        "alerted", "status", "flags", "reasons", "url", "notes"] if c in have]
    rows = conn.execute(f"SELECT {','.join(cols)} FROM jobs ORDER BY first_seen DESC, score DESC").fetchall()
    out = "jobs_export.csv"

with open(base / out, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(cols)
    w.writerows(rows)
print(f"Wrote {len(rows)} rows to {out}")
