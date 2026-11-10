"""Pure arbitrage math for Arb Lab.

No I/O, no framework: everything here is deterministic and unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import floor
from typing import Iterable

DRAW = "Draw"


def round_stake(x: float) -> float:
    """Round down to cents so a capped stake can never exceed the limit."""
    return floor(x * 100 + 1e-9) / 100


@dataclass
class BookLine:
    key: str
    title: str
    odds: dict[str, float]
    href: str | None = None
    max_stakes: dict[str, float] = field(default_factory=dict)


@dataclass
class Event:
    id: str
    league: str
    home: str
    away: str
    commence: str
    books: list[BookLine]
    market: str = "1X2"
    source: str = ""

    def labels(self) -> list[str]:
        has_draw = any(DRAW in b.odds for b in self.books)
        return [self.home] + ([DRAW] if has_draw else []) + [self.away]

    def codes(self) -> list[str]:
        has_draw = any(DRAW in b.odds for b in self.books)
        return ["1", "X", "2"] if has_draw else ["1", "2"]


@dataclass
class BestPrice:
    label: str
    code: str
    odds: float
    book_key: str
    book_title: str
    href: str | None = None
    max_stake: float | None = None


@dataclass
class Evaluation:
    event: Event
    best: list[BestPrice]
    s: float
    margin: float
    labels: list[str]
    codes: list[str]
    # Stakes are only filled when S < 1 (a real arbitrage).
    stakes: list[float] = field(default_factory=list)
    payouts: list[float] = field(default_factory=list)
    total_stake: float = 0.0
    min_payout: float = 0.0
    profit: float = 0.0
    capped: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def is_arb(self) -> bool:
        return self.s < 1.0

    @property
    def status(self) -> str:
        if self.s < 1.0:
            return "arb" if self.profit > 0 else "rounding"
        if self.s <= 1.02:
            return "near"
        return "none"

    @property
    def market(self) -> str:
        return self.event.market


def implied_sum(odds: Iterable[float]) -> float:
    return sum(1.0 / o for o in odds)


def implied_probabilities(odds: Iterable[float]) -> list[float]:
    """Normalize 1/odds into probabilities that sum to 1.

    This is the market-implied chance of each outcome at the best available
    prices. It is descriptive, not a prediction.
    """
    inv = [1.0 / o for o in odds]
    total = sum(inv)
    if total <= 0:
        return []
    return [x / total for x in inv]


def evaluate_event(
    event: Event,
    bankroll: float = 1000.0,
    min_stake: float = 0.5,
) -> Evaluation | None:
    """Pick the best price per outcome and evaluate the market.

    Returns None when the market is incomplete (at least one outcome has no
    price at any book): an incomplete market cannot be covered.
    """
    labels = event.labels()
    codes = event.codes()

    best: list[BestPrice] = []
    for label, code in zip(labels, codes):
        candidates: list[tuple[float, BookLine]] = [
            (b.odds[label], b)
            for b in event.books
            if label in b.odds and b.odds[label] > 1.0
        ]
        if not candidates:
            return None
        odds, book = max(candidates, key=lambda item: item[0])
        best.append(
            BestPrice(
                label=label,
                code=code,
                odds=odds,
                book_key=book.key,
                book_title=book.title,
                href=book.href,
                max_stake=book.max_stakes.get(label),
            )
        )

    odds = [p.odds for p in best]
    s = implied_sum(odds)
    margin = (1.0 / s) - 1.0

    ev = Evaluation(
        event=event,
        best=best,
        s=s,
        margin=margin,
        labels=labels,
        codes=codes,
    )

    if s >= 1.0:
        return ev

    raw = [bankroll * (1.0 / o) / s for o in odds]
    scale = 1.0
    capped = False
    for price, amount in zip(best, raw):
        if price.max_stake and price.max_stake > 0:
            limit = price.max_stake / amount
            if limit < scale:
                scale = limit
                capped = True

    stakes = [round_stake(x * scale) for x in raw]
    payouts = [stake * price.odds for stake, price in zip(stakes, best)]
    total = round(sum(stakes), 2)
    min_payout = min(payouts) if payouts else 0.0

    ev.stakes = stakes
    ev.payouts = [round(x, 2) for x in payouts]
    ev.total_stake = total
    ev.min_payout = round(min_payout, 2)
    ev.profit = round(min_payout - total, 2)
    ev.capped = capped

    for stake, price in zip(stakes, best):
        if stake < min_stake:
            ev.warnings.append(
                f"{price.label}: stake {stake:.2f} is below the {min_stake:.2f} minimum"
            )
    if ev.profit <= 0:
        ev.warnings.append("rounding ate the edge: guaranteed profit is zero or negative")

    return ev


def find_opportunities(
    events: Iterable[Event],
    bankroll: float = 1000.0,
    min_stake: float = 0.5,
) -> list[Evaluation]:
    out: list[Evaluation] = []
    for event in events:
        ev = evaluate_event(event, bankroll=bankroll, min_stake=min_stake)
        if ev is not None:
            out.append(ev)
    out.sort(key=lambda e: e.s)
    return out


def alert_key(event_id: str, market: str, margin: float) -> str:
    """One alert per 0.5 percentage-point margin bucket."""
    bucket = int(round(margin * 200))
    return f"{event_id}|{market}|{bucket}"


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def italian_league(name: str) -> bool:
    lowered = (name or "").strip().lower()
    if any(x in lowered for x in ("women", "femminile", "primavera", "u19", "u20", "u21")):
        return False
    # Providers such as odds-api.io prefix the country: "Italy - Serie A".
    # Trust that prefix so "Brazil - Serie A" is not mistaken for Italy.
    if " - " in lowered:
        country = lowered.split(" - ", 1)[0].strip()
        if country not in ("italy", "italia"):
            return False
    return any(
        x in lowered
        for x in ("serie a", "serie b", "serie c", "coppa italia", "supercoppa")
    )


ITALY_SPORTS = {
    "soccer_italy_serie_a": "Serie A",
    "soccer_italy_serie_b": "Serie B",
    "soccer_italy_coppa_italia": "Coppa Italia",
}
