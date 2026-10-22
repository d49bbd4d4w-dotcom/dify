import math
from datetime import timedelta

import pytest

import test_sports as ts
from tradebot import cli, notify, parlays
from tradebot.config import Config
from tradebot.tracker import Tracker


def leg(name, p, price, game=None):
    return parlays.Leg(f"KXNBAGAME-X{name}-{name}", f"KXNBAGAME-X{name}", "basketball_nba",
                       game or f"{name} game", name, p, price, ts.START)


def test_parlay_math():
    p = parlays.Parlay("value", [leg("A", 0.56, 0.50), leg("B", 0.60, 0.55)])
    assert p.chance == pytest.approx(0.336)
    assert p.payout == pytest.approx(1 / 0.275)
    assert p.value == pytest.approx(0.336 / 0.275)
    assert p.min_payout(0.03) == pytest.approx(1.03 / 0.336)


def test_build_kinds_and_filters():
    cfg = Config()
    legs = [leg("FAV1", 0.80, 0.76), leg("FAV2", 0.75, 0.71), leg("FAV3", 0.70, 0.67),
            leg("DOG1", 0.30, 0.25), leg("DOG2", 0.28, 0.24), leg("DOG3", 0.35, 0.31),
            leg("MID", 0.52, 0.48)]
    tickets = {p.kind: p for p in parlays.build(legs, cfg)}
    assert [x.team for x in tickets["safer"].legs] == ["FAV1", "FAV2", "FAV3"]
    assert tickets["safer"].chance == pytest.approx(0.8 * 0.75 * 0.7)
    value = tickets["value"]
    assert all(x.p >= cfg.parlay_value_min_leg_prob for x in value.legs)
    assert value.chance >= cfg.parlay_value_min_chance and len(value.legs) >= 2
    shot = tickets["longshot"]
    assert shot.payout >= cfg.parlay_longshot_payout and len(shot.legs) <= cfg.parlay_max_legs
    assert all(p.value >= 1 + cfg.parlay_min_value_margin for p in tickets.values())


def test_no_parlay_without_enough_value():
    cfg = Config()
    # Each leg barely beats its price, so the parlay doesn't clear the 3% margin.
    assert parlays.build([leg("A", 0.505, 0.50), leg("B", 0.505, 0.50)], cfg) == []
    assert parlays.build([leg("A", 0.6, 0.5)], cfg) == []  # one leg is not a parlay


def test_legs_one_per_game_and_only_checked():
    cfg = Config()
    m = ts.sports.Market.from_api
    raw = ts.KALSHI["KXNBAGAME"]
    e1 = ts.sports.SportsEvaluation(m(raw[0]), "basketball_nba", "g1", "LAL", ts.START, "", 0.56, None, None, True)
    e2 = ts.sports.SportsEvaluation(m(raw[1]), "basketball_nba", "g1", "LAC", ts.START, "", 0.54, None, None, True)
    unchecked = ts.sports.SportsEvaluation(m(raw[2]), "basketball_nba", "g2", "BOS", ts.START, "", 0.9, None,
                                           "game already started", False)
    legs = parlays.legs_from([e1, e2, unchecked], cfg)
    assert [x.team for x in legs] == ["LAC"]  # best value in game 1 (0.54/0.47 > 0.56/0.50); g2 excluded


def tracker_with(tmp_path, legs, stake=20.0):
    t = Tracker(str(tmp_path / "t.db"))
    p = parlays.Parlay("value", legs)
    stored = [{"ticker": x.ticker, "label": x.team, "p": x.p, "price": x.price} for x in legs]
    assert t.log_parlay(p.kind, stored, stake, p.chance, p.payout)
    assert not t.log_parlay(p.kind, list(reversed(stored)), stake, p.chance, p.payout)  # same legs
    return t, p


def test_settle_win_loss_void(tmp_path):
    a, b, c = leg("A", 0.56, 0.50), leg("B", 0.60, 0.55), leg("C", 0.55, 0.40)
    t, p = tracker_with(tmp_path, [a, b, c])
    assert set(t.unresolved_tickers()) == {a.ticker, b.ticker, c.ticker}
    t.record_outcome(a.ticker, "yes")
    assert t.settle_parlays() == []  # still waiting
    t.record_void(b.ticker)  # cancelled leg drops out
    t.record_outcome(c.ticker, "yes")
    [won] = t.settle_parlays()
    assert won["result"] == "win" and won["pnl"] == pytest.approx(round(20 / (0.50 * 0.40) - 20, 2))
    assert t.unresolved_tickers() == []

    (tmp_path / "second").mkdir()
    t2, _ = tracker_with(tmp_path / "second", [a, b])
    t2.record_outcome(b.ticker, "no")
    [lost] = t2.settle_parlays()
    assert lost["result"] == "loss" and lost["pnl"] == -20 and lost["lost_on"] == ["B"]
    assert "hit 0" in t2.parlay_summary()


def test_parlay_text_is_honest():
    cfg = Config()
    shot = parlays.Parlay("longshot", [leg(n, 0.30, 0.25) for n in "ABCD"])
    text = cli.parlay_text(shot, cfg)
    assert "LONG-SHOT PARLAY · 4 legs" in text
    assert f"about 1 in {1 / 0.3 ** 4:,.0f}" in text
    assert "most likely you lose the $20" in text
    assert f"pays ~${20 / 0.25 ** 4:,.0f}" in text
    safe = parlays.Parlay("safer", [leg("A", 0.8, 0.75), leg("B", 0.8, 0.75)])
    assert "wins about 6 times in 10" in cli.parlay_text(safe, cfg)


def test_sports_scan_suggests_and_settles_parlay(tmp_path, monkeypatch):
    nba = list(ts.KALSHI["KXNBAGAME"])
    nba[2] = ts.kmarket(ts.BOS, "BOS", "Boston", 0.53, 0.55, "Boston at New York Winner?")
    epl = list(ts.KALSHI["KXEPLGAME"])
    epl[0] = ts.kmarket(ts.EPL, "MCI", "Man City", 0.48, 0.50, "Man City vs Arsenal Winner?")
    monkeypatch.setitem(ts.KALSHI, "KXNBAGAME", nba)
    monkeypatch.setitem(ts.KALSHI, "KXEPLGAME", epl)

    http = ts.FakeHttp()
    c = ts.cfg(db_path=str(tmp_path / "t.db"), telegram_bot_token="1:x", telegram_chat_id="9")
    tracker = Tracker(c.db_path)
    cli.sports_scan(c, http, tracker, verbose=False, notifier=notify.Notifier(http, c))
    texts = [p["text"] for p in http.posts]
    parlay_msgs = [t for t in texts if "PARLAY" in t]
    assert len(parlay_msgs) == 1 and "VALUE PARLAY · 3 legs" in parlay_msgs[0]
    assert f"{0.56 * 0.60 * 0.55:.1%}" in parlay_msgs[0]

    # Second scan: same legs, no repeat alert.
    cli.sports_scan(c, http, tracker, verbose=False, notifier=notify.Notifier(http, c))
    assert sum("PARLAY" in p["text"] for p in http.posts) == 1

    # All three legs win.
    for t in (f"{ts.LA}-LAL", f"{ts.BOS}-BOS", f"{ts.EPL}-MCI"):
        tracker.record_outcome(t, "yes")
    http.posts.clear()
    monkeypatch.setattr(cli.Kalshi, "markets", lambda self, tickers: [])
    monkeypatch.setattr(cli.Kalshi, "market", lambda self, t: (_ for _ in ()).throw(RuntimeError("n/a")))
    cli.resolve(c, http, tracker, notify.Notifier(http, c))
    [msg] = [p["text"] for p in http.posts]
    expected = round(20 / (0.50 * 0.55 * 0.50) - 20, 2)
    assert msg.startswith(f"🎟️ ✅ PARLAY HIT +${expected:,.2f}")
    assert math.isclose(tracker.db.execute("SELECT pnl FROM parlays").fetchone()[0], expected)


def test_big_hitters_pick_most_likely_combo_for_each_payout():
    import itertools
    cfg = Config(big_hitter_payouts=[25.0, 50.0, 100.0])
    legs = [leg("FAV1", 0.80, 0.76), leg("FAV2", 0.75, 0.71), leg("FAV3", 0.70, 0.67),
            leg("MID1", 0.52, 0.48), leg("MID2", 0.50, 0.46), leg("DOG1", 0.30, 0.25),
            leg("DOG2", 0.28, 0.24), leg("DOG3", 0.35, 0.31), leg("DOG4", 0.17, 0.15)]
    tickets = parlays.big_hitters(legs, cfg)
    assert [t.kind for t in tickets] == ["big25", "big50", "big100"]
    assert len({t.key for t in tickets}) == 3
    for t, target in zip(tickets, (25, 50, 100)):
        assert t.payout >= target and t.value >= 1 + cfg.parlay_min_value_margin
        # No other qualifying combination has a better chance of hitting.
        for k in range(2, cfg.parlay_max_legs + 1):
            for combo in itertools.combinations(legs, k):
                other = parlays.Parlay("", list(combo))
                if other.payout >= target and other.value >= 1 + cfg.parlay_min_value_margin \
                        and other.key not in {x.key for x in tickets if x is not t}:
                    assert other.chance <= t.chance + 1e-12
    # Safer than just stacking the best-priced underdogs to the same payout.
    greedy = next(p for p in parlays.build(legs, Config(parlay_longshot_payout=25.0)) if p.kind == "longshot")
    assert tickets[0].chance >= greedy.chance
    assert parlays.title("big50") == "BIG HITTER · 50x+"


def test_big_hitters_none_when_payout_out_of_reach():
    cfg = Config(big_hitter_payouts=[25.0])
    assert parlays.big_hitters([leg("A", 0.80, 0.76), leg("B", 0.75, 0.71)], cfg) == []


def big_style_market(monkeypatch, boston_ask=0.55):
    nba = list(ts.KALSHI["KXNBAGAME"])
    nba[2] = ts.kmarket(ts.BOS, "BOS", "Boston", boston_ask - 0.02, boston_ask, "Boston at New York Winner?")
    epl = list(ts.KALSHI["KXEPLGAME"])
    epl[1] = ts.kmarket(ts.EPL, "ARS", "Arsenal", 0.16, 0.18, "Man City vs Arsenal Winner?")
    monkeypatch.setitem(ts.KALSHI, "KXNBAGAME", nba)
    monkeypatch.setitem(ts.KALSHI, "KXEPLGAME", epl)


def test_big_style_sends_only_big_hitters_with_cooldown(tmp_path, monkeypatch):
    big_style_market(monkeypatch)
    http = ts.FakeHttp()
    c = ts.cfg(db_path=str(tmp_path / "t.db"), telegram_bot_token="1:x", telegram_chat_id="9",
               sports_style="big", big_hitter_payouts=[10.0, 20.0])
    tracker = Tracker(c.db_path)
    cli.sports_scan(c, http, tracker, verbose=False, notifier=notify.Notifier(http, c))
    texts = [p["text"] for p in http.posts]
    # Single picks (LAL, BOS) are tracked but not sent; only the two big hitters are.
    assert len(tracker.recent_picks()) == 2
    assert len(texts) == 2 and all("BIG HITTER" in t for t in texts)
    ten, twenty = texts
    # 10x+: Boston + Arsenal is the most likely combo (12.6%) that pays 10x.
    assert "BIG HITTER · 10x+ · 2 legs" in ten and f"{0.60 * 0.21:.1%}" in ten
    assert "BIG HITTER · 20x+ · 3 legs" in twenty

    # Prices change so a different 10x ticket appears, but one was sent in the
    # last 12 hours: hold it (not sent, not counted in the paper results).
    big_style_market(monkeypatch, boston_ask=0.61)
    cli.sports_scan(c, http, tracker, verbose=True, notifier=notify.Notifier(http, c))
    assert len(http.posts) == 2
    assert tracker.db.execute("SELECT COUNT(*) FROM parlays").fetchone()[0] == 2
