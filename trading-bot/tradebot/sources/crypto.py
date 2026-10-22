"""Spot BTC prices, candles, order book and sentiment from public endpoints."""

from __future__ import annotations

from ..signals import Candle
from . import Http

COINBASE = "https://api.exchange.coinbase.com"


def coinbase_candles(http: Http, granularity: int = 60, product: str = "BTC-USD") -> list[Candle]:
    """Latest (up to 300) candles, oldest first. granularity in seconds:
    60, 300, 900, 3600, 21600 or 86400."""
    rows = http.get_json(f"{COINBASE}/products/{product}/candles", {"granularity": granularity})
    # Coinbase rows: [time, low, high, open, close, volume], newest first.
    candles = [Candle(int(t), float(o), float(h), float(lo), float(c), float(v))
               for t, lo, h, o, c, v in rows]
    candles.sort(key=lambda c: c.ts)
    return candles


def coinbase_ticker(http: Http, product: str = "BTC-USD") -> float:
    d = http.get_json(f"{COINBASE}/products/{product}/ticker")
    bid, ask = float(d["bid"]), float(d["ask"])
    return (bid + ask) / 2


def coinbase_book(http: Http, product: str = "BTC-USD") -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    d = http.get_json(f"{COINBASE}/products/{product}/book", {"level": 2})
    bids = [(float(p), float(s)) for p, s, *_ in d["bids"]]
    asks = [(float(p), float(s)) for p, s, *_ in d["asks"]]
    return bids, asks


def kraken_ticker(http: Http) -> float:
    d = http.get_json("https://api.kraken.com/0/public/Ticker", {"pair": "XBTUSD"})
    if d.get("error"):
        raise RuntimeError(f"kraken: {d['error']}")
    t = next(iter(d["result"].values()))
    return (float(t["b"][0]) + float(t["a"][0])) / 2


def bitstamp_ticker(http: Http) -> float:
    d = http.get_json("https://www.bitstamp.net/api/v2/ticker/btcusd/")
    return (float(d["bid"]) + float(d["ask"])) / 2


def fear_greed(http: Http) -> tuple[int, str] | None:
    """Crypto Fear & Greed index (0 = extreme fear, 100 = extreme greed). Daily."""
    d = http.get_json("https://api.alternative.me/fng/", {"limit": 1})
    rows = d.get("data") or []
    if not rows:
        return None
    return int(rows[0]["value"]), rows[0]["value_classification"]
