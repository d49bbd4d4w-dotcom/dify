"""Kalshi sports game-winner markets vs. sharp sportsbook prices.

The bot does not try to predict who wins. It takes the prices of sharp
sportsbooks (Pinnacle first, otherwise a consensus of several books), removes
the bookmaker margin ("vig") to get fair win probabilities, and flags Kalshi
contracts priced cheaper than that, after Kalshi's fee.
"""

from __future__ import annotations

import re
import statistics
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import model
from .config import Config
from .sources import Http
from .sources.kalshi import Kalshi, Market
from .sources.odds_api import OddsApi

EASTERN = ZoneInfo("America/New_York")

# Middle part of a Kalshi game series ticker (KX<code>GAME / FIGHT / MATCH)
# -> The Odds API sport key prefixes.
SPORT_MAP: dict[str, list[str]] = {
    "NFL": ["americanfootball_nfl"],
    "NCAAF": ["americanfootball_ncaaf"],
    "NBA": ["basketball_nba"],
    "WNBA": ["basketball_wnba"],
    "NCAAMB": ["basketball_ncaab"],
    "NCAAWB": ["basketball_wncaab"],
    "MLB": ["baseball_mlb"],
    "NHL": ["icehockey_nhl"],
    "EPL": ["soccer_epl"],
    "UCL": ["soccer_uefa_champs_league"],
    "UEL": ["soccer_uefa_europa_league"],
    "LALIGA": ["soccer_spain_la_liga"],
    "SERIEA": ["soccer_italy_serie_a"],
    "BUNDESLIGA": ["soccer_germany_bundesliga"],
    "LIGUE1": ["soccer_france_ligue_one"],
    "MLS": ["soccer_usa_mls"],
    "UFC": ["mma_mixed_martial_arts"],
    "BOXING": ["boxing_boxing"],
    "ATP": ["tennis_atp"],
    "WTA": ["tennis_wta"],
}
SERIES_SUFFIXES = ("GAME", "FIGHT", "MATCH")
DEFAULT_SERIES = ([f"KX{k}GAME" for k in SPORT_MAP if k not in ("UFC", "BOXING", "ATP", "WTA")]
                  + ["KXUFCFIGHT", "KXBOXINGFIGHT", "KXATPMATCH", "KXWTAMATCH"])

ICONS = {"americanfootball": "🏈", "basketball": "🏀", "baseball": "⚾", "icehockey": "🏒",
         "soccer": "⚽", "mma": "🥊", "boxing": "🥊", "tennis": "🎾"}


def series_code(series_ticker: str) -> str | None:
    """KXNBAGAME -> NBA. None if it isn't a game-winner series."""
    t = series_ticker.upper()
    if not t.startswith("KX"):
        return None
    for suffix in SERIES_SUFFIXES:
        if t.endswith(suffix) and len(t) > 2 + len(suffix):
            return t[2:-len(suffix)]
    return None


def odds_keys_for(series_ticker: str, active: list[str]) -> list[str]:
    prefixes = SPORT_MAP.get(series_code(series_ticker) or "", [])
    return [k for k in active if any(k == p or k.startswith(p + "_") for p in prefixes)]


def icon(sport_key: str) -> str:
    return ICONS.get(sport_key.split("_", 1)[0], "🎯")


# ---------- fair prices from sportsbooks ----------

def devig(decimal_odds: dict[str, float]) -> dict[str, float]:
    """Remove the bookmaker margin by normalizing implied probabilities to 100%."""
    inv = {k: 1.0 / v for k, v in decimal_odds.items() if v and v > 1.0}
    total = sum(inv.values())
    return {k: x / total for k, x in inv.items()} if total else {}


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class FairPrice:
    probs: dict[str, float]  # outcome name -> fair probability
    source: str
    books: int
    book_range: float  # biggest disagreement between books on any outcome


def fair_price(event: dict, cfg: Config, now: datetime) -> FairPrice | None:
    outcomes = {event["home_team"], event["away_team"]}
    per_book: dict[str, dict[str, float]] = {}
    max_age = timedelta(minutes=cfg.sports_max_quote_age_min)
    for b in event.get("bookmakers", []):
        for mk in b.get("markets", []):
            if mk.get("key") != "h2h":
                continue
            updated = _parse_time(mk.get("last_update") or b.get("last_update"))
            if updated and now - updated > max_age:
                continue  # stale quote
            prices = {o["name"]: float(o["price"]) for o in mk.get("outcomes", [])}
            if not outcomes <= set(prices):
                continue
            per_book[b["key"]] = devig(prices)
    if not per_book:
        return None
    names = set.intersection(*(set(p) for p in per_book.values()))
    book_range = max(max(p[n] for p in per_book.values()) - min(p[n] for p in per_book.values())
                     for n in names)
    if "pinnacle" in per_book:
        return FairPrice(per_book["pinnacle"], "Pinnacle, no-vig", len(per_book), book_range)
    if len(per_book) < cfg.sports_min_books:
        return None
    med = {n: statistics.median(p[n] for p in per_book.values()) for n in names}
    total = sum(med.values())
    return FairPrice({n: v / total for n, v in med.items()},
                     f"consensus of {len(per_book)} books, no-vig", len(per_book), book_range)


# ---------- matching Kalshi games to sportsbook games ----------

_STOP = {"fc", "cf", "sc", "afc", "the", "club", "de", "cd", "ac", "winner"}
_ALIAS = {"st": "state", "la": "los angeles", "ny": "new york", "man": "manchester",
          "utd": "united", "intl": "international", "u": "university"}
_TIE = {"tie", "draw"}


def tokens(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s.replace("&", " and "))
    out: list[str] = []
    for t in s.split():
        out += _ALIAS.get(t, t).split()
    return [t for t in out if t not in _STOP]


def name_matches(label: str, team: str) -> bool:
    """'Los Angeles L' ~ 'Los Angeles Lakers', 'Man City' ~ 'Manchester City'."""
    a, b = tokens(label), tokens(team)
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    # Each word must match a different word on the other side: exact matches
    # first, then abbreviations ("L" -> "Lakers"), so "L" can't reuse "Los".
    remaining = list(long_)
    pending = []
    for t in short:
        if t in remaining:
            remaining.remove(t)
        else:
            pending.append(t)
    for t in sorted(pending, key=len, reverse=True):
        hit = next((u for u in remaining if u.startswith(t)), None)
        if hit is None:
            return False
        remaining.remove(hit)
    return True


_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def event_date(event_ticker: str) -> date | None:
    """KXNFLGAME-25OCT26BUFCAR -> 2025-10-26."""
    m = _DATE_RE.search(event_ticker)
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def match_event(labels: list[str], game_date: date | None, events: list[dict],
                now: datetime) -> tuple[dict | None, dict[str, str], str]:
    """Find the sportsbook event for a Kalshi game. Returns (event, label->outcome, reason)."""
    found = []
    for ev in events:
        start = _parse_time(ev.get("commence_time"))
        if start is None:
            continue
        if game_date is not None:
            if abs((start.astimezone(EASTERN).date() - game_date).days) > 1:
                continue
        elif not (now - timedelta(hours=6) <= start <= now + timedelta(days=7)):
            continue
        teams = [ev["home_team"], ev["away_team"]]
        mapping: dict[str, str] = {}
        for label in labels:
            if label.strip().lower() in _TIE:
                mapping[label] = "Draw"
                continue
            hits = [t for t in teams if name_matches(label, t)]
            if len(hits) != 1:
                break
            mapping[label] = hits[0]
        else:
            team_hits = [v for v in mapping.values() if v != "Draw"]
            if team_hits and len(set(team_hits)) == len(team_hits):
                found.append((ev, mapping))
    if len(found) == 1:
        return found[0][0], found[0][1], ""
    if not found:
        return None, {}, "no matching sportsbook game"
    return None, {}, "matches more than one sportsbook game (e.g. doubleheader)"


# ---------- evaluation ----------

@dataclass
class SportsEvaluation:
    market: Market
    sport_key: str
    game: str
    team: str
    commence: datetime | None
    fair_source: str
    p_yes: float | None
    best: model.Opportunity | None
    skip_reason: str | None
    # True once the game passed every safety check (fresh prices, not started,
    # books agree...), even if the edge was too small for a single pick.
    checked: bool = False

    @property
    def market_mid(self) -> float | None:
        return self.market.mid

    @property
    def minutes_left(self) -> float:
        """Minutes until the game starts (negative once it has started)."""
        if not self.commence:
            return 0.0
        return (self.commence - datetime.now(timezone.utc)).total_seconds() / 60

    @property
    def is_pick(self) -> bool:
        return self.best is not None and self.skip_reason is None


def evaluate(m: Market, sport_key: str, game: str, team: str, outcome: str | None,
             ev: dict | None, fair: FairPrice | None, cfg: Config, now: datetime,
             reason: str = "") -> SportsEvaluation:
    commence = _parse_time(ev.get("commence_time")) if ev else None
    out = SportsEvaluation(m, sport_key, game, team, commence, fair.source if fair else "",
                           None, None, None)
    if reason:
        out.skip_reason = reason
        return out
    if fair is None:
        out.skip_reason = "not enough fresh sportsbook prices"
        return out
    if outcome not in fair.probs:
        out.skip_reason = f"sportsbooks don't price '{outcome}'"
        return out
    out.p_yes = fair.probs[outcome]
    if commence and commence <= now and not cfg.sports_include_live:
        out.skip_reason = "game already started"
    elif commence and commence - now > timedelta(days=cfg.sports_max_days_ahead):
        out.skip_reason = "game is too far away"
    elif fair.book_range > cfg.sports_max_book_spread:
        out.skip_reason = f"sportsbooks disagree by {fair.book_range:.0%}; prices may be moving"
    elif m.spread is not None and m.spread > cfg.sports_max_spread:
        out.skip_reason = f"Kalshi spread {m.spread:.2f} too wide"
    elif m.mid is not None and abs(out.p_yes - m.mid) > cfg.sports_max_disagreement:
        out.skip_reason = (f"books {out.p_yes:.0%} vs Kalshi {m.mid:.0%}: gap too large, "
                           "likely news or a matching problem")
    if out.skip_reason:
        return out
    out.checked = True
    opps = model.evaluate_sides(out.p_yes, m.yes_ask, m.no_ask, fee_rate=cfg.fee_rate,
                                bankroll=cfg.bankroll, kelly_multiplier=cfg.kelly_multiplier,
                                max_stake=cfg.max_stake)
    if not opps:
        out.skip_reason = "no tradable Kalshi prices"
        return out
    out.best = opps[0]
    if out.best.ev_per_contract < cfg.sports_min_edge:
        out.skip_reason = (f"best edge {out.best.ev_per_contract * 100:.1f}¢ "
                           f"< {cfg.sports_min_edge * 100:.0f}¢")
    elif out.best.contracts == 0:
        out.skip_reason = "edge found but suggested size rounds to 0 contracts"
    return out


@dataclass
class SportsScan:
    evaluations: list[SportsEvaluation] = field(default_factory=list)
    credits_remaining: float | None = None
    credits_used: float = 0.0
    warnings: list[str] = field(default_factory=list)


def discover_series(kalshi: Kalshi, cfg: Config) -> list[str]:
    if cfg.sports_series:
        return list(cfg.sports_series)
    try:
        found = [s["ticker"] for s in kalshi.series("Sports") if series_code(s.get("ticker", ""))]
        mapped = [t for t in found if series_code(t) in SPORT_MAP]
        if mapped:
            return sorted(set(mapped) | set(DEFAULT_SERIES))
    except Exception as e:
        print(f"[warn] Kalshi series lookup failed, using built-in list: {e}", file=sys.stderr)
    return list(DEFAULT_SERIES)


def collect(http: Http, cfg: Config, now: datetime | None = None) -> SportsScan:
    now = now or datetime.now(timezone.utc)
    scan = SportsScan()
    kalshi = Kalshi(http)
    odds = OddsApi(http, cfg.odds_api_key)
    active = odds.active_sports()

    # Kalshi side: open game markets grouped by game (event).
    games: dict[str, tuple[str, list[Market]]] = {}
    for series in discover_series(kalshi, cfg):
        if not odds_keys_for(series, active):
            continue  # out of season at the sportsbooks, or unsupported
        try:
            markets = kalshi.open_markets(series)
        except Exception as e:
            scan.warnings.append(f"Kalshi {series}: {e}")
            continue
        for m in markets:
            games.setdefault(m.event_ticker, (series, []))[1].append(m)

    # Sportsbook side: one odds request per sport that has Kalshi games.
    needed = sorted({k for series, _ in games.values() for k in odds_keys_for(series, active)})
    events_by_key: dict[str, list[dict]] = {}
    for key in needed:
        try:
            res = odds.moneyline_odds(key, cfg.sports_bookmakers)
        except Exception as e:
            scan.warnings.append(f"odds for {key}: {e}")
            continue
        events_by_key[key] = res.events
        scan.credits_used += res.credits_used
        if res.credits_remaining is not None:
            scan.credits_remaining = res.credits_remaining

    for event_ticker, (series, markets) in games.items():
        keys = [k for k in odds_keys_for(series, active) if k in events_by_key]
        candidates = [(k, ev) for k in keys for ev in events_by_key[k]]
        labels = [m.subtitle for m in markets if m.subtitle]
        game = re.sub(r"\s*Winner\??$", "", markets[0].title).strip()
        ev, mapping, reason = match_event(labels, event_date(event_ticker), [ev for _, ev in candidates], now)
        sport_key = next((k for k, e in candidates if e is ev), keys[0] if keys else "")
        fair = fair_price(ev, cfg, now) if ev else None
        for m in markets:
            scan.evaluations.append(evaluate(m, sport_key, game, m.subtitle, mapping.get(m.subtitle),
                                             ev, fair, cfg, now, reason))
    scan.evaluations.sort(key=lambda e: (not e.is_pick, -(e.best.ev_per_contract if e.best else -1)))
    return scan


def paced_interval_minutes(base: float, credits_remaining: float | None, credits_per_scan: float,
                           now: datetime) -> float:
    """Spread the remaining Odds API credits evenly over the rest of the month."""
    if credits_remaining is None or credits_per_scan <= 0:
        return base
    nxt = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)).replace(day=1)
    minutes_left = (nxt - now).total_seconds() / 60
    scans_left = credits_remaining / credits_per_scan
    if scans_left < 1:
        return minutes_left
    return max(base, minutes_left / scans_left)
