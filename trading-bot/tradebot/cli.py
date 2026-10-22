"""Command line entry point: python -m tradebot <command>."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from . import analyzer, notify, swing, thesis
from .config import Config, load_config
from .sources import Http, crypto
from .sources.kalshi import Kalshi
from .tracker import Tracker

DISCLAIMER = "Suggestions only — a model estimate, not financial advice. Nothing is traded for you."


def fmt_pick(e: analyzer.Evaluation) -> str:
    m, b = e.market, e.best
    assert b is not None
    return (
        f"PICK  {m.ticker}  {m.title} {m.subtitle}\n"
        f"      BUY {b.side.upper()} @ ${b.price:.2f}  | model {b.win_prob:.0%} vs market {e.market_mid or 0:.0%}"
        f"  | edge {b.ev_per_contract * 100:+.1f}¢/contract after fees ({b.edge_pct:+.0%})\n"
        f"      size: {b.contracts} contracts ≈ ${b.stake:.2f}  | closes in {e.minutes_left:.1f} min"
        + (f"\n      note: {e.strike_note}" if e.strike_note else "")
    )


def fmt_skip(e: analyzer.Evaluation) -> str:
    p = f"model {e.p_yes:.0%}" if e.p_yes is not None else "model n/a"
    mid = f"market {e.market_mid:.0%}" if e.market_mid is not None else "market n/a"
    return f"pass  {e.market.ticker:<34} {p:<10} {mid:<11} — {e.skip_reason}"


def alert_text(e: analyzer.Evaluation, note: str | None) -> str:
    """Compact, phone-friendly version of a pick."""
    m, b = e.market, e.best
    assert b is not None
    closes = f" ({m.close_time:%H:%M} UTC)" if m.close_time else ""
    text = "\n".join([
        f"🟢 PICK: BUY {b.side.upper()} @ ${b.price:.2f}",
        f"{m.title} {m.subtitle}".strip(),
        f"Model {b.win_prob:.0%} vs market {e.market_mid or 0:.0%} | edge {b.ev_per_contract * 100:+.1f}¢ after fees",
        f"Size: {b.contracts} contracts (~${b.stake:.2f})",
        f"Closes in {e.minutes_left:.0f} min{closes}",
        f"Ticker: {m.ticker}",
    ])
    if note:
        text += f"\n\n{note}"
    return text + "\n\nModel estimate, not financial advice. Skip it if the price has moved."


def result_text(p: dict) -> str:
    icon = "✅ WIN" if p["result"] == "win" else "❌ LOSS"
    return (f"{icon} {p['pnl']:+.2f} USD\n{p['title']}\n"
            f"{p['side'].upper()} @ ${p['price']:.2f} x {p['contracts']} | {p['ticker']}")


def scan_once(cfg: Config, http: Http, tracker: Tracker, explain: bool, verbose: bool,
              notifier: notify.Notifier | None = None) -> list[analyzer.Evaluation]:
    kalshi = Kalshi(http)
    ctx = analyzer.build_context(http, cfg)
    now = datetime.now(timezone.utc)
    evals: list[analyzer.Evaluation] = []
    for series in cfg.series:
        try:
            markets = kalshi.open_markets(series)
        except Exception as e:
            print(f"[warn] could not load Kalshi series {series}: {e}", file=sys.stderr)
            continue
        evals += [analyzer.evaluate_market(m, ctx, cfg, now) for m in markets]

    print(f"\n[{now:%Y-%m-%d %H:%M:%S} UTC] BTC ${ctx.spot:,.2f} | vol {ctx.sigma_per_min * 100:.3f}%/min"
          f" | 15m mom {(ctx.mom_15m or 0) * 100:+.2f}%"
          + (f" | F&G {ctx.fear_greed[0]} {ctx.fear_greed[1]}" if ctx.fear_greed else ""))
    for w in ctx.warnings:
        print(f"[warn] {w}")

    ranked = analyzer.rank(evals)
    picks = [e for e in ranked if e.is_pick]
    for e in evals:
        tracker.log_evaluation(e, ctx.spot)
    for e in picks:
        note = thesis.write_thesis(e, ctx, cfg) if explain else None
        is_new = tracker.log_pick(e, note)
        print(fmt_pick(e) + ("" if is_new else "\n      (already suggested earlier)"))
        if note:
            print("      " + note.replace("\n", "\n      "))
        if is_new and notifier:
            notifier.send(alert_text(e, note))
    if not picks:
        print("No picks right now — nothing clears the edge threshold after fees.")
    if verbose:
        for e in ranked:
            if not e.is_pick:
                print(fmt_skip(e))
    return evals


def resolve(cfg: Config, http: Http, tracker: Tracker, notifier: notify.Notifier | None = None) -> int:
    kalshi = Kalshi(http)
    n = 0
    for ticker in tracker.unresolved_tickers():
        try:
            m = kalshi.market(ticker)
        except Exception as e:
            print(f"[warn] {ticker}: {e}", file=sys.stderr)
            continue
        if m.result in ("yes", "no"):
            settled = tracker.record_outcome(ticker, m.result, cfg.fee_rate)
            n += 1
            if notifier and cfg.notify_results:
                for p in settled:
                    notifier.send(result_text(p))
    return n


def cmd_swing(cfg: Config, http: Http, timeframe: str, explain: bool) -> None:
    gran = {"1h": 3600, "6h": 21600, "1d": 86400}[timeframe]
    candles = crypto.coinbase_candles(http, gran)
    fg = None
    try:
        fg = crypto.fear_greed(http)
    except Exception:
        pass
    view = swing.analyze(candles, timeframe, fg)
    text = view.render()
    print(text)
    if explain:
        note = thesis.write_swing_thesis(text, cfg)
        if note:
            print("\n" + note)
    print(f"\n{DISCLAIMER}")


def cmd_telegram_setup(cfg: Config, http: Http) -> None:
    token = cfg.telegram_bot_token
    if not token:
        print("No bot token yet. In Telegram, message @BotFather, send /newbot, follow the prompts,\n"
              "then set TELEGRAM_BOT_TOKEN (or telegram_bot_token in config.toml) and run this again.")
        return
    if cfg.telegram_chat_id:
        notify.telegram(http, token, cfg.telegram_chat_id, "✅ tradebot is connected. Picks will show up here.")
        print(f"Sent a test message to chat {cfg.telegram_chat_id}. Check Telegram.")
        return
    chats = notify.telegram_chats(http, token)
    if not chats:
        print("The bot hasn't seen any chats yet. Open your bot in Telegram and send it any message\n"
              "(or add it as an admin to your channel and post something), then run this again.")
        return
    print("Chats your bot can post to:")
    for chat_id, name in chats:
        print(f"  {chat_id}   {name}")
    print("\nSet TELEGRAM_CHAT_ID (or telegram_chat_id in config.toml) to one of these ids,\n"
          "then run this again to send a test message.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tradebot", description="Kalshi BTC + crypto suggestion bot. " + DISCLAIMER)
    ap.add_argument("--config", help="path to config.toml (default: ./config.toml if present)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="evaluate open Kalshi BTC markets once")
    s.add_argument("--explain", action="store_true", help="add a Claude-written thesis to each pick")
    s.add_argument("-v", "--verbose", action="store_true", help="also show markets that were passed on")

    w = sub.add_parser("watch", help="scan repeatedly and auto-score past picks")
    w.add_argument("--interval", type=int, default=60, help="seconds between scans (default 60)")
    w.add_argument("--explain", action="store_true")
    w.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("resolve", help="fetch results for closed markets and score picks")
    sub.add_parser("report", help="show track record and model-vs-market calibration")

    sw = sub.add_parser("swing", help="BTC spot swing/day-trade view (for Robinhood etc.)")
    sw.add_argument("--timeframe", choices=["1h", "6h", "1d"], default="1d")
    sw.add_argument("--explain", action="store_true")

    sub.add_parser("telegram-setup", help="find your Telegram chat id and send a test alert")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    http = Http()

    if args.cmd == "swing":
        cmd_swing(cfg, http, args.timeframe, args.explain)
        return
    if args.cmd == "telegram-setup":
        cmd_telegram_setup(cfg, http)
        return

    notifier = notify.Notifier(http, cfg)

    tracker = Tracker(cfg.db_path)
    if args.cmd == "scan":
        scan_once(cfg, http, tracker, args.explain, args.verbose, notifier)
        print(f"\n{DISCLAIMER}")
    elif args.cmd == "watch":
        print(f"Watching {', '.join(cfg.series)} every {args.interval}s. Ctrl+C to stop.\n{DISCLAIMER}")
        if not notifier.enabled:
            print("[info] no Telegram/Discord alerts configured; picks only print here.")
        last_resolve = 0.0
        while True:
            try:
                scan_once(cfg, http, tracker, args.explain, args.verbose, notifier)
                if time.time() - last_resolve > 300:
                    resolved = resolve(cfg, http, tracker, notifier)
                    if resolved:
                        print(f"[info] scored {resolved} settled market(s). Run `report` to see results.")
                    last_resolve = time.time()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # keep the loop alive through network blips
                print(f"[error] {e}", file=sys.stderr)
            time.sleep(args.interval)
    elif args.cmd == "resolve":
        print(f"Scored {resolve(cfg, http, tracker, notifier)} settled market(s).")
    elif args.cmd == "report":
        print(tracker.report().render())
        recent = tracker.recent_picks(10)
        if recent:
            print("\nRecent picks:")
            for p in recent:
                res = p["result"] or "pending"
                pnl = f"{p['pnl']:+.2f}" if p["pnl"] is not None else ""
                print(f"  {p['ts'][:16]}  {p['ticker']:<32} {p['side'].upper():<3} @ {p['price']:.2f}  {res:<7} {pnl}")

