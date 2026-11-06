#!/usr/bin/env python3
"""Find which ATS (and board token) a company uses.

  py discover.py seed_names.txt             # report only
  py discover.py seed_names.txt --write     # append confirmed matches to companies.csv
  py discover.py "Glean" "Hugging Face"     # names directly
Only matches with at least one open job are written automatically; companies with a
board but zero openings are listed as 'maybe' (add manually if you care)."""
import argparse
import csv
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ats
from watchlist import norm, read_blocklist

BASE = Path(__file__).parent
SUFFIX = re.compile(r"\b(inc|llc|ltd|gmbh|labs?|hq|ai|technologies|technology|group|company)\b\.?", re.I)


def slugs(name):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    core = re.sub(r"\s+", " ", SUFFIX.sub("", base)).strip()
    cands = [base.replace(" ", ""), base.replace(" ", "-"), core.replace(" ", ""), core.replace(" ", "-"),
             base.replace(" ", "") + "ai", base.replace(" ", "") + "hq"]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def probe(args):
    name, ats_name, token = args
    try:
        jobs = ats.FETCHERS[ats_name](token)
        return name, ats_name, token, len(jobs)
    except Exception:
        return name, ats_name, token, None


def existing():
    p = BASE / "companies.csv"
    if not p.exists():
        return set()
    with open(p, newline="", encoding="utf-8-sig") as f:
        return {(r["ats"], r["token"]) for r in csv.DictReader(f) if r.get("ats")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    names = []
    for i in a.inputs:
        p = Path(i)
        if p.is_file():
            names += [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
                      if l.strip() and not l.startswith("#")]
        else:
            names.append(i)
    blocked = read_blocklist()
    names = [n for n in names if norm(n) not in blocked]
    tasks = [(n, an, s) for n in names for s in slugs(n) for an in ats.FETCHERS]
    found = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for name, an, tok, cnt in ex.map(probe, tasks):
            if cnt is not None:
                found.setdefault(name, []).append((an, tok, cnt))
    have = existing()
    rows = []
    for n in names:
        hits = found.get(n, [])
        good = [h for h in hits if h[2] > 0]
        if good:
            best = max(good, key=lambda h: h[2])
            print(f"OK     {n:<26} {best[0]}/{best[1]}  ({best[2]} jobs)"
                  + (f"  [also: {[(h[0], h[1]) for h in good if h is not best]}]" if len(good) > 1 else ""))
            rows.append((n, best[0], best[1]))
        elif hits:
            print(f"MAYBE  {n:<26} board exists but 0 openings: {[(h[0], h[1]) for h in hits]}")
        else:
            print(f"NONE   {n:<26} not found on supported ATSs (find its careers page manually)")
    if a.write and rows:
        p = BASE / "companies.csv"
        new = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["name", "ats", "token"])
            added = 0
            for n, an, tok in rows:
                if (an, tok) not in have:
                    w.writerow([n, an, tok])
                    added += 1
        print(f"\nAppended {added} companies to companies.csv")


if __name__ == "__main__":
    main()
