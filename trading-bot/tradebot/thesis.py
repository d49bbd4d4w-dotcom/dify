"""Optional plain-English write-ups of a pick, using Claude.

The numbers (probability, edge, size) always come from the quant model. Claude
only explains them and lists what could go wrong; it is told not to invent a
different probability.
"""

from __future__ import annotations

import anthropic

from .analyzer import BtcContext, Evaluation
from .config import Config

SYSTEM = (
    "You are a cautious trading analyst writing a short note for a retail trader. "
    "You are given the output of a quantitative model for a Kalshi Bitcoin contract "
    "and the market context it used. Explain the thesis in plain English in 3-5 sentences, "
    "then give 2-3 concrete risks or reasons the pick could be wrong. Use only the data "
    "provided; do not invent news, prices or a different probability. Do not use hype. "
    "End with one line stating this is a model estimate, not a guarantee."
)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def context_text(ctx: BtcContext) -> str:
    fg = f"{ctx.fear_greed[0]} ({ctx.fear_greed[1]})" if ctx.fear_greed else "n/a"
    imb = "n/a" if ctx.book_imbalance is None else f"{ctx.book_imbalance:+.2f}"
    return (
        f"BTC spot: ${ctx.spot:,.2f} (sources: {', '.join(f'{k} ${v:,.0f}' for k, v in ctx.spot_sources.items())})\n"
        f"Per-minute volatility: {ctx.sigma_per_min * 100:.3f}% "
        f"(EWMA {ctx.sigma_ewma * 100:.3f}%, last-hour {ctx.sigma_realized * 100:.3f}%)\n"
        f"Momentum: 5m {_pct(ctx.mom_5m)}, 15m {_pct(ctx.mom_15m)}, 60m {_pct(ctx.mom_60m)}\n"
        f"Coinbase order book imbalance near mid (+1 all bids, -1 all asks): {imb}\n"
        f"Crypto Fear & Greed index: {fg}\n"
        + ("Data warnings: " + "; ".join(ctx.warnings) + "\n" if ctx.warnings else "")
    )


def write_thesis(e: Evaluation, ctx: BtcContext, cfg: Config) -> str | None:
    if not cfg.anthropic_enabled or e.best is None:
        return None
    m, b = e.market, e.best
    strike = (f"floor ${m.floor_strike:,.2f}" if m.floor_strike is not None else "") + \
             (f" cap ${m.cap_strike:,.2f}" if m.cap_strike is not None else "")
    prompt = (
        f"Contract: {m.title} {m.subtitle} [{m.ticker}]\n"
        f"Strike: {strike or e.strike_note}\n"
        f"Minutes until close: {e.minutes_left:.1f}\n"
        f"Market: yes bid {m.yes_bid}, yes ask {m.yes_ask}, no ask {m.no_ask}, mid {e.market_mid}\n"
        f"Model P(YES): {e.p_yes:.3f}\n"
        f"Suggested: buy {b.side.upper()} at ${b.price:.2f}; model win prob {b.win_prob:.3f}; "
        f"fee ~${b.fee_per_contract:.3f}/contract; expected profit {b.ev_per_contract * 100:.1f}¢/contract; "
        f"size {b.contracts} contracts (${b.stake:.2f})\n\n"
        f"Market context:\n{context_text(ctx)}"
    )
    return _ask(SYSTEM, prompt, cfg)


def write_swing_thesis(summary: str, cfg: Config) -> str | None:
    if not cfg.anthropic_enabled:
        return None
    return _ask(SYSTEM.replace("a Kalshi Bitcoin contract", "a Bitcoin spot swing trade"), summary, cfg)


def _ask(system: str, prompt: str, cfg: Config) -> str | None:
    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=cfg.thesis_model,
            max_tokens=2000,
            # If a request is declined, let the API retry it on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": cfg.thesis_effort},
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as err:
        return f"(thesis unavailable: {err.__class__.__name__})"
    if resp.stop_reason == "refusal":
        return "(thesis unavailable: request declined)"
    return "".join(b.text for b in resp.content if b.type == "text").strip() or None
