# Arb Lab

Local, automatic arbitrage scanner and win-probability dashboard for football
and the NBA. It polls odds from a provider, finds every match where the best
prices across different bookmakers imply a total probability below 100%,
computes the stake split, stores history in SQLite, and shows everything in a
live dashboard. It also fits a small model on previous results and shows 1 / X /
2 win percentages for upcoming fixtures.

Tracked competitions: Serie A, Serie B, Coppa Italia, Champions League, La Liga
and the NBA. NBA markets are two-way: `1` and `2`, with no draw.

Zero dependencies: Python 3.10+ standard library only.

```sh
./run.sh            # or: python3 app.py
open http://127.0.0.1:8765
```

## Demo mode (works immediately)

Default `PROVIDER=demo` generates a deterministic simulated slate of Serie A,
Serie B, Coppa Italia, Champions League, La Liga and NBA matches with realistic
bookmaker margins and a few deliberate price gaps. Use it to explore the maths
and the UI. The odds are **not real**.

The win-probability section is different: it always downloads real previous
results from ESPN, even in demo mode. Only the odds are simulated.

## Win probabilities

The dashboard has a **Win probabilities** section with the next fixtures for
Serie A, La Liga, Champions League and the NBA.

- **Football:** a time-weighted Poisson goals model with a Dixon–Coles low-score
  correction. Attack and defence ratings are fitted separately for each
  competition. Output: `1`, `X`, `2`, expected goals, most likely scoreline,
  probable anytime scorers, recent form and the sample size for each team.
- **NBA:** time-weighted point-margin ratings plus home-court advantage,
  converted to a win probability with a normal approximation. Output: `1`, `2`,
  projected exact score, projected margin, each team's top points-per-game
  scorer and recent form.
- **Exact results and stats:** for football, each fixture shows its most likely
  scoreline and the last-10-match averages for corners won and fouls committed.
  NBA rows add projected made three-pointers. Expanding a fixture lists the
  probable scorers, then the exact scoreline with corners/fouls (and 3PM for the
  NBA) for every recent match. These stats are descriptive, not inputs to the
  win model.
- **Data:** [ESPN's public scoreboard API](https://site.api.espn.com/apis/site/v2/sports).
  No key is required. Football corners/fouls and NBA 3PM come from the same
  scoreboard; NBA fouls, NBA scoring leaders and football scorer stats need
  extra endpoints and are cached in `data/espn_stats.json`,
  `data/espn_nba_leaders.json` and `data/espn_rosters.json`.
- **Validation:** each competition is refitted on an earlier training window
  and scored on a holdout period. The dashboard shows holdout accuracy, log loss
  and the number of test matches. This is a sanity check, not a promise.
- **Caching:** the model refreshes every `PREDICTIONS_REFRESH_MINUTES` (default 30).
  The snapshot is stored in `data/predictions.json`. If ESPN is unreachable, the
  previous snapshot is kept and marked `cached`.
- **Training data:** only finished matches whose final score is available
  (`state=post`). Scheduled, live, postponed and cancelled events never enter the
  model, so every goal/point in the ratings was actually scored.
- **Fixtures:** only matches that have not started, within the next 14 days,
  soonest first.

## Injury report

A separate chart lists players unavailable for the clubs in the upcoming
fixtures: player, club, injury, status, days out, expected return date and a
short/medium/long absence estimate. It is **not** an input to the win model.

- **Football:** [Transfermarkt's injury tables](https://www.transfermarkt.us)
  for Serie A, La Liga and the Champions League. The page provides the injury
  type, the date it started and the expected return. ESPN does not publish a
  soccer injury feed.
- **NBA:** ESPN's league injury report, including status (`Out`,
  `Day-To-Day`), body part, expected return and the latest report note.
- **Severity:** based on the expected time remaining (≤7 days short, 8–28
  medium, >28 long); without a return date it falls back to the injury text.
  It is an absence estimate, not a medical diagnosis.
- **Clubs:** Transfermarkt club names are normalised to the ESPN names used in
  the fixtures; only clubs playing in the next 14 days are shown.
- **Caching:** injury feeds refresh every 6 hours and are stored in
  `data/injuries.json`, so a temporary block does not remove the last report.

## Deploy on Render

The repo includes `render.yaml`. In Render: **New + → Blueprint**, pick the
`arb-lab` repository, then paste the value of `ODDS_API_KEY` when prompted.

- The service binds to `0.0.0.0` and uses Render's `PORT` automatically.
- `HOST`, sports, poll interval and prediction refresh are set in
  `render.yaml`; edit them there (or in the Render dashboard) and redeploy.
- The free plan spins down after 15 minutes without traffic. While asleep the
  background poller does not run; the dashboard and a fresh scan start on the
  next visit. `data/` is ephemeral on the free plan, so the database and caches
  are rebuilt after each restart/deploy.
- `ODDS_API_KEY` is a secret and is never committed; `.env` is gitignored.


The model uses previous results only. It does not know lineups, injuries,
suspensions, travel, managerial changes or current market prices. Treat the
percentages as a second opinion, not as a betting edge.

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

The sport keys are `soccer_spain_la_liga`, `soccer_uefa_champs_league` and
`basketball_nba`, in addition to the Italian football keys. Sisal is **not** in
The Odds API catalogue, so this provider cannot see Sisal prices.

### Option B — Odds-API.io (paid, includes Sisal)

1. Subscribe at <https://odds-api.io> (Solo plan: 2 bookmakers).
2. Set:

```env
PROVIDER=oddsapiio
ODDS_API_IO_KEY=your_key
BOOKMAKERS=Sisal IT,Snai IT
ODDS_API_IO_SPORTS=football,basketball
```

This provider calls `/v3/arbitrage-bets` for football and basketball, keeps the
tracked competitions, and normalizes the legs (including `netOdds` after
exchange fees and `optimalStakes`) into the same dashboard format. Use the exact
bookmaker names shown in your odds-api.io plan; other ADM names include
`Eurobet IT`, `Goldbet IT`, `Lottomatica IT`.

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
NBA cards are two-way and show only `1` and `2`.

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
- Prediction thread refreshes the ESPN snapshot every
  `PREDICTIONS_REFRESH_MINUTES` (minimum 5).
- Dashboard polls `/api/state` every 10 s, `/api/predictions` every 2 min, and
  can show browser notifications.
- Optional Telegram push: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in
  `.env` (create a bot with @BotFather, send it a message, read the chat id from
  `getUpdates`).
- **No bets are ever placed.** The app only reads odds and does arithmetic.

## Quota maths

**The Odds API:** one odds request costs 1 credit per region per market. The
default six tracked sports × 1 market (`h2h`) × 1 region = 6 credits per full
scan. When you pin bookmakers, every group of 10 bookmakers counts as one
region.

- every 5 min → 1,728 credits/day
- every 30 min → 288 credits/day
- every 6 h → 24 credits/day ≈ 720/month

The free tier is small, so use long intervals, fewer sports (`SPORTS=...`) or
demo mode for continuous monitoring. Check the `Quota` tile: it reads the
`x-requests-remaining` header.

**Odds-API.io:** rate-limited by plan (Solo: 5,000 requests/hour), not metered
per sport; `POLL_SECONDS=300` is safe on paid plans. Each scan makes one request
per entry in `ODDS_API_IO_SPORTS` (two by default).

## API

| Method | Path                    | Purpose                                      |
| ------ | ----------------------- | -------------------------------------------- |
| GET    | `/api/state`            | Latest payload: stats, opportunities, alerts |
| GET    | `/api/predictions`      | Cached win-probability snapshot              |
| GET    | `/api/health`           | Liveness, last run, last error               |
| GET    | `/api/history`          | Alert history (`?limit=30`)                  |
| POST   | `/api/refresh`          | Wake the poller and prediction model         |
| POST   | `/api/predictions/refresh` | Rebuild the win model in the background   |
| POST   | `/api/settings`         | Update `bankroll`, `threshold`, `min_stake`, `alerts` |

Settings persist in `data/settings.json`; snapshots and alerts in
`data/arb.db`; the prediction cache in `data/predictions.json`. Delete `data/`
to reset.

## Project layout

```
app.py           stdlib HTTP server + polling threads
core.py          arbitrage maths, competition filters
providers.py     demo, The Odds API and odds-api.io feeds
predictions.py   ESPN download, Poisson / NBA models, cache
store.py         SQLite snapshots, alerts, settings
static/          dashboard (HTML/CSS/vanilla JS)
tests/           arbitrage, parsing and model tests
data/            git-ignored cache and SQLite database
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## Limitations and legal notes

- Prices move in seconds. A quoted arb can vanish or be limited before you
  stake; the margin is compensation for that risk, not a guarantee.
- Win probabilities are fitted on previous results and have no access to
  lineups, injuries, suspensions, travel, motivation or market prices. A good
  holdout score does not imply a profitable betting strategy.
- NBA has no draw; the dashboard shows a two-way market for NBA games.
- Champions League historical scores may include extra time on knockout ties;
  the model does not reconstruct the 90-minute result.
- Cross-book arbs require accounts at multiple ADM-licensed operators. Bonus
  abuse, multi-accounting and automated bet placement violate operator terms
  and can lead to account closure or confiscation.
- Settlement rules differ per book (extra time, void conditions, dead heats,
  maximum payouts). Verify each leg before staking.
- Some books restrict or ban arbitrage customers. Stake sizes and account
  history matter.
- 18+. Bet only with money you can afford to lose. This is not financial
  advice. The software is provided as-is, with no warranty.
