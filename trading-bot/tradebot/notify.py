"""Push alerts to Telegram and/or Discord. Both are optional."""

from __future__ import annotations

import sys

from .config import Config
from .sources import Http

TELEGRAM_API = "https://api.telegram.org"


def discord(http: Http, webhook_url: str, text: str) -> None:
    # Discord caps messages at 2000 characters.
    http.post_json(webhook_url, {"content": text[:1990]})


def telegram(http: Http, token: str, chat_id: str, text: str) -> None:
    # Plain text (no parse_mode) so tickers and $ signs never need escaping.
    # Telegram caps messages at 4096 characters.
    http.post_json(f"{TELEGRAM_API}/bot{token}/sendMessage",
                   {"chat_id": chat_id, "text": text[:4090], "disable_web_page_preview": True})


def telegram_chats(http: Http, token: str) -> list[tuple[str, str]]:
    """(chat_id, name) for every chat that has recently messaged the bot or added it."""
    data = http.get_json(f"{TELEGRAM_API}/bot{token}/getUpdates")
    chats: dict[str, str] = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            name = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            chats[str(chat["id"])] = f"{name} ({chat.get('type', '')})"
    return list(chats.items())


class Notifier:
    def __init__(self, http: Http, cfg: Config):
        self.http = http
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.discord_webhook_url or
                    (self.cfg.telegram_bot_token and self.cfg.telegram_chat_id))

    def send(self, text: str) -> None:
        """Send to every configured channel. A failed alert never stops the bot."""
        cfg = self.cfg
        if cfg.telegram_bot_token and cfg.telegram_chat_id:
            try:
                telegram(self.http, cfg.telegram_bot_token, cfg.telegram_chat_id, text)
            except Exception as e:
                print(f"[warn] telegram alert failed: {e}", file=sys.stderr)
        if cfg.discord_webhook_url:
            try:
                discord(self.http, cfg.discord_webhook_url, text)
            except Exception as e:
                print(f"[warn] discord alert failed: {e}", file=sys.stderr)
