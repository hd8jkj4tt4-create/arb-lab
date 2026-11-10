"""Odds providers.

- demo: deterministic simulated Italian football slate (always works, no key)
- theoddsapi: https://the-odds-api.com (free tier, EU books, no Sisal)
- oddsapiio: https://odds-api.io (paid, covers the 48 ADM books incl. Sisal)

Every provider returns normalized Event objects (for local evaluation) or
precomputed Evaluation objects (when the provider already calculated arbs).
"""
from __future__ import annotations

import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from core import (
    DRAW, ITALY_SPORTS, BestPrice, BookLine, Evaluation, Event,
    round_stake,
)
import core


class ProviderError(RuntimeError):
    pass


@dataclass
class ProviderResult:
    events: list[Event] = field(default_factory=list)
    precomputed: list[Evaluation] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def http_json(url: str, timeout: int = 25) -> tuple[Any, dict[str, str]]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ArbLab/1.0 (local research tool)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return json.loads(body.decode("utf-8")), headers
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ProviderError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"Invalid JSON from provider: {exc}") from exc


# ---------------------------------------------------------------------------
# Demo provider
# ---------------------------------------------------------------------------

DEMO_FIXTURES = [
    # id, league, home, away, true 1X2 probabilities
    ("demo-1", "Serie A", "Inter", "Milan", (0.42, 0.30, 0.28)),
    ("demo-2", "Serie A", "Juventus", "Roma", (0.44, 0.28, 0.28)),
    ("demo-3", "Serie A", "Napoli", "Atalanta", (0.45, 0.27, 0.28)),
    ("demo-4", "Serie A", "Lazio", "Fiorentina", (0.40, 0.29, 0.31)),
    ("demo-5", "Serie B", "Palermo", "Sampdoria", (0.46, 0.27, 0.27)),
    ("demo-6", "Serie B", "Parma", "Cremonese", (0.38, 0.31, 0.31)),
    ("demo-7", "Coppa Italia", "Bologna", "Torino", (0.43, 0.29, 0.28)),
    ("demo-8", "Coppa Italia", "Atalanta", "Genoa", (0.51, 0.26, 0.23)),
]

DEMO_BOOKS = [
    # key, title, base margin, homepage
    ("sisal", "Sisal", 0.055, "https://www.sisal.it/scommesse-matchpoint"),
    ("snai", "Snai", 0.060, "https://www.snai.it/sport"),
    ("eurobet", "Eurobet", 0.052, "https://www.eurobet.it/it/scommesse/"),
    ("unibet_it", "Unibet IT", 0.050, "https://www.unibet.it/betting"),
    ("betsson", "Betsson", 0.058, "https://www.betsson.it/scommesse"),
    ("lottomatica", "Lottomatica", 0.062, "https://www.lottomatica.it/scommesse"),
]

# A deliberate price gap, so the demo always contains something to inspect.
DEMO_BOOSTS = {
    "demo-1": ("sisal", "Inter", 1.14),
    "demo-3": ("snai", "Atalanta", 1.14),
    "demo-6": ("eurobet", "Parma", 1.15),
    "demo-8": ("lottomatica", "Atalanta", 1.14),
}

def _demo_start() -> datetime:
    """Next 18:00 UTC, so demo kickoffs stay in the future."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=18, minute=0, second=0, microsecond=0)
    return start if start > now else start + timedelta(days=1)


class DemoProvider:
    key = "demo"
    label = "Demo (simulated Italian football)"

    def fetch(self, cfg) -> ProviderResult:
        return ProviderResult(
            events=self._events(cfg),
            meta={"provider": "demo", "quota": None, "simulated": True},
            warnings=["Demo data: not real odds. Add an API key for live Italian football."],
        )

    def _events(self, cfg) -> list[Event]:
        minute_bucket = int(time.time() // 60)
        start = _demo_start()
        events: list[Event] = []
        for index, (event_id, league, home, away, probs) in enumerate(DEMO_FIXTURES):
            kickoff = start + timedelta(hours=index * 2)
            books: list[BookLine] = []
            for book_key, title, base_margin, href in DEMO_BOOKS:
                rng = random.Random(f"{event_id}:{book_key}:{minute_bucket}")
                odds: dict[str, float] = {}
                for label, p in zip((home, DRAW, away), probs):
                    margin = base_margin + rng.uniform(-0.008, 0.008)
                    price = (1.0 / p) * (1.0 - margin) * math.exp(rng.gauss(0, 0.025))
                    price *= math.exp(rng.gauss(0, 0.004))  # small tick every poll
                    if DEMO_BOOSTS.get(event_id, (None, None, 0))[0] == book_key:
                        target = DEMO_BOOSTS[event_id][1]
                        if target == label:
                            price *= DEMO_BOOSTS[event_id][2]
                    odds[label] = max(1.01, round(price, 2))
                max_stakes = {}
                if book_key in ("sisal", "snai") and index % 3 == 0:
                    max_stakes = {label: 400.0 for label in odds}
                books.append(BookLine(book_key, title, odds, href, max_stakes))
            events.append(
                Event(
                    id=event_id,
                    league=league,
                    home=home,
                    away=away,
                    commence=kickoff.isoformat().replace("+00:00", "Z"),
                    books=books,
                    source="demo",
                )
            )
        return events


# ---------------------------------------------------------------------------
# The Odds API
# ---------------------------------------------------------------------------

THE_ODDS_API = "https://api.the-odds-api.com/v4"


class TheOddsAPIProvider:
    key = "theoddsapi"
    label = "The Odds API (live, EU books)"

    def __init__(self, api_key: str, regions: str, bookmakers: list[str], sports: list[str]):
        self.api_key = api_key
        self.regions = regions
        self.bookmakers = bookmakers
        self.sports = sports

    def fetch(self, cfg) -> ProviderResult:
        if not self.api_key:
            raise ProviderError("ODDS_API_KEY is missing. Put it in .env or switch PROVIDER=demo.")

        events: list[Event] = []
        warnings: list[str] = []
        quota = {"remaining": None, "used": None, "last_cost": None}
        headers_seen: dict[str, str] = {}

        for sport in self.sports:
            params = {
                "apiKey": self.api_key,
                "markets": "h2h",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
                "includeLinks": "true",
            }
            if self.bookmakers:
                params["bookmakers"] = ",".join(self.bookmakers)
            else:
                params["regions"] = self.regions
            url = f"{THE_ODDS_API}/sports/{urllib.parse.quote(sport)}/odds/?" + urllib.parse.urlencode(params)
            data, headers = http_json(url)
            headers_seen = headers
            for key, dest in (("x-requests-remaining", "remaining"), ("x-requests-used", "used"), ("x-requests-last", "last_cost")):
                if key in headers:
                    try:
                        value = int(float(headers[key]))
                    except ValueError:
                        continue
                    if dest == "remaining":
                        quota[dest] = value if quota[dest] is None else min(quota[dest], value)
                    else:
                        quota[dest] = value
            for item in data:
                event = self._parse_event(item)
                if event is not None:
                    events.append(event)

        remaining = quota.get("remaining")
        if isinstance(remaining, int) and remaining < 50:
            warnings.append(f"Low API quota: {remaining} requests left. Raise POLL_SECONDS.")
        return ProviderResult(
            events=events,
            meta={
                "provider": "theoddsapi",
                "quota": quota,
                "books": sorted({b.title for e in events for b in e.books}),
            },
            warnings=warnings,
        )

    def _parse_event(self, item: dict[str, Any]) -> Event | None:
        sport_key = item.get("sport_key", "")
        league = ITALY_SPORTS.get(sport_key, item.get("sport_title", "Italy"))
        home = item.get("home_team") or "Home"
        away = item.get("away_team") or "Away"
        books: list[BookLine] = []
        for bm in item.get("bookmakers", []):
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                odds: dict[str, float] = {}
                for outcome in market.get("outcomes", []):
                    name = outcome.get("name")
                    try:
                        price = float(outcome.get("price"))
                    except (TypeError, ValueError):
                        continue
                    if name and price > 1.0:
                        odds[name] = price
                if len(odds) >= 2:
                    books.append(
                        BookLine(
                            key=bm.get("key", ""),
                            title=bm.get("title", bm.get("key", "Book")),
                            odds=odds,
                            href=bm.get("link") or None,
                        )
                    )
        if not books:
            return None
        return Event(
            id=str(item.get("id")),
            league=league,
            home=home,
            away=away,
            commence=item.get("commence_time", ""),
            books=books,
            source="theoddsapi",
        )


# ---------------------------------------------------------------------------
# odds-api.io (Sisal, Snai, Eurobet, Goldbet, ...)
# ---------------------------------------------------------------------------

ODDS_API_IO = "https://api.odds-api.io/v3"
SIDE_TO_CODE = {"home": "1", "draw": "X", "away": "2"}


class OddsApiIoProvider:
    key = "oddsapiio"
    label = "Odds-API.io (ADM books incl. Sisal)"

    def __init__(self, api_key: str, bookmakers: list[str], markets: str = "ML"):
        self.api_key = api_key
        self.bookmakers = bookmakers
        self.markets = markets

    def fetch(self, cfg) -> ProviderResult:
        if not self.api_key:
            raise ProviderError("ODDS_API_IO_KEY is missing. Put it in .env or switch PROVIDER=demo.")
        if not self.bookmakers:
            raise ProviderError(
                "BOOKMAKERS is empty. Use the exact names from odds-api.io, e.g. "
                "BOOKMAKERS=Sisal IT,Snai IT"
            )

        params = {
            "apiKey": self.api_key,
            "bookmakers": ",".join(self.bookmakers),
            "sport": "football",
            "markets": self.markets,
            "includeEventDetails": "true",
            "limit": "500",
        }
        url = f"{ODDS_API_IO}/arbitrage-bets?" + urllib.parse.urlencode(params)
        data, headers = http_json(url)
        if not isinstance(data, list):
            raise ProviderError(f"Unexpected response from odds-api.io: {str(data)[:300]}")

        arbs: list[Evaluation] = []
        skipped = 0
        for row in data:
            event = row.get("event") or {}
            league = event.get("league") or "Italy"
            if not core.italian_league(league):
                skipped += 1
                continue
            converted = self._convert(row, cfg)
            if converted is not None:
                arbs.append(converted)

        arbs.sort(key=lambda e: e.s)
        return ProviderResult(
            precomputed=arbs,
            meta={
                "provider": "oddsapiio",
                "quota": {
                    "remaining": headers.get("x-ratelimit-remaining"),
                    "used": headers.get("x-ratelimit-used"),
                    "last_cost": None,
                },
                "books": self.bookmakers,
                "scanned": len(data),
                "skipped_non_italian": skipped,
            },
            warnings=[] if arbs else ["No Italian arbitrage right now (or your plan does not include these books)."],
        )

    def _convert(self, row: dict[str, Any], cfg) -> Evaluation | None:
        event = row.get("event") or {}
        home = event.get("home") or "Home"
        away = event.get("away") or "Away"
        league = event.get("league") or "Italy"
        market_info = row.get("market") or {}
        market_name = market_info.get("name") or "ML"
        hdp = market_info.get("hdp")
        market_label = f"{market_name} {hdp}" if hdp not in (None, "") else market_name

        try:
            s = float(row.get("impliedProbability"))
            margin = float(row.get("profitMargin")) / 100.0
            total_stake = float(row.get("totalStake") or 1000.0)
        except (TypeError, ValueError):
            return None

        raw_legs = row.get("legs") or []
        raw_stakes = row.get("optimalStakes") or []
        if len(raw_legs) < 2:
            return None

        stakes_by_leg: dict[tuple[str, str], tuple[float, float]] = {}
        for item in raw_stakes:
            key = (str(item.get("bookmaker", "")), str(item.get("side", "")).lower())
            try:
                stakes_by_leg[key] = (float(item.get("stake")), float(item.get("potentialReturn") or 0))
            except (TypeError, ValueError):
                continue

        base_scale = cfg.bankroll / total_stake if total_stake > 0 else 1.0
        scale = base_scale
        for leg in raw_legs:
            max_stake = leg.get("maxStake")
            item = stakes_by_leg.get((str(leg.get("bookmaker", "")), str(leg.get("side", "")).lower()))
            if max_stake and item and item[0] > 0:
                scale = min(scale, float(max_stake) / item[0])
        capped = scale < base_scale - 1e-9

        best: list[BestPrice] = []
        stakes: list[float] = []
        payouts: list[float] = []
        labels: list[str] = []
        codes: list[str] = []

        for leg in raw_legs:
            bookmaker = str(leg.get("bookmaker") or "Book")
            side = str(leg.get("side") or "").lower()
            code = SIDE_TO_CODE.get(side, side.upper() or "?")
            label = {"home": home, "draw": DRAW, "away": away}.get(side, side.capitalize() or "?")
            try:
                price = float(leg.get("netOdds") or leg.get("odds"))
            except (TypeError, ValueError):
                return None
            if price <= 1.0:
                return None
            item = stakes_by_leg.get((bookmaker, side))
            if item is None:
                return None
            stake = round_stake(item[0] * scale)
            href = leg.get("href")
            if not href or href == "N/A":
                href = None
            max_stake = leg.get("maxStake")
            best.append(
                BestPrice(
                    label=label,
                    code=code,
                    odds=price,
                    book_key=bookmaker,
                    book_title=bookmaker,
                    href=href,
                    max_stake=float(max_stake) if max_stake else None,
                )
            )
            labels.append(label)
            codes.append(code)
            stakes.append(stake)
            payouts.append(round(stake * price, 2))

        if not best:
            return None
        total = round(sum(stakes), 2)
        min_payout = min(payouts)
        ev = Evaluation(
            event=Event(
                id=str(row.get("eventId") or row.get("id") or ""),
                league=league,
                home=home,
                away=away,
                commence=event.get("date") or "",
                books=[],
                market=market_label,
                source="oddsapiio",
            ),
            best=best,
            s=s,
            margin=margin,
            labels=labels,
            codes=codes,
            stakes=stakes,
            payouts=payouts,
            total_stake=total,
            min_payout=round(min_payout, 2),
            profit=round(min_payout - total, 2),
            capped=capped,
        )
        for price, stake in zip(best, stakes):
            if stake < cfg.min_stake:
                ev.warnings.append(
                    f"{price.book_title}: stake {stake:.2f} is below the {cfg.min_stake:.2f} minimum"
                )
        return ev


# ---------------------------------------------------------------------------

def get_provider(cfg):
    provider = (cfg.provider or "demo").lower()
    if provider == "demo":
        return DemoProvider()
    if provider in ("theoddsapi", "odds_api", "the-odds-api"):
        return TheOddsAPIProvider(cfg.odds_api_key, cfg.regions, cfg.bookmakers, cfg.sports)
    if provider in ("oddsapiio", "odds-api-io", "odds_api_io"):
        return OddsApiIoProvider(cfg.odds_api_io_key, cfg.bookmakers)
    raise ProviderError(f"Unknown PROVIDER={cfg.provider!r}. Use demo, theoddsapi or oddsapiio.")
