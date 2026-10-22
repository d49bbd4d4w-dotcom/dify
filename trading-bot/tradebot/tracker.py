"""SQLite log of every evaluation and every suggested pick, plus scoring.

This is the most important part of the bot: it tells you, with real outcomes,
whether the model is actually better than the market. If it isn't, don't trade.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .analyzer import Evaluation
from .model import kalshi_fee

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    minutes_left REAL,
    spot REAL,
    p_yes REAL,
    market_mid REAL,
    outcome INTEGER  -- 1 yes, 0 no, NULL unresolved
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
    result TEXT,  -- 'win' / 'loss' / NULL
    pnl REAL,
    UNIQUE(ticker, side)
);
"""


class Tracker:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def log_evaluation(self, e: Evaluation, spot: float) -> None:
        if e.p_yes is None:
            return
        self.db.execute(
            "INSERT INTO evaluations(ts, ticker, minutes_left, spot, p_yes, market_mid) VALUES (?,?,?,?,?,?)",
            (_now(), e.market.ticker, e.minutes_left, spot, e.p_yes, e.market_mid))
        self.db.commit()

    def log_pick(self, e: Evaluation, thesis: str | None = None) -> bool:
        """Record a suggestion. Returns False if this ticker/side was already suggested."""
        assert e.best is not None
        cur = self.db.execute(
            "INSERT OR IGNORE INTO picks(ts, ticker, title, side, price, win_prob, ev, contracts, close_time, thesis)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_now(), e.market.ticker, f"{e.market.title} {e.market.subtitle}".strip(), e.best.side,
             e.best.price, e.best.win_prob, e.best.ev_per_contract, max(e.best.contracts, 1),
             e.market.close_time.isoformat() if e.market.close_time else None, thesis))
        self.db.commit()
        return cur.rowcount > 0

    def unresolved_tickers(self) -> list[str]:
        now = _now()
        rows = self.db.execute(
            "SELECT DISTINCT ticker FROM picks WHERE result IS NULL AND (close_time IS NULL OR close_time < ?)"
            " UNION SELECT DISTINCT ticker FROM evaluations WHERE outcome IS NULL", (now,)).fetchall()
        return [r["ticker"] for r in rows]

    def record_outcome(self, ticker: str, result: str, fee_rate: float = 0.07) -> None:
        """result is Kalshi's 'yes' or 'no'."""
        yes = 1 if result == "yes" else 0
        self.db.execute("UPDATE evaluations SET outcome=? WHERE ticker=? AND outcome IS NULL", (yes, ticker))
        for p in self.db.execute("SELECT * FROM picks WHERE ticker=? AND result IS NULL", (ticker,)).fetchall():
            won = (p["side"] == result)
            n = p["contracts"]
            cost = n * p["price"] + kalshi_fee(n, p["price"], fee_rate)
            pnl = (n if won else 0) - cost
            self.db.execute("UPDATE picks SET result=?, pnl=? WHERE id=?",
                            ("win" if won else "loss", round(pnl, 2), p["id"]))
        self.db.commit()

    def recent_picks(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM picks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def report(self) -> "Report":
        picks = self.db.execute("SELECT * FROM picks WHERE result IS NOT NULL").fetchall()
        pending = self.db.execute("SELECT COUNT(*) FROM picks WHERE result IS NULL").fetchone()[0]
        evals = self.db.execute(
            "SELECT p_yes, market_mid, outcome FROM evaluations WHERE outcome IS NOT NULL AND market_mid IS NOT NULL"
        ).fetchall()
        wins = sum(1 for p in picks if p["result"] == "win")
        staked = sum(p["contracts"] * p["price"] for p in picks)
        pnl = sum(p["pnl"] for p in picks)
        expected = sum(p["ev"] * p["contracts"] for p in picks)
        model_brier = _brier([(e["p_yes"], e["outcome"]) for e in evals])
        market_brier = _brier([(e["market_mid"], e["outcome"]) for e in evals])
        return Report(len(picks), pending, wins, staked, pnl, expected, len(evals), model_brier, market_brier)


@dataclass
class Report:
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
        lines = ["=== Track record (suggested picks, simulated at the quoted ask) ==="]
        if self.resolved_picks:
            lines += [
                f"Resolved picks: {self.resolved_picks}  (pending: {self.pending_picks})",
                f"Win rate:       {self.wins / self.resolved_picks:.1%}",
                f"Staked:         ${self.staked:,.2f}",
                f"P&L after fees: ${self.pnl:+,.2f}  (ROI {self.pnl / self.staked:+.1%})" if self.staked else "",
                f"Model expected: ${self.expected_pnl:+,.2f}",
            ]
        else:
            lines.append(f"No resolved picks yet (pending: {self.pending_picks}).")
        lines.append("")
        lines.append("=== Calibration: is the model better than the market? (lower Brier = better) ===")
        if self.scored_evals and self.model_brier is not None:
            lines += [
                f"Scored evaluations: {self.scored_evals}",
                f"Model Brier:  {self.model_brier:.4f}",
                f"Market Brier: {self.market_brier:.4f}",
            ]
            if self.scored_evals < 300:
                lines.append("Verdict: not enough data yet. Keep paper-tracking (aim for 300+).")
            elif self.model_brier < self.market_brier:
                lines.append("Verdict: model is beating the market price so far. Still start small.")
            else:
                lines.append("Verdict: market is at least as accurate as the model. Do NOT trade real money on it.")
        else:
            lines.append("No resolved evaluations yet. Run `watch` for a while, then `resolve`.")
        return "\n".join(l for l in lines if l is not None)


def _brier(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
