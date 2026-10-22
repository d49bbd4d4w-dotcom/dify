"""Kalshi daily high-temperature markets vs. the National Weather Service and
the GFS / ECMWF weather models.

How a probability is built for each temperature bracket:
1. Center: the NWS forecast high (it already corrects for the local station),
   blended with the average of ~80 model runs.
2. Spread: how much the model runs disagree, widened a bit (raw model runs are
   known to be overconfident), and never narrower than typical forecast error
   for that lead time.
3. Today only: the final high is the larger of what the station has already
   recorded and the warmest it still gets. Brackets below the recorded high get
   0%, and after mid-afternoon the "still to come" part fades as the day cools.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import arbs, model
from .config import Config
from .sources import Http
from .sources.kalshi import Kalshi, Market
from .sources.weather import STATIONS, Station, Weather
from .sports import event_date

# Typical error of a day-ahead high-temperature forecast, by days ahead (°F).
MIN_SD = {0: 1.5, 1: 2.3, 2: 3.0}
PEAK_HOUR = 15.0  # highs usually happen around 3pm local time...
COOLING_PER_HOUR = 2.5  # ...after which the rest of the day runs this much cooler (°F/hour)


@dataclass
class Forecast:
    probs: dict[int, float]  # high temperature (°F) -> probability
    center: float
    sd: float
    nws_high: int | None
    model_mean: float | None
    model_runs: int
    observed_high: int | None  # highest whole °F already recorded today


def climate_day(day: date, tz: str) -> tuple[datetime, datetime]:
    """NWS climate reports use local *standard* time, so in summer the day runs
    1am to 1am on the clock."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz))
    start = midnight + (midnight.dst() or timedelta(0))
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def build_forecast(nws_high: int | None, runs: list[float], observed: int | None, lead_days: int,
                   cfg: Config, local_hour: float | None = None) -> tuple[Forecast | None, str]:
    model_mean = statistics.fmean(runs) if runs else None
    if nws_high is None and len(runs) < 10:
        return None, "not enough forecast data"
    if nws_high is not None and model_mean is not None and abs(nws_high - model_mean) > cfg.weather_max_source_gap:
        return None, (f"NWS ({nws_high}°) and the weather models ({model_mean:.0f}°) disagree too much; "
                      "the forecast is too uncertain")
    if nws_high is not None and model_mean is not None:
        center = 0.6 * nws_high + 0.4 * model_mean
    else:
        center = nws_high if nws_high is not None else model_mean
    spread = statistics.pstdev(runs) * cfg.weather_spread_inflation if len(runs) >= 10 else 0.0
    sd = max(spread, MIN_SD.get(lead_days, 3.5))

    # Warmest temperature still to come today: the forecast high, cooling off
    # after the usual afternoon peak.
    rest = center
    if lead_days == 0 and local_hour is not None:
        rest -= COOLING_PER_HOUR * max(0.0, local_hour - PEAK_HOUR)
    lo, hi = math.floor(rest - 6 * sd), math.ceil(rest + 6 * sd)
    probs = {k: model.norm_cdf((k + 0.5 - rest) / sd) - model.norm_cdf((k - 0.5 - rest) / sd)
             for k in range(lo, hi + 1)}
    floor_f = observed
    if floor_f is not None:
        # Final high = max(recorded so far, still to come): every outcome at or
        # below the recorded high collapses onto it.
        at_floor = sum(p for k, p in probs.items() if k <= floor_f)
        probs = {k: p for k, p in probs.items() if k > floor_f}
        probs[floor_f] = at_floor if at_floor > 0 or probs else 1.0
    total = sum(probs.values())
    probs = {k: p / total for k, p in probs.items()}
    return Forecast(probs, rest, sd, nws_high, model_mean, len(runs), floor_f), ""


def bracket_probability(m: Market, probs: dict[int, float]) -> float:
    lo, hi = arbs.integer_span(m)
    return sum(p for k, p in probs.items() if lo <= k <= hi)


@dataclass
class WeatherEvaluation:
    market: Market
    station: Station
    day: date | None
    forecast: Forecast | None
    p_yes: float | None
    best: model.Opportunity | None
    skip_reason: str | None

    @property
    def market_mid(self) -> float | None:
        return self.market.mid

    @property
    def minutes_left(self) -> float:
        if not self.market.close_time:
            return 0.0
        return (self.market.close_time - datetime.now(timezone.utc)).total_seconds() / 60

    @property
    def is_pick(self) -> bool:
        return self.best is not None and self.skip_reason is None


def evaluate(m: Market, st: Station, day: date | None, fc: Forecast | None, cfg: Config,
             now: datetime, reason: str = "") -> WeatherEvaluation:
    out = WeatherEvaluation(m, st, day, fc, None, None, None)
    if reason or fc is None:
        out.skip_reason = reason or "no forecast"
        return out
    if m.close_time and m.close_time <= now:
        out.skip_reason = "market closed"
        return out
    try:
        out.p_yes = bracket_probability(m, fc.probs)
    except ValueError as e:
        out.skip_reason = str(e)
        return out
    if m.spread is not None and m.spread > cfg.weather_max_spread:
        out.skip_reason = f"Kalshi spread {m.spread:.2f} too wide"
    elif m.mid is not None and abs(out.p_yes - m.mid) > cfg.weather_max_disagreement:
        out.skip_reason = (f"forecast {out.p_yes:.0%} vs Kalshi {m.mid:.0%}: gap too large, "
                           "the station or forecast may be off")
    if out.skip_reason:
        return out
    opps = model.evaluate_sides(out.p_yes, m.yes_ask, m.no_ask, fee_rate=cfg.fee_rate, bankroll=cfg.bankroll,
                                kelly_multiplier=cfg.kelly_multiplier, max_stake=cfg.max_bet())
    if not opps:
        out.skip_reason = "no tradable Kalshi prices"
        return out
    out.best = opps[0]
    if out.best.ev_per_contract < cfg.weather_min_edge:
        out.skip_reason = (f"best edge {out.best.ev_per_contract * 100:.1f}¢ "
                           f"< {cfg.weather_min_edge * 100:.0f}¢")
    elif out.best.contracts == 0:
        out.skip_reason = "edge found but suggested size rounds to 0 contracts"
    return out


@dataclass
class WeatherScan:
    evaluations: list[WeatherEvaluation] = field(default_factory=list)
    arbs: list[arbs.Arb] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def collect(http: Http, cfg: Config, now: datetime | None = None) -> WeatherScan:
    now = now or datetime.now(timezone.utc)
    scan = WeatherScan()
    kalshi, wx = Kalshi(http), Weather(http)
    for series in cfg.weather_series:
        st = STATIONS.get(series)
        if st is None:
            scan.warnings.append(f"{series}: settlement station unknown, skipped")
            continue
        try:
            markets = kalshi.open_markets(series)
        except Exception as e:
            scan.warnings.append(f"Kalshi {series}: {e}")
            continue
        events: dict[str, list[Market]] = {}
        for m in markets:
            events.setdefault(m.event_ticker, []).append(m)
        today = now.astimezone(ZoneInfo(st.tz)).date()
        for event_ticker, ms in events.items():
            day = event_date(event_ticker)
            title = f"{st.name} high on {day:%a %b %d}" if day else ms[0].title
            if arbs.covers_integers(ms):
                scan.arbs += arbs.outcome_arbs(ms, cfg, title)
            lead = (day - today).days if day else None
            if lead is None or lead < 0 or lead > cfg.weather_max_lead_days:
                reason = "day already over" if lead is not None and lead < 0 else "too far ahead"
                scan.evaluations += [evaluate(m, st, day, None, cfg, now, reason) for m in ms]
                continue
            nws_high, runs, observed = None, [], None
            try:
                nws_high = wx.nws_high(st, day)
            except Exception as e:
                scan.warnings.append(f"NWS forecast for {st.name}: {e}")
            try:
                runs = wx.ensemble_highs(st, day)
            except Exception as e:
                scan.warnings.append(f"weather models for {st.name}: {e}")
            if lead == 0:
                start, end = climate_day(day, st.tz)
                try:
                    observed = wx.observed_high(st, start, min(end, now))
                except Exception as e:
                    scan.warnings.append(f"NWS observations for {st.station_id}: {e}")
            local = now.astimezone(ZoneInfo(st.tz))
            fc, reason = build_forecast(nws_high, runs, observed, lead, cfg, local.hour + local.minute / 60)
            scan.evaluations += [evaluate(m, st, day, fc, cfg, now, reason) for m in ms]
    scan.evaluations.sort(key=lambda e: (not e.is_pick, -(e.best.ev_per_contract if e.best else -1)))
    return scan
