"""End-to-end tests with a fake HTTP layer shaped like the real public APIs."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from tradebot import analyzer, cli, swing
from tradebot.config import Config, load_config
from tradebot.signals import Candle
from tradebot.sources.kalshi import Market
from tradebot.tracker import Tracker

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
SPOT = 100_000.0


def coinbase_rows(n=300, gran=60, step=0.0007):
    """Newest-first rows like Coinbase: [time, low, high, open, close, volume]."""
    start = int(NOW.timestamp()) - (n - 1) * gran
    rows, price = [], SPOT * math.exp(-step * (n - 1) / 4)
    for i in range(n):
        o = price
        price = o * math.exp(step if i % 2 == 0 else -step / 2)
        rows.append([start + i * gran, min(o, price) * 0.9999, max(o, price) * 1.0001, o, price, 5.0])
    rows[-1][4] = SPOT  # make the final close the spot
    return list(reversed(rows))


def kalshi_market(ticker, yes_bid, yes_ask, floor=None, minutes_left=10, **extra):
    raw = {
        "ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": "BTC up in 15 mins?",
        "status": "active",
        "open_time": (NOW + timedelta(minutes=minutes_left - 15)).isoformat().replace("+00:00", "Z"),
        "close_time": (NOW + timedelta(minutes=minutes_left)).isoformat().replace("+00:00", "Z"),
        "strike_type": "greater_or_equal", "yes_bid_dollars": f"{yes_bid:.4f}",
        "yes_ask_dollars": f"{yes_ask:.4f}", "volume": 1200, "result": "",
    }
    if floor is not None:
        raw["floor_strike"] = floor
    raw.update(extra)
    return raw


class FakeHttp:
    def __init__(self, markets, results=None):
        self.markets = markets
        self.results = results or {}
        self.posts = []

    def get_json(self, url, params=None):
        if url.endswith("/candles"):
            return coinbase_rows(gran=params["granularity"])
        if url.endswith("/ticker") and "coinbase" in url:
            return {"bid": str(SPOT - 1), "ask": str(SPOT + 1)}
        if "kraken" in url:
            return {"error": [], "result": {"XXBTZUSD": {"b": [str(SPOT - 2)], "a": [str(SPOT + 2)]}}}
        if "bitstamp" in url:
            raise RuntimeError("bitstamp down")  # exercise the fallback path
        if url.endswith("/book"):
            return {"bids": [[str(SPOT - 5), "2", 1]], "asks": [[str(SPOT + 5), "1", 1]]}
        if "alternative.me" in url:
            return {"data": [{"value": "61", "value_classification": "Greed"}]}
        if url.endswith("/markets"):
            return {"markets": self.markets, "cursor": ""}
        if "/markets/" in url:
            t = url.rsplit("/", 1)[1]
            m = next(m for m in self.markets if m["ticker"] == t)
            return {"market": {**m, "status": "finalized", "result": self.results.get(t, "")}}
        raise AssertionError(f"unexpected url {url}")

    def post_json(self, url, payload):
        self.posts.append((url, payload))


def test_market_parsing_handles_cents_and_dollars():
    m = Market.from_api({"ticker": "T", "yes_bid": 40, "yes_ask": 45, "strike_type": "greater"})
    assert (m.yes_bid, m.yes_ask, m.no_ask, m.no_bid) == (0.40, 0.45, 0.60, 0.55)
    m2 = Market.from_api({"ticker": "T", "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4500",
                          "no_ask_dollars": "0.6100", "floor_strike": 101000})
    assert m2.no_ask == 0.61 and m2.floor_strike == 101000.0
    assert m2.mid == pytest.approx(0.425)
    # 0 / 100 cent quotes mean "no order there"
    assert Market.from_api({"ticker": "T", "yes_bid": 0, "yes_ask": 100}).yes_ask is None


def test_scan_finds_mispriced_market_and_skips_fair_one(tmp_path, capsys):
    markets = [
        # Spot is $300 above the strike with 10 min left -> fair YES well above 50%, priced at 52c.
        kalshi_market("KXBTC15M-A", 0.50, 0.52, floor=SPOT - 300),
        # Fairly priced at-the-money market.
        kalshi_market("KXBTC15M-B", 0.48, 0.52, floor=SPOT),
        # Closing too soon.
        kalshi_market("KXBTC15M-C", 0.10, 0.12, floor=SPOT - 300, minutes_left=1),
    ]
    http = FakeHttp(markets)
    cfg = Config(db_path=str(tmp_path / "t.db"), max_disagreement=0.5)
    tracker = Tracker(cfg.db_path)
    evals = cli.scan_once(cfg, http, tracker, explain=False, verbose=True)
    by = {e.market.ticker: e for e in evals}
    assert by["KXBTC15M-A"].is_pick and by["KXBTC15M-A"].best.side == "yes"
    assert not by["KXBTC15M-B"].is_pick
    assert "closes in" in by["KXBTC15M-C"].skip_reason
    out = capsys.readouterr().out
    assert "PICK  KXBTC15M-A" in out and "bitstamp price unavailable" in out

    # Second scan doesn't double-log the same pick.
    cli.scan_once(cfg, http, tracker, explain=False, verbose=False)
    assert len(tracker.recent_picks()) == 1

    # Settle: A resolves YES -> the pick wins, P&L positive.
    http.results = {"KXBTC15M-A": "yes", "KXBTC15M-B": "no", "KXBTC15M-C": "yes"}
    tracker.db.execute("UPDATE picks SET close_time='2000-01-01T00:00:00+00:00'")
    # C was skipped before scoring, so only A and B were logged.
    assert cli.resolve(cfg, http, tracker) == 2
    pick = tracker.recent_picks()[0]
    assert pick["result"] == "win" and pick["pnl"] > 0
    rep = tracker.report()
    assert rep.resolved_picks == 1 and rep.wins == 1 and rep.scored_evals >= 4
    assert "not enough data" in rep.render()


def test_disagreement_guard_blocks_pick(tmp_path):
    http = FakeHttp([kalshi_market("KXBTC15M-A", 0.20, 0.22, floor=SPOT - 300)])
    cfg = Config(db_path=str(tmp_path / "t.db"))
    ctx = analyzer.build_context(http, cfg)
    m = Market.from_api(http.markets[0])
    e = analyzer.evaluate_market(m, ctx, cfg, NOW)
    assert not e.is_pick and "disagreement" in e.skip_reason


def test_updown_market_without_strike_uses_window_open_price():
    cfg = Config()
    http = FakeHttp([])
    ctx = analyzer.build_context(http, cfg)
    raw = kalshi_market("KXBTC15M-X", 0.48, 0.52)
    raw.pop("strike_type")
    e = analyzer.evaluate_market(Market.from_api(raw), ctx, cfg, NOW)
    assert e.p_yes is not None and "inferred" in e.strike_note


def test_swing_view_renders():
    rows = coinbase_rows(gran=86400)
    candles = sorted((Candle(int(t), o, h, lo, c, v) for t, lo, h, o, c, v in rows), key=lambda c: c.ts)
    view = swing.analyze(candles, "1d", (61, "Greed"))
    assert view.sma200 is not None and view.stop < view.price < view.target
    assert "Stance" in view.render()


def test_config_rejects_typos(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[tradebot]\nmin_edge = 0.05\nseries = ["KXBTC15M", "KXBTCD"]\n')
    cfg = load_config(p)
    assert cfg.min_edge == 0.05 and cfg.series == ["KXBTC15M", "KXBTCD"]
    p.write_text("[tradebot]\nmin_egde = 0.05\n")
    with pytest.raises(ValueError):
        load_config(p)
