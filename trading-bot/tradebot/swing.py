"""Rule-based BTC swing/day-trade view for spot trading (e.g. on Robinhood)."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import signals
from .signals import Candle


@dataclass
class SwingView:
    timeframe: str
    price: float
    stance: str  # "long setup", "no trade", "avoid / downtrend"
    score: int
    reasons: list[str] = field(default_factory=list)
    stop: float | None = None
    target: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    rsi14: float | None = None
    atr14: float | None = None

    def render(self) -> str:
        def f(x: float | None) -> str:
            return "n/a" if x is None else f"${x:,.0f}"
        lines = [
            f"=== BTC {self.timeframe} swing view ===",
            f"Price {f(self.price)} | SMA20 {f(self.sma20)} | SMA50 {f(self.sma50)} | SMA200 {f(self.sma200)}",
            f"RSI14 {self.rsi14:.0f} | ATR14 {f(self.atr14)}" if self.rsi14 is not None else "",
            f"Stance: {self.stance.upper()} (score {self.score:+d})",
        ]
        lines += [f"  - {r}" for r in self.reasons]
        if self.stop and self.target:
            risk = self.price - self.stop
            lines.append(f"If entering long: stop {f(self.stop)} (-{risk / self.price:.1%}), "
                         f"target {f(self.target)} (+{(self.target - self.price) / self.price:.1%}), "
                         f"reward:risk {(self.target - self.price) / risk:.1f}")
        return "\n".join(l for l in lines if l)


def analyze(candles: list[Candle], timeframe: str, fear_greed: tuple[int, str] | None = None) -> SwingView:
    closes = [c.close for c in candles]
    price = closes[-1]
    s20, s50, s200 = signals.sma(closes, 20), signals.sma(closes, 50), signals.sma(closes, 200)
    r = signals.rsi(closes, 14)
    a = signals.atr(candles, 14)
    v = SwingView(timeframe, price, "no trade", 0, sma20=s20, sma50=s50, sma200=s200, rsi14=r, atr14=a)

    if s50 and s200:
        if price > s50 > s200:
            v.score += 2
            v.reasons.append("uptrend: price > SMA50 > SMA200")
        elif price < s50 < s200:
            v.score -= 2
            v.reasons.append("downtrend: price < SMA50 < SMA200")
        else:
            v.reasons.append("mixed trend (moving averages not aligned)")
    if s20:
        if price > s20:
            v.score += 1
            v.reasons.append("short-term strength: above SMA20")
        else:
            v.score -= 1
            v.reasons.append("short-term weakness: below SMA20")
    if r is not None:
        if r > 75:
            v.score -= 1
            v.reasons.append(f"RSI {r:.0f}: overbought, chasing here is risky")
        elif r < 30:
            v.reasons.append(f"RSI {r:.0f}: oversold, bounces possible but trend is weak")
        elif 45 <= r <= 65:
            v.score += 1
            v.reasons.append(f"RSI {r:.0f}: healthy momentum, not stretched")
    if fear_greed:
        val, label = fear_greed
        if val >= 80:
            v.score -= 1
            v.reasons.append(f"sentiment extreme greed ({val}): crowded")
        elif val <= 20:
            v.reasons.append(f"sentiment extreme fear ({val}): contrarian bounces happen, but size down")
        else:
            v.reasons.append(f"sentiment {label} ({val})")

    if v.score >= 3:
        v.stance = "long setup"
    elif v.score <= -2:
        v.stance = "avoid / downtrend"
    if a:
        v.stop = price - 2 * a
        v.target = price + 3 * a
    return v
