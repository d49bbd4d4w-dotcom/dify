"""Kalshi public market data (no login needed to read prices)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import Http

BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _price(raw: dict, key: str) -> float | None:
    """Kalshi reports prices as `<key>_dollars` strings or legacy integer cents."""
    d = raw.get(f"{key}_dollars")
    if d not in (None, ""):
        v = float(d)
    elif raw.get(key) is not None:
        v = raw[key] / 100.0
    else:
        return None
    return v if 0.0 < v < 1.0 else None


def _time(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _num(v) -> float | None:
    return float(v) if v not in (None, "") else None


@dataclass
class Market:
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    status: str
    open_time: datetime | None
    close_time: datetime | None
    strike_type: str
    floor_strike: float | None
    cap_strike: float | None
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume: float
    result: str

    @property
    def mid(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return self.last_price

    @property
    def spread(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return self.yes_ask - self.yes_bid
        return None

    @classmethod
    def from_api(cls, raw: dict) -> "Market":
        yes_bid, yes_ask = _price(raw, "yes_bid"), _price(raw, "yes_ask")
        no_bid, no_ask = _price(raw, "no_bid"), _price(raw, "no_ask")
        # Buying NO at the no ask is the same as selling YES at the yes bid.
        if no_ask is None and yes_bid is not None:
            no_ask = round(1.0 - yes_bid, 4)
        if no_bid is None and yes_ask is not None:
            no_bid = round(1.0 - yes_ask, 4)
        return cls(
            ticker=raw["ticker"],
            event_ticker=raw.get("event_ticker", ""),
            title=raw.get("title", ""),
            subtitle=raw.get("yes_sub_title") or raw.get("subtitle", ""),
            status=raw.get("status", ""),
            open_time=_time(raw.get("open_time")),
            close_time=_time(raw.get("close_time")),
            strike_type=raw.get("strike_type", ""),
            floor_strike=_num(raw.get("floor_strike")),
            cap_strike=_num(raw.get("cap_strike")),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            last_price=_price(raw, "last_price"),
            volume=float(raw.get("volume_fp") or raw.get("volume") or 0),
            result=raw.get("result", "") or "",
        )


class Kalshi:
    def __init__(self, http: Http):
        self.http = http

    def open_markets(self, series_ticker: str, limit: int = 200) -> list[Market]:
        markets: list[Market] = []
        cursor = None
        while True:
            params = {"series_ticker": series_ticker, "status": "open", "limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = self.http.get_json(f"{BASE}/markets", params)
            markets += [Market.from_api(m) for m in data.get("markets", [])]
            cursor = data.get("cursor")
            if not cursor or len(markets) >= 1000:
                return markets

    def market(self, ticker: str) -> Market:
        return Market.from_api(self.http.get_json(f"{BASE}/markets/{ticker}")["market"])

    def markets(self, tickers: list[str]) -> list[Market]:
        """Look up many markets by ticker, 50 per request."""
        out: list[Market] = []
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i + 50]
            data = self.http.get_json(f"{BASE}/markets", {"tickers": ",".join(chunk), "limit": len(chunk)})
            out += [Market.from_api(m) for m in data.get("markets", [])]
        return out

    def series(self, category: str) -> list[dict]:
        data = self.http.get_json(f"{BASE}/series", {"category": category})
        return data.get("series") or []
