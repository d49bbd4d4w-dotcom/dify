"""Spots where Kalshi's own prices contradict each other ("the math is off").

Each check finds a set of contracts that pays at least $1 whatever happens, for
less than $1 after fees, i.e. a locked-in profit (arbitrage). These are rare,
small and usually gone within seconds, but they're risk-free when you get them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .model import fee_per_contract
from .sources.kalshi import Market


@dataclass
class Arb:
    kind: str  # "outcomes", "ladder" or "brackets"
    title: str
    legs: list[tuple[Market, str, float]]  # (market, side, ask price)
    cost: float  # per set, including fees
    payout: float  # guaranteed minimum payout per set
    note: str = ""

    @property
    def profit(self) -> float:
        return self.payout - self.cost

    @property
    def key(self) -> str:
        return ",".join(sorted(f"{m.ticker}:{side}" for m, side, _ in self.legs))


def _cost(prices: list[float], cfg: Config) -> float:
    return sum(p + fee_per_contract(p, cfg.fee_rate) for p in prices)


def outcome_arbs(markets: list[Market], cfg: Config, title: str, note: str = "") -> list[Arb]:
    """One market per possible outcome (e.g. a game: team A, team B, tie).
    Buying YES on every outcome pays exactly $1; buying NO on every outcome
    pays (outcomes - 1) dollars."""
    out: list[Arb] = []
    if len(markets) < 2:
        return out
    yes = [m.yes_ask for m in markets]
    if all(a is not None for a in yes):
        cost = _cost(yes, cfg)
        if 1.0 - cost >= cfg.arb_min_profit:
            out.append(Arb("outcomes", title, [(m, "yes", a) for m, a in zip(markets, yes)], cost, 1.0, note))
    no = [m.no_ask for m in markets]
    if all(a is not None for a in no):
        cost, payout = _cost(no, cfg), float(len(markets) - 1)
        if payout - cost >= cfg.arb_min_profit:
            out.append(Arb("outcomes", title, [(m, "no", a) for m, a in zip(markets, no)], cost, payout, note))
    return out


def ladder_arbs(markets: list[Market], cfg: Config, title: str) -> list[Arb]:
    """'Above $X' markets for the same moment. A higher strike can never be more
    likely than a lower one, so if YES on the lower strike plus NO on the higher
    strike costs under $1, that pair pays at least $1 whatever happens."""
    ladder = sorted((m for m in markets if m.strike_type in ("greater", "greater_or_equal")
                     and m.floor_strike is not None), key=lambda m: m.floor_strike)
    best: Arb | None = None
    for i, low in enumerate(ladder):
        for high in ladder[i + 1:]:
            if (low.yes_ask is None or high.no_ask is None or high.floor_strike == low.floor_strike
                    or low.close_time != high.close_time):  # only strikes settled at the same moment
                continue
            cost = _cost([low.yes_ask, high.no_ask], cfg)
            if 1.0 - cost >= cfg.arb_min_profit and (best is None or 1.0 - cost > best.profit):
                best = Arb("ladder", title, [(low, "yes", low.yes_ask), (high, "no", high.no_ask)], cost, 1.0)
    return [best] if best else []


def _covers_everything(markets: list[Market]) -> bool:
    """True if the markets' price ranges fit together with no gaps or overlaps
    from -infinity to +infinity, so exactly one of them must resolve YES."""
    spans = []
    for m in markets:
        if m.strike_type in ("less", "less_or_equal") and m.cap_strike is not None:
            spans.append((-math.inf, m.cap_strike))
        elif m.strike_type in ("greater", "greater_or_equal") and m.floor_strike is not None:
            spans.append((m.floor_strike, math.inf))
        elif m.strike_type == "between" and m.floor_strike is not None and m.cap_strike is not None:
            spans.append((m.floor_strike, m.cap_strike))
        else:
            return False
    spans.sort()
    if spans[0][0] != -math.inf or spans[-1][1] != math.inf:
        return False
    return all(abs(a[1] - b[0]) < 1e-6 for a, b in zip(spans, spans[1:]))


def integer_span(m: Market) -> tuple[float, float]:
    """Whole-number values a market covers, e.g. a daily high of '61° to 62°' -> (61, 62),
    '>68°' -> (69, inf), '<61°' -> (-inf, 60)."""
    st, lo, hi = m.strike_type, m.floor_strike, m.cap_strike
    if st == "greater" and lo is not None:
        return math.floor(lo) + 1, math.inf
    if st == "greater_or_equal" and lo is not None:
        return math.ceil(lo), math.inf
    if st == "less" and hi is not None:
        return -math.inf, math.ceil(hi) - 1
    if st == "less_or_equal" and hi is not None:
        return -math.inf, math.floor(hi)
    if st == "between" and lo is not None and hi is not None:
        return math.ceil(lo), math.floor(hi)
    raise ValueError(f"unsupported strike type {st!r}")


def covers_integers(markets: list[Market]) -> bool:
    """True if the markets split every whole number between them with no gaps or
    overlaps (e.g. temperature brackets), so exactly one resolves YES."""
    try:
        spans = sorted(integer_span(m) for m in markets)
    except ValueError:
        return False
    if len(spans) < 2 or spans[0][0] != -math.inf or spans[-1][1] != math.inf:
        return False
    return all(b[0] == a[1] + 1 for a, b in zip(spans, spans[1:]))


def bracket_arbs(markets: list[Market], cfg: Config, title: str) -> list[Arb]:
    """Price-range markets that together cover every possible price."""
    if len(markets) < 2 or not _covers_everything(markets):
        return []
    found = outcome_arbs(markets, cfg, title)
    for a in found:
        a.kind = "brackets"
    return found


def scan_events(markets: list[Market], cfg: Config) -> list[Arb]:
    """Ladder and bracket checks for every event in a list of crypto markets."""
    events: dict[str, list[Market]] = {}
    for m in markets:
        events.setdefault(m.event_ticker, []).append(m)
    out: list[Arb] = []
    for event, ms in events.items():
        title = ms[0].title
        if any(m.strike_type == "between" for m in ms):
            out += bracket_arbs(ms, cfg, title)
        else:
            out += ladder_arbs(ms, cfg, title)
    return out
