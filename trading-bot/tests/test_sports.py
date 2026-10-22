from datetime import date, datetime, timedelta, timezone

import pytest

from tradebot import cli, notify, sports
from tradebot.config import Config
from tradebot.tracker import Tracker

NOW = datetime.now(timezone.utc).replace(microsecond=0)
START = NOW + timedelta(hours=3)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def ticker_date(dt):
    d = dt.astimezone(sports.EASTERN)
    return d.strftime("%y%b%d").upper()


def kmarket(event, suffix, label, yes_bid, yes_ask, title):
    return {"ticker": f"{event}-{suffix}", "event_ticker": event, "title": title,
            "yes_sub_title": label, "status": "active", "close_time": iso(NOW + timedelta(days=14)),
            "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}", "volume": 5000}


def book(key, prices, updated=NOW):
    return {"key": key, "last_update": iso(updated), "markets": [{
        "key": "h2h", "last_update": iso(updated),
        "outcomes": [{"name": n, "price": p} for n, p in prices.items()]}]}


def vigged(probs, vig=1.03):
    return {n: round(1 / (p * vig), 4) for n, p in probs.items()}


D = ticker_date(START)
LA = f"KXNBAGAME-{D}LALLAC"
BOS = f"KXNBAGAME-{D}BOSNYK"
EPL = f"KXEPLGAME-{D}MCIARS"

KALSHI = {
    "KXNBAGAME": [
        # Pinnacle says Lakers 56%; Kalshi sells YES at 50c -> pick.
        kmarket(LA, "LAL", "Los Angeles L", 0.48, 0.50, "Los Angeles L at Los Angeles C Winner?"),
        kmarket(LA, "LAC", "Los Angeles C", 0.45, 0.47, "Los Angeles L at Los Angeles C Winner?"),
        # Fairly priced vs the book consensus -> pass.
        kmarket(BOS, "BOS", "Boston", 0.59, 0.61, "Boston at New York Winner?"),
        kmarket(BOS, "NYK", "New York", 0.39, 0.41, "Boston at New York Winner?"),
    ],
    "KXEPLGAME": [
        kmarket(EPL, "MCI", "Man City", 0.54, 0.56, "Man City vs Arsenal Winner?"),
        kmarket(EPL, "ARS", "Arsenal", 0.20, 0.22, "Man City vs Arsenal Winner?"),
        kmarket(EPL, "TIE", "Tie", 0.23, 0.25, "Man City vs Arsenal Winner?"),
    ],
}

ODDS = {
    "basketball_nba": [
        {"id": "1", "sport_key": "basketball_nba", "commence_time": iso(START),
         "home_team": "Los Angeles Clippers", "away_team": "Los Angeles Lakers",
         "bookmakers": [book("pinnacle", vigged({"Los Angeles Lakers": 0.56, "Los Angeles Clippers": 0.44})),
                        book("draftkings", vigged({"Los Angeles Lakers": 0.55, "Los Angeles Clippers": 0.45}, 1.05))]},
        {"id": "2", "sport_key": "basketball_nba", "commence_time": iso(START),
         "home_team": "New York Knicks", "away_team": "Boston Celtics",
         "bookmakers": [book(k, vigged({"Boston Celtics": 0.60, "New York Knicks": 0.40}, 1.045))
                        for k in ("draftkings", "fanduel", "betmgm")]},
    ],
    "soccer_epl": [
        {"id": "3", "sport_key": "soccer_epl", "commence_time": iso(START),
         "home_team": "Manchester City", "away_team": "Arsenal",
         "bookmakers": [book("pinnacle", vigged({"Manchester City": 0.55, "Arsenal": 0.21, "Draw": 0.24}))]},
    ],
}


class FakeHttp:
    def __init__(self):
        self.odds_calls = []
        self.posts = []

    def get_json(self, url, params=None):
        return self.get_json_and_headers(url, params)[0]

    def get_json_and_headers(self, url, params=None):
        if url.endswith("/series"):
            return {"series": [{"ticker": "KXNBAGAME"}, {"ticker": "KXEPLGAME"}, {"ticker": "KXNFLGAME"},
                               {"ticker": "KXNBASERIES"}]}, {}
        if url.endswith("/markets"):
            return {"markets": KALSHI.get(params.get("series_ticker"), []), "cursor": ""}, {}
        if url.endswith("/v4/sports"):
            return [{"key": k, "active": True, "has_outrights": False}
                    for k in ("basketball_nba", "soccer_epl", "americanfootball_nfl")], {}
        if url.endswith("/odds"):
            key = url.split("/sports/")[1].split("/")[0]
            self.odds_calls.append(key)
            assert params["markets"] == "h2h" and params["apiKey"] == "KEY"
            return ODDS.get(key, []), {"x-requests-remaining": "480", "x-requests-last": "1"}
        raise AssertionError(f"unexpected url {url}")

    def post_json(self, url, payload):
        self.posts.append(payload)


def cfg(**kw):
    kw.setdefault("sports_style", "all")
    return Config(odds_api_key="KEY", sports_series=["KXNBAGAME", "KXEPLGAME", "KXNFLGAME"], **kw)


def test_devig_and_pinnacle_preferred():
    p = sports.devig({"A": 1.8, "B": 2.1})
    assert sum(p.values()) == pytest.approx(1.0) and p["A"] > p["B"]
    fair = sports.fair_price(ODDS["basketball_nba"][0], cfg(), NOW)
    assert fair.source.startswith("Pinnacle")
    assert fair.probs["Los Angeles Lakers"] == pytest.approx(0.56, abs=1e-3)


def test_consensus_needs_enough_fresh_books():
    ev = ODDS["basketball_nba"][1]
    fair = sports.fair_price(ev, cfg(), NOW)
    assert fair.books == 3 and fair.probs["Boston Celtics"] == pytest.approx(0.60, abs=1e-3)
    stale = {**ev, "bookmakers": [book(b["key"], vigged({"Boston Celtics": 0.6, "New York Knicks": 0.4}),
                                       NOW - timedelta(hours=2)) for b in ev["bookmakers"]]}
    assert sports.fair_price(stale, cfg(), NOW) is None
    assert sports.fair_price({**ev, "bookmakers": ev["bookmakers"][:2]}, cfg(), NOW) is None


@pytest.mark.parametrize("label,team,ok", [
    ("Los Angeles L", "Los Angeles Lakers", True),
    ("Los Angeles L", "Los Angeles Clippers", False),
    ("Man City", "Manchester City", True),
    ("Man City", "Manchester United", False),
    ("Ohio St.", "Ohio State Buckeyes", True),
    ("Boston", "Boston Celtics", True),
    ("Atlético Madrid", "Atletico Madrid", True),
    ("Jon Jones", "Jon Jones", True),
    ("Arizona St.", "Arizona Wildcats", False),
])
def test_name_matching(label, team, ok):
    assert sports.name_matches(label, team) is ok


def test_ticker_helpers():
    assert sports.event_date("KXNFLGAME-25OCT26BUFCAR") == date(2025, 10, 26)
    assert sports.event_date("KXNFLGAME-NODATE") is None
    assert sports.series_code("KXWNBAGAME") == "WNBA" and sports.series_code("KXNBAGAME") == "NBA"
    assert sports.series_code("KXUFCFIGHT") == "UFC" and sports.series_code("KXNBASERIES") is None
    assert sports.odds_keys_for("KXATPMATCH", ["tennis_atp_us_open", "tennis_wta_us_open"]) == ["tennis_atp_us_open"]


def test_match_event_soccer_three_way_and_ambiguity():
    ev, mapping, _ = sports.match_event(["Man City", "Arsenal", "Tie"], START.date(), ODDS["soccer_epl"], NOW)
    assert ev["id"] == "3" and mapping == {"Man City": "Manchester City", "Arsenal": "Arsenal", "Tie": "Draw"}
    double = ODDS["basketball_nba"][1:] * 2  # same teams twice in a day
    ev, _, reason = sports.match_event(["Boston", "New York"], START.date(), double, NOW)
    assert ev is None and "more than one" in reason


def test_sports_scan_end_to_end(tmp_path, capsys):
    http = FakeHttp()
    c = cfg(db_path=str(tmp_path / "t.db"), telegram_bot_token="1:x", telegram_chat_id="9")
    tracker = Tracker(c.db_path)
    scan = cli.sports_scan(c, http, tracker, verbose=True, notifier=notify.Notifier(http, c))
    # NFL is in season but has no Kalshi games here, so no credits are spent on it.
    assert sorted(http.odds_calls) == ["basketball_nba", "soccer_epl"] and scan.credits_used == 2
    picks = [e for e in scan.evaluations if e.is_pick]
    assert [(e.market.ticker, e.best.side) for e in picks] == [(f"{LA}-LAL", "yes")]
    assert picks[0].p_yes == pytest.approx(0.56, abs=1e-3)
    assert len(scan.evaluations) == 7 and all(e.p_yes is not None for e in scan.evaluations)
    assert len(http.posts) == 1 and "SPORTS PICK: BUY YES @ $0.50" in http.posts[0]["text"]
    assert "Los Angeles L to win" in http.posts[0]["text"]
    out = capsys.readouterr().out
    assert "Odds API credits left: 480" in out and "pass" in out

    # Logged under its own category; the second scan does not re-alert.
    cli.sports_scan(c, http, tracker, verbose=False, notifier=notify.Notifier(http, c))
    assert len(http.posts) == 1
    assert tracker.categories() == ["sports"]
    rep = tracker.report("sports")
    assert rep.pending_picks == 1 and "Kalshi sports" in rep.render()


def test_started_or_far_games_are_skipped():
    m = sports.Market.from_api(KALSHI["KXNBAGAME"][0])
    ev = ODDS["basketball_nba"][0]
    fair = sports.fair_price(ev, cfg(), NOW)
    started = sports.evaluate(m, "basketball_nba", "g", "t", "Los Angeles Lakers",
                              {**ev, "commence_time": iso(NOW - timedelta(minutes=5))}, fair, cfg(), NOW)
    assert started.skip_reason == "game already started"
    far = sports.evaluate(m, "basketball_nba", "g", "t", "Los Angeles Lakers",
                          {**ev, "commence_time": iso(NOW + timedelta(days=9))}, fair, cfg(), NOW)
    assert far.skip_reason == "game is too far away"


def test_credit_pacing():
    now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)  # 9.5 days left in the month
    assert sports.paced_interval_minutes(30, 480, 2, now) == pytest.approx(13680 / 240)
    assert sports.paced_interval_minutes(30, 100_000, 2, now) == 30
    assert sports.paced_interval_minutes(30, None, 2, now) == 30
    assert sports.paced_interval_minutes(30, 1, 2, now) == pytest.approx(13680)


def test_void_refunds_pick(tmp_path):
    http = FakeHttp()
    c = cfg(db_path=str(tmp_path / "t.db"))
    tracker = Tracker(c.db_path)
    cli.sports_scan(c, http, tracker, verbose=False)
    voided = tracker.record_void(f"{LA}-LAL")
    assert [v["result"] for v in voided] == ["void"]
    assert tracker.record_void(f"{LA}-LAL") == []  # nothing left to void
    p = tracker.recent_picks()[0]
    assert p["result"] == "void" and p["pnl"] == 0
    assert tracker.report("sports").resolved_picks == 0
