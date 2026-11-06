import re
from datetime import date


def _c(p):
    return re.compile(p, re.I)


_AMT = r"(\d{2,3}(?:[.,]\d{3})+|\d{2,3}\s?[kK])\b"
USD = re.compile(r"\$\s?" + _AMT)
EUR = [re.compile(r"(?:€|\bEUR\b)\s?" + _AMT, re.I), re.compile(_AMT + r"\s?(?:€|\bEUR\b)", re.I)]


def _to_int(txt):
    t = txt.replace(",", "").replace(".", "").replace(" ", "").lower()
    return int(t[:-1]) * 1000 if t.endswith("k") else int(t)


def _vals(patterns, text):
    out = []
    for rx in patterns:
        for m in rx.finditer(text):
            v = _to_int(m.group(1))
            if 30000 <= v <= 1_000_000:
                out.append(v)
    return out


def _money_values(text):
    """USD amounts (kept for backward compatibility)."""
    return _vals([USD], text)



_N = r"(\d{1,3})"
_RANGE = _N + r"(?:\s?(?:-|\u2013|\u2014|to)\s?" + _N + r")?\s?(?:%|percent|per cent)"
TRAVEL_A = re.compile(r"travel[^.\n]{0,120}?" + _RANGE, re.I)       # "travel ... 30-50%"
TRAVEL_B = re.compile(_RANGE + r"[^.\n]{0,40}?travel", re.I)        # "50% travel"
TRAVEL_WEEKS = re.compile(r"travel[^.\n]{0,100}?\b(one|two|three|1|2|3)\s+weeks?\s+(?:per|a|each|every)\s+month", re.I)
TRAVEL_DAYS = re.compile(r"travel[^.\n]{0,80}?\b(1|2|3|4|one|two|three|four)\s+days?\s+(?:per|a|each|every)\s+week", re.I)
TRAVEL_QUAL = re.compile(r"\b(extensive|significant|frequent|heavy|substantial|considerable|constant)\s+(?:business\s+)?travel|"
                         r"travel\s+(?:extensively|frequently|heavily)", re.I)
TRAVEL_MENTION = re.compile(r"\b(regular|occasional|periodic|some|required|willingness to|willing to)\s+(?:business\s+)?travel|"
                            r"\btravel\s+to\s+(?:customer|client)", re.I)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4}
_NOT_TRAVEL_PCT = re.compile(r"remote|distributed|work from|home|office|salary|equity|match|bonus|401", re.I)


def travel_info(text):
    """Return (percent_or_None, kind) where kind is 'pct', 'qual', 'mention', or ''. Uses the highest stated figure."""
    best = None
    for rx in (TRAVEL_A, TRAVEL_B):
        for m in rx.finditer(text):
            if _NOT_TRAVEL_PCT.search(m.group(0)):
                continue
            nums = [int(g) for g in m.groups() if g]
            if nums and max(nums) <= 100:
                best = max(best or 0, max(nums))
    for m in TRAVEL_WEEKS.finditer(text):
        n = _WORDS.get(m.group(1).lower(), None) or int(m.group(1))
        best = max(best or 0, n * 25)
    for m in TRAVEL_DAYS.finditer(text):
        n = _WORDS.get(m.group(1).lower(), None) or int(m.group(1))
        best = max(best or 0, n * 20)
    if best is not None:
        return best, "pct"
    if TRAVEL_QUAL.search(text):
        return None, "qual"
    if TRAVEL_MENTION.search(text):
        return None, "mention"
    return None, ""


class Scorer:
    def __init__(self, cfg):
        f, self.sc = cfg["filters"], cfg["scores"]
        self.inc = [(_c(p), w, p) for p, w in f["title_include"]]
        self.exc = [_c(p) for p in f["title_exclude"]]
        self.boost = [(_c(p), w) for p, w in f["desc_boost"]]
        self.cap = f.get("desc_boost_cap", 9)
        self.avoid = [(n, _c(p)) for n, p in f["avoid_patterns"]]
        self.defense = _c(f["defense_pattern"])
        self.technical = [_c(p) for p in f.get("title_technical", [])]
        self.defense_title = _c(f.get("defense_title_pattern", r"defen[cs]e|military"))
        d = {"funded_recent_months": 6, "funded_recent_bonus": 2, "stage_bonus": 2, "size_min": 50,
             "size_max": 900, "size_large_penalty": -1, "size_small_penalty": -2}
        d.update(cfg.get("company_scores", {}))
        self.cs = d
        self.localized_pay = [_c(p) for p in f.get("pay_localized", [])]
        self.geo = [_c(p) for p in f.get("geo_restricted", [])]
        self.remote_culture = _c(f["remote_culture"]) if f.get("remote_culture") else None
        t = {"max_pct": 30, "over_penalty": -3, "heavy_pct": 40, "heavy_penalty": -6, "qualitative_penalty": -2}
        t.update(cfg.get("travel", {}))
        self.travel = t
        home = f.get("home_places", [])          # where you already live: on-site roles there carry no relocation penalty
        self.home = _c(r"\b(" + "|".join(re.escape(p) for p in home) + r")\b") if home else None
        self.home_label = cfg.get("general", {}).get("home_label", "your location")
        self.uk_note = cfg.get("flags", {}).get("uk_note", "UK: check your work authorization before applying")
        self.preferred = _c(r"\b(" + "|".join(re.escape(p) for p in f.get("preferred_relocation", [])) + r")\b") if f.get("preferred_relocation") else None
        self.ex_companies = [c.lower() for c in f.get("exclude_companies", [])]
        self.places = [_c(re.escape(p)) for p in f["onsite_ok_places"]]
        region = "|".join(re.escape(w) for w in f.get("home_region_words", []))
        self.generic = _c(r"\b(united states|north america|americas|europe|emea|worldwide|anywhere|global|european union"
                          + (("|" + region) if region else "") + r")\b")
        self.remote_ok = _c(f["remote_ok"])
        self.remote_eu_ok = _c(f["remote_eu_ok"])

    def classify_location(self, job):
        loc = job.get("location") or ""
        wp = (job.get("workplace") or "").lower().replace("-", "").replace(" ", "")
        word = bool(re.search(r"\bremote\b|work from home|anywhere|worldwide", loc, re.I))
        has_place = any(p.search(loc) for p in self.places)
        feed_remote = job.get("remote") is True
        multi = bool(re.search(r"[;|]|\bor\b|\band\b", loc, re.I))
        single_city = has_place and not multi and not word
        note = ""
        if wp in ("hybrid", "onsite"):
            remote = False
        elif word or wp == "remote":
            remote = True
        elif feed_remote and single_city:
            remote = False                      # e.g. "New York, New York, United States" flagged remote by the job board
            note = "feed_remote"
        elif feed_remote and (not has_place or self.generic.search(loc)):
            remote = True
        else:
            remote = False
            note = "feed_remote" if feed_remote else ""   # feed says remote, but location is a specific city
        if remote:
            if has_place and not word and not self.generic.search(loc):
                return "remote_unclear", "city"
            if self.remote_ok.search(loc):
                return "remote_ok", ""
            leftover = re.sub(r"remote|\bwfh\b|work from home|[-,;/()|]", " ", loc, flags=re.I).strip()
            if not leftover:
                return "remote_unclear", ""
            return "remote_restricted", ""
        if not loc.strip():
            return "unknown", ""
        for p in self.places:
            if p.search(loc):
                return "onsite_target", note
        return "onsite_other", note

    def company_adj(self, meta):
        """Bonus/penalty from companies.csv metadata (stage, last_round, employees). Missing data = no effect."""
        if not meta:
            return 0, []
        cs, adj, why = self.cs, 0, []
        stage = (meta.get("stage") or "").lower().strip()
        if re.search(r"late[- ]?seed|series\s*[ab]\b|^[ab]$", stage):
            adj += cs["stage_bonus"]
            why.append(f"stage {stage} +{cs['stage_bonus']}")
        m = re.match(r"^\s*(\d{4})-(\d{1,2})", meta.get("last_round") or "")
        if m:
            today = date.today()
            age = (today.year - int(m.group(1))) * 12 + today.month - int(m.group(2))
            if 0 <= age <= cs["funded_recent_months"]:
                adj += cs["funded_recent_bonus"]
                why.append(f"funded {age}mo ago +{cs['funded_recent_bonus']}")
        nums = [int(x) for x in re.findall(r"\d+", (meta.get("employees") or "").replace(",", ""))]
        if nums:
            n = sum(nums[:2]) / len(nums[:2])
            if n < cs["size_min"]:
                adj += cs["size_small_penalty"]
                why.append(f"small ({n:.0f} staff) {cs['size_small_penalty']}")
            elif n > cs["size_max"]:
                adj += cs["size_large_penalty"]
                why.append(f"large ({n:.0f} staff) {cs['size_large_penalty']}")
        return adj, why

    def evaluate(self, job, company, meta=None):
        title = job["title"] or ""
        self.reject = ""
        if any(p.search(title) for p in self.exc):
            self.reject = "title on your exclude list (engineering, sales, junior, etc.)"
            return None
        if any(c in company.lower() for c in self.ex_companies):
            self.reject = "company on your exclude list"
            return None
        best = None
        for rx, w, raw in self.inc:
            if rx.search(title) and (best is None or w > best[0]):
                best = (w, raw)
        if not best:
            self.reject = "title does not match any of your target titles"
            return None
        reasons, flags = [f"title +{best[0]}"], []
        score = best[0]
        desc = job.get("description") or ""

        # avoided industries
        for name, rx in self.avoid:
            if rx.search(title) or rx.search(company):
                self.reject = "avoided industry (gambling/crypto)"
                return None
            hits = len(rx.findall(desc))
            if hits >= 3:
                self.reject = "avoided industry (gambling/crypto)"
                return None
            if hits:
                flags.append(f"mentions {name} ({hits}x)")
                score += self.sc["avoid_flag_penalty"]
        if len(self.defense.findall(desc)) >= 2:
            flags.append("defense-related language")
            score += self.sc["defense_flag_penalty"]

        if self.defense_title.search(title):
            flags.append("defense-focused role")
            score += self.sc.get("defense_title_penalty", -4)
        if any(p.search(title) for p in self.technical):
            flags.append("deeply technical domain: check fit (you are non-developer)")
            score += self.sc.get("title_technical_penalty", -3)

        # description boosts
        b = 0
        for rx, w in self.boost:
            if rx.search(desc) or rx.search(title):
                b += w
        b = min(b, self.cap)
        if b:
            reasons.append(f"desc +{b}")
            score += b

        # location
        cls, note = self.classify_location(job)
        loc = job.get("location") or ""
        remote_ok_adj = self.sc["remote_ok_eu"] if self.remote_eu_ok.search(loc) else self.sc["remote_ok_us"]
        adj = {"remote_ok": remote_ok_adj, "remote_unclear": self.sc["remote_unclear"],
               "remote_restricted": self.sc["remote_restricted"], "onsite_target": self.sc["onsite_target"],
               "onsite_other": self.sc["onsite_other"], "unknown": 0}[cls]
        if self.remote_culture and cls in ("remote_ok", "remote_unclear") and self.remote_culture.search(desc):
            rc = self.sc.get("remote_culture_bonus", 2)
            adj += rc
            flags.append("remote-first company language")
        at_home = bool(self.home and self.home.search(loc))
        if cls == "onsite_target" and at_home:
            adj += self.sc.get("onsite_home_bonus", 3)
            flags.append("in your home area")
        if cls == "onsite_target" and self.preferred and self.preferred.search(loc) and not at_home:
            adj += self.sc.get("onsite_preferred_bonus", 4)
            flags.append("preferred relocation city")
        score += adj
        reasons.append(f"{cls} {adj:+d}")
        if cls == "remote_ok" and not self.remote_eu_ok.search(loc):
            flags.append("US-only remote: check hours overlap")
        if cls == "remote_unclear":
            flags.append("listed remote but location is a city: probably hybrid/onsite, verify" if note == "city"
                         else f"remote, country not stated: verify {self.home_label} is allowed")
        if cls in ("onsite_target", "onsite_other"):
            flags.append("onsite/hybrid: relocation or ~25% travel")
            if note == "feed_remote":
                flags.append("feed says remote, but location is a city: verify")
        if re.search(r"\b(london|uk|united kingdom|england|wales|scotland)\b", loc, re.I):
            flags.append(self.uk_note)

        cadj, cwhy = self.company_adj(meta)
        if cadj or cwhy:
            score += cadj
            reasons.append("company: " + ", ".join(cwhy))

        # salary (USD floor and EUR floor; penalize only if every stated currency is below its floor)
        usd, eur = _vals([USD], desc), _vals(EUR, desc)
        below, ok = [], []
        if usd:
            (below if max(usd) < self.sc["salary_floor_usd"] else ok).append(f"${max(usd):,}")
        if eur:
            (below if max(eur) < self.sc.get("salary_floor_eur", 120000) else ok).append(f"EUR {max(eur):,}")
        if below and not ok:
            score += self.sc["salary_below_floor"]
            flags.append("salary seems below floor (max " + " / ".join(below) + ")")
        elif ok:
            reasons.append("salary up to " + " / ".join(ok))

        # wording that limits who can be hired (country, region, or US-resident only)
        if self.geo and any(rx.search(desc) for rx in self.geo):
            score += self.sc.get("geo_restricted_penalty", -5)
            flags.append(f"wording limits who can be hired (country/region): verify {self.home_label} is allowed")

        # location-adjusted pay (a hire in your location would be paid at local-market rates)
        for rx in self.localized_pay:
            if rx.search(desc):
                score += self.sc.get("pay_localized_penalty", -3)
                flags.append(f"pay adjusted by location: expect {self.home_label}-level salary")
                break

        tpct, tkind = travel_info(desc)
        tr = self.travel
        if tkind == "pct" and tpct > tr["max_pct"]:
            pen = tr["heavy_penalty"] if tpct > tr["heavy_pct"] else tr["over_penalty"]
            score += pen
            flags.append(f"heavy travel: up to ~{tpct}% (your limit {tr['max_pct']}%)")
        elif tkind == "qual":
            score += tr["qualitative_penalty"]
            flags.append("extensive travel mentioned (percentage not stated): ask")
        elif tkind == "mention":
            flags.append("travel mentioned (percentage not stated)")
        elif tkind == "pct":
            reasons.append(f"travel ~{tpct}%")
        if re.search(r"forward[- ]deployed", title, re.I) and re.search(r"engineer", title, re.I):
            flags.append("engineering title: check technical depth")
            score += self.sc["fde_engineer_penalty"]

        return {"score": score, "loc_class": cls, "reasons": reasons, "flags": flags,
                "title_w": best[0], "eu_remote": bool(self.remote_eu_ok.search(loc)),
                "travel_pct": tpct if tkind == "pct" else 0}


_REGION = re.compile(r"\b(east|west|central|north|south|uk|emea|apac|us|usa|nyc|sf|remote)\b", re.I)


def family_key(company, title):
    t = re.sub(r"\(.*?\)", " ", title or "")
    t = re.split(r"\s[-–—]\s", t)[0] if re.search(
        r"\s[-–—]\s*(new york|san francisco|london|nyc|sf|seattle|austin|boston|paris|dublin|remote)", t, re.I) else t
    t = _REGION.sub(" ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return company.lower() + "|" + re.sub(r"\s+", " ", t).strip()
