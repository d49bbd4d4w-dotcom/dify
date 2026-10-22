"""Indicators computed from candles and order books."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Candle:
    ts: int  # unix seconds, start of the bar
    open: float
    high: float
    low: float
    close: float
    volume: float


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def realized_vol(returns: list[float]) -> float:
    """Root-mean-square return (mean assumed ~0 at short horizons)."""
    if not returns:
        return 0.0
    return math.sqrt(sum(r * r for r in returns) / len(returns))


def ewma_vol(returns: list[float], halflife: float) -> float:
    """Exponentially weighted volatility; recent bars count more."""
    if not returns:
        return 0.0
    lam = 0.5 ** (1.0 / halflife) if halflife > 0 else 0.0
    var = returns[0] ** 2
    for r in returns[1:]:
        var = lam * var + (1.0 - lam) * r * r
    return math.sqrt(var)


def momentum(closes: list[float], bars: int) -> float | None:
    """Log return over the last `bars` bars."""
    if len(closes) <= bars or closes[-1 - bars] <= 0:
        return None
    return math.log(closes[-1] / closes[-1 - bars])


def book_imbalance(bids: list[tuple[float, float]], asks: list[tuple[float, float]],
                   mid: float, band_pct: float = 0.001) -> float | None:
    """(bid size - ask size) / total within +/- band of mid. +1 = all bids."""
    lo, hi = mid * (1 - band_pct), mid * (1 + band_pct)
    b = sum(size for price, size in bids if price >= lo)
    a = sum(size for price, size in asks if price <= hi)
    if a + b == 0:
        return None
    return (b - a) / (b + a)


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(closes) <= n:
        return None
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def atr(candles: list[Candle], n: int = 14) -> float | None:
    """Wilder's average true range."""
    if len(candles) <= n:
        return None
    trs = []
    for prev, cur in zip(candles, candles[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    val = sum(trs[:n]) / n
    for tr in trs[n:]:
        val = (val * (n - 1) + tr) / n
    return val
