"""SQLite log of every evaluation and every suggested pick, plus scoring.

This is the most important part of the bot: it tells you, with real outcomes,
whether the model is actually better than the market. If it isn't, don't trade.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .model import Opportunity, kalshi_fee
from .sources.kalshi import Market

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    minutes_left REAL,
    spot REAL,
    p_yes REAL,
    market_mid REAL,
    outcome INTEGER,  -- 1 yes, 0 no, -1 voided, NULL unresolved
    category TEXT NOT NULL DEFAULT 'btc'
);
CREATE INDEX IF NOT EXISTS ix_eval_ticker ON evaluations(ticker);
CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    title TEXT,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    win_prob REAL NOT NULL,
    ev REAL NOT NULL,
    contracts INTEGER NOT NULL,
    close_time TEXT,
    thesis TEXT,
    result TEXT,  -- 'win' / 'loss' / 'void' / NULL
    pnl REAL,
    category TEXT NOT NULL DEFAULT 'btc',
    UNIQUE(ticker, side)
);
"""

CATEGORY_NAMES = {"btc": "Kalshi BTC 15-minute", "sports": "Kalshi sports"}


class Scored(Protocol):
    """Anything evaluated by the bot: a BTC or a sports evaluation."""
    market: Market
    best: Opportunity | None
    p_yes: float | None

    @property
    def market_mid(self) -> float | None: ...

    @property
    def minutes_left(self) -> float: ...


class Tracker:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._migrate()
        self.db.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Add columns introduced after the first version to an existing database."""
        for table in ("evaluations", "picks"):
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if cols and "category" not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN category TEXT NOT NULL DEFAULT 'btc'")
        self.db.commit()

    def log_evaluation(self, e: Scored, spot: float | None = None, category: str = "btc") -> None:
        if e.p_yes is None:
            return
        self.db.execute(
            "INSERT INTO evaluations(ts, ticker, minutes_left, spot, p_yes, market_mid, category)"
            " VALUES (?,?,?,?,?,?,?)",
            (_now(), e.market.ticker, e.minutes_left, spot, e.p_yes, e.market_mid, category))
        self.db.commit()

    def log_pick(self, e: Scored, thesis: str | None = None, category: str = "btc",
                 title: str | None = None) -> bool:
        """Record a suggestion. Returns False if this ticker/side was already suggested."""
        assert e.best is not None
        cur = self.db.execute(
            "INSERT OR IGNORE INTO picks(ts, ticker, title, side, price, win_prob, ev, contracts, close_time,"
            " thesis, category) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), e.market.ticker, title or f"{e.market.title} {e.market.subtitle}".strip(), e.best.side,
             e.best.price, e.best.win_prob, e.best.ev_per_contract, max(e.best.contracts, 1),
             e.market.close_time.isoformat() if e.market.close_time else None, thesis, category))
        self.db.commit()
        return cur.rowcount > 0

    def unresolved_tickers(self, max_age_days: int = 30) -> list[str]:
        since = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        rows = self.db.execute(
            "SELECT DISTINCT ticker FROM picks WHERE result IS NULL AND ts > ?"
            " UNION SELECT DISTINCT ticker FROM evaluations WHERE outcome IS NULL AND ts > ?",
            (since, since)).fetchall()
        return [r["ticker"] for r in rows]

    def record_outcome(self, ticker: str, result: str, fee_rate: float = 0.07) -> list[dict]:
        """result is Kalshi's 'yes' or 'no'. Returns the picks that just settled."""
        yes = 1 if result == "yes" else 0
        self.db.execute("UPDATE evaluations SET outcome=? WHERE ticker=? AND outcome IS NULL", (yes, ticker))
        settled = []
        for p in self.db.execute("SELECT * FROM picks WHERE ticker=? AND result IS NULL", (ticker,)).fetchall():
            won = (p["side"] == result)
            n = p["contracts"]
            cost = n * p["price"] + kalshi_fee(n, p["price"], fee_rate)
            pnl = round((n if won else 0) - cost, 2)
            outcome = "win" if won else "loss"
            self.db.execute("UPDATE picks SET result=?, pnl=? WHERE id=?", (outcome, pnl, p["id"]))
            settled.append({**dict(p), "result": outcome, "pnl": pnl})
        self.db.commit()
        return settled

    def record_void(self, ticker: str) -> list[dict]:
        """Market was cancelled (e.g. game postponed): stakes are refunded. Returns the voided picks."""
        voided = [{**dict(p), "result": "void", "pnl": 0.0} for p in self.db.execute(
            "SELECT * FROM picks WHERE ticker=? AND result IS NULL", (ticker,)).fetchall()]
        self.db.execute("UPDATE evaluations SET outcome=-1 WHERE ticker=? AND outcome IS NULL", (ticker,))
        self.db.execute("UPDATE picks SET result='void', pnl=0 WHERE ticker=? AND result IS NULL", (ticker,))
        self.db.commit()
        return voided

    def recent_picks(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM picks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def categories(self) -> list[str]:
        rows = self.db.execute("SELECT category FROM evaluations UNION SELECT category FROM picks").fetchall()
        return sorted(r[0] for r in rows) or ["btc"]

    def report(self, category: str = "btc") -> "Report":
        picks = self.db.execute("SELECT * FROM picks WHERE result IN ('win','loss') AND category=?",
                                (category,)).fetchall()
        pending = self.db.execute("SELECT COUNT(*) FROM picks WHERE result IS NULL AND category=?",
                                  (category,)).fetchone()[0]
        evals = self.db.execute(
            "SELECT p_yes, market_mid, outcome FROM evaluations"
            " WHERE outcome IN (0,1) AND market_mid IS NOT NULL AND category=?", (category,)).fetchall()
        wins = sum(1 for p in picks if p["result"] == "win")
        staked = sum(p["contracts"] * p["price"] for p in picks)
        pnl = sum(p["pnl"] for p in picks)
        expected = sum(p["ev"] * p["contracts"] for p in picks)
        model_brier = _brier([(e["p_yes"], e["outcome"]) for e in evals])
        market_brier = _brier([(e["market_mid"], e["outcome"]) for e in evals])
        return Report(CATEGORY_NAMES.get(category, category), "sharp sportsbooks" if category == "sports" else "model",
                      len(picks), pending, wins, staked, pnl, expected, len(evals), model_brier, market_brier)


@dataclass
class Report:
    name: str
    model_name: str
    resolved_picks: int
    pending_picks: int
    wins: int
    staked: float
    pnl: float
    expected_pnl: float
    scored_evals: int
    model_brier: float | None
    market_brier: float | None

    def render(self) -> str:
        lines: list[str | None] = [f"=== {self.name}: track record (simulated at the quoted ask) ==="]
        if self.resolved_picks:
            lines += [
                f"Resolved picks: {self.resolved_picks}  (pending: {self.pending_picks})",
                f"Win rate:       {self.wins / self.resolved_picks:.1%}",
                f"Staked:         ${self.staked:,.2f}",
                f"P&L after fees: ${self.pnl:+,.2f}  (ROI {self.pnl / self.staked:+.1%})" if self.staked else None,
                f"Expected P&L:   ${self.expected_pnl:+,.2f}",
            ]
        else:
            lines.append(f"No resolved picks yet (pending: {self.pending_picks}).")
        lines.append("")
        lines.append(f"=== {self.name}: is the {self.model_name} price better than Kalshi's? (lower Brier = better) ===")
        if self.scored_evals and self.model_brier is not None:
            lines += [
                f"Scored estimates: {self.scored_evals}",
                f"{self.model_name.capitalize()} Brier: {self.model_brier:.4f}",
                f"Kalshi Brier: {self.market_brier:.4f}",
            ]
            if self.scored_evals < 300:
                lines.append("Verdict: not enough data yet. Keep paper-tracking (aim for 300+).")
            elif self.model_brier < self.market_brier:
                lines.append(f"Verdict: the {self.model_name} price is beating Kalshi so far. Still start small.")
            else:
                lines.append(f"Verdict: Kalshi is at least as accurate as the {self.model_name}. "
                             "Do NOT trade real money on it.")
        else:
            lines.append("No settled estimates yet. Run `watch` for a while; results are scored automatically.")
        return "\n".join(line for line in lines if line is not None)


def _brier(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
