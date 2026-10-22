"""Data sources. Everything is public except The Odds API, which needs a free key."""

from __future__ import annotations

import re
import time

import requests

USER_AGENT = "tradebot/0.1 (+personal research tool)"

_SECRETS = [(re.compile(r"(apiKey=)[^&\s']+"), r"\1***"),
            (re.compile(r"(/bot)\d+:[\w-]+"), r"\1***")]


def redact(text: str) -> str:
    """Hide API keys and bot tokens that requests puts in error messages."""
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


class Http:
    """Thin requests wrapper with a timeout and a couple of retries."""

    def __init__(self, timeout: float = 10.0, retries: int = 2):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.timeout = timeout
        self.retries = retries

    def get_json(self, url: str, params: dict | None = None):
        return self.get_json_and_headers(url, params)[0]

    def get_json_and_headers(self, url: str, params: dict | None = None):
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} from {url}", response=r)
                r.raise_for_status()
                return r.json(), r.headers
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                last = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    break
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(redact(f"GET {url} failed: {last}")) from None

    def post_json(self, url: str, payload: dict) -> None:
        self.session.post(url, json=payload, timeout=self.timeout).raise_for_status()
