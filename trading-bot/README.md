# tradebot: Kalshi 15-minute Bitcoin pick finder

A personal research bot that watches Kalshi's **"Bitcoin up or down in 15 minutes"** markets (`KXBTC15M`), estimates the real odds from live BTC prices, and tells you when Kalshi's price looks wrong by enough to beat the fees. It also has a simple BTC swing/day-trade view you can use for spot trading on Robinhood.

**It only makes suggestions. It never logs into your accounts or places trades.** You decide and you click.

---

## How it decides

1. **It collects live data** from public sources. No API keys or logins are needed.
   - Kalshi: open BTC markets, strikes, bid and ask prices, settlement results
   - Coinbase: 1-minute candles, the order book and the BTC price
   - Kraken and Bitstamp: BTC price, used as a cross-check
   - alternative.me: the Crypto Fear & Greed index
2. **It estimates fair odds.** It measures how much BTC has been moving minute to minute, then works out how likely it is that BTC ends above the strike before the market closes. It also accounts for how Kalshi settles: on the average price over the final 60 seconds, from the CF Benchmarks BRTI index.
3. **It compares those odds to Kalshi's price, after Kalshi's fee.** For example, if the model says YES has a 70% chance and YES costs 55¢ plus about 2¢ in fees, that's roughly a 13¢ edge per contract.
4. **It applies filters.** It only suggests a pick when all of these hold:
   - the edge is at least 3¢ per contract
   - the market has at least 2 minutes left
   - the bid/ask spread isn't too wide
   - the model and the market don't disagree wildly. A huge gap usually means the market knows something the model doesn't, such as news, so the bot passes.
5. **It suggests a size.** It uses "quarter-Kelly" sizing, capped at `max_stake` (default $10).
6. **It keeps score.** Every pick and every estimate is saved and scored once the market settles. The `report` command shows your win rate, profit and loss after fees, and whether the model is actually more accurate than the market price.

The optional `--explain` flag uses Claude to write a short plain-English thesis and list risks for each pick. The numbers always come from the model; Claude only explains them.

---

## Please read before using real money

- **Kalshi's 15-minute BTC markets are very competitive.** Professional market makers price them with similar math. Most of the time the bot will say "no picks", and that's correct.
- **Paper-track first.** Run `watch` for at least a week, then run `report`. Only consider real money if the model's **Brier score** is below the market's across 300 or more estimates, and the simulated profit after fees is positive. Brier score measures accuracy, and lower is better. If those conditions aren't met, the honest conclusion is that the bot has no edge.
- **Never stake money you can't afford to lose.** Keep `bankroll` and `max_stake` small.

---

## Setup (about 10 minutes, one time)

You need Python 3.11 or newer. On a Mac, install it from python.org, or run `brew install python` if you use Homebrew. On Windows, install it from python.org and tick "Add to PATH" during installation.

Then open a terminal (Terminal on Mac, PowerShell on Windows) and run:

```bash
git clone https://github.com/d49bbd4d4w-dotcom/dify.git
cd dify
git checkout claude/trading-bot-data-aggregation-qwpxcz
cd trading-bot

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config.example.toml config.toml # Windows: copy config.example.toml config.toml
```

Optional extras:

- **Claude write-ups:** get an API key at https://console.anthropic.com, then run `export ANTHROPIC_API_KEY=sk-ant-...` (on Windows: `setx ANTHROPIC_API_KEY sk-ant-...`). Each write-up costs about a cent.
- **Phone alerts:** in Discord, create a webhook (Server Settings → Integrations → Webhooks) and paste its URL into `discord_webhook_url` in `config.toml`.

Each time you open a new terminal, run `cd dify/trading-bot` and `source .venv/bin/activate` first.

---

## Using it

```bash
python -m tradebot scan -v            # check every open market once; -v also shows the ones it passed on
python -m tradebot scan --explain     # same, with a Claude-written thesis for each pick
python -m tradebot watch              # re-check every 60s, log everything, auto-score settled markets
python -m tradebot report             # track record + "is the model better than the market?"
python -m tradebot swing              # daily BTC trend view (for Robinhood spot trades)
python -m tradebot swing --timeframe 1h --explain   # intraday view with a write-up
```

Example pick:

```
PICK  KXBTC15M-26OCT221415-15  BTC price up in next 15 mins?
      BUY YES @ $0.52  | model 63% vs market 51%  | edge +9.3¢/contract after fees (+18%)
      size: 9 contracts ≈ $4.68  | closes in 9.1 min
```

To act on a pick, open that market in the Kalshi app and buy the side shown, at or below the price shown. If the price has moved, the edge may be gone, so skip it.

### Where to run it

Start by running it **on your own computer**. Leave `watch` running in a terminal window while you're at the desk. If you later want it running 24/7, a small cloud server ($5/month on DigitalOcean, Hetzner or similar) or a Raspberry Pi works well: copy the folder over and run `watch` inside `tmux` or `screen`. A 15-minute market needs checks every minute, so scheduled jobs like GitHub Actions aren't a good fit.

---

## Settings (`config.toml`)

| Setting | Default | What it does |
|---|---|---|
| `series` | `["KXBTC15M"]` | Kalshi series to scan. Add `"KXBTCD"` for the hourly/daily "BTC above $X" ladder. |
| `min_edge` | `0.03` | Minimum expected profit per contract after fees ($). |
| `max_disagreement` | `0.25` | Skip a market when the model and the market differ by more than this. |
| `vol_multiplier` | `1.15` | Raise this to make the model more cautious (BTC has fat tails). |
| `momentum_weight` | `0.0` | Experimental trend tilt. Leave at 0 unless `report` shows it helps. |
| `bankroll`, `kelly_multiplier`, `max_stake` | `100`, `0.25`, `10` | Position sizing. |

## Developing

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Code layout: `tradebot/model.py` holds the probability, fee and sizing math. `analyzer.py` turns market data into picks. `sources/` holds the data fetchers. `tracker.py` handles the SQLite log and scoring. `swing.py` is the spot view, and `thesis.py` writes the Claude explanations.

### Ideas for later

- Add a Kalshi API key to read your own positions and balance (still read-only)
- Add more signals, such as funding rates, ETF flows or a news feed, and keep each one only if `report` shows it helps
- Add a small web dashboard of picks and results
