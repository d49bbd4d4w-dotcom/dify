"""Turns raw market + BTC data into evaluated, ranked suggestions."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import model, signals
from .config import Config
from .signals import Candle
from .sources import Http, crypto
from .sources.kalshi import Market


@dataclass
class BtcContext:
    spot: float
    spot_sources: dict[str, float]
    candles: list[Candle]  # 1-minute bars, oldest first
    sigma_per_min: float  # blended per-minute vol, after vol_multiplier
    sigma_ewma: float
    sigma_realized: float
    mom_5m: float | None
    mom_15m: float | None
    mom_60m: float | None
    book_imbalance: float | None
    fear_greed: tuple[int, str] | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class Evaluation:
    market: Market
    minutes_left: float
    strike_note: str
    p_yes: float | None
    market_mid: float | None
    best: model.Opportunity | None
    skip_reason: str | None

    @property
    def is_pick(self) -> bool:
        return self.best is not None and self.skip_reason is None


def build_context(http: Http, cfg: Config) -> BtcContext:
    warnings: list[str] = []
    candles = crypto.coinbase_candles(http, 60)
    closes = [c.close for c in candles][-cfg.vol_lookback_min:]
    rets = signals.log_returns(closes)
    s_ewma = signals.ewma_vol(rets, cfg.vol_halflife_min)
    s_real = signals.realized_vol(rets[-60:])
    sigma = 0.5 * (s_ewma + s_real) * cfg.vol_multiplier

    spots: dict[str, float] = {}
    for name, fn in (("coinbase", crypto.coinbase_ticker), ("kraken", crypto.kraken_ticker),
                     ("bitstamp", crypto.bitstamp_ticker)):
        try:
            spots[name] = fn(http)
        except Exception as e:  # one exchange being down shouldn't stop the scan
            warnings.append(f"{name} price unavailable: {e}")
    if not spots:
        spots["coinbase_last_candle"] = candles[-1].close
        warnings.append("no live ticker; using last 1m candle close")
    spot = statistics.median(spots.values())
    if len(spots) > 1 and (max(spots.values()) - min(spots.values())) / spot > 0.003:
        warnings.append("exchanges disagree by >0.3% on BTC price; data may be stale")

    imbalance = None
    try:
        bids, asks = crypto.coinbase_book(http)
        imbalance = signals.book_imbalance(bids, asks, spot)
    except Exception as e:
        warnings.append(f"order book unavailable: {e}")

    fg = None
    try:
        fg = crypto.fear_greed(http)
    except Exception as e:
        warnings.append(f"fear & greed unavailable: {e}")

    all_closes = [c.close for c in candles]
    return BtcContext(
        spot=spot, spot_sources=spots, candles=candles, sigma_per_min=sigma,
        sigma_ewma=s_ewma, sigma_realized=s_real,
        mom_5m=signals.momentum(all_closes, 5), mom_15m=signals.momentum(all_closes, 15),
        mom_60m=signals.momentum(all_closes, 60), book_imbalance=imbalance,
        fear_greed=fg, warnings=warnings,
    )


def reference_price_at(candles: list[Candle], when: datetime) -> float | None:
    """Open of the 1-minute candle covering `when`."""
    ts = int(when.timestamp()) // 60 * 60
    for c in candles:
        if c.ts == ts:
            return c.open
    return None


def evaluate_market(m: Market, ctx: BtcContext, cfg: Config, now: datetime | None = None) -> Evaluation:
    now = now or datetime.now(timezone.utc)
    minutes_left = (m.close_time - now).total_seconds() / 60 if m.close_time else -1.0

    def skip(reason: str, p: float | None = None, note: str = "") -> Evaluation:
        return Evaluation(m, minutes_left, note, p, m.mid, None, reason)

    if minutes_left < cfg.min_minutes_left:
        return skip(f"closes in {minutes_left:.1f} min (min {cfg.min_minutes_left})")
    if minutes_left > cfg.max_minutes_left:
        return skip("too far from expiry")
    if m.open_time and m.open_time > now:
        return skip("window has not started yet")

    strike_type, floor, cap = m.strike_type, m.floor_strike, m.cap_strike
    note = ""
    if floor is None and cap is None:
        # Up/down markets: the strike is BTC's price when the window opened.
        ref = reference_price_at(ctx.candles, m.open_time) if m.open_time else None
        if ref is None:
            return skip("no strike published and window-open price unknown")
        strike_type, floor = "greater", ref
        note = f"strike inferred from Coinbase price at window open (${ref:,.2f}); Kalshi settles on CF Benchmarks BRTI"

    drift = 0.0
    if cfg.momentum_weight and ctx.mom_15m is not None:
        drift = cfg.momentum_weight * ctx.mom_15m / 15.0

    t_eff = model.effective_minutes(minutes_left, cfg.settlement_avg_window_sec / 60.0)
    try:
        p_yes = model.contract_yes_probability(strike_type, floor, cap, ctx.spot,
                                               ctx.sigma_per_min, t_eff, drift)
    except ValueError as e:
        return skip(str(e), note=note)

    if m.spread is not None and m.spread > cfg.max_spread:
        return skip(f"spread {m.spread:.2f} too wide", p_yes, note)
    mid = m.mid
    if mid is not None and abs(p_yes - mid) > cfg.max_disagreement:
        return skip(f"model {p_yes:.2f} vs market {mid:.2f}: disagreement too large, "
                    "market likely knows something we don't", p_yes, note)

    opps = model.evaluate_sides(p_yes, m.yes_ask, m.no_ask, fee_rate=cfg.fee_rate,
                                bankroll=cfg.bankroll, kelly_multiplier=cfg.kelly_multiplier,
                                max_stake=cfg.max_bet())
    if not opps:
        return skip("no tradable asks", p_yes, note)
    best = opps[0]
    ev = Evaluation(m, minutes_left, note, p_yes, mid, best, None)
    if best.ev_per_contract < cfg.min_edge:
        ev.skip_reason = f"best edge {best.ev_per_contract * 100:.1f}¢ < {cfg.min_edge * 100:.0f}¢"
    elif best.contracts == 0:
        ev.skip_reason = "edge found but suggested size rounds to 0 contracts"
    return ev


def rank(evals: list[Evaluation]) -> list[Evaluation]:
    return sorted(evals, key=lambda e: (not e.is_pick,
                                        -(e.best.ev_per_contract if e.best else -1)))
