#!/usr/bin/env python3
"""Job poller: checks company ATS feeds, scores new postings, alerts via Telegram.

  py poller.py --test          fetch everything, show top matches + failures; no DB, no Telegram
  py poller.py --once          one polling cycle (writes DB, sends alerts)
  py poller.py --loop          poll forever (use this for normal operation)
  py poller.py --test-telegram send a test message
"""
import argparse
import csv
from collections import Counter
import logging
import re
import random
import sqlite3
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import ats
import linkedin_links
import notify
import threading
import watchlist
from scoring import Scorer, family_key

BASE = Path(__file__).parent
DB_PATH = BASE / "jobs.db"
log = logging.getLogger("poller")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_key TEXT PRIMARY KEY, company TEXT, ats TEXT, title TEXT, location TEXT, workplace TEXT,
  url TEXT, posted_at TEXT, first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1,
  score REAL, loc_class TEXT, reasons TEXT, flags TEXT, description TEXT,
  alerted INTEGER DEFAULT 0, status TEXT DEFAULT 'new', notes TEXT, digested INTEGER DEFAULT 0, fast_lane INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS company_health (
  company TEXT PRIMARY KEY, last_ok TEXT, fail_count INTEGER DEFAULT 0, last_error TEXT,
  last_count INTEGER DEFAULT 0, warned INTEGER DEFAULT 0, last_kind TEXT, zero_since TEXT
);
CREATE TABLE IF NOT EXISTS attention (
  company TEXT, kind TEXT, first_seen TEXT, PRIMARY KEY (company, kind)
);
"""


def load_cfg():
    with open(BASE / "config.toml", "rb") as f:
        return tomllib.load(f)


def load_companies():
    rows = []
    for r in watchlist.read_companies():
        if r["ats"] not in ats.FETCHERS or not r["token"]:
            log.warning("Skipping %s: unknown ATS %r or missing token", r["name"], r["ats"])
            continue
        rows.append(r)
    return rows


def setup_logging():
    h = RotatingFileHandler(BASE / "poller.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[h, sh])


def fetch_company(row):
    try:
        return row, ats.FETCHERS[row["ats"]](row["token"]), None
    except Exception as e:  # noqa
        return row, None, f"{type(e).__name__}: {e}"[:300]


def fetch_all(companies, workers):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_company, r) for r in companies]
        for f in as_completed(futs):
            yield f.result()


def ago(iso):
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        mins = (datetime.now(timezone.utc) - d).total_seconds() / 60
        if mins < 0:
            return "just now"
        if mins < 90:
            return f"{int(mins)} min ago"
        if mins < 48 * 60:
            return f"{mins / 60:.0f} h ago"
        return f"{mins / 1440:.0f} days ago"
    except Exception:
        return None


def collapse(rows):
    """Group near-identical postings (same company, same title across cities/regions)."""
    groups = {}
    for r in rows:
        groups.setdefault(family_key(r["company"], r["title"]), []).append(r)
    return list(groups.values())


def age_hours(iso):
    """Hours since an ISO date/datetime string, or None if unknown."""
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds() / 3600
    except Exception:
        return None


def format_alert(r, extra=None):
    posted = ago(r["posted_at"])
    lines = [
        ("⚡ " if r["fast_lane"] else "") + f"<b>{notify.esc(r['title'])}</b>",
        f"{notify.esc(r['company'])} · {notify.esc(r['location'] or 'location n/a')}",
        f"Score <b>{r['score']:.0f}</b> · {notify.esc(r['loc_class'])}"
        + (f" · posted {notify.esc(posted)}" if posted else " · posting date unknown"),
        f"<i>{notify.esc(r['reasons'])}</i>",
    ]
    if r["flags"]:
        lines.append("⚠ " + notify.esc(r["flags"]))
    if extra:
        lines.append("+" + str(len(extra)) + " similar: " + notify.esc("; ".join((e["location"] or "?") for e in extra[:6])))
    lines.append(f'<a href="{notify.esc(r["url"])}">Open posting</a>')
    return "\n".join(lines)


def is_quiet(cfg):
    q = cfg["general"].get("quiet_hours")
    if not q:
        return False
    h = datetime.now(ZoneInfo(cfg["general"].get("timezone", "UTC"))).hour
    a, b = q
    return (h >= a or h < b) if a > b else (a <= h < b)


ATTENTION_PATH = BASE / "Needs_Attention.csv"
ZERO_DAYS = 14


def sync_attention(conn, companies):
    """Rewrite Needs_Attention.csv: companies whose job board needs your help. Clears itself when fixed."""
    now = datetime.now(timezone.utc)
    names = {c["name"] for c in companies}
    items = []  # (company, kind, issue, ats, token, detail, action)
    health = conn.execute("SELECT company,fail_count,last_error,last_kind,last_count,zero_since "
                          "FROM company_health").fetchall()
    by_name = {c["name"]: c for c in companies}
    for h in health:
        name, fails, err, kind, cnt, zero_since = h[0], h[1], h[2], h[3], h[4], h[5]
        if name not in names:
            continue
        c = by_name[name]
        if fails >= 2 and kind == "hard":
            items.append((name, "not_found", "Job board not found (HTTP error); token may have changed",
                          c["ats"], c["token"], err,
                          f"Open the careers page, copy the address, send the bot:  +{name} <careers URL>   (or -{name} to drop)"))
        elif fails >= 5:
            items.append((name, "errors", f"Failing {fails} cycles in a row (connection errors)",
                          c["ats"], c["token"], err,
                          f"Usually temporary. If still failing tomorrow, send:  +{name} <careers URL>"))
        elif cnt == 0 and zero_since:
            try:
                days = (now - datetime.fromisoformat(zero_since)).days
            except Exception:
                days = 0
            if days >= ZERO_DAYS:
                items.append((name, "zero_jobs", f"No open jobs for {days} days; board may have moved",
                              c["ats"], c["token"], "",
                              f"Check the careers page. If they hire elsewhere send:  +{name} <careers URL>; "
                              f"if simply not hiring, ignore or -{name}"))
    for o in watchlist.pending_additions():
        items.append((o["name"], "no_board", "No job board found yet (retrying automatically)", "", "", "",
                      f"Send the bot:  +{o['name']} <careers URL>   (or +{o['name']} greenhouse:token)"))
    # first_seen bookkeeping: keep original date while an issue stays open; drop rows once fixed
    open_keys = {(i[0], i[1]) for i in items}
    for k in conn.execute("SELECT company,kind FROM attention").fetchall():
        if (k[0], k[1]) not in open_keys:
            conn.execute("DELETE FROM attention WHERE company=? AND kind=?", (k[0], k[1]))
    for company, kind in open_keys:
        conn.execute("INSERT OR IGNORE INTO attention(company,kind,first_seen) VALUES(?,?,?)",
                     (company, kind, now.date().isoformat()))
    since = {(r[0], r[1]): r[2] for r in conn.execute("SELECT company,kind,first_seen FROM attention")}
    rows = sorted(items, key=lambda i: (since.get((i[0], i[1]), ""), i[0].lower()))
    tmp = ATTENTION_PATH.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["company", "issue", "since", "ats", "token", "last_error", "what_to_do"])
        for company, kind, issue, ats_name, token, detail, action in rows:
            w.writerow([company, issue, since.get((company, kind), ""), ats_name, token, (detail or "")[:200], action])
    tmp.replace(ATTENTION_PATH)
    conn.commit()
    return len(rows)


def maybe_send_linkedin_reminder(cfg, conn):
    """Optional: at the hours listed in [linkedin] reminder_hours, send tappable 'past hour' LinkedIn links."""
    hours = cfg.get("linkedin", {}).get("reminder_hours", [])
    if not hours:
        return
    now = datetime.now(ZoneInfo(cfg["general"].get("timezone", "UTC")))
    if now.hour not in hours:
        return
    if cfg.get("linkedin", {}).get("reminder_weekdays_only", True) and now.weekday() >= 5:
        return                                  # no weekend reminders
    stamp = now.strftime("%Y-%m-%d-%H")
    last = conn.execute("SELECT value FROM meta WHERE key='last_li'").fetchone()
    if last and last[0] == stamp:
        return
    try:
        notify.send_telegram(cfg, linkedin_links.telegram_message(cfg), silent=True)
        conn.execute("INSERT INTO meta(key,value) VALUES('last_li',?) ON CONFLICT(key) DO UPDATE SET value=?", (stamp, stamp))
        conn.commit()
    except Exception as e:  # noqa
        log.error("LinkedIn reminder failed: %s", e)


def maybe_send_digest(cfg, conn, force=False):
    """Once a day (after digest.hour local time) send one message with near-miss roles."""
    d = cfg.get("digest", {})
    if not force and not d.get("enabled", True):
        return
    now = datetime.now(ZoneInfo(cfg["general"].get("timezone", "UTC")))
    today = now.date().isoformat()
    if not force:
        if now.hour < d.get("hour", 8):
            return
        last = conn.execute("SELECT value FROM meta WHERE key='last_digest'").fetchone()
        if last and last[0] == today:
            return
    lo, hi = d.get("min_score", 9), cfg["general"]["alert_min_score"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=d.get("max_age_days", 7))).isoformat()
    conn.execute("UPDATE jobs SET digested=1 WHERE digested=0 AND first_seen<?", (cutoff,))
    rows = conn.execute("SELECT * FROM jobs WHERE active=1 AND digested=0 AND score>=? AND score<? "
                        "ORDER BY score DESC, first_seen DESC", (lo, hi)).fetchall()
    groups = collapse(rows)
    shown = groups[: d.get("max_items", 15)]
    if shown:
        lines = [f"<b>Near-miss roles</b> - {len(shown)} of {len(groups)} waiting (scores {lo:.0f}-{hi - 1:.0f})"]
        for i, g in enumerate(shown, 1):
            r = g[0]
            more = f" (+{len(g) - 1} similar)" if len(g) > 1 else ""
            flag = (r["flags"] or "").split("; ")[0]
            line = (f'{i}. <a href="{notify.esc(r["url"])}">{notify.esc(r["title"])}</a> · {notify.esc(r["company"])}'
                    f' · {notify.esc((r["location"] or "?")[:40])} · {r["score"]:.0f}{more}')
            if flag:
                line += f"\n    ⚠ {notify.esc(flag)}"
            lines.append(line)
        try:
            n_a = sum(1 for _ in open(ATTENTION_PATH, encoding="utf-8-sig")) - 1
            if n_a > 0:
                lines.append(f"\nNeeds attention: {n_a} compan{'y' if n_a == 1 else 'ies'} (send /attention)")
        except OSError:
            pass
        chunks, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) > 3500:
                chunks.append(cur)
                cur = ""
            cur += ln + "\n"
        chunks.append(cur)
        try:
            for ch in chunks:
                notify.send_telegram(cfg, ch.strip(), silent=is_quiet(cfg))
                time.sleep(0.6)
        except Exception as e:  # noqa
            log.error("Digest send failed: %s", e)
            conn.commit()
            return
        for g in shown:
            for r in g:
                conn.execute("UPDATE jobs SET digested=1 WHERE job_key=?", (r["job_key"],))
        log.info("Digest sent: %d role groups", len(shown))
    else:
        log.info("Digest: nothing waiting")
    conn.execute("INSERT INTO meta(key,value) VALUES('last_digest',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=?", (today, today))
    conn.commit()


def run_once(cfg, send=True):
    companies = load_companies()
    scorer = Scorer(cfg)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    for alter in ("ALTER TABLE jobs ADD COLUMN digested INTEGER DEFAULT 0",       # upgrade older DBs
                  "ALTER TABLE jobs ADD COLUMN fast_lane INTEGER DEFAULT 0",
                  "ALTER TABLE company_health ADD COLUMN last_kind TEXT",
                  "ALTER TABLE company_health ADD COLUMN zero_since TEXT"):
        try:
            conn.execute(alter)
        except sqlite3.OperationalError:
            pass
    now = datetime.now(timezone.utc).isoformat()
    first_run = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    n_new = n_fail = n_alert = n_fast = 0
    funnel = Counter()
    for row, jobs, err in fetch_all(companies, cfg["general"]["workers"]):
        name = row["name"]
        if err:
            n_fail += 1
            kind = "hard" if re.search(r"\b(401|403|404|410)\b", err) else "transient"
            conn.execute("INSERT INTO company_health(company,fail_count,last_error,last_kind) VALUES(?,1,?,?) "
                         "ON CONFLICT(company) DO UPDATE SET fail_count=fail_count+1,last_error=?,last_kind=?",
                         (name, err, kind, err, kind))
            log.warning("FAIL %s (%s/%s): %s", name, row["ats"], row["token"], err)
            continue
        seen_before = conn.execute("SELECT 1 FROM company_health WHERE company=? AND last_ok IS NOT NULL",
                                   (name,)).fetchone() is not None
        zs = now if not jobs else None
        conn.execute("INSERT INTO company_health(company,last_ok,fail_count,last_count,warned,last_kind,zero_since) "
                     "VALUES(?,?,0,?,0,NULL,?) ON CONFLICT(company) DO UPDATE SET last_ok=?,fail_count=0,"
                     "last_count=?,warned=0,last_kind=NULL,"
                     "zero_since=CASE WHEN ?=0 THEN COALESCE(zero_since,?) ELSE NULL END",
                     (name, now, len(jobs), zs, now, len(jobs), len(jobs), now))
        prefix = f"{row['ats']}:{row['token']}:"
        for j in jobs:
            key = prefix + str(j["id"])
            funnel["jobs on all boards"] += 1
            if conn.execute("UPDATE jobs SET last_seen=?, active=1 WHERE job_key=?", (now, key)).rowcount:
                funnel["already stored (relevant)"] += 1
                continue
            res = scorer.evaluate(j, name, row)
            if not res:
                funnel["not relevant: " + (scorer.reject or "other")] += 1
                continue
            funnel["new relevant"] += 1
            n_new += 1
            hi = res["score"] >= cfg["general"]["alert_min_score"]
            n_alert += hi
            fl, fast = cfg.get("fast_lane", {}), 0
            if (fl.get("enabled", True) and not hi and not first_run and seen_before
                    and res.get("title_w", 0) >= fl.get("min_title_weight", 7)
                    and res["loc_class"] == "remote_ok"
                    and (res.get("eu_remote") or not fl.get("eu_remote_only", True))
                    and res["score"] >= fl.get("min_score", 10)
                    and res.get("travel_pct", 0) <= cfg.get("travel", {}).get("max_pct", 30)):
                age = age_hours(j.get("posted_at"))
                if age is None or age <= fl.get("hours", 48):
                    fast = 1
                    n_fast += 1
            band = ("ALERT" if hi else "FAST LANE alert" if fast
                    else "digest" if res["score"] >= cfg.get("digest", {}).get("min_score", 9) else "stored only")
            log.info("New relevant job: %s | %s | %s | score %.0f -> %s", name, j["title"], j["location"], res["score"], band)
            conn.execute(
                "INSERT INTO jobs(job_key,company,ats,title,location,workplace,url,posted_at,first_seen,last_seen,"
                "score,loc_class,reasons,flags,description,fast_lane) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, name, row["ats"], j["title"], j["location"], j.get("workplace"), j["url"], j.get("posted_at"),
                 now, now, res["score"], res["loc_class"], "; ".join(res["reasons"]), "; ".join(res["flags"]),
                 (j.get("description") or "")[:30000], fast))
        conn.execute("UPDATE jobs SET active=0 WHERE job_key LIKE ? AND last_seen<?", (prefix + "%", now))
        conn.commit()
    log.info("Cycle done: %d companies, %d failed, %d new relevant jobs (%d alert-level, %d fast-lane)",
             len(companies), n_fail, n_new, n_alert, n_fast)
    if funnel:
        log.info("Funnel (all current openings on your watchlist): " + "; ".join(f"{k}: {v}" for k, v in funnel.most_common()))
    n_attn = sync_attention(conn, companies)
    if n_attn:
        log.info("%d item(s) need your attention - see Needs_Attention.csv (or send /attention to the bot)", n_attn)

    # alerts (also retries any that failed to send earlier)
    thr = cfg["general"]["alert_min_score"]
    conn.row_factory = sqlite3.Row
    pending = conn.execute("SELECT * FROM jobs WHERE alerted=0 AND active=1 AND (score>=? OR fast_lane=1) "
                           "ORDER BY fast_lane DESC, score DESC, first_seen DESC", (thr,)).fetchall()
    if first_run:
        keep = cfg["general"]["first_run_digest"]
        kept_groups = collapse(pending)[:keep]
        kept_keys = {g["job_key"] for grp in kept_groups for g in grp}
        for r in pending:
            if r["job_key"] not in kept_keys:
                conn.execute("UPDATE jobs SET alerted=1 WHERE job_key=?", (r["job_key"],))
        conn.execute("UPDATE jobs SET alerted=1 WHERE alerted=0 AND score<?", (thr,))
        pending = [r for grp in kept_groups for r in grp]
        log.info("First run: alerting top %d groups, rest stored silently", keep)
    if send:
        silent = is_quiet(cfg)
        for grp in collapse(pending)[:25]:
            r, extra = grp[0], grp[1:]
            try:
                notify.send_telegram(cfg, format_alert(r, extra), silent=silent)
                for g in grp:
                    conn.execute("UPDATE jobs SET alerted=1, digested=1 WHERE job_key=?", (g["job_key"],))
                conn.commit()
                time.sleep(0.6)
            except Exception as e:  # noqa
                log.error("Telegram send failed: %s", e)
                break
        if len(pending) > 25:
            log.info("%d more pending alerts will go out next cycle", len(pending) - 25)
        bad = conn.execute("SELECT company FROM company_health WHERE fail_count>=5 AND warned=0").fetchall()
        if bad:
            names = ", ".join(b[0] for b in bad[:15])
            try:
                notify.send_telegram(cfg, f"⚠ Poller: {len(bad)} companies failing 5+ cycles in a row: "
                                          f"{notify.esc(names)}. Check poller.log (token may have changed).", silent=True)
                conn.execute("UPDATE company_health SET warned=1 WHERE fail_count>=5")
            except Exception as e:  # noqa
                log.error("Health warning failed: %s", e)
        maybe_send_digest(cfg, conn)
        maybe_send_linkedin_reminder(cfg, conn)
    conn.commit()
    conn.close()


def run_test(cfg):
    companies = load_companies()
    scorer = Scorer(cfg)
    results, fails, per = [], [], []
    funnel = Counter()
    gaps = []        # titles rejected as "no title match" that still look like delivery / operations / program work
    NEAR = re.compile(r"program|project|operations|ops\b|delivery|implementation|deployment|customer (success|operations|experience|outcomes)|"
                      r"solutions|engagement|strategy|strategic|chief|business|enablement|transformation|professional services|onboarding|"
                      r"integration|partnerships?|special projects|enterprise", re.I)
    for row, jobs, err in fetch_all(companies, cfg["general"]["workers"]):
        if err:
            fails.append((row["name"], row["ats"], row["token"], err))
            continue
        hits = 0
        for j in jobs:
            funnel["jobs on all boards"] += 1
            res = scorer.evaluate(j, row["name"], row)
            if not res:
                funnel["not relevant: " + (scorer.reject or "other")] += 1
                if scorer.reject.startswith("title does not match") and NEAR.search(j["title"] or ""):
                    gaps.append((row["name"], j["title"], j.get("location") or ""))
            if res:
                funnel["relevant"] += 1
                hits += 1
                results.append((res["score"], row["name"], j, res))
        per.append((row["name"], len(jobs), hits))
    thr = cfg["general"]["alert_min_score"]
    if gaps:
        with open(BASE / "title_gaps.csv", "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.writer(f)
            wr.writerow(["company", "title", "location"])
            wr.writerows(sorted(gaps, key=lambda g: g[1].lower()))
    out = [f"=== {len(per)} companies OK, {len(fails)} failed ===", "", "=== Funnel: everything currently open on your watchlist ==="]
    out += [f"  {v:>5}  {k}" for k, v in funnel.most_common()]
    if gaps:
        out.append(f"        ({len(gaps)} of the title mismatches look like possible misses; see title_gaps.csv)")
    out.append("")
    for n, tot, hits in sorted(per):
        out.append(f"  {n:<28} {tot:>4} jobs  {hits:>3} relevant")
    for n, a, t, e in fails:
        out.append(f"  FAILED {n} [{a}/{t}]: {e}")
    out.append("\n=== Score distribution (relevant jobs) ===")
    for lo in (24, 20, 18, 16, 14, 12, 10, 8, 0):
        out.append(f"  score >= {lo:>2}: {sum(1 for r in results if r[0] >= lo)}")
    out.append(f"  (alert threshold is {thr}: {sum(1 for r in results if r[0] >= thr)} jobs would alert)")
    out.append("\n=== Top 60 matches ===")
    seen_f, shown = {}, []
    for s, c, j, res in sorted(results, key=lambda x: -x[0]):
        k = family_key(c, j["title"])
        if k in seen_f:
            seen_f[k][0] += 1
            seen_f[k][1].append(j["location"])
            continue
        seen_f[k] = [0, []]
        shown.append((s, c, j, res, k))
    out.append(f"({len(results)} jobs collapse into {len(shown)} distinct role families)")
    for s, c, j, res, k in shown[:60]:
        n, locs = seen_f[k]
        out.append(f"{s:>4.0f}  {c} | {j['title']} | {j['location']} | {'; '.join(res['flags'])}")
        out.append(f"      {j['url']}")
        if n:
            out.append(f"      [+{n} similar: {'; '.join(l or '?' for l in locs[:5])}]")
    text = "\n".join(out)
    (BASE / "test_report.txt").write_text(text, encoding="utf-8")
    print(text.encode("ascii", "replace").decode())
    print("\nSaved to test_report.txt")


def run_rescore(cfg):
    """Re-evaluate every stored job with the current rules and update score, location class, reasons, and flags."""
    scorer = Scorer(cfg)
    meta = {r["name"]: r for r in watchlist.read_companies(include_blocked=True)}
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM jobs").fetchall()
    changed, big = 0, []
    for r in rows:
        job = {"title": r["title"], "location": r["location"], "workplace": r["workplace"], "remote": None,
               "description": r["description"] or "", "id": r["job_key"], "url": r["url"], "posted_at": r["posted_at"]}
        res = scorer.evaluate(job, r["company"], meta.get(r["company"]))
        if res is None:
            new = (0.0, r["loc_class"], "no longer matches the title rules", "")
        else:
            new = (res["score"], res["loc_class"], "; ".join(res["reasons"]), "; ".join(res["flags"]))
        if abs((r["score"] or 0) - new[0]) >= 0.5 or new[1] != r["loc_class"]:
            changed += 1
            if abs((r["score"] or 0) - new[0]) >= 3:
                big.append((new[0] - (r["score"] or 0), r["company"], r["title"], r["score"], new[0]))
        conn.execute("UPDATE jobs SET score=?, loc_class=?, reasons=?, flags=? WHERE job_key=?",
                     (new[0], new[1], new[2], new[3], r["job_key"]))
    conn.commit()
    conn.close()
    print(f"Re-scored {len(rows)} jobs; {changed} changed.")
    for diff, co, title, old_s, new_s in sorted(big)[:15]:
        print(f"  {co} | {title}: {old_s:.0f} -> {new_s:.0f}")


def run_find(cfg, pattern):
    """List stored, still-open jobs whose location or title matches a pattern, best score first. Example: --find "dublin|ireland|cork"."""
    rx = re.compile(pattern, re.I)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    rows = conn.execute("SELECT score, company, title, location, loc_class, status, first_seen, url FROM jobs WHERE active=1 ORDER BY score DESC").fetchall()
    hits = [r for r in rows if rx.search(r[3] or "") or rx.search(r[2] or "")]
    print(f"{len(hits)} open stored jobs match '{pattern}' (showing the best 40):\n")
    for sc, co, ti, lo, lc, st, fs, url in hits[:40]:
        print(f"{sc:>4.0f}  {co} | {ti[:60]} | {lo[:40]} | {lc} | {st} | seen {str(fs)[:10]}\n      {url}")


def run_stats(cfg):
    """Offline snapshot from your database and watchlist: how many jobs are arriving, in which bands, and how healthy the watchlist is."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)                      # works even before the first poll
    thr, dmin = cfg["general"]["alert_min_score"], cfg.get("digest", {}).get("min_score", 9)
    cos = watchlist.read_companies()
    by_ats = Counter(c["ats"] for c in cos)
    print(f"Watchlist: {len(cos)} companies  (" + ", ".join(f"{k}: {v}" for k, v in by_ats.most_common()) + ")")
    if not conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone():
        print("\nNo jobs stored yet. Run one polling cycle first:  python poller.py --once   (or --loop)")
        return
    h = conn.execute("SELECT COUNT(*), SUM(fail_count>=2), SUM(last_count=0) FROM company_health").fetchone()
    print(f"Board health: {h[0]} checked | {h[1] or 0} failing 2+ cycles | {h[2] or 0} with zero openings")
    print(f"\nNew relevant jobs found per day (last 14 days; alert >= {thr}, digest >= {dmin}):")
    rows = conn.execute("SELECT substr(first_seen,1,10) d, COUNT(*), SUM(score>=?), SUM(score>=? AND score<?), SUM(score<?) "
                        "FROM jobs WHERE first_seen >= date('now','-14 day') GROUP BY d ORDER BY d DESC", (thr, dmin, thr, dmin)).fetchall()
    if not rows:
        print("  none in the last 14 days")
    for d, n, a, g, lo in rows:
        print(f"  {d}: {n:>3} new | {a or 0} alert-level | {g or 0} digest-level | {lo or 0} stored only")
    print("\nAll stored jobs by location class:")
    for lc, n in conn.execute("SELECT loc_class, COUNT(*) FROM jobs GROUP BY loc_class ORDER BY 2 DESC"):
        print(f"  {lc:<18} {n}")
    print("\nBest remote-eligible roles you have not yet marked (remote_ok / remote_unclear), by score:")
    best = conn.execute("SELECT score, company, title, location, loc_class, first_seen FROM jobs WHERE active=1 AND status='new' "
                        "AND loc_class IN ('remote_ok','remote_unclear') ORDER BY score DESC LIMIT 15").fetchall()
    for sc, co, ti, lo, lc, fs in best:
        print(f"  {sc:>4.0f}  {co} | {ti[:52]} | {lo[:34]} | {lc} | seen {str(fs)[:10]}")
    if not best:
        print("  none")
    last = conn.execute("SELECT MAX(last_ok) FROM company_health").fetchone()[0]
    print(f"\nMost recent successful board check: {last}")
    print("For the full picture of what is on the boards right now, run:  py poller.py --test   (see the 'Funnel' section)")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--test", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--test-telegram", action="store_true")
    g.add_argument("--stats", action="store_true", help="show job flow and watchlist health from your database")
    g.add_argument("--find", metavar="PATTERN", help="list stored open jobs whose location or title matches a pattern, e.g. \"dublin|ireland\"")
    g.add_argument("--rescore", action="store_true", help="re-score all stored jobs with the current rules")
    g.add_argument("--digest-now", action="store_true", help="send the near-miss digest immediately")
    a = ap.parse_args()
    setup_logging()
    cfg = load_cfg()
    if a.test_telegram:
        notify.send_telegram(cfg, "✅ Job poller connected.")
        print("Sent.")
    elif a.stats:
        run_stats(cfg)
    elif a.find:
        run_find(cfg, a.find)
    elif a.rescore:
        run_rescore(cfg)
    elif a.digest_now:
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(SCHEMA)
        conn.row_factory = sqlite3.Row
        maybe_send_digest(cfg, conn, force=True)
        conn.close()
    elif a.test:
        run_test(cfg)
    elif a.once:
        run_once(cfg)
    else:
        log.info("Starting loop. Ctrl+C to stop.")
        threading.Thread(target=watchlist.listener, args=(load_cfg,), daemon=True).start()
        while True:
            try:
                cfg = load_cfg()
                msgs = watchlist.apply_changes()
                for m in msgs:
                    log.info("watchlist: %s", m)
                if msgs:
                    try:
                        shown = msgs[:12]
                        extra = f"\n...and {len(msgs) - 12} more changes (see poller.log and /attention)." if len(msgs) > 12 else ""
                        notify.send_telegram(cfg, notify.esc("Watchlist update:\n" + "\n".join(shown) + extra), silent=True)
                    except Exception as e:  # noqa
                        log.error("Watchlist notice failed: %s", e)
                run_once(cfg)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Cycle crashed; will retry next interval")
            m = cfg["general"]["poll_minutes"]
            time.sleep(m * 60 * random.uniform(0.9, 1.1))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
