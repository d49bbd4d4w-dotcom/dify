"""Settings, loaded from config.toml (optional) with sensible defaults."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    # Kalshi series to scan. KXBTC15M = "BTC up or down in 15 minutes".
    # Add "KXBTCD" for the hourly/daily "BTC above $X" ladder.
    series: list[str] = field(default_factory=lambda: ["KXBTC15M"])

    # A pick needs at least this much expected profit per $1 contract, after fees.
    min_edge: float = 0.03
    # Skip markets that close too soon (data lag matters) or are too far out.
    min_minutes_left: float = 2.0
    max_minutes_left: float = 24 * 60
    # Skip thin markets where bid/ask spread is wider than this (dollars).
    max_spread: float = 0.10
    # If the model and the market disagree by more than this, assume *we* are
    # missing something (stale data, news) and do not suggest a trade.
    max_disagreement: float = 0.25

    # Volatility model.
    vol_halflife_min: float = 30.0
    vol_lookback_min: int = 240
    vol_multiplier: float = 1.15  # >1 accounts for BTC's fat tails
    settlement_avg_window_sec: float = 60.0
    # Tilt toward recent 15m momentum. 0 = off. Only raise this if `report`
    # shows it helps; it is unproven by default.
    momentum_weight: float = 0.0

    # Position sizing (suggestions only, nothing is ever placed for you).
    # Paper bankroll: what you start with. Stakes are sized from the *current*
    # paper bankroll (start + results of every pick the bot suggested), so they
    # grow when it's winning and shrink when it's losing.
    bankroll: float = 100.0
    kelly_multiplier: float = 0.25  # quarter-Kelly
    max_stake: float = 10.0  # never more than this on one bet...
    max_bet_fraction: float = 0.05  # ...or more than 5% of the bankroll
    max_open_fraction: float = 0.60  # keep at least 40% of the bankroll out of open bets
    parlay_min_stake: float = 1.0  # practical minimum for a parlay ticket
    fee_rate: float = 0.07

    db_path: str = "tradebot.db"

    # Optional plain-English write-ups via Claude (needs ANTHROPIC_API_KEY).
    thesis_model: str = "claude-opus-5-5"
    thesis_effort: str = "low"

    # ----- Sports (Kalshi game-winner markets vs. sharp sportsbooks) -----
    # Free key from https://the-odds-api.com (prefer the ODDS_API_KEY env var).
    odds_api_key: str = ""
    # Kalshi series to scan; empty = find all supported game series automatically.
    sports_series: list[str] = field(default_factory=list)
    # Up to 10 books per request cost 1 credit. Pinnacle is used as "the" fair
    # price when present; otherwise the median of the others.
    sports_bookmakers: list[str] = field(default_factory=lambda: [
        "pinnacle", "betfair_ex_eu", "matchbook", "smarkets", "betonlineag", "lowvig",
        "draftkings", "fanduel", "betmgm", "williamhill_us"])
    # Sources trusted for the fair price: Pinnacle and betting exchanges, where
    # winning bettors aren't limited, so prices are the most accurate. The big
    # US books are only used as a fallback consensus.
    sharp_books: list[str] = field(default_factory=lambda: [
        "pinnacle", "betfair_ex_eu", "matchbook", "smarkets", "betonlineag", "lowvig"])
    sports_min_edge: float = 0.02  # min expected profit per contract after fees
    sports_max_spread: float = 0.05  # skip thin Kalshi markets
    sports_max_disagreement: float = 0.10  # books vs Kalshi gap that means "news or mismatch"
    sports_max_book_spread: float = 0.06  # skip when the books themselves disagree this much
    sports_min_books: int = 3  # books needed for a consensus when Pinnacle is missing
    sports_max_quote_age_min: float = 30.0
    sports_max_days_ahead: float = 3.0
    sports_include_live: bool = False  # in-game betting is riskier; off by default
    sports_poll_minutes: float = 30.0  # slowed down automatically to fit your Odds API plan

    # "bankroll": single picks + big-hitter parlays, all sized for your bankroll.
    # "big": only big-payout parlays are sent (singles still tracked quietly).
    # "all": single picks + safer / value / long-shot parlays.
    sports_style: str = "bankroll"
    # Payout sizes for big-hitter parlays, as multiples of the stake:
    # 25x / 50x / 100x = $500+ / $1,000+ / $2,000+ on a $20 ticket.
    big_hitter_payouts: list[float] = field(default_factory=lambda: [25.0, 50.0, 100.0])
    parlay_candidate_legs: int = 14  # best-priced legs considered when searching combinations
    parlay_alert_cooldown_hours: float = 12.0  # at most one ticket of each size per 12h

    # Parlays built from sports legs priced below the sharp books (paper-tracked).
    parlays_enabled: bool = True
    parlay_min_leg_edge: float = 0.01  # each leg's fair chance must beat its price by this
    parlay_min_value_margin: float = 0.03  # parlay must return >= 3% more than it costs, on average
    parlay_safe_min_leg_prob: float = 0.65  # "safer" parlays only use favorites like this
    parlay_value_min_leg_prob: float = 0.40  # "value" parlays skip big underdogs...
    parlay_value_min_chance: float = 0.10  # ...and keep at least a 10% chance of hitting
    parlay_longshot_payout: float = 50.0  # "long shot" = pays at least 50x
    parlay_max_legs: int = 8

    # "Math is off" checks on Kalshi: sets of contracts that pay out no matter
    # what, for less than they cost (arbitrage). Checked on every scan.
    arbs_enabled: bool = True
    arb_series: list[str] = field(default_factory=lambda: ["KXBTCD", "KXBTC"])
    arb_min_profit: float = 0.01  # per $1 set, after fees

    timezone: str = "America/Los_Angeles"  # for times shown in alerts

    # Alerts. Prefer the TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / DISCORD_WEBHOOK_URL
    # environment variables so secrets stay out of files.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    notify_results: bool = True  # also alert when a pick settles (win/loss)

    @property
    def anthropic_enabled(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def max_bet(self) -> float:
        return min(self.max_stake, self.bankroll * self.max_bet_fraction)


DEPRECATED_KEYS = {"parlay_stake"}


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    p = Path(path) if path else Path("config.toml")
    if p.exists():
        data = tomllib.loads(p.read_text())
        data = data.get("tradebot", data)
        known = {f.name for f in fields(Config)}
        for old in DEPRECATED_KEYS & set(data):
            print(f"[info] config key '{old}' is no longer used; stakes are now sized from "
                  "your bankroll. You can delete it.", file=sys.stderr)
            data.pop(old)
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown config keys in {p}: {sorted(unknown)}")
        for k, v in data.items():
            setattr(cfg, k, v)
    for env, attr in (("DISCORD_WEBHOOK_URL", "discord_webhook_url"),
                      ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
                      ("TELEGRAM_CHAT_ID", "telegram_chat_id"),
                      ("ODDS_API_KEY", "odds_api_key")):
        if os.environ.get(env):
            setattr(cfg, attr, os.environ[env])
    return cfg
