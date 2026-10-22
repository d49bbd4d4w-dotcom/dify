"""Command line entry point: python -m tradebot <command>."""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import analyzer, arbs, model, notify, parlays, sports, swing, thesis, weather
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


def stake_line(b: model.Opportunity, cfg: Config) -> str:
    pct = b.stake / cfg.bankroll if cfg.bankroll else 0
    return f"Bet: {b.contracts} contracts = ${b.stake:.2f} ({pct:.0%} of your ${cfg.bankroll:,.2f} paper bankroll)"


def with_bankroll(cfg: Config, tracker: Tracker) -> Config:
    """Size bets from the current paper bankroll (start + results so far)."""
    return dataclasses.replace(cfg, bankroll=max(0.0, tracker.current_bankroll(cfg.bankroll)))


def room_left(cfg: Config, tracker: Tracker) -> float:
    """How much more can go into open bets without passing max_open_fraction."""
    return cfg.bankroll * cfg.max_open_fraction - tracker.open_exposure()


def fit_to_room(b: model.Opportunity, room: float) -> bool:
    """Shrink a bet so open bets stay under the cap. False if not even 1 contract fits."""
    if b.stake > room:
        b.contracts = int(max(room, 0.0) // b.price)
        b.stake = round(b.contracts * b.price, 2)
    return b.contracts > 0


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
        stake_line(b, cfg),
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
        f"Sharp books ({e.fair_source}) {b.win_prob:.0%} chance vs price {b.price * 100:.0f}¢"
        f" | edge {b.ev_per_contract * 100:+.1f}¢ after fees",
        stake_line(b, cfg),
    ]
    if e.commence:
        lines.append(f"Starts {local_time(e.commence, cfg, with_day=True)}")
    lines.append(f"Ticker: {m.ticker}")
    return "\n".join(lines) + "\n\nBased on sportsbook prices, not a guarantee. Skip it if the price has moved."


def leg_label(leg: parlays.Leg) -> str:
    return f"{sports.icon(leg.sport_key)} {leg.team} to win ({leg.game})"


def money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def parlay_text(p: parlays.Parlay, cfg: Config, stake: float, raw_stake: float | None = None) -> str:
    lines = [f"🎟️ {parlays.title(p.kind)} · {len(p.legs)} legs · paper pick"]
    far = datetime.max.replace(tzinfo=timezone.utc)
    for i, leg in enumerate(sorted(p.legs, key=lambda x: x.commence or far), 1):
        when = f" · {local_time(leg.commence, cfg, with_day=True)}" if leg.commence else ""
        lines.append(f"{i}. {leg_label(leg)} @ {leg.price * 100:.0f}¢{when}")
    odds = f"about 1 in {1 / p.chance:,.0f}" if p.chance < 0.2 else f"wins about {p.chance * 10:.0f} times in 10"
    lines += [
        f"True chance: {p.chance:.1%} ({odds})",
        f"Bet ${stake:,.2f} → pays ~${stake * p.payout:,.2f} ({p.payout:,.1f}x) if Kalshi prices it like the legs",
        f"Only take it if Kalshi pays at least {p.min_payout(cfg.parlay_min_value_margin):,.1f}x",
        f"Expected profit: {money(stake * (p.value - 1))} on ${stake:,.2f}"
        + (f", but most likely you lose the ${stake:,.2f}." if p.chance < 0.5 else "."),
    ]
    if raw_stake is not None and raw_stake < stake:
        lines.append(f"(For a ${cfg.bankroll:,.0f} bankroll the math says ${raw_stake:.2f}; "
                     f"${stake:,.2f} is the practical minimum. Long shots deserve small stakes.)")
    return "\n".join(lines)


def arb_text(a: arbs.Arb, cfg: Config, sets: int) -> str:
    lines = ["🧮 MATH IS OFF: locked-in profit (arbitrage)", a.title]
    for m, side, price in a.legs:
        lines.append(f"• Buy {side.upper()} on {m.subtitle or m.ticker} @ {price * 100:.0f}¢  [{m.ticker}]")
    lines.append(f"One set costs {a.cost * 100:.1f}¢ incl. fees and pays at least ${a.payout:.2f} whatever happens "
                 f"(+{a.profit * 100:.1f}¢ per set)")
    if sets:
        lines.append(f"Up to {sets} sets (~${sets * a.cost:,.2f}) → {money(sets * a.profit)} locked in")
    if a.note:
        lines.append(a.note)
    lines.append("Prices like this usually disappear within seconds. Check every price before buying, "
                 "and buy all legs or none.")
    return "\n".join(lines)


def report_arbs(found: list[arbs.Arb], cfg: Config, tracker: Tracker,
                notifier: notify.Notifier | None) -> None:
    for a in sorted(found, key=lambda a: -a.profit):
        if tracker.arb_seen_recently(a.key):
            continue
        tracker.log_arb(a.key, a.profit)
        sets = int(max(0.0, min(cfg.max_bet(), room_left(cfg, tracker))) // a.cost) if a.cost > 0 else 0
        text = arb_text(a, cfg, sets)
        print("\n" + text)
        if notifier:
            notifier.send(text)


def fmt_weather(e: weather.WeatherEvaluation, cfg: Config) -> str:
    m, b, fc = e.market, e.best, e.forecast
    assert b is not None and fc is not None
    return (f"PICK  {m.ticker}  🌡️ {e.station.name} high on {e.day:%a %b %d}: {m.subtitle}\n"
            f"      BUY {b.side.upper()} @ ${b.price:.2f}  | forecast {b.win_prob:.0%} vs price {b.price * 100:.0f}¢"
            f"  | edge {b.ev_per_contract * 100:+.1f}¢/contract after fees\n"
            f"      {forecast_line(fc)}\n"
            f"      size: {b.contracts} contracts ≈ ${b.stake:.2f}")


def forecast_line(fc: weather.Forecast) -> str:
    parts = []
    if fc.nws_high is not None:
        parts.append(f"NWS {fc.nws_high}°")
    if fc.model_mean is not None:
        parts.append(f"models avg {fc.model_mean:.1f}° ({fc.model_runs} runs)")
    if fc.observed_high is not None:
        parts.append(f"high so far today {fc.observed_high}°")
    return "Forecast: " + ", ".join(parts) + f" → expected {fc.center:.1f}° ± {fc.sd:.1f}°"


def fmt_weather_skip(e: weather.WeatherEvaluation) -> str:
    p = f"forecast {e.p_yes:.0%}" if e.p_yes is not None else "forecast n/a"
    mid = f"Kalshi {e.market_mid:.0%}" if e.market_mid is not None else "Kalshi n/a"
    label = f"{e.station.name[:22]} {e.day:%b %d}" if e.day else e.station.name[:29]
    return f"pass  {label:<29} {e.market.subtitle[:14]:<14} {p:<13} {mid:<11} — {e.skip_reason}"


def weather_alert_text(e: weather.WeatherEvaluation, cfg: Config) -> str:
    m, b, fc = e.market, e.best, e.forecast
    assert b is not None and fc is not None
    outcome = m.subtitle if b.side == "yes" else f"NOT {m.subtitle}"
    return "\n".join([
        f"🌡️ WEATHER PICK: BUY {b.side.upper()} @ ${b.price:.2f}",
        f"{e.station.name} high on {e.day:%a %b %d}: {outcome}",
        forecast_line(fc),
        f"Our chance {b.win_prob:.0%} vs price {b.price * 100:.0f}¢ | edge {b.ev_per_contract * 100:+.1f}¢ after fees",
        stake_line(b, cfg),
        f"Settles on the NWS daily climate report for {e.station.station_id}",
        f"Ticker: {m.ticker}",
    ]) + "\n\nForecast-based, not a guarantee. Skip it if the price has moved."


def weather_scan(cfg: Config, http: Http, tracker: Tracker, verbose: bool,
                 notifier: notify.Notifier | None = None) -> weather.WeatherScan:
    cfg = with_bankroll(cfg, tracker)
    now = datetime.now(timezone.utc)
    scan = weather.collect(http, cfg, now)
    print(f"\n[{local_time(now, cfg, with_day=True)}] Weather: {len(scan.evaluations)} Kalshi markets checked")
    for w in scan.warnings:
        print(f"[warn] {w}")
    send = cfg.sports_style in ("bankroll", "all")
    picks = [e for e in scan.evaluations if e.is_pick]
    for e in scan.evaluations:
        tracker.log_evaluation(e, category="weather")
    for e in picks:
        title = f"{e.station.name} high {e.day:%b %d}: {e.market.subtitle}"
        if tracker.has_pick(e.market.ticker, e.best.side):
            print(fmt_weather(e, cfg) + "\n      (already suggested earlier)")
            continue
        if not send or not fit_to_room(e.best, room_left(cfg, tracker)):
            tracker.log_pick(e, category="weather", title=title, alerted=False)
            print(fmt_weather(e, cfg) + "\n      (tracked only)")
            continue
        tracker.log_pick(e, category="weather", title=title)
        print(fmt_weather(e, cfg))
        if notifier:
            notifier.send(weather_alert_text(e, cfg))
    if not picks:
        print("No weather picks right now — Kalshi prices are in line with the forecasts.")
    if cfg.arbs_enabled:
        report_arbs(scan.arbs, cfg, tracker, notifier)
    if verbose:
        for e in scan.evaluations:
            if not e.is_pick:
                print(fmt_weather_skip(e))
    return scan


def parlay_result_text(p: dict) -> str:
    n = len(p["legs"])
    if p["result"] == "win":
        return f"🎟️ ✅ PARLAY HIT {money(p['pnl'])} ({parlays.title(p['kind'])}, {n} legs)"
    if p["result"] == "void":
        return f"🎟️ ↩️ PARLAY VOID: every game was cancelled ({parlays.title(p['kind'])}, {n} legs)"
    return (f"🎟️ ❌ PARLAY MISSED {money(p['pnl'])} ({parlays.title(p['kind'])}, {n} legs)\n"
            f"Lost on: {', '.join(p['lost_on'])}")


def result_text(p: dict) -> str:
    icon = {"win": "✅ WIN", "loss": "❌ LOSS"}.get(p["result"], "↩️ VOID")
    return (f"{icon} {p['pnl']:+.2f} USD\n{p['title']}\n"
            f"{p['side'].upper()} @ ${p['price']:.2f} x {p['contracts']} | {p['ticker']}")


def scan_once(cfg: Config, http: Http, tracker: Tracker, explain: bool, verbose: bool,
              notifier: notify.Notifier | None = None) -> list[analyzer.Evaluation]:
    cfg = with_bankroll(cfg, tracker)
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
        if tracker.has_pick(e.market.ticker, e.best.side):
            print(fmt_pick(e) + "\n      (already suggested earlier)")
            continue
        if not fit_to_room(e.best, room_left(cfg, tracker)):
            tracker.log_pick(e, alerted=False)
            print(fmt_pick(e) + f"\n      (not suggested: {cfg.max_open_fraction:.0%} of the bankroll is "
                                "already in open bets)")
            continue
        note = thesis.write_thesis(e, ctx, cfg) if explain else None
        is_new = tracker.log_pick(e, note)
        print(fmt_pick(e))
        if note:
            print("      " + note.replace("\n", "\n      "))
        if is_new and notifier:
            notifier.send(alert_text(e, note, cfg))
    if not picks:
        print("No picks right now — nothing clears the edge threshold after fees.")
    if cfg.arbs_enabled:
        found: list[arbs.Arb] = []
        for series in cfg.arb_series:
            try:
                found += arbs.scan_events(kalshi.open_markets(series), cfg)
            except Exception as e:
                print(f"[warn] could not check {series} for arbitrage: {e}", file=sys.stderr)
        report_arbs(found, cfg, tracker, notifier)
    if verbose:
        for e in ranked:
            if not e.is_pick:
                print(fmt_skip(e))
    return evals


def sports_scan(cfg: Config, http: Http, tracker: Tracker, verbose: bool,
                notifier: notify.Notifier | None = None) -> sports.SportsScan:
    cfg = with_bankroll(cfg, tracker)
    now = datetime.now(timezone.utc)
    scan = sports.collect(http, cfg, now)
    credits = f" | Odds API credits left: {scan.credits_remaining:,.0f}" if scan.credits_remaining is not None else ""
    print(f"\n[{local_time(now, cfg, with_day=True)}] Sports: {len(scan.evaluations)} Kalshi markets checked{credits}")
    for w in scan.warnings:
        print(f"[warn] {w}")
    send_singles = cfg.sports_style in ("bankroll", "all")
    big_parlays = cfg.sports_style in ("bankroll", "big")
    picks = [e for e in scan.evaluations if e.is_pick]
    print(f"Paper bankroll: ${cfg.bankroll:,.2f} | in open bets: ${tracker.open_exposure():,.2f}")
    for e in scan.evaluations:
        tracker.log_evaluation(e, category="sports")
    for e in picks:
        title = f"{e.game}: {e.team}"
        if tracker.has_pick(e.market.ticker, e.best.side):
            print(fmt_sports_pick(e, cfg) + "\n      (already suggested earlier)")
            continue
        if not send_singles:  # "big" style: singles are tracked quietly, not suggested
            tracker.log_pick(e, category="sports", title=title, alerted=False)
            print(fmt_sports_pick(e, cfg) + "\n      (tracked only)")
            continue
        if not fit_to_room(e.best, room_left(cfg, tracker)):
            tracker.log_pick(e, category="sports", title=title, alerted=False)
            print(fmt_sports_pick(e, cfg) + f"\n      (not suggested: {cfg.max_open_fraction:.0%} of the "
                                            "bankroll is already in open bets)")
            continue
        tracker.log_pick(e, category="sports", title=title)
        print(fmt_sports_pick(e, cfg))
        if notifier:
            notifier.send(sports_alert_text(e, cfg))
    if not picks:
        print("No sports picks right now — Kalshi prices are in line with the sharp books.")
    if cfg.parlays_enabled:
        legs_now = parlays.legs_from(scan.evaluations, cfg)
        tickets = parlays.big_hitters(legs_now, cfg) if big_parlays else parlays.build(legs_now, cfg)
        for p in tickets:
            last = tracker.last_parlay_time(p.kind)
            if last and now - last < timedelta(hours=cfg.parlay_alert_cooldown_hours):
                if verbose:
                    print(f"(holding a new {parlays.title(p.kind)} ticket: one was sent in the last "
                          f"{cfg.parlay_alert_cooldown_hours:g}h)")
                continue
            stake, raw = parlays.kelly_stake(p, cfg)
            stake = min(stake, round(room_left(cfg, tracker), 2))
            if stake < cfg.parlay_min_stake:
                print(f"(skipping a {parlays.title(p.kind)} ticket: not enough bankroll free for it)")
                continue
            legs = [{"ticker": leg.ticker, "label": leg_label(leg), "p": leg.p, "price": leg.price}
                    for leg in p.legs]
            is_new = tracker.log_parlay(p.kind, legs, stake, p.chance, p.payout)
            text = parlay_text(p, cfg, stake, raw)
            print("\n" + text + ("" if is_new else "\n(already suggested earlier)"))
            if is_new and notifier:
                notifier.send(text + "\n\nBased on sportsbook prices, not a guarantee.")
        if not tickets:
            print(f"No {'big-hitter ' if big_parlays else ''}parlays right now — not enough legs that "
                  "Kalshi prices below the sharp books.")
    if cfg.arbs_enabled:
        events: dict[str, list[sports.SportsEvaluation]] = {}
        for e in scan.evaluations:
            events.setdefault(e.market.event_ticker, []).append(e)
        found: list[arbs.Arb] = []
        for evs in events.values():
            sport = evs[0].sport_key
            if sport.startswith("soccer") and len(evs) < 3:
                continue  # without the "Tie" market the set doesn't cover every result
            tie_risk = len(evs) == 2 and sport.split("_")[0] in ("americanfootball", "mma", "boxing")
            found += arbs.outcome_arbs([e.market for e in evs], cfg, evs[0].game,
                                       "Only risk-free if it can't end in a tie or draw; check Kalshi's rules."
                                       if tie_risk else "")
        report_arbs(found, cfg, tracker, notifier)
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
    for p in tracker.settle_parlays():
        if notifier and cfg.notify_results:
            notifier.send(parlay_result_text(p))
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
    w.add_argument("--only", choices=["btc", "sports", "weather"], help="watch just one market type")
    w.add_argument("--explain", action="store_true")
    w.add_argument("-v", "--verbose", action="store_true")

    wx = sub.add_parser("weather", help="compare Kalshi daily-high markets with NWS + weather models once")
    wx.add_argument("-v", "--verbose", action="store_true", help="also show brackets that were passed on")

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
    if cfg.sports_style not in ("bankroll", "big", "all"):
        raise SystemExit('sports_style in config.toml must be "bankroll", "big" or "all".')
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
    elif args.cmd == "weather":
        weather_scan(cfg, http, tracker, args.verbose, notifier)
        print(f"\n{DISCLAIMER}")
    elif args.cmd == "watch":
        do_btc = args.only in (None, "btc")
        do_sports = args.only in (None, "sports") and bool(cfg.odds_api_key)
        do_weather = args.only in (None, "weather") and cfg.weather_enabled
        if args.only == "sports" and not cfg.odds_api_key:
            raise SystemExit("Sports needs ODDS_API_KEY; see `python -m tradebot sports`.")
        watching = ((["BTC 15-minute"] if do_btc else []) + (["sports"] if do_sports else [])
                    + (["weather"] if do_weather else []))
        print(f"Watching {' + '.join(watching)}. Ctrl+C to stop.\n{DISCLAIMER}")
        if not do_sports and args.only in (None, "sports"):
            print("[info] sports is off: set ODDS_API_KEY to turn it on.")
        if not notifier.enabled:
            print("[info] no Telegram/Discord alerts configured; picks only print here.")
        last_resolve = 0.0
        next_sports = 0.0
        next_weather = 0.0
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
                if do_weather and time.time() >= next_weather:
                    next_weather = time.time() + cfg.weather_poll_minutes * 60
                    try:
                        weather_scan(cfg, http, tracker, args.verbose, notifier)
                    except Exception as e:
                        print(f"[error] weather check failed: {e}", file=sys.stderr)
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
        now_bank = tracker.current_bankroll(cfg.bankroll)
        change = (now_bank - cfg.bankroll) / cfg.bankroll if cfg.bankroll else 0
        print(f"=== Paper bankroll: started ${cfg.bankroll:,.2f} → now ${now_bank:,.2f} ({change:+.1%})"
              f" | in open bets: ${tracker.open_exposure():,.2f} ===\n")
        print("\n\n".join(tracker.report(c).render() for c in tracker.categories()))
        summary = tracker.parlay_summary()
        if summary:
            print("\n" + summary)
        recent = tracker.recent_picks(10)
        if recent:
            print("\nRecent picks:")
            for p in recent:
                res = p["result"] or "pending"
                pnl = f"{p['pnl']:+.2f}" if p["pnl"] is not None else ""
                print(f"  {p['ts'][:16]}  {p['ticker']:<32} {p['side'].upper():<3} @ {p['price']:.2f}  {res:<7} {pnl}")

