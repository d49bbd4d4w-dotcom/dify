"""Settings, loaded from config.toml (optional) with sensible defaults."""

from __future__ import annotations

import os
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
    bankroll: float = 100.0
    kelly_multiplier: float = 0.25  # quarter-Kelly
    max_stake: float = 10.0
    fee_rate: float = 0.07

    db_path: str = "tradebot.db"

    # Optional plain-English write-ups via Claude (needs ANTHROPIC_API_KEY).
    thesis_model: str = "claude-opus-5-5"
    thesis_effort: str = "low"

    # Alerts. Prefer the TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / DISCORD_WEBHOOK_URL
    # environment variables so secrets stay out of files.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    notify_results: bool = True  # also alert when a pick settles (win/loss)

    @property
    def anthropic_enabled(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    p = Path(path) if path else Path("config.toml")
    if p.exists():
        data = tomllib.loads(p.read_text())
        data = data.get("tradebot", data)
        known = {f.name for f in fields(Config)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown config keys in {p}: {sorted(unknown)}")
        for k, v in data.items():
            setattr(cfg, k, v)
    for env, attr in (("DISCORD_WEBHOOK_URL", "discord_webhook_url"),
                      ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
                      ("TELEGRAM_CHAT_ID", "telegram_chat_id")):
        if os.environ.get(env):
            setattr(cfg, attr, os.environ[env])
    return cfg
