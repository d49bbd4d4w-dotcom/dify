"""Optional push alerts to a Discord channel via webhook."""

from __future__ import annotations

from .sources import Http


def discord(http: Http, webhook_url: str, text: str) -> None:
    if not webhook_url:
        return
    # Discord caps messages at 2000 characters.
    http.post_json(webhook_url, {"content": text[:1990]})
