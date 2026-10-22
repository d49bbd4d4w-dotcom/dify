from datetime import timedelta

import pytest

import test_sports as ts
from tradebot import arbs, cli, model, notify, sports
from tradebot.config import Config, load_config
from tradebot.sources.kalshi import Market
from tradebot.tracker import Tracker

CLOSE = ts.iso(ts.NOW + timedelta(hours=2))


def mk(ticker, yes_bid, yes_ask, strike_type="greater", floor=None, cap=None, event="EV", label=None):
    raw = {"ticker": ticker, "event_ticker": event, "title": "BTC at 5pm", "yes_sub_title": label or ticker,
           "close_time": CLOSE, "strike_type": strike_type,
           "yes_bid_dollars": f"{yes_bid:.4f}", "yes_ask_dollars": f"{yes_ask:.4f}"}
    if floor is not None:
        raw["floor_strike"] = floor
    if cap is not None:
        raw["cap_strike"] = cap
    return Market.from_api(raw)


# ---------- "math is off" checks ----------

def test_outcome_arb_yes_and_no_sides():
    cfg = Config()
    fair = [mk("A", 0.48, 0.50), mk("B", 0.48, 0.50)]
    assert arbs.outcome_arbs(fair, cfg, "game") == []
    cheap = [mk("A", 0.44, 0.45), mk("B", 0.48, 0.50)]  # 45c + 50c + fees < $1
    [a] = arbs.outcome_arbs(cheap, cfg, "game")
    assert [s for _, s, _ in a.legs] == ["yes", "yes"] and a.payout == 1.0
    assert a.profit == pytest.approx(1 - (0.45 + 0.07 * 0.45 * 0.55 + 0.50 + 0.07 * 0.5 * 0.5))
    rich = [mk("A", 0.53, 0.55), mk("B", 0.52, 0.54), mk("T", 0.05, 0.07)]  # bids sum > $1
    found = arbs.outcome_arbs(rich, cfg, "game")
    assert any(s == "no" and a.payout == 2.0 for a in found for _, s, _ in a.legs)


def test_ladder_arb_when_higher_strike_priced_above_lower():
    cfg = Config()
    ok = [mk("K100", 0.60, 0.62, floor=100), mk("K101", 0.40, 0.42, floor=101)]
    assert arbs.ladder_arbs(ok, cfg, "t") == []
    # "Above 101" bids 65c while "above 100" sells for 60c: impossible.
    bad = [mk("K100", 0.58, 0.60, floor=100), mk("K101", 0.65, 0.67, floor=101)]
    [a] = arbs.ladder_arbs(bad, cfg, "t")
    assert [(m.ticker, s) for m, s, _ in a.legs] == [("K100", "yes"), ("K101", "no")]
    assert a.cost == pytest.approx(0.60 + 0.35 + 0.07 * 0.6 * 0.4 + 0.07 * 0.35 * 0.65)


def test_bracket_arb_needs_full_coverage():
    cfg = Config()
    brackets = [mk("LO", 0.05, 0.06, "less", cap=100), mk("MID", 0.40, 0.42, "between", 100, 101),
                mk("HI", 0.40, 0.42, "greater", floor=101)]
    [a] = arbs.bracket_arbs(brackets, cfg, "t")
    assert a.kind == "brackets" and a.profit > 0
    gap = [brackets[0], mk("MID", 0.40, 0.42, "between", 100, 100.5), brackets[2]]
    assert arbs.bracket_arbs(gap, cfg, "t") == []  # 100.5-101 not covered: not risk-free
    assert arbs.scan_events(brackets, cfg)[0].kind == "brackets"


def test_arb_alert_sent_once_per_hour(tmp_path):
    http = ts.FakeHttp()
    c = Config(telegram_bot_token="1:x", telegram_chat_id="9")
    tracker = Tracker(str(tmp_path / "t.db"))
    found = arbs.outcome_arbs([mk("A", 0.44, 0.45), mk("B", 0.48, 0.50)], c, "Boston at New York")
    cli.report_arbs(found, c, tracker, notify.Notifier(http, c))
    cli.report_arbs(found, c, tracker, notify.Notifier(http, c))
    [msg] = [p["text"] for p in http.posts]
    assert msg.startswith("🧮 MATH IS OFF") and "Buy YES on A @ 45¢" in msg and "Up to" in msg


# ---------- sharp sources ----------

def test_fair_price_uses_sharp_sources_weighted():
    ev = {"home_team": "H", "away_team": "A", "commence_time": ts.iso(ts.START), "bookmakers": [
        ts.book("pinnacle", ts.vigged({"H": 0.60, "A": 0.40})),
        ts.book("betfair_ex_eu", ts.vigged({"H": 0.57, "A": 0.43}, 1.01)),
        ts.book("draftkings", ts.vigged({"H": 0.70, "A": 0.30}, 1.05)),  # soft book: ignored
    ]}
    fair = sports.fair_price(ev, ts.cfg(), ts.NOW)
    assert fair.probs["H"] == pytest.approx((2 * 0.60 + 0.57) / 3, abs=1e-3)
    assert fair.source == "Pinnacle + Betfair, no-vig" and fair.books == 2
    assert fair.book_range == pytest.approx(0.03, abs=1e-3)


# ---------- bankroll ----------

def test_bankroll_follows_results_and_open_bets(tmp_path):
    t = Tracker(str(tmp_path / "t.db"))
    e = ts.sports.SportsEvaluation(Market.from_api(ts.KALSHI["KXNBAGAME"][0]), "basketball_nba", "g", "LAL",
                                   ts.START, "", 0.56,
                                   model.Opportunity("yes", 0.50, 0.56, 0.0175, 0.04, 0.1, 2.0, 4), None)
    t.log_pick(e, category="sports")
    quiet = ts.sports.SportsEvaluation(Market.from_api(ts.KALSHI["KXNBAGAME"][2]), "basketball_nba", "g", "BOS",
                                       ts.START, "", 0.6,
                                       model.Opportunity("yes", 0.61, 0.6, 0.0166, 0.0, 0.1, 6.1, 10), None)
    t.log_pick(quiet, category="sports", alerted=False)
    assert t.open_exposure() == 2.0  # the quiet pick doesn't count
    t.record_outcome(e.market.ticker, "yes")
    t.record_outcome(quiet.market.ticker, "no")
    assert t.current_bankroll(100) == pytest.approx(100 + 4 - 2.0 - 0.07, abs=0.01)
    assert t.open_exposure() == 0


def test_fit_to_room_shrinks_or_blocks():
    b = model.Opportunity("yes", 0.40, 0.5, 0.0168, 0.08, 0.1, 4.0, 10)
    assert cli.fit_to_room(b, 10.0) and b.contracts == 10
    assert cli.fit_to_room(b, 1.30) and (b.contracts, b.stake) == (3, 1.2)
    assert not cli.fit_to_room(b, 0.30)


def test_stakes_scale_with_bankroll():
    small, big = Config(bankroll=100), Config(bankroll=1000, max_stake=1000)
    s = model.evaluate_sides(0.60, 0.50, 0.52, fee_rate=0.07, bankroll=small.bankroll,
                             kelly_multiplier=0.25, max_stake=small.max_bet())[0]
    b = model.evaluate_sides(0.60, 0.50, 0.52, fee_rate=0.07, bankroll=big.bankroll,
                             kelly_multiplier=0.25, max_stake=big.max_bet())[0]
    assert s.stake <= 5.0 and b.stake > s.stake * 5  # 5% cap at $100; grows with the bankroll


def test_open_bet_cap_blocks_new_picks(tmp_path):
    http = ts.FakeHttp()
    c = ts.cfg(db_path=str(tmp_path / "t.db"), telegram_bot_token="1:x", telegram_chat_id="9",
               max_open_fraction=0.0, parlays_enabled=False, arbs_enabled=False)
    tracker = Tracker(c.db_path)
    cli.sports_scan(c, http, tracker, verbose=False, notifier=notify.Notifier(http, c))
    assert http.posts == []
    assert tracker.db.execute("SELECT alerted FROM picks").fetchall()[0][0] == 0


def test_bankroll_style_sends_singles_with_stake(tmp_path):
    http = ts.FakeHttp()
    c = ts.cfg(db_path=str(tmp_path / "t.db"), telegram_bot_token="1:x", telegram_chat_id="9",
               sports_style="bankroll")
    cli.sports_scan(c, http, Tracker(c.db_path), verbose=False, notifier=notify.Notifier(http, c))
    [msg] = [p["text"] for p in http.posts]
    assert "SPORTS PICK" in msg and "of your $100.00 paper bankroll" in msg


def test_deprecated_config_key_is_ignored(tmp_path, capsys):
    p = tmp_path / "config.toml"
    p.write_text("[tradebot]\nparlay_stake = 20\nbankroll = 100\n")
    assert load_config(p).bankroll == 100
    assert "no longer used" in capsys.readouterr().err


def test_sports_arb_skips_incomplete_soccer_and_flags_ties(tmp_path, monkeypatch):
    nba = list(ts.KALSHI["KXNBAGAME"])
    nba[2] = ts.kmarket(ts.BOS, "BOS", "Boston", 0.44, 0.45, "Boston at New York Winner?")
    nba[3] = ts.kmarket(ts.BOS, "NYK", "New York", 0.48, 0.50, "Boston at New York Winner?")
    epl = list(ts.KALSHI["KXEPLGAME"])[:2]  # Tie market missing
    epl[0] = ts.kmarket(ts.EPL, "MCI", "Man City", 0.40, 0.41, "Man City vs Arsenal Winner?")
    epl[1] = ts.kmarket(ts.EPL, "ARS", "Arsenal", 0.20, 0.21, "Man City vs Arsenal Winner?")
    monkeypatch.setitem(ts.KALSHI, "KXNBAGAME", nba)
    monkeypatch.setitem(ts.KALSHI, "KXEPLGAME", epl)
    http = ts.FakeHttp()
    c = ts.cfg(db_path=str(tmp_path / "t.db"), telegram_bot_token="1:x", telegram_chat_id="9",
               parlays_enabled=False)
    cli.sports_scan(c, http, Tracker(c.db_path), verbose=False, notifier=notify.Notifier(http, c))
    arb_msgs = [p["text"] for p in http.posts if "MATH IS OFF" in p["text"]]
    # Boston/New York (no ties in the NBA) is flagged; City/Arsenal at 62c isn't, since a draw is missing.
    assert len(arb_msgs) == 1 and "Boston at New York" in arb_msgs[0] and "tie" not in arb_msgs[0].lower()
