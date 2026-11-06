"""Fetchers for public ATS job feeds. Each returns a list of normalized job dicts:
id, title, location, workplace, remote, url, posted_at, description
Endpoints are public JSON feeds that the companies' own career pages use."""
import html
import random
import re
import threading
import time
from datetime import datetime, timezone

import requests

UA = {"User-Agent": "job-radar/1.0 (open-source job feed reader; polite, low-volume)"}
TIMEOUT = 25


def strip_html(s):
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse returns HTML-escaped HTML
    s = re.sub(r"<(br|/p|/li|/h\d|/div|/ul)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


_local = threading.local()
# Some hosts drop connections when hit by many parallel requests (seen with Ashby), so limit them.
_HOST_LIMITS = {"api.ashbyhq.com": threading.BoundedSemaphore(2)}
_RETRY_CODES = (429, 500, 502, 503, 504)
_ATTEMPTS = 4


def _session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(UA)
        _local.s = s
    return s


def _get(url, **params):
    sem = _HOST_LIMITS.get(url.split("/")[2])
    for attempt in range(_ATTEMPTS):
        try:
            if sem:
                sem.acquire()
            try:
                r = _session().get(url, params=params or None, timeout=TIMEOUT)
            finally:
                if sem:
                    sem.release()
            if r.status_code in _RETRY_CODES:
                raise requests.HTTPError(str(r.status_code), response=r)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code not in _RETRY_CODES or attempt == _ATTEMPTS - 1:
                raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            _local.s = None  # drop a possibly broken connection and start fresh
            if attempt == _ATTEMPTS - 1:
                raise
        time.sleep(2 ** attempt + random.random())


def _ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def greenhouse(token):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", content="true")
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id": str(j["id"]),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "workplace": None,
            "remote": None,
            "url": j.get("absolute_url", ""),
            "posted_at": j.get("updated_at"),
            "description": strip_html(j.get("content", "")),
        })
    return out


def _lever(host, token):
    data = _get(f"https://{host}/v0/postings/{token}", mode="json")
    if not isinstance(data, list):
        raise ValueError("unexpected Lever response")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        desc = j.get("descriptionPlain") or strip_html(j.get("description", ""))
        sal = j.get("salaryRange")
        if sal:
            desc += f"\nCompensation: {sal.get('currency','')} {sal.get('min','')}-{sal.get('max','')} {sal.get('interval','')}"
        out.append({
            "id": j["id"],
            "title": j.get("text", ""),
            "location": "; ".join(l for l in locs if l),
            "workplace": j.get("workplaceType"),
            "remote": (j.get("workplaceType") == "remote") or None,
            "url": j.get("hostedUrl", ""),
            "posted_at": _ms_to_iso(j.get("createdAt")),
            "description": desc,
        })
    return out


def lever(token):
    return _lever("api.lever.co", token)


def lever_eu(token):
    return _lever("api.eu.lever.co", token)


def ashby(token):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}", includeCompensation="true")
    out = []
    for j in data.get("jobs", []):
        locs = [j.get("location") or ""]
        for s in j.get("secondaryLocations") or []:
            locs.append(s.get("location", "") if isinstance(s, dict) else str(s))
        desc = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", ""))
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        if comp:
            desc += f"\nCompensation: {comp}"
        out.append({
            "id": j["id"],
            "title": j.get("title", ""),
            "location": "; ".join(l for l in locs if l),
            "workplace": j.get("workplaceType"),
            "remote": j.get("isRemote"),
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "posted_at": j.get("publishedAt"),
            "description": desc,
        })
    return out


def workable(token):
    data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{token}", details="true")
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        out.append({
            "id": j.get("shortcode") or j.get("id"),
            "title": j.get("title", ""),
            "location": loc,
            "workplace": "remote" if j.get("telecommuting") else None,
            "remote": bool(j.get("telecommuting")) or None,
            "url": j.get("url") or j.get("shortlink", ""),
            "posted_at": j.get("published_on") or j.get("created_at"),
            "description": strip_html(j.get("description", "")),
        })
    return out


def recruitee(token):
    data = _get(f"https://{token}.recruitee.com/api/offers/")
    out = []
    for j in data.get("offers", []):
        if j.get("status") not in (None, "published"):
            continue
        out.append({
            "id": str(j["id"]),
            "title": j.get("title", ""),
            "location": j.get("location") or ", ".join(x for x in [j.get("city"), j.get("country")] if x),
            # Recruitee's boolean "remote" only means remote work is allowed somewhere; use explicit flags when present
            "workplace": "onsite" if j.get("on_site") else "hybrid" if j.get("hybrid") else None,
            "remote": j.get("remote"),
            "url": j.get("careers_url", ""),
            "posted_at": j.get("created_at"),
            "description": strip_html(j.get("description", "")) + "\n" + strip_html(j.get("requirements", "")),
        })
    return out


FETCHERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "lever_eu": lever_eu,
    "ashby": ashby,
    "workable": workable,
    "recruitee": recruitee,
}
