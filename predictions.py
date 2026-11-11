"""Win probabilities from previous results.

Data source: ESPN's public scoreboard API (no key required).

Football (La Liga, Champions League)
    Time-weighted Poisson goals model with a Dixon-Coles low-score correction.
    Produces 1 / X / 2 probabilities and expected goals.

NBA
    Time-weighted point-margin ratings, converted to a home/away win
    probability with a normal approximation. NBA has no draw, so only 1 / 2.

Everything here is standard library only and deterministic. Network access is
limited to fetch_json(); the model functions can be unit-tested offline.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from core import utcnow

log = logging.getLogger("arb-lab.predictions")

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "data" / "predictions.json"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"

# The model uses a 0..10 score grid for football. The factorial table keeps the
# Poisson probability loop cheap.
GRID = 11
FACTORIALS = tuple(math.factorial(i) for i in range(GRID + 2))
RHO = -0.08
STALE_AFTER = 24 * 3600


class DataError(RuntimeError):
    """Raised when the ESPN feed cannot be read."""


@dataclass(frozen=True)
class Competition:
    key: str
    name: str
    sport: str
    espn: str
    lookback_days: int
    half_life_days: int
    validation_days: int
    min_train: int
    min_test: int
    fixture_horizon_days: int
    fixture_limit: int


COMPETITIONS: dict[str, Competition] = {
    "la_liga": Competition(
        key="la_liga",
        name="La Liga",
        sport="football",
        espn="soccer/esp.1",
        lookback_days=730,
        half_life_days=300,
        validation_days=60,
        min_train=100,
        min_test=20,
        fixture_horizon_days=14,
        fixture_limit=8,
    ),
    "champions_league": Competition(
        key="champions_league",
        name="Champions League",
        sport="football",
        espn="soccer/uefa.champions",
        lookback_days=900,
        half_life_days=300,
        validation_days=75,
        min_train=100,
        min_test=20,
        fixture_horizon_days=14,
        fixture_limit=8,
    ),
    "nba": Competition(
        key="nba",
        name="NBA",
        sport="basketball",
        espn="basketball/nba",
        lookback_days=430,
        half_life_days=120,
        validation_days=45,
        min_train=200,
        min_test=30,
        fixture_horizon_days=14,
        fixture_limit=10,
    ),
}

ORDER = ("la_liga", "champions_league", "nba")


# ---------------------------------------------------------------------------
# HTTP / ESPN
# ---------------------------------------------------------------------------

def fetch_json(url: str, timeout: int = 25) -> Any:
    """GET JSON with the headers ESPN expects for its public API."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ArbLab/1.1 (local research tool)",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
        return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise DataError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise DataError(f"Network error: {exc.reason}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DataError(f"Invalid JSON from ESPN: {exc}") from exc


def _espn_events(path: str, dates: str) -> list[dict[str, Any]]:
    url = f"{ESPN}/{path}/scoreboard?" + urllib.parse.urlencode({"dates": dates, "limit": 1000})
    data = fetch_json(url)
    events = data.get("events") if isinstance(data, dict) else None
    return events if isinstance(events, list) else []


def month_keys(start: datetime, end: datetime) -> list[str]:
    """Inclusive list of YYYYMM keys between two dates."""
    keys: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year}{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return keys


def _nba_team_ids(warnings: list[str]) -> set[str]:
    """ESPN's NBA scoreboard also contains All-Star and exhibition teams."""
    try:
        data = fetch_json(f"{ESPN}/basketball/nba/teams")
        teams = data["sports"][0]["leagues"][0]["teams"]
        return {str(item["team"]["id"]) for item in teams if item.get("team", {}).get("id")}
    except (DataError, KeyError, IndexError, TypeError) as exc:
        warnings.append(f"NBA team list: {exc}")
        return set()


def _competitor_ids(event: dict[str, Any]) -> set[str]:
    competitions = event.get("competitions") or []
    comp = competitions[0] if competitions else {}
    return {
        str((competitor.get("team") or {}).get("id"))
        for competitor in (comp.get("competitors") or [])
        if (competitor.get("team") or {}).get("id") is not None
    }


def load_events(spec: Competition, now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch the raw ESPN events used for training and upcoming fixtures."""
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    start = now - timedelta(days=spec.lookback_days)

    if spec.sport == "football":
        # A calendar-year query returns the whole year for football. Include
        # next year so fixtures published after New Year are visible too.
        for year in range(start.year, now.year + 2):
            try:
                events.extend(_espn_events(spec.espn, str(year)))
            except DataError as exc:
                warnings.append(f"{spec.name} {year}: {exc}")
    else:
        end = now + timedelta(days=spec.fixture_horizon_days + 1)
        for key in month_keys(start, end):
            try:
                events.extend(_espn_events(spec.espn, key))
            except DataError as exc:
                warnings.append(f"{spec.name} {key}: {exc}")
        valid_ids = _nba_team_ids(warnings)
        if valid_ids:
            events = [
                event
                for event in events
                if _competitor_ids(event) and _competitor_ids(event) <= valid_ids
            ]
    return events, warnings


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_score(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("value", value.get("displayValue"))
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_events(raw: Iterable[dict[str, Any]], spec: Competition) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for event in raw:
        competitions = event.get("competitions") or []
        comp = competitions[0] if competitions else {}
        status = comp.get("status") or event.get("status") or {}
        status_type = status.get("type") or {}
        state = str(status_type.get("state") or "")
        completed = bool(status_type.get("completed"))

        season = event.get("season") or {}
        season_type = str(season.get("slug") or (season.get("type") or {}).get("slug") or "")
        if spec.sport == "basketball" and season_type == "preseason":
            continue

        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home is None or away is None:
            continue

        def team_name(side: dict[str, Any]) -> str:
            team = side.get("team") or {}
            return str(
                team.get("displayName")
                or team.get("name")
                or team.get("shortDisplayName")
                or ""
            ).strip()

        home_name = team_name(home)
        away_name = team_name(away)
        commence = _parse_date(event.get("date") or comp.get("date"))
        if not home_name or not away_name or commence is None:
            continue

        parsed.append(
            {
                "id": str(event.get("id") or comp.get("id") or f"{home_name}-{away_name}-{commence.isoformat()}"),
                "league": spec.name,
                "home": home_name,
                "away": away_name,
                "commence": commence,
                "home_score": _parse_score(home.get("score")),
                "away_score": _parse_score(away.get("score")),
                "completed": completed,
                "state": state,
                "season_type": season_type,
            }
        )

    # ESPN sometimes repeats an event across paginated responses.
    unique: dict[str, dict[str, Any]] = {}
    for event in parsed:
        unique.setdefault(event["id"], event)
    return sorted(unique.values(), key=lambda item: item["commence"])


def build_matches(events: Iterable[dict[str, Any]], spec: Competition, now: datetime) -> list[dict[str, Any]]:
    """Completed matches inside the lookback window, ready for training.

    Only finished ESPN events are used. Scheduled, live, postponed and
    cancelled events never enter the model, so the training set contains
    real goals/points that were actually scored.
    """
    start = now - timedelta(days=spec.lookback_days)
    matches: list[dict[str, Any]] = []
    for event in events:
        if not event["completed"] or event.get("state") != "post":
            continue
        if not (start <= event["commence"] <= now):
            continue
        home_score, away_score = event["home_score"], event["away_score"]
        if home_score is None or away_score is None:
            continue
        if spec.sport == "football":
            if not (0 <= home_score <= 15 and 0 <= away_score <= 15):
                continue
        else:
            if home_score < 40 or away_score < 40:
                continue
        matches.append(event)
    return matches


# ---------------------------------------------------------------------------
# Football: time-weighted Poisson + Dixon-Coles correction
# ---------------------------------------------------------------------------

def _poisson_pmf(lam: float, goals: int) -> float:
    return math.exp(-lam) * (lam ** goals) / FACTORIALS[goals]


def score_matrix(home_rate: float, away_rate: float, rho: float = RHO) -> list[list[float]]:
    """Normalised probability matrix for every 0..10 scoreline."""
    home_rate = min(max(home_rate, 0.01), 12.0)
    away_rate = min(max(away_rate, 0.01), 12.0)
    matrix = [
        [_poisson_pmf(home_rate, h) * _poisson_pmf(away_rate, a) for a in range(GRID)]
        for h in range(GRID)
    ]
    # Dixon-Coles tau adjustment for 0-0, 1-0, 0-1 and 1-1.
    matrix[0][0] *= 1.0 - home_rate * away_rate * rho
    matrix[0][1] *= 1.0 + home_rate * rho
    matrix[1][0] *= 1.0 + away_rate * rho
    matrix[1][1] *= 1.0 - rho
    total = sum(sum(row) for row in matrix)
    if total <= 0:
        uniform = 1.0 / (GRID * GRID)
        return [[uniform for _ in range(GRID)] for _ in range(GRID)]
    return [[value / total for value in row] for row in matrix]


def matrix_outcomes(matrix: list[list[float]]) -> dict[str, float]:
    home = sum(matrix[h][a] for h in range(GRID) for a in range(GRID) if h > a)
    draw = sum(matrix[i][i] for i in range(GRID))
    away = max(0.0, 1.0 - home - draw)
    return {"1": home, "X": draw, "2": away}


@dataclass
class FootballModel:
    attack: dict[str, float]
    defence: dict[str, float]
    mu: float
    home_advantage: float
    teams: int
    matches: int
    half_life_days: int

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        attack_home = self.attack.get(home, 0.0)
        attack_away = self.attack.get(away, 0.0)
        defence_home = self.defence.get(home, 0.0)
        defence_away = self.defence.get(away, 0.0)
        home_rate = math.exp(self.mu + self.home_advantage + attack_home + defence_away)
        away_rate = math.exp(self.mu + attack_away + defence_home)
        return min(home_rate, 12.0), min(away_rate, 12.0)

    def probabilities(self, home: str, away: str) -> dict[str, float]:
        home_rate, away_rate = self.expected_goals(home, away)
        return matrix_outcomes(score_matrix(home_rate, away_rate))


def _time_weight(commence: datetime, reference: datetime, half_life_days: int) -> float:
    age_days = max(0.0, (reference - commence).total_seconds() / 86400.0)
    return 0.5 ** (age_days / max(1.0, float(half_life_days)))


def fit_football(
    matches: list[dict[str, Any]],
    reference: datetime,
    half_life_days: int,
    iterations: int = 450,
    learning_rate: float = 0.12,
    regularisation: float = 0.08,
) -> FootballModel:
    """Fit attack/defence ratings with weighted Poisson gradients."""
    if not matches:
        raise ValueError("no football matches to fit")

    teams = sorted({m["home"] for m in matches} | {m["away"] for m in matches})
    attack = {team: 0.0 for team in teams}
    defence = {team: 0.0 for team in teams}
    mean_goals = sum(m["home_score"] + m["away_score"] for m in matches) / (2 * len(matches))
    mu0 = math.log(max(0.25, mean_goals))
    mu = mu0
    home_advantage = 0.15

    rows = [
        (
            m["home"],
            m["away"],
            int(m["home_score"]),
            int(m["away_score"]),
            _time_weight(m["commence"], reference, half_life_days),
        )
        for m in matches
    ]
    total_weight = sum(row[4] for row in rows)
    if total_weight <= 0:
        raise ValueError("match weights are zero")

    for _ in range(max(1, iterations)):
        grad_attack = {team: 0.0 for team in teams}
        grad_defence = {team: 0.0 for team in teams}
        weight_attack = {team: 0.0 for team in teams}
        weight_defence = {team: 0.0 for team in teams}
        grad_mu = 0.0
        grad_home = 0.0

        for home, away, home_goals, away_goals, weight in rows:
            home_rate = math.exp(mu + home_advantage + attack[home] + defence[away])
            away_rate = math.exp(mu + attack[away] + defence[home])
            error_home = weight * (home_goals - home_rate)
            error_away = weight * (away_goals - away_rate)

            grad_attack[home] += error_home
            grad_attack[away] += error_away
            grad_defence[away] += error_home
            grad_defence[home] += error_away
            weight_attack[home] += weight
            weight_attack[away] += weight
            weight_defence[away] += weight
            weight_defence[home] += weight
            grad_mu += error_home + error_away
            grad_home += error_home

        for team in teams:
            attack[team] += learning_rate * (
                grad_attack[team] / max(weight_attack[team], 1e-9)
                - regularisation * attack[team]
            )
            defence[team] += learning_rate * (
                grad_defence[team] / max(weight_defence[team], 1e-9)
                - regularisation * defence[team]
            )
            attack[team] = max(-2.5, min(2.5, attack[team]))
            defence[team] = max(-2.5, min(2.5, defence[team]))

        mu += learning_rate * (grad_mu / total_weight - regularisation * (mu - mu0))
        mu = max(-1.0, min(1.5, mu))
        home_advantage += learning_rate * (grad_home / total_weight)
        home_advantage = max(-0.5, min(0.9, home_advantage))

        # Keep the model identifiable: shift the mean into mu after centring.
        mean_attack = sum(attack.values()) / len(teams)
        mean_defence = sum(defence.values()) / len(teams)
        for team in teams:
            attack[team] -= mean_attack
            defence[team] -= mean_defence
        mu += mean_attack + mean_defence

    return FootballModel(
        attack=attack,
        defence=defence,
        mu=mu,
        home_advantage=home_advantage,
        teams=len(teams),
        matches=len(matches),
        half_life_days=half_life_days,
    )


def _football_result(home_goals: int, away_goals: int) -> int:
    if home_goals > away_goals:
        return 0
    if home_goals == away_goals:
        return 1
    return 2


def validate_football(
    matches: list[dict[str, Any]],
    spec: Competition,
) -> dict[str, Any] | None:
    if len(matches) < spec.min_train + spec.min_test:
        return None
    matches = sorted(matches, key=lambda match: match["commence"])
    cutoff = matches[-1]["commence"] - timedelta(days=spec.validation_days)
    train = [m for m in matches if m["commence"] < cutoff]
    test = [m for m in matches if m["commence"] >= cutoff]
    if len(train) < spec.min_train or len(test) < spec.min_test:
        return None

    model = fit_football(train, train[-1]["commence"], spec.half_life_days)
    correct = 0
    log_loss = 0.0
    brier = 0.0
    for match in test:
        probs = model.probabilities(match["home"], match["away"])
        ordered = [probs["1"], probs["X"], probs["2"]]
        actual = _football_result(int(match["home_score"]), int(match["away_score"]))
        guess = max(range(3), key=ordered.__getitem__)
        correct += int(guess == actual)
        log_loss += -math.log(max(ordered[actual], 1e-9))
        brier += sum((ordered[i] - (1.0 if i == actual else 0.0)) ** 2 for i in range(3))

    count = len(test)
    return {
        "matches": count,
        "accuracy": round(correct / count, 4),
        "log_loss": round(log_loss / count, 4),
        "brier": round(brier / count, 4),
    }


# ---------------------------------------------------------------------------
# NBA: time-weighted point margin
# ---------------------------------------------------------------------------

def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


@dataclass
class NbaModel:
    ratings: dict[str, float]
    home_court: float
    sigma: float
    teams: int
    matches: int
    half_life_days: int

    def projected_margin(self, home: str, away: str) -> float:
        return self.home_court + self.ratings.get(home, 0.0) - self.ratings.get(away, 0.0)

    def probabilities(self, home: str, away: str) -> dict[str, float]:
        probability = normal_cdf(self.projected_margin(home, away) / self.sigma)
        return {"1": probability, "2": 1.0 - probability}


def fit_nba(
    matches: list[dict[str, Any]],
    reference: datetime,
    half_life_days: int,
    iterations: int = 400,
    learning_rate: float = 0.08,
    regularisation: float = 0.02,
) -> NbaModel:
    if not matches:
        raise ValueError("no NBA games to fit")

    teams = sorted({m["home"] for m in matches} | {m["away"] for m in matches})
    ratings = {team: 0.0 for team in teams}
    home_court = 2.5
    rows = [
        (
            m["home"],
            m["away"],
            float(m["home_score"] - m["away_score"]),
            _time_weight(m["commence"], reference, half_life_days),
        )
        for m in matches
    ]
    total_weight = sum(row[3] for row in rows)
    if total_weight <= 0:
        raise ValueError("game weights are zero")

    for _ in range(max(1, iterations)):
        grad = {team: 0.0 for team in teams}
        weight = {team: 0.0 for team in teams}
        grad_home = 0.0
        for home, away, margin, game_weight in rows:
            predicted = home_court + ratings[home] - ratings[away]
            error = game_weight * (margin - predicted)
            grad[home] += error
            grad[away] -= error
            weight[home] += game_weight
            weight[away] += game_weight
            grad_home += error

        for team in teams:
            ratings[team] += learning_rate * (
                grad[team] / max(weight[team], 1e-9)
                - regularisation * ratings[team]
            )
            ratings[team] = max(-20.0, min(20.0, ratings[team]))
        home_court += learning_rate * (grad_home / total_weight - regularisation * home_court)
        home_court = max(0.0, min(5.0, home_court))

        mean_rating = sum(ratings.values()) / len(teams)
        for team in teams:
            ratings[team] -= mean_rating

    residual = 0.0
    for home, away, margin, game_weight in rows:
        predicted = home_court + ratings[home] - ratings[away]
        residual += game_weight * (margin - predicted) ** 2
    sigma = math.sqrt(residual / total_weight)
    sigma = max(9.0, min(14.5, sigma))

    return NbaModel(
        ratings=ratings,
        home_court=home_court,
        sigma=sigma,
        teams=len(teams),
        matches=len(matches),
        half_life_days=half_life_days,
    )


def validate_nba(matches: list[dict[str, Any]], spec: Competition) -> dict[str, Any] | None:
    if len(matches) < spec.min_train + spec.min_test:
        return None
    matches = sorted(matches, key=lambda match: match["commence"])
    cutoff = matches[-1]["commence"] - timedelta(days=spec.validation_days)
    train = [m for m in matches if m["commence"] < cutoff]
    test = [m for m in matches if m["commence"] >= cutoff]
    if len(train) < spec.min_train or len(test) < spec.min_test:
        return None

    model = fit_nba(train, train[-1]["commence"], spec.half_life_days)
    correct = 0
    log_loss = 0.0
    brier = 0.0
    for match in test:
        probs = model.probabilities(match["home"], match["away"])
        ordered = [probs["1"], probs["2"]]
        actual = 0 if int(match["home_score"]) > int(match["away_score"]) else 1
        guess = max(range(2), key=ordered.__getitem__)
        correct += int(guess == actual)
        log_loss += -math.log(max(ordered[actual], 1e-9))
        brier += sum((ordered[i] - (1.0 if i == actual else 0.0)) ** 2 for i in range(2))

    count = len(test)
    return {
        "matches": count,
        "accuracy": round(correct / count, 4),
        "log_loss": round(log_loss / count, 4),
        "brier": round(brier / count, 4),
    }


# ---------------------------------------------------------------------------
# Snapshot building
# ---------------------------------------------------------------------------

def form_string(team: str, matches: list[dict[str, Any]], sport: str, limit: int = 5) -> str:
    results: list[str] = []
    for match in reversed(matches):
        if match["home"] != team and match["away"] != team:
            continue
        if match["home"] == team:
            scored, conceded = int(match["home_score"]), int(match["away_score"])
        else:
            scored, conceded = int(match["away_score"]), int(match["home_score"])
        if scored > conceded:
            results.append("W")
        elif scored == conceded and sport == "football":
            results.append("D")
        else:
            results.append("L")
        if len(results) >= limit:
            break
    return "".join(reversed(results))


def recent_results(team: str, matches: list[dict[str, Any]], limit: int = 5) -> list[str]:
    """Newest-first scorelines, e.g. ``W 2-1 vs Espanyol``.

    This is display-only proof that the model is fed real finished matches and
    their goals; it does not change the ratings.
    """
    rows: list[str] = []
    for match in reversed(matches):
        if match["home"] != team and match["away"] != team:
            continue
        home = match["home"] == team
        scored = int(match["home_score"] if home else match["away_score"])
        conceded = int(match["away_score"] if home else match["home_score"])
        opponent = match["away"] if home else match["home"]
        result = "W" if scored > conceded else "D" if scored == conceded else "L"
        date = match["commence"].date().isoformat()
        rows.append(f"{result} {scored}-{conceded} vs {opponent} ({date})")
        if len(rows) >= limit:
            break
    return rows


def _fixtures_for(
    events: list[dict[str, Any]],
    spec: Competition,
    now: datetime,
) -> list[dict[str, Any]]:
    """Upcoming matches in the near future, soonest first.

    Only events that have not started are eligible. The horizon is capped so
    the page shows the next round, not games several weeks away.
    """
    future = [
        event
        for event in events
        if event["state"] == "pre" and event["commence"] > now
    ]
    if spec.sport == "basketball":
        regular = [event for event in future if event["season_type"] == "regular-season"]
        if regular:
            future = regular
    inside = [
        event
        for event in future
        if event["commence"] <= now + timedelta(days=spec.fixture_horizon_days)
    ]
    selected = sorted(inside, key=lambda event: event["commence"])
    return selected[: spec.fixture_limit]


def _outcomes(model: FootballModel | NbaModel, home: str, away: str) -> list[dict[str, Any]]:
    probs = model.probabilities(home, away)
    if isinstance(model, FootballModel):
        codes = (("1", home), ("X", "Draw"), ("2", away))
    else:
        codes = (("1", home), ("2", away))
    return [
        {"code": code, "label": label, "probability": round(float(probs[code]), 4)}
        for code, label in codes
    ]


def build_competition(spec: Competition, now: datetime) -> dict[str, Any]:
    raw, warnings = load_events(spec, now)
    events = _parse_events(raw, spec)
    matches = build_matches(events, spec, now)
    fixtures = _fixtures_for(events, spec, now)

    if not matches:
        raise DataError(f"no completed {spec.name} results in the last {spec.lookback_days} days")

    if spec.sport == "football":
        model: FootballModel | NbaModel = fit_football(matches, now, spec.half_life_days)
        validation = validate_football(matches, spec)
        model_label = "Dixon-Coles Poisson"
    else:
        model = fit_nba(matches, now, spec.half_life_days)
        validation = validate_nba(matches, spec)
        model_label = "Point-margin ratings"

    if len(matches) < 60:
        warnings.append(f"small training sample ({len(matches)} matches)")

    sample = Counter()
    for match in matches:
        sample[match["home"]] += 1
        sample[match["away"]] += 1

    fixture_rows: list[dict[str, Any]] = []
    for event in fixtures:
        home, away = event["home"], event["away"]
        row: dict[str, Any] = {
            "id": event["id"],
            "home": home,
            "away": away,
            "commence": event["commence"].isoformat().replace("+00:00", "Z"),
            "espn_url": (
                f"https://www.espn.com/nba/game/_/gameId/{event['id']}"
                if spec.sport == "basketball"
                else f"https://www.espn.com/soccer/match/_/gameId/{event['id']}"
            ),
            "outcomes": _outcomes(model, home, away),
            "form": {
                "home": form_string(home, matches, spec.sport),
                "away": form_string(away, matches, spec.sport),
            },
            "recent": {
                "home": recent_results(home, matches),
                "away": recent_results(away, matches),
            },
            "sample": {
                "home": sample.get(home, 0),
                "away": sample.get(away, 0),
            },
        }
        if isinstance(model, FootballModel):
            home_rate, away_rate = model.expected_goals(home, away)
            row["expected_goals"] = [round(home_rate, 2), round(away_rate, 2)]
        else:
            row["projected_margin"] = round(model.projected_margin(home, away), 2)
        fixture_rows.append(row)

    if not fixture_rows:
        warnings.append("no upcoming fixtures published yet")

    return {
        "key": spec.key,
        "name": spec.name,
        "sport": spec.sport,
        "model": model_label,
        "trained_matches": len(matches),
        "trained_through": max(match["commence"] for match in matches).date().isoformat(),
        "training": {
            "completed_only": True,
            "scores_included": True,
            "lookback_days": spec.lookback_days,
            "half_life_days": spec.half_life_days,
        },
        "teams": model.teams,
        "lookback_days": spec.lookback_days,
        "half_life_days": spec.half_life_days,
        "validation": validation,
        "updated_at": utcnow(),
        "fixtures": fixture_rows,
        "warnings": warnings,
    }


def build_snapshot(
    now: datetime | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the full prediction snapshot, keeping cached data on failure."""
    now = now or datetime.now(timezone.utc)
    previous_by_key = {
        item.get("key"): item
        for item in (previous or {}).get("competitions", [])
        if isinstance(item, dict) and item.get("key")
    }

    competitions: list[dict[str, Any]] = []
    warnings: list[str] = []
    for key in ORDER:
        spec = COMPETITIONS[key]
        try:
            item = build_competition(spec, now)
        except Exception as exc:  # noqa: BLE001 - one feed must not kill the page
            item = {
                "key": spec.key,
                "name": spec.name,
                "sport": spec.sport,
                "model": "unavailable",
                "trained_matches": 0,
                "teams": 0,
                "fixtures": [],
                "warnings": [str(exc)],
                "error": str(exc),
            }
            warnings.append(f"{spec.name}: {exc}")

        cached = previous_by_key.get(key)
        if (item.get("error") or not item.get("fixtures")) and cached and cached.get("fixtures"):
            kept = dict(cached)
            kept["stale"] = True
            kept["warnings"] = list(kept.get("warnings") or []) + [
                item.get("error") or "No upcoming fixtures; showing cached data."
            ]
            item = kept

        competitions.append(item)

    return {
        "generated_at": utcnow(),
        "source": "ESPN public scoreboard API",
        "loading": False,
        "model_note": (
            "Trained only on finished matches with final scores (ESPN). "
            "Football: time-weighted Dixon-Coles Poisson. NBA: time-weighted "
            "point-margin ratings. No lineups, injuries or market prices."
        ),
        "competitions": competitions,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Cached service used by app.py
# ---------------------------------------------------------------------------

class PredictionService:
    def __init__(self, path: Path | None = None):
        self.path = path or CACHE_PATH
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._refreshing = False
        self._snapshot = self._load()

    def _load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            return {
                "loading": True,
                "generated_at": None,
                "competitions": [],
                "warnings": [],
            }
        out = dict(snapshot)
        out["loading"] = False
        generated = _parse_date(snapshot.get("generated_at"))
        if generated is not None:
            age = max(0, int((datetime.now(timezone.utc) - generated).total_seconds()))
            out["age_seconds"] = age
            out["stale"] = age > STALE_AFTER
        return out

    def refresh(self) -> dict[str, Any]:
        with self._refresh_lock:
            with self._lock:
                self._refreshing = True
                previous = self._snapshot
            try:
                snapshot = build_snapshot(previous=previous)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)
                with self._lock:
                    self._snapshot = snapshot
                return snapshot
            finally:
                with self._lock:
                    self._refreshing = False

    def request_refresh(self) -> bool:
        with self._lock:
            if self._refreshing:
                return False
        threading.Thread(target=self._safe_refresh, name="predictions", daemon=True).start()
        return True

    def _safe_refresh(self) -> None:
        try:
            self.refresh()
        except Exception as exc:  # noqa: BLE001 - background refresh is best effort
            log.warning("prediction refresh failed: %s", exc)


def refresh_snapshot() -> dict[str, Any]:
    """Convenience helper for scripts and debugging."""
    return build_snapshot()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-5s  %(message)s")
    result = refresh_snapshot()
    print(json.dumps(result, indent=2, ensure_ascii=False))
