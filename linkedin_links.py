#!/usr/bin/env python3
"""Ready-made LinkedIn "posted in the last hour" search links, built from config.toml [linkedin].

LinkedIn's terms prohibit automated scraping, so this tool does NOT fetch anything from LinkedIn.
It only builds links that YOU open (tap in Telegram, or run with --open to open them in your browser).

  py linkedin_links.py           print the links
  py linkedin_links.py --open    open every link in your default browser
Telegram: send /linkedin to your bot.
"""
import html
import sys
import tomllib
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

DEFAULTS = {
    "keywords": ["Program Manager", "Implementation Manager", "Delivery Manager"],
    "locations": ["European Union", "United States"],
    "remote_only": True,
    "window_seconds": 3600,      # 3600 = past hour; 86400 = past 24 hours
    "combine_keywords": True,    # one search per location covering all titles (3 links instead of 15)
}


def build(cfg):
    l = {**DEFAULTS, **cfg.get("linkedin", {})}
    out = []
    if l.get("combine_keywords", True) and len(l["keywords"]) > 1:
        groups = [("All target titles", "(" + " OR ".join(f'"{k}"' for k in l["keywords"]) + ")")]   # one boolean search
    else:
        groups = [(k, k) for k in l["keywords"]]
    for label, kw in groups:
        for loc in l["locations"]:
            q = {"keywords": kw, "location": loc, "f_TPR": f"r{int(l['window_seconds'])}", "sortBy": "DD"}
            if l["remote_only"]:
                q["f_WT"] = "2"          # LinkedIn's 'Remote' filter
            out.append((label, loc, "https://www.linkedin.com/jobs/search/?" + urlencode(q)))
    return out


def telegram_message(cfg):
    window = {**DEFAULTS, **cfg.get("linkedin", {})}["window_seconds"]
    label = "past hour" if int(window) == 3600 else f"past {int(window) // 3600} h"
    lines, by_kw = [f"<b>LinkedIn: newest remote jobs ({label})</b>"], {}
    for kw, loc, url in build(cfg):
        by_kw.setdefault(kw, []).append(f'<a href="{html.escape(url, quote=True)}">{html.escape(loc)}</a>')
    for kw, links in by_kw.items():
        lines.append(f"{html.escape(kw)}: " + " | ".join(links))
    return "\n".join(lines)


if __name__ == "__main__":
    with open(Path(__file__).parent / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    for kw, loc, url in build(cfg):
        print(f"{kw} / {loc}\n  {url}")
        if "--open" in sys.argv:
            webbrowser.open_new_tab(url)
