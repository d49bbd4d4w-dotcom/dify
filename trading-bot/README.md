# tradebot: Kalshi Bitcoin and sports pick finder

A personal research bot for Kalshi. It has two parts:

- **Bitcoin:** it watches Kalshi's **"Bitcoin up or down in 15 minutes"** markets (`KXBTC15M`), estimates the real odds from live BTC prices, and tells you when Kalshi's price looks wrong by enough to beat the fees.
- **Sports:** it compares Kalshi's game-winner markets (NFL, NBA, MLB, NHL, college, soccer, UFC, tennis and more) with the prices at sharp sportsbooks, and tells you when Kalshi is selling a team cheaper than the sportsbooks think it's worth. See [Sports picks](#sports-picks).

It also has a simple BTC swing/day-trade view you can use for spot trading on Robinhood.

**It only makes suggestions. It never logs into your accounts or places trades.** You decide and you click.

---

## How it decides (Bitcoin)

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
5. **It suggests a size** from your paper bankroll (see [Bankroll](#bankroll)).
6. **It keeps score.** Every pick and every estimate is saved and scored once the market settles. The `report` command shows your win rate, profit and loss after fees, and whether the model is actually more accurate than the market price.

The optional `--explain` flag uses Claude to write a short plain-English thesis and list risks for each pick. The numbers always come from the model; Claude only explains them.

---

## Please read before using real money

- **Kalshi's 15-minute BTC markets are very competitive.** Professional market makers price them with similar math. Most of the time the bot will say "no picks", and that's correct.
- **Paper-track first.** Run `watch` for at least a week, then run `report`. Only consider real money if the model's **Brier score** is below the market's across 300 or more estimates, and the simulated profit after fees is positive. Brier score measures accuracy, and lower is better. If those conditions aren't met, the honest conclusion is that the bot has no edge.
- **Never stake money you can't afford to lose.** Keep `bankroll` and `max_stake` small.

---

## Bankroll

Set `bankroll` in `config.toml` to the money you're putting aside (default **$100**). Every alert says exactly how much to bet:

```
Bet: 4 contracts = $2.00 (2% of your $100.00 paper bankroll)
```

**How stakes are sized:** stakes follow the **Kelly criterion**, the standard formula for growing a bankroll as fast as possible without going broke. The bot uses a quarter of the full Kelly amount, which is safer.
- Strong edges at good odds get more. Long shots get very little.
- No single bet is more than **5% of the bankroll** or `max_stake` ($10).
- At most **60%** of the bankroll is ever in unsettled bets at once. Picks beyond that are tracked but not suggested.

**It compounds:** stakes are based on the *current* paper bankroll, meaning your starting amount plus the results of every bet the bot has suggested. So they grow when it's winning and shrink when it's losing. `report` shows where it stands:

```
=== Paper bankroll: started $100.00 → now $112.40 (+12.4%) | in open bets: $8.50 ===
```

---

## "Math is off" checks (arbitrage)

On every scan, the bot also looks for spots where Kalshi's own prices contradict each other, so that buying a set of contracts pays out **no matter what happens**, for less than it costs:

- **Games:** buying YES on every outcome (both teams, plus "Tie" in soccer) costs less than $1.
- **"Above $X" ladders** (`KXBTCD`): a higher price target is selling for more than a lower one, which can't be right, because BTC can't finish above the higher target without also being above the lower one.
- **Price ranges** (`KXBTC`): the ranges together cover every possible price, yet cost less than $1.

```
🧮 MATH IS OFF: locked-in profit (arbitrage)
Boston at New York
• Buy YES on Boston @ 45¢
• Buy YES on New York @ 50¢
One set costs 98.5¢ incl. fees and pays at least $1.00 whatever happens (+1.5¢ per set)
```

These are **rare, small, and usually gone within seconds**, so treat them as a bonus. Always check every price before buying, and buy all the legs or none. For American football, it notes that a tie could make the set lose, so check Kalshi's tie rules.

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
- **Phone alerts on Telegram:** see [Telegram alerts](#telegram-alerts) below. Discord also works: create a webhook (Server Settings → Integrations → Webhooks) and set `DISCORD_WEBHOOK_URL` to its URL.

Each time you open a new terminal, run `cd dify/trading-bot` and `source .venv/bin/activate` first.

---

## Using it

```bash
python -m tradebot scan -v            # check every open market once; -v also shows the ones it passed on
python -m tradebot scan --explain     # same, with a Claude-written thesis for each pick
python -m tradebot watch              # BTC every 60s + sports (if set up), log everything, auto-score results
python -m tradebot watch --only btc   # just Bitcoin (or --only sports)
python -m tradebot sports -v          # check sports once (needs ODDS_API_KEY, see below)
python -m tradebot report             # track record + "is the model better than Kalshi?", BTC and sports separately
python -m tradebot swing              # daily BTC trend view (for Robinhood spot trades)
python -m tradebot swing --timeframe 1h --explain   # intraday view with a write-up
```

Example pick:

```
PICK  KXBTC15M-26OCT221415-15  BTC price up in next 15 mins?
      BUY YES @ $0.52  | model 63% vs price 52¢  | edge +9.3¢/contract after fees (+18%)
      size: 9 contracts ≈ $4.68  | closes in 9.1 min
```

To act on a pick, open that market in the Kalshi app and buy the side shown, at or below the price shown. If the price has moved, the edge may be gone, so skip it.

### Telegram alerts

`watch` can send each new pick to Telegram, then send a ✅ WIN or ❌ LOSS message with the profit or loss once that market settles. Times are shown in your timezone (`timezone` in `config.toml`, Pacific by default). A pick alert looks like this:

```
🟢 PICK: BUY YES @ $0.52
BTC up in 15 mins?
Model 62% chance vs price 52¢ | edge +8.3¢ after fees
Size: 8 contracts (~$4.16)
Closes in 10 min (11:23 PM PDT)
Ticker: KXBTC15M-26OCT220415-15
```

Setup takes about 3 minutes:

1. **Create the bot.** In Telegram, open a chat with **@BotFather**, send `/newbot`, and pick a name. BotFather replies with a token that looks like `123456789:AA...`. Keep it secret, because anyone with the token can post as your bot.
2. **Give the token to tradebot.**
   ```bash
   export TELEGRAM_BOT_TOKEN=123456789:AA...      # Windows: setx TELEGRAM_BOT_TOKEN 123456789:AA...
   ```
3. **Choose where alerts go.**
   - **Just you (simplest):** open your new bot in Telegram and send it any message, such as "hi".
   - **A channel** (so friends can follow): create the channel, add the bot as an **admin** that is allowed to post, then post anything in the channel.
4. **Find the chat ID.**
   ```bash
   python -m tradebot telegram-setup
   ```
   This lists the chats your bot can see. Set the one you want:
   ```bash
   export TELEGRAM_CHAT_ID=123456789               # a channel ID looks like -100...
   python -m tradebot telegram-setup               # sends a test message
   ```
5. **Start watching.** Run `python -m tradebot watch`. Alerts only arrive while `watch` is running, so for alerts while you're away, run it on an always-on machine (see below).

Each pick is sent only once, even though `watch` checks every minute. To turn off the win/loss messages, set `notify_results = false` in `config.toml`.

---

## Sports picks

### How it works

The bot doesn't try to guess who will win; that rarely works. Instead it uses the prices at **sharp sportsbooks** as the "true" odds:

1. **Kalshi:** it finds every open game-winner market for the sports Kalshi offers. No login is needed.
2. **Sportsbook prices:** from [The Odds API](https://the-odds-api.com) it fetches the moneyline (who wins) for the same games from 10 sources in one request:
   - **Sharp sources:** Pinnacle; the betting exchanges **Betfair, Matchbook and Smarkets**; and BetOnline and LowVig. These are places where winning bettors aren't limited, so their prices are the most accurate.
   - **The big US books** (DraftKings, FanDuel, BetMGM, Caesars): used only as a fallback. Their prices are less accurate, and adding more books like them would make the "true" price *worse*, not better.

   The fair price is the **average of the sharp sources** (Pinnacle counts double). If none of them price the game, it uses the middle (median) price of at least 3 US books.
3. **Fair odds:** it removes the bookmaker's built-in margin to get fair win chances. For example, odds that imply 52% / 52% become 50% / 50%.
4. **Comparing with Kalshi:** if Kalshi sells a side for less than its fair chance, by at least **2¢ per contract after Kalshi's fee**, you get a pick.
5. **Safety checks:** it skips the game if any of these are true:
   - the game has already started
   - the game is more than 3 days away
   - the sportsbooks disagree with each other
   - their prices are older than 30 minutes
   - Kalshi's market is thin
   - Kalshi and the books disagree by more than 10 points. A gap that big usually means injury news or a team-name matching mistake.
6. **Keeping score:** results are tracked and scored separately from Bitcoin, so `report` shows each one on its own.

Sportsbooks aren't legal in California, but you don't bet with them. The bot only reads their prices. You place your bets on **Kalshi**. Check that the Kalshi app lets you trade sports where you are.

**What to expect:** edges are usually small (2–5¢), so it only pays off over many picks. Kalshi's sports prices often track the sportsbooks closely, so some days there will be few or no picks. Paper-track it first, like Bitcoin.

### Setup (5 minutes)

1. Sign up for a free key at **https://the-odds-api.com**. They email it to you.
2. Add it to `config.toml`:
   ```
   odds_api_key = "your-key-here"
   ```
   Or set it in the terminal with `export ODDS_API_KEY=your-key-here`.
3. Check what it covers:
   ```bash
   python -m tradebot sports --list-series
   ```
4. Run one check and see what it finds and skips:
   ```bash
   python -m tradebot sports -v
   ```
5. Run `python -m tradebot watch`. It now does BTC every minute and sports every 30 minutes, with alerts going to the same Telegram channel.

Example sports alert:

```
🏀 SPORTS PICK: BUY YES @ $0.50
Los Angeles L at Los Angeles C: Los Angeles L to win
Sharp books 56% chance vs price 50¢ | edge +4.2¢ after fees
Size: 4 contracts (~$2.00)
Starts Thu Oct 22 12:21 PM PDT
Ticker: KXNBAGAME-26OCT22LALLAC-LAL
```

### Big-hitter parlays (the default)

By default the bot only sends **big-payout parlays** to Telegram, in three sizes:

- **25x+**
- **50x+**
- **100x+**

For each size, it checks **every combination** of qualifying legs and sends the one with the **highest chance of hitting** at that payout. A qualifying leg passes every safety check above, Kalshi sells it below the sharp-book price, and there's only one leg per game. That's usually a mix of a few favorites and one or two underdogs, which is safer than stacking all underdogs for the same payout.

```
🎟️ BIG HITTER · 50x+ · 5 legs · paper pick
1. ⚽ Arsenal to win (Arsenal vs Fulham) @ 63¢ · Thu Oct 22 1:00 AM PDT
2. 🏈 Miami to win (Miami at Florida St.) @ 48¢ · Thu Oct 22 9:00 AM PDT
3. 🏈 Kansas City to win (Las Vegas at Kansas City) @ 76¢ · Thu Oct 22 10:00 AM PDT
4. 🏈 Tennessee to win (Tennessee at Ole Miss) @ 32¢ · Thu Oct 22 12:00 PM PDT
5. 🏀 Orlando to win (Orlando at Boston) @ 27¢ · Thu Oct 22 4:00 PM PDT
True chance: 3.1% (about 1 in 33)
Bet $1.00 → pays ~$50.36 (50.4x) if Kalshi prices it like the legs
Only take it if Kalshi pays at least 33.6x
Expected profit: +$0.54 on $1.00, but most likely you lose the $1.00.
(For a $100 bankroll the math says $0.28; $1.00 is the practical minimum. Long shots deserve small stakes.)
```

The bot sends at most one ticket of each size every 12 hours (`parlay_alert_cooldown_hours`). Only the tickets it sends count in the paper results, so `report` reflects exactly what you'd have bet.

**Styles (`sports_style`):**

| Style | What gets sent to Telegram |
|---|---|
| `"bankroll"` (default) | Single picks and big-hitter parlays, all sized from your bankroll. |
| `"big"` | Big-hitter parlays only. Single picks are tracked but not sent. |
| `"all"` | Single picks plus the safer / value / long-shot parlays below. |

### All parlay types (`sports_style = "all"`)

In this mode each sports check can suggest up to three parlays, built from the same qualifying legs:

| Ticket | Built from | Typical chance | Typical payout (per $1) |
|---|---|---|---|
| 🎟️ **Safer** | 2–3 favorites (each 65%+ to win) | 30–50% | $2–$3.50 |
| 🎟️ **Value** | up to 4 of the best-priced legs, each 40%+ | 10–25% | $4–$10 |
| 🎟️ **Long shot** | best-priced legs until it pays 50x+ | about 1 in 50 to 1 in 200 | $50+ |

Example:

```
🎟️ LONG-SHOT PARLAY · 3 legs · paper pick
1. ⚽ Brentford to win (Brentford at Liverpool) @ 15¢ · Thu Oct 22 3:00 AM PDT
2. 🏀 Orlando to win (Orlando at Boston) @ 27¢ · Thu Oct 22 4:00 PM PDT
3. 🏒 San Jose to win (San Jose at Edmonton) @ 24¢ · Thu Oct 22 7:00 PM PDT
True chance: 1.4% (about 1 in 70)
Bet $1.00 → pays ~$102.88 (102.9x) if Kalshi prices it like the legs
Only take it if Kalshi pays at least 72.4x
Expected profit: +$0.46 on $1.00, but most likely you lose the $1.00.
```

**There's always a tradeoff between chance and payout.** A parlay only pays if every leg wins, so each leg you add shrinks the chance of hitting. A $20 ticket that pays $2,000 has to lose about 99 times out of 100; if it didn't, the market would charge more for it. What the bot adds is that every leg is priced *below* its fair chance. A parlay multiplies that edge, so the *average* result is positive. Most individual tickets will still lose.

For example, a 1-in-70 long shot played once a day goes a whole month without hitting about 65% of the time.

**How to use the numbers:**
- **True chance** comes from the sharp sportsbooks, with the bookmaker margin removed.
- **Payout** is estimated by multiplying the single-game Kalshi prices. The bot can't see the price Kalshi would charge for a combined bet.
- **"Only take it if Kalshi pays at least"** is the lowest payout that still leaves an edge. When you build the combo in Kalshi's app, divide its max payout by its cost and compare with this number. If Kalshi pays less, skip it.

Parlays are paper-tracked like everything else. You get a 🎟️ ✅ HIT or ❌ MISSED message (with the leg that lost), and `report` shows each kind's hits next to how many you'd *expect* by chance. A cancelled game drops out of the parlay, the same way sportsbooks handle it.

### About Odds API credits

Each sports check costs 1 credit per sport that has games on Kalshi. Sports that are out of season, or that have no Kalshi games, cost nothing. The **free plan has 500 credits a month**, enough for a few full checks per day. The bot automatically **slows its sports checks down to make your credits last the whole month**, and tells you when it does. If you want sports checked every 30 minutes all month, their $30/month plan (20,000 credits) covers it.

---

### Where to run it

Start by running it **on your own computer**. Leave `watch` running in a terminal window while you're at the desk. If you want Telegram alerts while you're away from your computer, it needs to run 24/7. A small cloud server ($5/month on DigitalOcean, Hetzner or similar) or a Raspberry Pi works well: copy the folder over and run `watch` inside `tmux` or `screen`. A 15-minute market needs checks every minute, so scheduled jobs like GitHub Actions aren't a good fit.

---

## Settings (`config.toml`)

| Setting | Default | What it does |
|---|---|---|
| `series` | `["KXBTC15M"]` | Kalshi series to scan. Add `"KXBTCD"` for the hourly/daily "BTC above $X" ladder. |
| `min_edge` | `0.03` | Minimum expected profit per contract after fees ($). |
| `max_disagreement` | `0.25` | Skip a market when the model and the market differ by more than this. |
| `vol_multiplier` | `1.15` | Raise this to make the model more cautious (BTC has fat tails). |
| `momentum_weight` | `0.0` | Experimental trend tilt. Leave at 0 unless `report` shows it helps. |
| `bankroll` | `100` | Starting paper bankroll. All stakes are sized from it (see [Bankroll](#bankroll)). |
| `kelly_multiplier` | `0.25` | Fraction of full Kelly to bet (quarter-Kelly). |
| `max_stake`, `max_bet_fraction` | `10`, `0.05` | Biggest single bet: $10 or 5% of the bankroll, whichever is smaller. |
| `max_open_fraction` | `0.6` | Most of the bankroll that can be in unsettled bets at once. |
| `arbs_enabled` | `true` | Look for "math is off" arbitrage on Kalshi. |
| `odds_api_key` | — | Your The Odds API key (or set `ODDS_API_KEY`). Turns sports on. |
| `sports_min_edge` | `0.02` | Minimum profit per contract after fees for a sports pick. |
| `sports_max_disagreement` | `0.10` | Skip when Kalshi and the books differ by more than this (news or mismatch). |
| `sports_poll_minutes` | `30` | Fastest sports check rate; slowed automatically to fit your credits. |
| `sports_style` | `"bankroll"` | What gets sent: see the styles table under [Big-hitter parlays](#big-hitter-parlays-the-default). |
| `big_hitter_payouts` | `[25, 50, 100]` | Payout sizes for big-hitter tickets (multiples of the stake). |
| `parlay_alert_cooldown_hours` | `12` | At most one ticket of each size per this many hours. |
| `parlays_enabled` | `true` | Turn parlay suggestions on or off. |
| `parlay_longshot_payout` | `50` | A "long shot" pays at least this many times the stake. |
| `sports_series` | auto | Limit to certain Kalshi series, e.g. `["KXNFLGAME", "KXNBAGAME"]`. |
| `timezone` | `America/Los_Angeles` | Timezone for times in alerts. |

## Developing

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Code layout: `tradebot/model.py` holds the probability, fee and sizing math. `analyzer.py` turns market data into picks. `sources/` holds the data fetchers. `tracker.py` handles the SQLite log and scoring. `sports.py` matches Kalshi games to sportsbook games and prices them. `parlays.py` builds parlays from those legs, and `arbs.py` runs the "math is off" checks. `swing.py` is the spot view, and `thesis.py` writes the Claude explanations.

### Ideas for later

- Add a Kalshi API key to read your own positions and balance (still read-only)
- Add more signals, such as funding rates, ETF flows or a news feed, and keep each one only if `report` shows it helps
- Add a small web dashboard of picks and results
- Two-way Telegram commands (e.g. send `/report` to the bot to get your track record)
- Sports point spreads and totals (over/under), when Kalshi and the books offer the same line
