import html

import requests

LIMIT = 3800   # Telegram rejects messages over 4096 characters


def _scrub(text, tok):
    return str(text).replace(tok, "<token>") if tok else str(text)


def _chunks(text):
    if len(text) <= LIMIT:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > LIMIT:               # a single enormous line: cut it
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:LIMIT])
            line = line[LIMIT:]
        if len(cur) + len(line) + 1 > LIMIT:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return [c.rstrip("\n") for c in out]


def send_telegram(cfg, text, silent=False):
    tg = cfg.get("telegram", {})
    tok, chat = tg.get("bot_token"), tg.get("chat_id")
    if not tok or not chat:
        raise RuntimeError("Telegram bot_token/chat_id not set in config.toml (run telegram_setup.py)")
    for part in _chunks(text):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                json={"chat_id": chat, "text": part, "parse_mode": "HTML",
                      "disable_notification": silent, "disable_web_page_preview": True},
                timeout=20,
            )
        except requests.RequestException as e:
            # never let the bot token appear in logs or error messages
            raise RuntimeError(f"Telegram request failed: {type(e).__name__}: {_scrub(e, tok)}") from None
        if r.status_code >= 400:
            try:
                why = r.json().get("description", "")
            except Exception:  # noqa
                why = ""
            raise RuntimeError(f"Telegram error {r.status_code}: {_scrub(why, tok)}")


def esc(s):
    return html.escape(str(s or ""))
