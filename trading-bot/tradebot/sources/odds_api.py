"""The Odds API (the-odds-api.com): live prices from many sportsbooks.

Listing sports is free. Each odds request costs credits: one per market type
per 10 bookmakers, so a moneyline request for up to 10 books costs 1 credit.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import Http

BASE = "https://api.the-odds-api.com/v4"


@dataclass
class OddsResult:
    events: list[dict]
    credits_remaining: float | None
    credits_used: float  # cost of this request


def _credit(headers, name: str) -> float | None:
    v = headers.get(name) if headers else None
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


class OddsApi:
    def __init__(self, http: Http, api_key: str):
        self.http = http
        self.api_key = api_key

    def active_sports(self) -> list[str]:
        """Keys of sports currently in season (free, uses no credits)."""
        data = self.http.get_json(f"{BASE}/sports", {"apiKey": self.api_key})
        return [s["key"] for s in data if s.get("active") and not s.get("has_outrights")]

    def moneyline_odds(self, sport_key: str, bookmakers: list[str]) -> OddsResult:
        data, headers = self.http.get_json_and_headers(
            f"{BASE}/sports/{sport_key}/odds",
            {"apiKey": self.api_key, "markets": "h2h", "oddsFormat": "decimal",
             "bookmakers": ",".join(bookmakers[:10])})
        return OddsResult(data, _credit(headers, "x-requests-remaining"),
                          _credit(headers, "x-requests-last") or 1.0)
