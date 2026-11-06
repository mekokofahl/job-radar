#!/usr/bin/env python3
"""Watchlist management: add/remove companies by name, URL, or Telegram message.

Telegram (send to your bot):
  +Glean                                add (job board auto-detected)
  +Glean https://glean.com/careers      add, detecting the ATS from the careers page
  +Glean greenhouse:gleanwork           add with an explicit ATS and board token
  -Mercury                              remove from the watchlist AND block it from coming back
  /list                                 show the current watchlist
Same syntax works as lines in Company_Additions.txt (processed every cycle; last line per company wins).

Command line:
  py watchlist.py add "Glean" [url]     py watchlist.py remove "Mercury"
  py watchlist.py list                  py watchlist.py apply
  py watchlist.py import research.csv   (columns: name,url,stage,last_round,employees,sector,notes)
  py watchlist.py unresolved            (writes Unresolved_All.csv: every researched company not yet on the watchlist)
  py watchlist.py atsreport [file]      (reads the url column of Unresolved_All.csv and counts which job boards those companies use)
"""
import csv
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import ats
import notify

BASE = Path(__file__).parent
CSV_PATH = BASE / "companies.csv"
ADD_PATH = BASE / "Company_Additions.txt"
BLOCK_PATH = BASE / "company_blocklist.txt"
FIELDS = ["name", "ats", "token", "stage", "last_round", "employees", "sector", "notes"]
LOCK = threading.RLock()
log = logging.getLogger("watchlist")
_last_fail = {}
_reported = set()   # failure messages already shown this session
_checked = set()    # (key, url, hint) re-resolutions already done this session
RETRY_SECONDS = 6 * 3600

SUFFIX = re.compile(r"\b(inc|llc|ltd|gmbh|labs?|hq|ai|technologies|technology|group|company)\b\.?", re.I)
PATTERNS = [
    (r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", "greenhouse"),
    (r"jobs\.eu\.lever\.co/([A-Za-z0-9_.-]+)", "lever_eu"),
    (r"jobs\.lever\.co/([A-Za-z0-9_.-]+)", "lever"),
    (r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", "ashby"),
    (r"apply\.workable\.com/([A-Za-z0-9_-]+)", "workable"),
    (r"([A-Za-z0-9-]+)\.recruitee\.com", "recruitee"),
    # forms that appear inside a site's JavaScript when the job list is loaded dynamically
    (r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", "greenhouse"),
    (r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)", "greenhouse"),
    (r"api\.eu\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)", "lever_eu"),
    (r"api\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)", "lever"),
    (r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)", "ashby"),
    (r"organizationHostedJobsPageName[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_.%-]+)[\"']", "ashby"),
    (r"apply\.workable\.com/api/v\d/accounts/([A-Za-z0-9_-]+)", "workable"),
]
LOCALE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$", re.I)          # en, en-GB, de ... are page languages, not company names
SKIP_SCRIPT_HOSTS = re.compile(r"(google|gstatic|googletagmanager|facebook|fbcdn|segment|hotjar|intercom|hubspot|hs-scripts|hs-analytics|"
                               r"clarity|cookiebot|onetrust|sentry|datadog|cloudflareinsights|doubleclick|linkedin|twitter|tiktok|"
                               r"stripe|typekit|fontawesome|jquery|cdnjs|unpkg|jsdelivr|youtube|vimeo|calendly|drift|hotjar|optimizely)", re.I)


def _script_texts(html, base, limit=12):
    """Fetch a page's own JavaScript files (job boards are often referenced there, not in the HTML)."""
    from urllib.parse import urljoin, urlparse
    srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html or "", re.I)
    ranked = sorted(dict.fromkeys(urljoin(base, s) for s in srcs),
                    key=lambda u: (0 if re.search(r"career|job|opening|position|pages|app|main", u, re.I) else 1))
    out = []
    for u in ranked:
        if not u.startswith("http") or SKIP_SCRIPT_HOSTS.search(urlparse(u).netloc):
            continue
        try:
            out.append(requests.get(u, headers=ats.UA, timeout=15).text[:1500000])
        except Exception:  # noqa
            pass
        if len(out) >= limit:
            break
    return out
IGNORE_TOKENS = {"embed", "job_board", "api", "www", "v1", "widget", "accounts"}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


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


# ---------- storage ----------
def read_companies(include_blocked=False):
    with LOCK:
        if not CSV_PATH.exists():
            return []
        rows = []
        with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if not r.get("name") or r["name"].startswith("#"):
                    continue
                rows.append({k: (r.get(k) or "").strip() for k in FIELDS})
    if include_blocked:
        return rows
    blocked = read_blocklist()
    return [r for r in rows if norm(r["name"]) not in blocked]


def write_companies(rows):
    with LOCK:
        tmp = CSV_PATH.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in FIELDS})
        os.replace(tmp, CSV_PATH)


def read_blocklist():
    if not BLOCK_PATH.exists():
        return set()
    return {norm(l) for l in BLOCK_PATH.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")}


def _block_lines():
    if not BLOCK_PATH.exists():
        return []
    return [l.rstrip() for l in BLOCK_PATH.read_text(encoding="utf-8").splitlines()]


def add_block(name):
    with LOCK:
        if norm(name) not in read_blocklist():
            with open(BLOCK_PATH, "a", encoding="utf-8") as f:
                f.write(name + "\n")


def remove_block(name):
    with LOCK:
        lines = [l for l in _block_lines() if norm(l) != norm(name) or l.startswith("#")]
        BLOCK_PATH.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# ---------- ATS detection ----------
def verify(ats_name, token):
    try:
        return len(ats.FETCHERS[ats_name](token))
    except Exception:
        return None


def _match(texts):
    for t in texts:
        for pat, name in PATTERNS:
            for m in re.finditer(pat, t, re.I):
                tok = m.group(1)
                if tok.lower() not in IGNORE_TOKENS and not LOCALE.match(tok):
                    return name, tok
    return None


def detect_from_url(url):
    texts, html, final = [url], "", url
    try:
        r = requests.get(url, headers=ats.UA, timeout=20)
        final, html = r.url, r.text[:600000]
        texts += [final, html]
    except Exception:
        pass
    hit = _match(texts)
    if hit or not html:
        return hit
    return _match(_script_texts(html, final))      # second pass: the page's JavaScript


def resolve(name, url=None, hint=None):
    """Return (ats, token, job_count) or None."""
    if hint:
        c = verify(*hint)
        return (hint[0], hint[1], c) if c is not None else None
    if url:
        found = detect_from_url(url)
        if found:
            c = verify(*found)
            if c is not None:
                return found[0], found[1], c
    cands = [(a, s) for s in slugs(name) for a in ats.FETCHERS]
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda x: (x[0], x[1], verify(*x)), cands))
    good = [r for r in results if r[2]]
    return max(good, key=lambda r: r[2]) if good else None


# ---------- commands / additions file ----------
def parse_line(line):
    m = re.match(r"^([+-])\s*(.+?)\s*$", (line or "").strip())
    if not m:
        return None
    op, rest = m.groups()
    url = hint = None
    um = re.search(r"https?://\S+", rest)
    if um:
        url = um.group(0)
        rest = rest.replace(url, " ").strip()
    hm = re.search(r"\b(" + "|".join(ats.FETCHERS) + r")\s*[:/]\s*([A-Za-z0-9_.-]+)\s*$", rest, re.I)
    if hm:
        hint = (hm.group(1).lower(), hm.group(2))
        rest = rest[:hm.start()].strip()
    rest = rest.strip(" |,")
    if not rest and url:
        found = detect_from_url(url)
        rest = found[1].replace("-", " ").title() if found else ""
    return (op, rest, url, hint) if rest else None


def _parse_additions():
    ops = {}
    if not ADD_PATH.exists():
        return ops
    for line in ADD_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        p = parse_line(line)
        if p:
            op, name, url, hint = p
            ops.pop(norm(name), None)  # last line wins, keep file order of final ops
            ops[norm(name)] = {"op": op, "name": name, "url": url, "hint": hint}
    return ops


def pending_additions():
    """'+' lines in Company_Additions.txt that are not (yet) on the watchlist, e.g. no job board found."""
    ops, rows, blocked = _parse_additions(), read_companies(include_blocked=True), read_blocklist()
    out = []
    for key, o in ops.items():
        if o["op"] != "+" or key in blocked:
            continue
        if any(norm(r["name"]) == key or norm(r["token"]) == key for r in rows):
            continue
        out.append(o)
    return out


def apply_changes(only_key=None):
    """Apply Company_Additions.txt to companies.csv / blocklist. Returns human-readable messages."""
    msgs = []
    with LOCK:
        ops = _parse_additions()
        rows = read_companies(include_blocked=True)
        changed = False
        for key, o in ops.items():
            if only_key and key != only_key:
                continue
            existing = [r for r in rows if norm(r["name"]) == key or norm(r["token"]) == key]
            if o["op"] == "-":
                if existing or key not in read_blocklist():
                    rows = [r for r in rows if r not in existing]
                    add_block(o["name"])
                    changed = changed or bool(existing)
                    msgs.append(f"Removed {o['name']} from the watchlist and blocked it from coming back.")
                continue
            was_blocked = key in read_blocklist()
            if was_blocked:
                remove_block(o["name"])
            if existing:
                sig = (key, o["url"], o["hint"])
                if (o["url"] or o["hint"]) and (only_key or sig not in _checked):
                    _checked.add(sig)
                    res = resolve(o["name"], o["url"], o["hint"])
                    cur = existing[0]
                    if res and (res[0], res[1]) != (cur["ats"], cur["token"]):
                        cur["ats"], cur["token"] = res[0], res[1]
                        changed = True
                        msgs.append(f"Updated {cur['name']} -> {res[0]}/{res[1]} ({res[2]} open jobs).")
                    elif res:
                        if only_key:
                            msgs.append(f"{cur['name']} already uses {res[0]}/{res[1]}; no change.")
                    else:
                        msgs.append(f"Could not read a job board from the details given for {o['name']}; "
                                    f"left it unchanged.")
                    continue
                if was_blocked:
                    msgs.append(f"{o['name']} unblocked (already on the watchlist).")
                elif only_key:
                    msgs.append(f"{o['name']} is already on the watchlist.")
                continue
            if not only_key and time.time() - _last_fail.get(key, 0) < RETRY_SECONDS:
                continue
            res = resolve(o["name"], o["url"], o["hint"])
            if res:
                rows.append({"name": o["name"], "ats": res[0], "token": res[1], "stage": "", "last_round": "",
                             "employees": "", "sector": "", "notes": "added by +command"})
                changed = True
                _last_fail.pop(key, None)
                msgs.append(f"Added {o['name']} -> {res[0]}/{res[1]} ({res[2]} open jobs).")
            else:
                _last_fail[key] = time.time()
                if only_key or key not in _reported:
                    _reported.add(key)
                    msgs.append(f"Could not find a job board for {o['name']}. Reply with its careers page, e.g. "
                                f"+{o['name']} https://their-site.com/careers (or +{o['name']} greenhouse:token). "
                                f"I'll retry automatically every few hours.")
        if changed:
            write_companies(rows)
    return msgs


def append_addition(line):
    with LOCK:
        if not ADD_PATH.exists():
            ADD_PATH.write_text("", encoding="utf-8")
        with open(ADD_PATH, "a", encoding="utf-8") as f:
            f.write(line.strip() + "\n")


def import_research(path):
    """Import a research CSV (name,url,stage,last_round,employees,sector,notes) into the watchlist."""
    added = updated = skipped = 0
    unresolved = []
    blocked = read_blocklist()
    with open(path, newline="", encoding="utf-8-sig") as f:
        inputs = list(csv.DictReader(f))
    with LOCK:
        rows = read_companies(include_blocked=True)
    for r in inputs:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        if norm(name) in blocked:
            skipped += 1
            continue
        meta = {k: (r.get(k) or "").strip() for k in ("stage", "last_round", "employees", "sector", "notes")}
        ex = next((x for x in rows if norm(x["name"]) == norm(name)), None)
        if ex:
            for k, v in meta.items():
                if v and not ex.get(k):
                    ex[k] = v
            updated += 1
            continue
        res = resolve(name, (r.get("url") or "").strip() or None)
        if res:
            rows.append({"name": name, "ats": res[0], "token": res[1], **meta})
            added += 1
            print(f"OK    {name:<28} {res[0]}/{res[1]} ({res[2]} jobs)")
        else:
            unresolved.append(name)
            print(f"NONE  {name:<28} not found; add manually with +{name} <careers url>")
    write_companies(rows)
    print(f"\nAdded {added}, updated metadata on {updated}, skipped (blocked) {skipped}, unresolved {len(unresolved)}")
    if unresolved:
        have = ADD_PATH.read_text(encoding="utf-8").lower() if ADD_PATH.exists() else ""
        for n in unresolved:
            if f"+{n}".lower() not in have:
                append_addition(f"+{n}")
        print("Unresolved names were added to Company_Additions.txt and will be retried automatically; "
              "they also appear in Needs_Attention.csv.")
        up = BASE / "unresolved_companies.txt"
        prev = set(up.read_text(encoding="utf-8").splitlines()) if up.exists() else set()
        merged = sorted(prev | set(unresolved), key=str.lower)
        up.write_text("\n".join(merged) + "\n", encoding="utf-8")
        print("Unresolved names added to unresolved_companies.txt (it now keeps names from every import)")


def unresolved_all():
    """Rebuild the full to-do list: every company in any research_batch*.csv that is not on the watchlist."""
    rows = read_companies(include_blocked=True)
    have = {norm(r["name"]) for r in rows} | {norm(r["token"]) for r in rows}
    blocked = read_blocklist()
    out = {}
    for f in sorted(BASE.glob("research_batch*.csv")):
        with open(f, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                n = (r.get("name") or "").strip()
                if not n or norm(n) in have or norm(n) in blocked or norm(n) in out:
                    continue
                out[norm(n)] = {"name": n, "url": (r.get("url") or "").strip(), "stage": r.get("stage", ""),
                                "last_round": r.get("last_round", ""), "employees": r.get("employees", ""),
                                "sector": r.get("sector", ""), "notes": (r.get("notes") or "")[:240],
                                "source_file": f.name}
    for o in pending_additions():
        out.setdefault(norm(o["name"]), {"name": o["name"], "url": o["url"] or "", "stage": "", "last_round": "",
                                         "employees": "", "sector": "", "notes": "", "source_file": "Company_Additions.txt"})
    path = BASE / "Unresolved_All.csv"
    cols = ["name", "url", "stage", "last_round", "employees", "sector", "notes", "source_file"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        for r in sorted(out.values(), key=lambda x: x["name"].lower()):
            wr.writerow(r)
    print(f"{len(out)} companies are not on the watchlist yet. Wrote Unresolved_All.csv")
    print("Fill in the 'url' column (the company's careers page), save, then run:  py watchlist.py import Unresolved_All.csv")


# ---------- Which job board does a careers page use? ----------
# Platforms we can already read are marked supported; the others are counted so you can decide what to build next.
PLATFORMS = [
    ("greenhouse", True,  r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    ("lever",      True,  r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)"),
    ("ashby",      True,  r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("workable",   True,  r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("recruitee",  True,  r"([A-Za-z0-9-]+)\.recruitee\.com"),
    ("greenhouse", True,  r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("lever",      True,  r"api\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)"),
    ("ashby",      True,  r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)"),
    ("ashby",      True,  r"organizationHostedJobsPageName[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_.%-]+)[\"']"),
    ("workable",   True,  r"apply\.workable\.com/api/v\d/accounts/([A-Za-z0-9_-]+)"),
    ("zohorecruit", False, r"([A-Za-z0-9-]+)\.zohorecruit\.(?:com|eu)"),
    ("screenloop", False, r"screenloop\.com/careers/([A-Za-z0-9_-]+)"),
    ("deel-jobs",  False, r"jobs\.deel\.com/([A-Za-z0-9_-]+)"),
    ("phenom",     False, r"([A-Za-z0-9-]+)\.phenompeople\.com"),
    ("avature",    False, r"([A-Za-z0-9-]+)\.avature\.net"),
    ("successfactors", False, r"([A-Za-z0-9-]+)\.successfactors\.(?:com|eu)"),
    ("oracle-hcm", False, r"([A-Za-z0-9-]+)\.fa\.[a-z0-9-]+\.oraclecloud\.com"),
    ("jobvite",    False, r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)"),
    ("eightfold",  False, r"([A-Za-z0-9-]+)\.eightfold\.ai"),
    ("personio",   False, r"([A-Za-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
    ("smartrecruiters", False, r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("teamtailor", False, r"([A-Za-z0-9-]+)\.teamtailor\.com"),
    ("bamboohr",   False, r"([A-Za-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)"),
    ("workday",    False, r"([A-Za-z0-9-]+)\.wd\d+\.myworkdayjobs\.com"),
    ("breezy",     False, r"([A-Za-z0-9-]+)\.breezy\.hr"),
    ("pinpoint",   False, r"([A-Za-z0-9-]+)\.pinpointhq\.com"),
    ("rippling",   False, r"ats\.rippling\.com/(?:[a-z]{2}-[A-Za-z]{2}/)?([A-Za-z0-9_-]+)"),
    ("gem",        False, r"jobs\.gem\.com/([A-Za-z0-9_-]+)"),
    ("join",       False, r"join\.com/companies/([A-Za-z0-9_-]+)"),
    ("homerun",    False, r"([A-Za-z0-9-]+)\.homerun\.co"),
    ("jazzhr",     False, r"([A-Za-z0-9-]+)\.applytojob\.com"),
    ("icims",      False, r"([A-Za-z0-9-]+)\.icims\.com"),
    ("comeet",     False, r"comeet\.com/jobs/([A-Za-z0-9_-]+)"),
    ("workforcenow", False, r"workforcenow\.adp\.com"),
]


def _platform_match(texts):
    for name, supported, pat in PLATFORMS:
        for t in texts:
            m = re.search(pat, t, re.I)
            if m:
                tok = m.group(1) if m.groups() else ""
                if tok.lower() in IGNORE_TOKENS or LOCALE.match(tok or "xx-"):
                    continue
                return (name, tok, supported)
    return None


def detect_platform(url):
    """Return (platform, token, supported) for a careers URL, reading the page and, if needed, its JavaScript."""
    texts, html, final = [url], "", url
    try:
        r = requests.get(url, headers=ats.UA, timeout=20)
        final, html = r.url, r.text[:800000]
        texts += [final, html]
    except Exception as e:  # noqa
        return ("unreachable", type(e).__name__, False)
    hit = _platform_match(texts)
    if not hit and html:
        hit = _platform_match(_script_texts(html, final))
        if hit:
            hit = (hit[0], hit[1], hit[2])
    return hit or ("not detected", "", False)


def ats_report(path=None):
    """Read a CSV with name and url columns (default Unresolved_All.csv), detect each company's job board, and summarize."""
    path = Path(path) if path else BASE / "Unresolved_All.csv"
    if not path.exists():
        print(f"{path.name} not found. Run:  py watchlist.py unresolved   then fill the url column.")
        return
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("url") or "").strip()]
    if not rows:
        print("No URLs found. Fill the 'url' column with each company's careers page, then run this again.")
        return
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r, det in zip(rows, ex.map(lambda r: detect_platform(r["url"].strip()), rows)):
            results.append((r["name"], r["url"].strip(), det))
    from collections import Counter
    cnt = Counter(d[0] for _, _, d in results)
    print(f"\nJob boards used by the {len(results)} companies you gave URLs for:\n")
    for name, n in cnt.most_common():
        sup = next((s for p, s, _ in PLATFORMS if p == name), None)
        tag = "ALREADY READABLE (add with +Company <url>)" if sup else ("(cannot read yet)" if name not in ("not detected", "unreachable") else "")
        print(f"  {n:>3}  {name:<16} {tag}")
    out = BASE / "ats_report.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["company", "careers_url", "platform", "token", "readable_today"])
        for n, u, (p, t, s) in results:
            wr.writerow([n, u, p, t, "yes" if s else "no"])
    print(f"\nDetails saved to {out.name}. The top 'cannot read yet' boards are the ones worth building next.")


# ---------- Telegram command listener ----------
HELP = ("Commands:\n+Company  add\n+Company https://careers-url  add (detect ATS from page)\n"
        "+Company greenhouse:token  add explicitly (or fix a company's board)\n-Company  remove and block\n"
        "/list  show watchlist\n/attention  companies that need your help\n/linkedin  newest-LinkedIn-jobs links\n/recent  latest unreviewed job alerts\n/interested  jobs you marked Interested")


def _reply(cfg, text):
    try:
        notify.send_telegram(cfg, notify.esc(text))
    except Exception as e:  # noqa
        log.error("Reply failed: %s", e)


def handle_command(cfg, text):
    t = (text or "").strip()
    if not t:
        return
    if t.lower() in ("/start", "/help", "help"):
        _reply(cfg, HELP)
    elif t.lower() in ("/list", "/watchlist"):
        names = sorted(r["name"] for r in read_companies())
        out, cur = [], ""
        for n in names:
            if len(cur) + len(n) > 3400:
                out.append(cur)
                cur = ""
            cur += n + ", "
        out.append(cur.rstrip(", "))
        _reply(cfg, f"{len(names)} companies on the watchlist:")
        for c in out:
            _reply(cfg, c)
    elif t.lower() in ("/linkedin", "/li"):
        import linkedin_links
        try:
            notify.send_telegram(cfg, linkedin_links.telegram_message(cfg))
        except Exception as e:  # noqa
            log.error("LinkedIn links failed: %s", e)
    elif t.lower() == "/recent":
        import review
        try:
            notify.send_telegram(cfg, review.recent_text(cfg))
        except Exception as e:  # noqa
            log.error("/recent failed: %s", e)
    elif t.lower() == "/interested":
        import review
        try:
            notify.send_telegram(cfg, review.recent_text(cfg, n=20, status="interested"))
        except Exception as e:  # noqa
            log.error("/interested failed: %s", e)
    elif t.lower() == "/attention":
        path = BASE / "Needs_Attention.csv"
        rows = list(csv.DictReader(open(path, newline="", encoding="utf-8-sig"))) if path.exists() else []
        if not rows:
            _reply(cfg, "Nothing needs attention right now.")
        else:
            out, cur = [], f"{len(rows)} item(s) need attention:\n"
            for r in rows:
                line = f"- {r['company']}: {r['issue']} (since {r['since']})\n"
                if len(cur) + len(line) > 3500:
                    out.append(cur)
                    cur = ""
                cur += line
            out.append(cur + "\nFix with: +Company <careers URL>   or drop with: -Company")
            for c in out:
                _reply(cfg, c)
    elif t[0] in "+-":
        p = parse_line(t)
        if not p:
            _reply(cfg, "Couldn't read that. " + HELP)
            return
        op, name, url, hint = p
        append_addition(t)
        msgs = apply_changes(only_key=norm(name))
        _reply(cfg, "\n".join(msgs) if msgs else f"No change for {name}.")


def listener(get_cfg):
    offset = None
    while True:
        cfg = get_cfg()
        tg = cfg.get("telegram", {})
        tok, chat = tg.get("bot_token"), str(tg.get("chat_id"))
        if not tok or not chat:
            time.sleep(30)
            continue
        try:
            r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates",
                             params={"timeout": 25, **({"offset": offset} if offset else {})}, timeout=40).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != chat:
                    continue
                try:
                    handle_command(cfg, msg.get("text", ""))
                except Exception:
                    log.exception("Command failed")
        except Exception as e:  # noqa
            log.warning("Listener error: %s", e)
            time.sleep(15)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "list":
        for r in sorted(read_companies(), key=lambda r: r["name"].lower()):
            print(f"{r['name']:<28} {r['ats']}/{r['token']}  {r['stage']} {r['last_round']} {r['employees']}")
    elif a[0] in ("add", "remove") and len(a) >= 2:
        append_addition(("+" if a[0] == "add" else "-") + " ".join(a[1:]))
        print("\n".join(apply_changes(only_key=norm(a[1]))) or "No change.")
    elif a[0] == "unresolved":
        unresolved_all()
    elif a[0] == "atsreport":
        ats_report(a[1] if len(a) > 1 else None)
    elif a[0] == "apply":
        print("\n".join(apply_changes()) or "Nothing to apply.")
    elif a[0] == "import" and len(a) == 2:
        import_research(a[1])
    else:
        print(__doc__)
