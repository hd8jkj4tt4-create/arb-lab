# Arb Lab

Local, automatic arbitrage scanner for Italian football. It polls odds from a
provider, finds every match where the best prices across different bookmakers
imply a total probability below 100%, computes the stake split, stores history
in SQLite, and shows everything in a live dashboard.

Zero dependencies: Python 3.10+ standard library only.

```sh
./run.sh            # or: python3 app.py
open http://127.0.0.1:8765
```

## Demo mode (works immediately)

Default `PROVIDER=demo` generates a deterministic simulated slate of Serie A,
Serie B and Coppa Italia matches with realistic bookmaker margins and a few
deliberate price gaps. Use it to explore the maths and the UI. The odds are
**not real**.

## Live mode

### Option A — The Odds API (free tier, no Sisal)

1. Get a key at <https://the-odds-api.com>.
2. `cp .env.example .env`, then set:

```env
PROVIDER=theoddsapi
ODDS_API_KEY=your_key
REGIONS=eu
POLL_SECONDS=3600
```

`REGIONS=eu` returns a basket of EU bookmakers. To pin specific books, use the
exact API keys from the bookmaker list and leave `REGIONS` empty:

```env
BOOKMAKERS=unibet_it,betsson
```

Sisal is **not** in The Odds API catalogue, so this provider cannot see Sisal
prices.

### Option B — Odds-API.io (paid, includes Sisal)

1. Subscribe at <https://odds-api.io> (Solo plan: 2 bookmakers).
2. Set:

```env
PROVIDER=oddsapiio
ODDS_API_IO_KEY=your_key
BOOKMAKERS=Sisal IT,Snai IT
```

This provider calls `/v3/arbitrage-bets` pre-filtered to football, keeps Italian
leagues, and normalizes the legs (including `netOdds` after exchange fees and
`optimalStakes`) into the same dashboard format. Use the exact bookmaker names
shown in your odds-api.io plan; other ADM names include `Eurobet IT`,
`Goldbet IT`, `Lottomatica IT`.

## What the numbers mean

For every outcome take the best price across books:

```
S = 1/odds_home + 1/odds_draw + 1/odds_away
```

- `S < 1` → arbitrage. Margin = `1/S - 1`.
- stake for outcome *i* = `bankroll × (1/odds_i) / S`
- every outcome returns `bankroll / S`, so profit is locked **if** every leg is
  accepted at the quoted price.
- `maxStake` caps are respected: the whole split is scaled down so no leg
  exceeds its limit.

Each card tells you exactly what to do: the pick (`1`, `X` or `2`), the
bookmaker, the odds, the stake for that pick, and the total return if it wins
(stake included). The card footer shows the worst return and the net profit.

Each card also marks a **Best single**: the outcome with the highest
market-implied probability at the best available price. That is the most likely
single winner, not a prediction and not risk-free — the arbitrage itself needs
every outcome covered. Use it only if you deliberately want one bet instead of
the arb.

`S` between 1.000 and 1.020 is shown as a near miss. The threshold setting only
controls which arbs trigger alerts; all arbs are still listed.

## Automation

- Poller thread scans every `POLL_SECONDS` (minimum 30), immune to provider
  errors (exponential backoff up to 15 min).
- New arbs are deduplicated per 0.5 percentage-point margin bucket and 30-minute
  cooldown, persisted in `data/arb.db`.
- Dashboard polls `/api/state` every 10 s and can show browser notifications.
- Optional Telegram push: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in
  `.env` (create a bot with @BotFather, send it a message, read the chat id from
  `getUpdates`).
- **No bets are ever placed.** The app only reads odds and does arithmetic.

## Quota maths

**The Odds API:** one odds request costs 1 credit per region per market. Default
coverage is 3 sports × 1 market (`h2h`) × 1 region = 3 credits per full scan.
When you pin bookmakers, every group of 10 bookmakers counts as one region.

- every 5 min → 864 credits/day
- every 30 min → 144 credits/day
- every 6 h → 12 credits/day ≈ 360/month

The free tier is small, so use long intervals or demo mode for continuous
monitoring. Check the `Quota` tile: it reads the `x-requests-remaining` header.

**Odds-API.io:** rate-limited by plan (Solo: 5,000 requests/hour), not metered
per sport; `POLL_SECONDS=300` is safe on paid plans.

## API

| Method | Path            | Purpose                                      |
| ------ | --------------- | -------------------------------------------- |
| GET    | `/api/state`    | Latest payload: stats, opportunities, alerts |
| GET    | `/api/health`   | Liveness, last run, last error               |
| GET    | `/api/history`  | Alert history (`?limit=30`)                  |
| POST   | `/api/refresh`  | Wake the poller immediately                  |
| POST   | `/api/settings` | Update `bankroll`, `threshold`, `min_stake`, `alerts` |

Settings persist in `data/settings.json`; snapshots and alerts in
`data/arb.db`. Delete `data/` to reset.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## Limitations and legal notes

- Prices move in seconds. A quoted arb can vanish or be limited before you
  stake; the margin is compensation for that risk, not a guarantee.
- Cross-book arbs require accounts at multiple ADM-licensed operators. Bonus
  abuse, multi-accounting and automated bet placement violate operator terms
  and can lead to account closure or confiscation.
- Settlement rules differ per book (extra time, void conditions, dead heats,
  maximum payouts). Verify each leg before staking.
- Some books restrict or ban arbitrage customers. Stake sizes and account
  history matter.
- 18+. Bet only with money you can afford to lose. This is not financial
  advice. The software is provided as-is, with no warranty.
