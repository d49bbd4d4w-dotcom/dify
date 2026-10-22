"""Pricing math for Kalshi binary BTC contracts.

Everything in here is pure (no network) so it can be unit-tested and reasoned
about. The core idea: estimate the probability that BTC settles above/below a
strike using recent realized volatility, then compare that "fair" probability
with what Kalshi is charging, after fees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def kalshi_fee(contracts: int, price: float, rate: float = 0.07) -> float:
    """Kalshi taker fee in dollars: rate * C * P * (1 - P), rounded up to the cent."""
    if contracts <= 0:
        return 0.0
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 100 - 1e-9) / 100


def fee_per_contract(price: float, rate: float = 0.07) -> float:
    """Unrounded per-contract fee, used for edge math (rounding is per order)."""
    return rate * price * (1.0 - price)


def effective_minutes(minutes_left: float, avg_window_min: float) -> float:
    """Variance horizon (in minutes) for a contract settled on an *average* price.

    Kalshi's BTC contracts settle on the average of the CF Benchmarks BRTI over
    the final 60 seconds, not a single tick. Averaging a random walk over the
    last `w` minutes gives variance sigma^2 * (T - 2w/3) instead of sigma^2 * T.
    """
    if minutes_left <= 0:
        return 0.0
    w = max(avg_window_min, 0.0)
    if w == 0 or minutes_left >= w:
        return minutes_left - 2.0 * w / 3.0
    # Already inside the averaging window: only the unobserved tail remains.
    return minutes_left**3 / (3.0 * w * w)


def prob_above(spot: float, strike: float, sigma_per_min: float, minutes: float,
               drift_per_min: float = 0.0) -> float:
    """P(price at horizon > strike) under a driftless-ish lognormal random walk."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if minutes <= 0 or sigma_per_min <= 0:
        if spot == strike:
            return 0.5
        return 1.0 if spot > strike else 0.0
    sd = sigma_per_min * math.sqrt(minutes)
    z = (math.log(spot / strike) + drift_per_min * minutes) / sd
    return norm_cdf(z)


def contract_yes_probability(strike_type: str, floor: float | None, cap: float | None,
                             spot: float, sigma_per_min: float, minutes: float,
                             drift_per_min: float = 0.0) -> float:
    """Probability that a Kalshi contract with the given strike structure resolves YES."""
    def above(k: float) -> float:
        return prob_above(spot, k, sigma_per_min, minutes, drift_per_min)

    st = (strike_type or "").lower()
    if st in ("greater", "greater_or_equal"):
        if floor is None:
            raise ValueError("greater-type market without floor_strike")
        return above(floor)
    if st in ("less", "less_or_equal"):
        if cap is None:
            raise ValueError("less-type market without cap_strike")
        return 1.0 - above(cap)
    if st == "between":
        if floor is None or cap is None:
            raise ValueError("between market needs floor and cap strikes")
        return max(0.0, above(floor) - above(cap))
    raise ValueError(f"unsupported strike_type: {strike_type!r}")


@dataclass
class Opportunity:
    side: str  # "yes" or "no"
    price: float  # ask price paid per contract, dollars
    win_prob: float  # model probability this side wins
    fee_per_contract: float
    ev_per_contract: float  # expected profit per contract after fees, dollars
    kelly_fraction: float  # full-Kelly fraction of bankroll
    stake: float  # suggested dollars to spend (after fractional Kelly + cap)
    contracts: int

    @property
    def edge_pct(self) -> float:
        cost = self.price + self.fee_per_contract
        return self.ev_per_contract / cost if cost > 0 else 0.0


def size_position(win_prob: float, cost: float, bankroll: float, kelly_multiplier: float,
                  max_stake: float, price: float) -> tuple[float, float, int]:
    """Return (full_kelly_fraction, stake_dollars, contracts) for a binary bet."""
    if cost >= 1.0 or cost <= 0:
        return 0.0, 0.0, 0
    f = max(0.0, (win_prob - cost) / (1.0 - cost))
    stake = min(bankroll * kelly_multiplier * f, max_stake)
    contracts = int(stake // price) if price > 0 else 0
    return f, round(contracts * price, 2), contracts


def evaluate_sides(p_yes: float, yes_ask: float | None, no_ask: float | None, *,
                   fee_rate: float, bankroll: float, kelly_multiplier: float,
                   max_stake: float) -> list[Opportunity]:
    """Score buying YES at the yes ask and buying NO at the no ask."""
    out: list[Opportunity] = []
    for side, ask, q in (("yes", yes_ask, p_yes), ("no", no_ask, 1.0 - p_yes)):
        if ask is None or not (0.0 < ask < 1.0):
            continue
        fee = fee_per_contract(ask, fee_rate)
        cost = ask + fee
        ev = q - cost
        f, stake, contracts = size_position(q, cost, bankroll, kelly_multiplier, max_stake, ask)
        out.append(Opportunity(side, ask, q, fee, ev, f, stake, contracts))
    out.sort(key=lambda o: o.ev_per_contract, reverse=True)
    return out
