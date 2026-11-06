#!/usr/bin/env python3
"""One-time Telegram setup: stores your bot token + chat id in config.toml."""
import re
from pathlib import Path

import requests

cfg_path = Path(__file__).parent / "config.toml"
text = cfg_path.read_text(encoding="utf-8")
token = input("Paste the bot token from @BotFather: ").strip()
input("Now open your new bot in Telegram, press START, send it any message (e.g. 'hi'), then press Enter here... ")
r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
chat_id = None
for u in reversed(r.get("result", [])):
    msg = u.get("message") or u.get("channel_post")
    if msg:
        chat_id = msg["chat"]["id"]
        break
if not chat_id:
    raise SystemExit("No message found. Make sure you sent a message to the bot, then run this again.")
text = re.sub(r'bot_token\s*=\s*".*?"', f'bot_token = "{token}"', text)
text = re.sub(r'chat_id\s*=\s*".*?"', f'chat_id   = "{chat_id}"', text)
cfg_path.write_text(text, encoding="utf-8")
requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
              json={"chat_id": chat_id, "text": "✅ Job poller connected."}, timeout=20)
print(f"Saved. chat_id={chat_id}. You should have a message in Telegram.")
