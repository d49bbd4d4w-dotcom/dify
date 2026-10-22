"""Command line entry point: python -m tradebot <command>."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import analyzer, notify, sports, swing, thesis
from .config import Config, load_config
from .sources import Http, crypto
from .sources.kalshi import Kalshi
from .sources.odds_api import OddsApi
from .tracker import Tracker

DISCLAIMER = "Suggestions only — a model estimate, not financial advice. Nothing is traded for you."


def local_time(dt: datetime, cfg: Config, with_day: bool = False) -> str:
    t = dt.astimezone(ZoneInfo(cfg.timezone))
    clock = t.strftime("%I:%M %p").lstrip("0")
    return (t.strftime("%a %b %d ") if with_day else "") + f"{clock} {t.strftime('%Z')}"


def fmt_pick(e: analyzer.Evaluation) -> str:
    m, b = e.market, e.best
    assert b is not None
    return (
        f"PICK  {m.ticker}  {m.title} {m.subtitle}\n"
        f"      BUY {b.side.upper()} @ ${b.price:.2f}  | model {b.win_prob:.0%} vs price {b.price * 100:.0f}¢"
        f"  | edge {b.ev_per_contract * 100:+.1f}¢/contract after fees ({b.edge_pct:+.0%})\n"
        f"      size: {b.contracts} contracts ≈ ${b.stake:.2f}  | closes in {e.minutes_left:.1f} min"
        + (f"\n      note: {e.strike_note}" if e.strike_note else "")
    )


def fmt_skip(e: analyzer.Evaluation) -> str:
    p = f"model {e.p_yes:.0%}" if e.p_yes is not None else "model n/a"
    mid = f"market {e.market_mid:.0%}" if e.market_mid is not None else "market n/a"
    return f"pass  {e.market.ticker:<34} {p:<10} {mid:<11} — {e.skip_reason}"


def alert_text(e: analyzer.Evaluation, note: str | None, cfg: Config) -> str:
    """Compact, phone-friendly version of a pick."""
    m, b = e.market, e.best
    assert b is not None
    closes = f" ({local_time(m.close_time, cfg)})" if m.close_time else ""
    text = "\n".join([
        f"🟢 PICK: BUY {b.side.upper()} @ ${b.price:.2f}",
        f"{m.title} {m.subtitle}".strip(),
        f"Model {b.win_prob:.0%} chance vs price {b.price * 100:.0f}¢ | edge {b.ev_per_contract * 100:+.1f}¢ after fees",
        f"Size: {b.contracts} contracts (~${b.stake:.2f})",
        f"Closes in {e.minutes_left:.0f} min{closes}",
        f"Ticker: {m.ticker}",
    ])
    if note:
        text += f"\n\n{note}"
    return text + "\n\nModel estimate, not financial advice. Skip it if the price has moved."


def fmt_sports_pick(e: sports.SportsEvaluation, cfg: Config) -> str:
    m, b = e.market, e.best
    assert b is not None
    starts = f"starts {local_time(e.commence, cfg, with_day=True)}" if e.commence else ""
    return (
        f"PICK  {m.ticker}  {sports.icon(e.sport_key)} {e.game}: {e.team}\n"
        f"      BUY {b.side.upper()} @ ${b.price:.2f}  | books {b.win_prob:.0%} ({e.fair_source}) vs price "
        f"{b.price * 100:.0f}¢  | edge {b.ev_per_contract * 100:+.1f}¢/contract after fees ({b.edge_pct:+.0%})\n"
        f"      size: {b.contracts} contracts ≈ ${b.stake:.2f}  | {starts}"
    )


def fmt_sports_skip(e: sports.SportsEvaluation) -> str:
    p = f"books {e.p_yes:.0%}" if e.p_yes is not None else "books n/a"
    mid = f"Kalshi {e.market_mid:.0%}" if e.market_mid is not None else "Kalshi n/a"
    return f"pass  {e.game[:34]:<34} {e.team[:18]:<18} {p:<10} {mid:<11} — {e.skip_reason}"


def sports_alert_text(e: sports.SportsEvaluation, cfg: Config) -> str:
    m, b = e.market, e.best
    assert b is not None
    side = "to win" if b.side == "yes" else "NOT to win"
    lines = [
        f"{sports.icon(e.sport_key)} SPORTS PICK: BUY {b.side.upper()} @ ${b.price:.2f}",
        f"{e.game}: {e.team} {side}",
        f"Sharp books {b.win_prob:.0%} chance vs price {b.price * 100:.0f}¢ | edge {b.ev_per_contract * 100:+.1f}¢ after fees",
        f"Size: {b.contracts} contracts (~${b.stake:.2f})",
    ]
    if e.commence:
        lines.append(f"Starts {local_time(e.commence, cfg, with_day=True)}")
    lines.append(f"Ticker: {m.ticker}")
    return "\n".join(lines) + "\n\nBased on sportsbook prices, not a guarantee. Skip it if the price has moved."


def result_text(p: dict) -> str:
    icon = {"win": "✅ WIN", "loss": "❌ LOSS"}.get(p["result"], "↩️ VOID")
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
            notifier.send(alert_text(e, note, cfg))
    if not picks:
        print("No picks right now — nothing clears the edge threshold after fees.")
    if verbose:
        for e in ranked:
            if not e.is_pick:
                print(fmt_skip(e))
    return evals


def sports_scan(cfg: Config, http: Http, tracker: Tracker, verbose: bool,
                notifier: notify.Notifier | None = None) -> sports.SportsScan:
    now = datetime.now(timezone.utc)
    scan = sports.collect(http, cfg, now)
    credits = f" | Odds API credits left: {scan.credits_remaining:,.0f}" if scan.credits_remaining is not None else ""
    print(f"\n[{local_time(now, cfg, with_day=True)}] Sports: {len(scan.evaluations)} Kalshi markets checked{credits}")
    for w in scan.warnings:
        print(f"[warn] {w}")
    picks = [e for e in scan.evaluations if e.is_pick]
    for e in scan.evaluations:
        tracker.log_evaluation(e, category="sports")
    for e in picks:
        is_new = tracker.log_pick(e, category="sports", title=f"{e.game}: {e.team}")
        print(fmt_sports_pick(e, cfg) + ("" if is_new else "\n      (already suggested earlier)"))
        if is_new and notifier:
            notifier.send(sports_alert_text(e, cfg))
    if not picks:
        print("No sports picks right now — Kalshi prices are in line with the sharp books.")
    if verbose:
        for e in scan.evaluations:
            if not e.is_pick:
                print(fmt_sports_skip(e))
    if scan.credits_remaining is not None and scan.credits_remaining < 50:
        print(f"[warn] only {scan.credits_remaining:.0f} Odds API credits left this month.")
    return scan


def resolve(cfg: Config, http: Http, tracker: Tracker, notifier: notify.Notifier | None = None) -> int:
    kalshi = Kalshi(http)
    tickers = tracker.unresolved_tickers()
    wanted = set(tickers)
    markets = {}
    try:
        markets = {m.ticker: m for m in kalshi.markets(tickers) if m.ticker in wanted}
    except Exception as e:
        print(f"[warn] batch result lookup failed: {e}", file=sys.stderr)
    for ticker in [t for t in tickers if t not in markets][:30]:  # fall back to one at a time
        try:
            markets[ticker] = kalshi.market(ticker)
        except Exception as e:
            print(f"[warn] {ticker}: {e}", file=sys.stderr)
    n = 0
    for ticker, m in markets.items():
        if m.result in ("yes", "no"):
            settled = tracker.record_outcome(ticker, m.result, cfg.fee_rate)
        elif m.result:  # e.g. "void": game cancelled, money refunded
            settled = tracker.record_void(ticker)
        else:
            continue
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


def cmd_sports_series(cfg: Config, http: Http) -> None:
    kalshi = Kalshi(http)
    active = OddsApi(http, cfg.odds_api_key).active_sports() if cfg.odds_api_key else None
    print("Kalshi game series the bot will scan:")
    for series in sports.discover_series(kalshi, cfg):
        code = sports.series_code(series)
        if code not in sports.SPORT_MAP:
            print(f"  {series:<20} not supported yet")
        elif active is None:
            print(f"  {series:<20} -> {', '.join(sports.SPORT_MAP[code])}")
        else:
            keys = sports.odds_keys_for(series, active)
            print(f"  {series:<20} -> {', '.join(keys) if keys else 'out of season'}")
    if active is None:
        print("\n(Set ODDS_API_KEY to also see which sports are in season.)")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tradebot", description="Kalshi BTC + sports suggestion bot. " + DISCLAIMER)
    ap.add_argument("--config", help="path to config.toml (default: ./config.toml if present)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="evaluate open Kalshi BTC markets once")
    s.add_argument("--explain", action="store_true", help="add a Claude-written thesis to each pick")
    s.add_argument("-v", "--verbose", action="store_true", help="also show markets that were passed on")

    w = sub.add_parser("watch", help="scan repeatedly and auto-score past picks")
    w.add_argument("--interval", type=int, default=60, help="seconds between BTC scans (default 60)")
    w.add_argument("--only", choices=["btc", "sports"], help="watch just one of BTC or sports")
    w.add_argument("--explain", action="store_true")
    w.add_argument("-v", "--verbose", action="store_true")

    sp = sub.add_parser("sports", help="compare Kalshi sports markets with sharp sportsbooks once")
    sp.add_argument("-v", "--verbose", action="store_true", help="also show games that were passed on")
    sp.add_argument("--list-series", action="store_true", help="show which Kalshi sports the bot covers")

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
    if args.cmd == "sports" and args.list_series:
        cmd_sports_series(cfg, http)
        return
    if args.cmd == "sports" and not cfg.odds_api_key:
        raise SystemExit("Sports needs a free Odds API key: sign up at https://the-odds-api.com,\n"
                         "then set ODDS_API_KEY (or odds_api_key in config.toml).")

    notifier = notify.Notifier(http, cfg)

    tracker = Tracker(cfg.db_path)
    if args.cmd == "scan":
        scan_once(cfg, http, tracker, args.explain, args.verbose, notifier)
        print(f"\n{DISCLAIMER}")
    elif args.cmd == "sports":
        sports_scan(cfg, http, tracker, args.verbose, notifier)
        print(f"\n{DISCLAIMER}")
    elif args.cmd == "watch":
        do_btc = args.only != "sports"
        do_sports = args.only != "btc" and bool(cfg.odds_api_key)
        if args.only == "sports" and not cfg.odds_api_key:
            raise SystemExit("Sports needs ODDS_API_KEY; see `python -m tradebot sports`.")
        watching = (["BTC 15-minute"] if do_btc else []) + (["sports"] if do_sports else [])
        print(f"Watching {' + '.join(watching)}. Ctrl+C to stop.\n{DISCLAIMER}")
        if not do_sports and args.only != "btc":
            print("[info] sports is off: set ODDS_API_KEY to turn it on.")
        if not notifier.enabled:
            print("[info] no Telegram/Discord alerts configured; picks only print here.")
        last_resolve = 0.0
        next_sports = 0.0
        while True:
            try:
                if do_btc:
                    scan_once(cfg, http, tracker, args.explain, args.verbose, notifier)
                if do_sports and time.time() >= next_sports:
                    # Even if this check fails, wait before retrying so a bad key or outage
                    # doesn't stop BTC or result scoring.
                    next_sports = time.time() + cfg.sports_poll_minutes * 60
                    try:
                        scan = sports_scan(cfg, http, tracker, args.verbose, notifier)
                    except Exception as e:
                        print(f"[error] sports check failed: {e}", file=sys.stderr)
                    else:
                        wait = sports.paced_interval_minutes(cfg.sports_poll_minutes, scan.credits_remaining,
                                                             scan.credits_used, datetime.now(timezone.utc))
                        next_sports = time.time() + wait * 60
                        if wait > cfg.sports_poll_minutes:
                            print(f"[info] next sports check in {wait:.0f} min to make your Odds API credits "
                                  "last the month.")
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
        print("\n\n".join(tracker.report(c).render() for c in tracker.categories()))
        recent = tracker.recent_picks(10)
        if recent:
            print("\nRecent picks:")
            for p in recent:
                res = p["result"] or "pending"
                pnl = f"{p['pnl']:+.2f}" if p["pnl"] is not None else ""
                print(f"  {p['ts'][:16]}  {p['ticker']:<32} {p['side'].upper():<3} @ {p['price']:.2f}  {res:<7} {pnl}")

