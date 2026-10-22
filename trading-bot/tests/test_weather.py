import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tradebot import arbs, cli, model, notify, weather
from tradebot.config import Config
from tradebot.sources.kalshi import Market
from tradebot.tracker import Tracker

NY = ZoneInfo("America/New_York")
NOW = datetime.now(timezone.utc).replace(microsecond=0)
TODAY = NOW.astimezone(NY).date()
TOMORROW = TODAY + timedelta(days=1)


def code(d):
    return d.strftime("%y%b%d").upper()


def bracket(event, suffix, strike_type, yes_bid, yes_ask, floor=None, cap=None, label=""):
    raw = {"ticker": f"{event}-{suffix}", "event_ticker": event, "title": "Highest temperature in NYC",
           "yes_sub_title": label, "status": "active", "strike_type": strike_type,
           "close_time": (NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
           "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}"}
    if floor is not None:
        raw["floor_strike"] = floor
    if cap is not None:
        raw["cap_strike"] = cap
    return raw


T_EV, M_EV = f"KXHIGHNY-{code(TODAY)}", f"KXHIGHNY-{code(TOMORROW)}"
KALSHI = [
    # Today: the station already hit ~64.9°F, so "61° to 62°" can't happen, yet YES sells at 10c.
    bracket(T_EV, "B60", "less", 0.02, 0.04, cap=61, label="60° or below"),
    bracket(T_EV, "B61", "between", 0.08, 0.10, 61, 62, "61° to 62°"),
    bracket(T_EV, "B63", "between", 0.45, 0.47, 63, 64, "63° to 64°"),
    bracket(T_EV, "B65", "between", 0.38, 0.40, 65, 66, "65° to 66°"),
    bracket(T_EV, "T66", "greater", 0.04, 0.06, floor=66, label="67° or above"),
    # Tomorrow: forecasts center near 69.6°F, but "69° to 70°" sells at 15c.
    bracket(M_EV, "B66", "less", 0.10, 0.12, cap=67, label="66° or below"),
    bracket(M_EV, "B67", "between", 0.25, 0.27, 67, 68, "67° to 68°"),
    bracket(M_EV, "B69", "between", 0.13, 0.15, 69, 70, "69° to 70°"),
    bracket(M_EV, "B71", "between", 0.25, 0.27, 71, 72, "71° to 72°"),
    bracket(M_EV, "T72", "greater", 0.10, 0.12, floor=72, label="73° or above"),
]


def ensemble(center_today, center_tomorrow, n=20, spread=1.2):
    days = [TODAY.isoformat(), TOMORROW.isoformat(), (TOMORROW + timedelta(days=1)).isoformat()]
    offsets = [spread * (i - (n - 1) / 2) / ((n - 1) / 2) for i in range(n)]
    daily = {"time": days}
    for i, off in enumerate(offsets):
        daily[f"temperature_2m_max_member{i + 1:02d}"] = [center_today + off, center_tomorrow + off, None]
    return {"daily": daily}


class FakeHttp:
    def __init__(self, nws_tomorrow=70, models_tomorrow=69.0, obs_c=(15.0, 18.3, 17.0)):
        self.nws_tomorrow, self.models_tomorrow, self.obs_c = nws_tomorrow, models_tomorrow, obs_c
        self.posts = []
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append(url)
        if "/points/" in url:
            return {"properties": {"forecast": "https://api.weather.gov/gridpoints/OKX/33,37/forecast"}}
        if url.endswith("/forecast"):
            return {"properties": {"periods": [
                {"isDaytime": True, "startTime": f"{TODAY}T06:00:00-04:00", "temperature": 64, "temperatureUnit": "F"},
                {"isDaytime": False, "startTime": f"{TODAY}T18:00:00-04:00", "temperature": 55, "temperatureUnit": "F"},
                {"isDaytime": True, "startTime": f"{TOMORROW}T06:00:00-04:00", "temperature": self.nws_tomorrow,
                 "temperatureUnit": "F"}]}}
        if "/observations" in url:
            assert url.endswith("/stations/KNYC/observations")
            return {"features": [{"properties": {"temperature": {"value": c}}} for c in self.obs_c]
                    + [{"properties": {"temperature": {"value": None}}}]}
        if "ensemble-api.open-meteo.com" in url:
            assert params["temperature_unit"] == "fahrenheit"
            return ensemble(64.0, self.models_tomorrow)
        if url.endswith("/markets"):
            return {"markets": KALSHI if params.get("series_ticker") == "KXHIGHNY" else [], "cursor": ""}
        raise AssertionError(f"unexpected url {url}")

    def post_json(self, url, payload):
        self.posts.append(payload)


def cfg(**kw):
    kw.setdefault("weather_series", ["KXHIGHNY"])
    return Config(**kw)


def test_integer_spans_and_coverage():
    ms = [Market.from_api(m) for m in KALSHI[:5]]
    spans = [arbs.integer_span(m) for m in ms]
    assert spans == [(-math.inf, 60), (61, 62), (63, 64), (65, 66), (67, math.inf)]
    assert arbs.covers_integers(ms)
    assert not arbs.covers_integers(ms[:2] + ms[3:])  # 63-64 missing
    half = Market.from_api(bracket("E", "X", "greater", 0.1, 0.2, floor=68.5))
    assert arbs.integer_span(half) == (69, math.inf)


def test_celsius_readings_map_back_to_whole_fahrenheit():
    from tradebot.sources.weather import lowest_f
    for f in range(-10, 111):  # stations record whole °F and report °C to a tenth
        assert lowest_f(round((f - 32) / 1.8, 1)) == f
    assert lowest_f(17.0) == 62  # whole °C: 16.5-17.5°C could be 62°F or 63°F
    assert lowest_f(20.0) == 68  # 19.5-20.5°C: 67.1-68.9°F, lowest whole °F a station reports is 68


def test_climate_day_uses_standard_time():
    start, end = weather.climate_day(date(2026, 7, 4), "America/New_York")
    assert start == datetime(2026, 7, 4, 5, tzinfo=timezone.utc)  # 1am EDT = midnight EST
    start, _ = weather.climate_day(date(2026, 1, 4), "America/New_York")
    assert start == datetime(2026, 1, 4, 5, tzinfo=timezone.utc)  # midnight EST


def test_forecast_distribution():
    c = cfg()
    fc, _ = weather.build_forecast(70, [69.0] * 20, None, 1, c)
    assert sum(fc.probs.values()) == pytest.approx(1.0)
    assert fc.center == pytest.approx(0.6 * 70 + 0.4 * 69)
    assert fc.sd == weather.MIN_SD[1]  # identical runs: fall back to typical forecast error
    assert max(fc.probs, key=fc.probs.get) == 70
    # Recorded 64°F by noon, forecast 64: the high is 64 unless it still warms up.
    fc, _ = weather.build_forecast(64, [64.0] * 20, 64, 0, c, local_hour=12)
    assert fc.observed_high == 64 and all(p == 0 for k, p in fc.probs.items() if k < 64)
    assert sum(fc.probs.values()) == pytest.approx(1.0)
    assert fc.probs[64] == pytest.approx(model.norm_cdf(0.5 / 1.5))  # P(rest of day stays below 64.5)
    assert fc.probs[65] + fc.probs[66] == pytest.approx(model.norm_cdf(2.5 / 1.5) - model.norm_cdf(0.5 / 1.5))
    # By 7pm it has cooled off: 64 is almost certainly the final high.
    late, _ = weather.build_forecast(64, [64.0] * 20, 64, 0, c, local_hour=19)
    assert late.probs[64] > 0.99 and late.center == pytest.approx(64 - 2.5 * 4)
    # Recorded high already above anything forecast: it is the answer.
    hot, _ = weather.build_forecast(64, [64.0] * 20, 75, 0, c, local_hour=13)
    assert hot.probs == {75: pytest.approx(1.0)} or hot.probs[75] > 0.999
    # Sources disagree by more than 4°F: no forecast.
    fc, reason = weather.build_forecast(75, [64.0] * 20, None, 1, c)
    assert fc is None and "disagree" in reason


def test_weather_scan_end_to_end(tmp_path):
    http = FakeHttp()
    c = cfg(db_path=str(tmp_path / "t.db"), telegram_bot_token="1:x", telegram_chat_id="9")
    tracker = Tracker(c.db_path)
    scan = cli.weather_scan(c, http, tracker, verbose=True, notifier=notify.Notifier(http, c))
    picks = {e.market.ticker: e for e in scan.evaluations if e.is_pick}
    # Today: NO on an already-impossible bracket.
    assert picks[f"{T_EV}-B61"].best.side == "no" and picks[f"{T_EV}-B61"].p_yes == 0
    # Tomorrow: YES on the bracket the forecasts favor but Kalshi prices at 15c.
    tm = picks[f"{M_EV}-B69"]
    assert tm.best.side == "yes" and tm.p_yes > 0.3
    texts = [p["text"] for p in http.posts]
    # 18.3°C recorded = 65°F, so "61° to 62°" is impossible.
    assert any("WEATHER PICK: BUY NO @ $0.92" in t and "high so far today 65°" in t for t in texts)
    assert any("69° to 70°" in t and "KNYC" in t and "paper bankroll" in t for t in texts)
    assert tracker.categories() == ["weather"] and "Kalshi weather" in tracker.report("weather").render()
    # Observations are only fetched for today.
    assert sum("/observations" in u for u in http.calls) == 1


def test_weather_skips_when_sources_disagree(tmp_path):
    http = FakeHttp(nws_tomorrow=78)
    c = cfg(db_path=str(tmp_path / "t.db"))
    scan = weather.collect(http, c, NOW)
    tomorrow = [e for e in scan.evaluations if e.market.event_ticker == M_EV]
    assert tomorrow and all("disagree" in e.skip_reason for e in tomorrow)


def test_weather_bracket_arb(tmp_path):
    cheap = [dict(m) for m in KALSHI[:5]]
    for m, ask in zip(cheap, (0.03, 0.05, 0.40, 0.33, 0.05)):  # all five YES for 86c + fees
        m["yes_ask_dollars"], m["yes_bid_dollars"] = f"{ask:.4f}", f"{ask - 0.01:.4f}"
    found = arbs.outcome_arbs([Market.from_api(m) for m in cheap], cfg(), "NYC high")
    assert found and found[0].payout == 1.0 and found[0].profit > 0.05
