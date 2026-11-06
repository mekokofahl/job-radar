#!/usr/bin/env python3
"""Local review page for job alerts: see them all, open them, and mark Interested / Applied / Skip.

  py review.py            start the page and open it in your browser (Ctrl+C in PowerShell to stop)
  py review.py --port N   use a different port (default 8765)

Runs only on your own computer (127.0.0.1); nothing is sent anywhere. Your choices are saved in jobs.db
(the 'status' column), so skipped jobs stay out of your way and the Telegram feed is unaffected.
"""
import argparse
import html
import json
import secrets
import sqlite3
import tomllib
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).parent
DB_PATH = BASE / "jobs.db"
TOKEN = secrets.token_urlsafe(16)
STATUSES = {"new", "interested", "applied", "skipped"}
PORT = 8765


def load_cfg():
    with open(BASE / "config.toml", "rb") as f:
        return tomllib.load(f)


def connect():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def thresholds(cfg):
    return cfg["general"]["alert_min_score"], cfg.get("digest", {}).get("min_score", 9)


def where_for(view, thr, dmin):
    alert = "(score>=:thr OR fast_lane=1)"
    return {
        "review": f"active=1 AND status='new' AND {alert}",
        "interested": "status='interested'",
        "applied": "status='applied'",
        "skipped": "status='skipped'",
        "near": f"active=1 AND status='new' AND NOT {alert} AND score>=:dmin",
    }.get(view, f"active=1 AND status='new' AND {alert}")


def fetch(view, cfg, limit=300):
    thr, dmin = thresholds(cfg)
    with connect() as c:
        rows = c.execute(f"SELECT * FROM jobs WHERE {where_for(view, thr, dmin)} "
                         "ORDER BY first_seen DESC LIMIT :lim", {"thr": thr, "dmin": dmin, "lim": limit}).fetchall()
        counts = {v: c.execute(f"SELECT COUNT(*) FROM jobs WHERE {where_for(v, thr, dmin)}",
                               {"thr": thr, "dmin": dmin}).fetchone()[0]
                  for v in ("review", "interested", "applied", "skipped", "near")}
    return rows, counts


def ago(iso):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        m = (datetime.now(timezone.utc) - d).total_seconds() / 60
        if m < 90:
            return f"{max(int(m), 0)} min ago"
        if m < 48 * 60:
            return f"{m / 60:.0f} h ago"
        return f"{m / 1440:.0f} d ago"
    except Exception:
        return ""


TABS = [("review", "To review"), ("interested", "Interested"), ("applied", "Applied"),
        ("skipped", "Skipped"), ("near", "Near-misses")]


def page(view, cfg):
    rows, counts = fetch(view, cfg)
    e = html.escape
    tabs = "".join(f'<a class="tab{" on" if v == view else ""}" href="/?view={v}">{e(n)} ({counts[v]})</a>' for v, n in TABS)
    body = []
    for r in rows:
        seen, posted = ago(r["first_seen"]), ago(r["posted_at"])
        bolt = "⚡ " if r["fast_lane"] else ""
        btns = "".join(f'<button data-s="{s}">{lab}</button>' for s, lab in
                       (("interested", "Interested"), ("applied", "Applied"), ("skipped", "Skip"), ("new", "Reset")))
        body.append(
            f'<tr data-k="{e(r["job_key"])}" data-t="{e(r["title"])}" data-c="{e(r["company"])}" data-l="{e(r["location"] or "")}" '
            f'data-u="{e(r["url"])}" data-sc="{r["score"]:.0f}"><td class="sc">{bolt}{r["score"]:.0f}</td>'
            f'<td><a href="{e(r["url"])}" target="_blank" rel="noopener noreferrer"><b>{e(r["title"])}</b></a><br>{e(r["company"])}</td>'
            f'<td>{e(r["location"] or "")}</td>'
            f'<td>{e(seen)}{"<br><span class=m>posted " + e(posted) + "</span>" if posted else ""}</td>'
            f'<td class="m">{e(r["flags"] or "")}</td><td class="act">{btns}</td></tr>')
    if not body:
        body.append('<tr><td colspan="6" class="m">Nothing here.</td></tr>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Job alerts</title><style>
body{{font:14px Arial,sans-serif;margin:20px;color:#222}} h1{{font-size:20px;margin:0 0 10px}}
.tab{{display:inline-block;padding:6px 12px;margin-right:4px;border:1px solid #bbb;border-radius:4px;text-decoration:none;color:#1F3864}}
.tab.on{{background:#1F3864;color:#fff}} table{{border-collapse:collapse;width:100%;margin-top:12px}}
th,td{{border-bottom:1px solid #ddd;padding:7px 8px;text-align:left;vertical-align:top}} th{{background:#f2f2f2}}
.sc{{font-weight:bold;white-space:nowrap}} .m{{color:#666;font-size:12px}} .act button{{margin:1px;padding:3px 7px;cursor:pointer}}
tr.gone{{opacity:.35}} a{{color:#0563C1}}</style></head><body><h1>Job alerts</h1>{tabs}
<p><button id="copy">Copy this list</button> <span id="copied" class="m"></span></p>
<table><tr><th>Score</th><th>Role / company</th><th>Location</th><th>Seen</th><th>Notes</th><th>Action</th></tr>{"".join(body)}</table>
<script>const T={json.dumps(TOKEN)};
document.querySelectorAll('.act button').forEach(b=>b.onclick=async()=>{{const tr=b.closest('tr');
const r=await fetch('/status',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token:T,job_key:tr.dataset.k,status:b.dataset.s}})}});
if(r.ok){{tr.classList.add('gone');setTimeout(()=>location.reload(),350);}}else{{alert('Could not save (is the page stale? refresh it).');}}}});
document.getElementById('copy').onclick=async()=>{{const rows=[...document.querySelectorAll('tr[data-k]:not(.gone)')];
const text=rows.map(t=>'- '+t.dataset.t+' | '+t.dataset.c+' | '+t.dataset.l+' | score '+t.dataset.sc+' | '+t.dataset.u).join('\\n');
try{{await navigator.clipboard.writeText(text);document.getElementById('copied').textContent='Copied '+rows.length+' job(s).';}}
catch(e){{window.prompt('Copy this text:',text);}}}};</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    cfg = None

    def _host_ok(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        if u.path != "/":
            return self._send(404, "not found", "text/plain")
        view = (parse_qs(u.query).get("view") or ["review"])[0]
        self._send(200, page(view if view in {v for v, _ in TABS} else "review", load_cfg()))

    def do_POST(self):
        if not self._host_ok() or self.path != "/status":
            return self._send(403, "forbidden", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(min(n, 10000)))
            ok = (secrets.compare_digest(str(data.get("token", "")), TOKEN) and data.get("status") in STATUSES
                  and isinstance(data.get("job_key"), str))
        except Exception:
            ok = False
        if not ok:
            return self._send(400, '{"ok":false}', "application/json")
        with connect() as c:
            c.execute("UPDATE jobs SET status=? WHERE job_key=?", (data["status"], data["job_key"]))
        self._send(200, '{"ok":true}', "application/json")

    def log_message(self, *a):
        pass


def recent_text(cfg, n=10, status="new"):
    """Telegram /recent (unreviewed alerts) and /interested (jobs you marked Interested), as tappable links."""
    thr, _ = thresholds(cfg)
    e = html.escape
    with connect() as c:
        if status == "interested":
            rows = c.execute("SELECT * FROM jobs WHERE status='interested' ORDER BY first_seen DESC LIMIT ?", (n,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM jobs WHERE active=1 AND status='new' AND (score>=? OR fast_lane=1) "
                             "ORDER BY first_seen DESC LIMIT ?", (thr, n)).fetchall()
    label = "Interested jobs" if status == "interested" else "unreviewed alerts"
    if not rows:
        return f"No {label.lower()}."
    lines = [f"<b>Latest {len(rows)} {label}</b>"]
    for r in rows:
        lines.append(f'{"⚡ " if r["fast_lane"] else ""}{r["score"]:.0f} · <a href="{e(r["url"])}">{e(r["title"])}</a> · {e(r["company"])} · {e(r["location"] or "?")} · {e(ago(r["first_seen"]))}')
    return "\n".join(lines)


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    PORT = a.port
    if not DB_PATH.exists():
        raise SystemExit("jobs.db not found. Run the poller first (py poller.py --once).")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Job alert review page: {url}   (press Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
