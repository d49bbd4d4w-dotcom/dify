import math

import pytest

from tradebot import model, signals


def test_fee_matches_kalshi_schedule():
    # 100 contracts at 50c: 0.07 * 100 * 0.5 * 0.5 = $1.75
    assert model.kalshi_fee(100, 0.50) == 1.75
    # 1 contract at 50c: 0.0175 rounds up to 2 cents
    assert model.kalshi_fee(1, 0.50) == 0.02
    assert model.kalshi_fee(0, 0.50) == 0.0


def test_prob_above_at_the_money_is_half():
    assert model.prob_above(100_000, 100_000, 0.001, 10) == pytest.approx(0.5)


def test_prob_above_monotonic_in_spot_and_time():
    p_near = model.prob_above(100_100, 100_000, 0.0008, 1)
    p_far = model.prob_above(100_100, 100_000, 0.0008, 60)
    assert p_near > p_far > 0.5
    assert model.prob_above(99_900, 100_000, 0.0008, 10) < 0.5


def test_prob_above_expired():
    assert model.prob_above(101, 100, 0.001, 0) == 1.0
    assert model.prob_above(99, 100, 0.001, 0) == 0.0


def test_effective_minutes_accounts_for_settlement_average():
    assert model.effective_minutes(10, 1) == pytest.approx(10 - 2 / 3)
    # continuous at the window boundary
    assert model.effective_minutes(1, 1) == pytest.approx(1 / 3)
    assert model.effective_minutes(0.5, 1) < 1 / 3
    assert model.effective_minutes(-1, 1) == 0.0


def test_contract_types():
    kw = dict(spot=100_000, sigma_per_min=0.001, minutes=15)
    above = model.contract_yes_probability("greater", 100_000, None, **kw)
    below = model.contract_yes_probability("less", None, 100_000, **kw)
    assert above + below == pytest.approx(1.0)
    band = model.contract_yes_probability("between", 99_500, 100_500, **kw)
    assert 0 < band < 1
    with pytest.raises(ValueError):
        model.contract_yes_probability("custom", 1, 2, **kw)


def test_evaluate_sides_picks_underpriced_side():
    opps = model.evaluate_sides(0.70, yes_ask=0.55, no_ask=0.47, fee_rate=0.07,
                                bankroll=100, kelly_multiplier=0.25, max_stake=10)
    best = opps[0]
    assert best.side == "yes"
    fee = 0.07 * 0.55 * 0.45
    assert best.ev_per_contract == pytest.approx(0.70 - 0.55 - fee)
    assert 0 < best.stake <= 10
    assert best.contracts == int(best.stake / 0.55 + 1e-9)
    no = [o for o in opps if o.side == "no"][0]
    assert no.ev_per_contract < 0 and no.contracts == 0


def test_kelly_zero_without_edge():
    f, stake, n = model.size_position(0.5, 0.52, 100, 0.25, 10, 0.5)
    assert (f, stake, n) == (0.0, 0.0, 0)


def test_signals():
    closes = [100 * math.exp(0.001 * i) for i in range(30)]
    rets = signals.log_returns(closes)
    assert signals.realized_vol(rets) == pytest.approx(0.001)
    assert signals.ewma_vol(rets, 10) == pytest.approx(0.001)
    assert signals.momentum(closes, 5) == pytest.approx(0.005)
    assert signals.momentum(closes, 100) is None
    assert signals.rsi(closes) == 100.0
    assert signals.sma([1, 2, 3, 4], 2) == 3.5
    assert signals.book_imbalance([(99.95, 3)], [(100.05, 1)], 100) == pytest.approx(0.5)
    assert signals.book_imbalance([(90, 3)], [(110, 1)], 100) is None
