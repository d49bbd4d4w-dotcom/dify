"""Parlays built from sports legs that Kalshi prices below the sharp sportsbooks.

A parlay only pays if every leg wins, so its true chance is the product of the
legs' chances (games are independent). Parlaying legs that each have an edge
multiplies the edge, but it also multiplies the risk: a 100x payout means you
lose roughly 99 times out of 100. Every suggestion shows both numbers.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .sports import SportsEvaluation


@dataclass(frozen=True)
class Leg:
    ticker: str
    event_ticker: str
    sport_key: str
    game: str
    team: str
    p: float  # fair chance this team wins (from the sharp books)
    price: float  # Kalshi YES ask
    commence: datetime | None

    @property
    def value(self) -> float:
        """Expected return per $1 on this leg alone (1.0 = break-even)."""
        return self.p / self.price


@dataclass
class Parlay:
    kind: str  # "safer", "value", "longshot"
    legs: list[Leg]

    @property
    def chance(self) -> float:
        return math.prod(leg.p for leg in self.legs)

    @property
    def payout(self) -> float:
        """Total payout per $1 staked if Kalshi prices the parlay like its legs."""
        return 1.0 / math.prod(leg.price for leg in self.legs)

    @property
    def value(self) -> float:
        """Expected return per $1 staked (1.0 = break-even)."""
        return self.chance * self.payout

    def min_payout(self, margin: float) -> float:
        """Smallest payout (per $1) still worth taking."""
        return (1.0 + margin) / self.chance

    @property
    def key(self) -> str:
        return ",".join(sorted(leg.ticker for leg in self.legs))


TITLES = {"safer": "SAFER PARLAY", "value": "VALUE PARLAY", "longshot": "LONG-SHOT PARLAY"}


def title(kind: str) -> str:
    if kind.startswith("big"):
        return f"BIG HITTER · {kind[3:]}x+"
    return TITLES.get(kind, kind.upper())


def legs_from(evaluations: list[SportsEvaluation], cfg: Config) -> list[Leg]:
    """Best qualifying leg per game: passed all safety checks and priced below fair."""
    best: dict[str, Leg] = {}
    for e in evaluations:
        m = e.market
        if not e.checked or e.p_yes is None or m.yes_ask is None:
            continue
        if e.p_yes - m.yes_ask < cfg.parlay_min_leg_edge:
            continue
        leg = Leg(m.ticker, m.event_ticker, e.sport_key, e.game, e.team, e.p_yes, m.yes_ask, e.commence)
        if m.event_ticker not in best or leg.value > best[m.event_ticker].value:
            best[m.event_ticker] = leg  # one leg per game keeps the legs independent
    return list(best.values())


def build(legs: list[Leg], cfg: Config) -> list[Parlay]:
    """Up to three tickets: a safer one (favorites), a value one, and a long shot."""
    out: list[Parlay] = []
    by_value = sorted(legs, key=lambda leg: leg.value, reverse=True)

    favorites = sorted((leg for leg in legs if leg.p >= cfg.parlay_safe_min_leg_prob),
                       key=lambda leg: leg.p, reverse=True)
    if len(favorites) >= 2:
        out.append(Parlay("safer", favorites[:3]))

    # Value: the best-priced legs that are close to a coin flip or better,
    # stopping before the chance of hitting drops below ~10%.
    value: list[Leg] = []
    for leg in (x for x in by_value if x.p >= cfg.parlay_value_min_leg_prob):
        if len(value) == 4 or math.prod(x.p for x in value + [leg]) < cfg.parlay_value_min_chance:
            break
        value.append(leg)
    if len(value) >= 2:
        out.append(Parlay("value", value))

    shot: list[Leg] = []
    for leg in by_value[:cfg.parlay_max_legs]:
        shot.append(leg)
        if 1.0 / math.prod(x.price for x in shot) >= cfg.parlay_longshot_payout:
            out.append(Parlay("longshot", shot))
            break

    seen: set[str] = set()
    good = []
    for p in out:
        if p.key in seen or p.value < 1.0 + cfg.parlay_min_value_margin:
            continue
        seen.add(p.key)
        good.append(p)
    return good


def big_hitters(legs: list[Leg], cfg: Config) -> list[Parlay]:
    """For each payout size (e.g. 25x, 50x, 100x), the combination of legs most
    likely to hit. Checks every combination of the best-priced legs, rather than
    just stacking underdogs, which is the riskiest way to reach a big payout."""
    pool = sorted(legs, key=lambda leg: leg.value, reverse=True)[:cfg.parlay_candidate_legs]
    combos = []
    for k in range(2, min(cfg.parlay_max_legs, len(pool)) + 1):
        for legs_k in itertools.combinations(pool, k):
            p = Parlay("", list(legs_k))
            if p.value >= 1.0 + cfg.parlay_min_value_margin:
                combos.append((p.chance, p.value, p.payout, p))
    out: list[Parlay] = []
    seen: set[str] = set()
    for target in sorted(cfg.big_hitter_payouts):
        fits = [c for c in combos if c[2] >= target and c[3].key not in seen]
        if not fits:
            continue
        best = max(fits, key=lambda c: (c[0], c[1]))[3]
        seen.add(best.key)
        out.append(Parlay(f"big{target:g}", best.legs))
    return out


def kelly_stake(p: Parlay, cfg: Config) -> tuple[float, float]:
    """(suggested stake, what the math alone says) for this parlay, from cfg.bankroll.

    Kelly sizing bets more when the edge is big and the chance is high, and very
    little on long shots: that is what grows a bankroll fastest without going broke.
    """
    b = p.payout - 1.0
    f = (p.value - 1.0) / b if b > 0 else 0.0
    raw = max(0.0, cfg.bankroll * cfg.kelly_multiplier * f)
    if raw <= 0:
        return 0.0, 0.0
    return round(min(max(raw, cfg.parlay_min_stake), cfg.max_bet()), 2), raw
