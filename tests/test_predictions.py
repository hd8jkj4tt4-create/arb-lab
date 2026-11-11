import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from predictions import (
    COMPETITIONS,
    FootballModel,
    NbaModel,
    ORDER,
    _fixtures_for,
    _parse_events,
    build_matches,
    fit_football,
    fit_nba,
    form_string,
    month_keys,
    normal_cdf,
    probable_scorers,
    projected_stats,
    recent_results,
    score_matrix,
    team_averages,
    validate_football,
)
from providers import DemoProvider, TheOddsAPIProvider


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def football_matches():
    """A tiny league with a clear strength order: A > B > C > D."""
    teams = ["A", "B", "C", "D"]
    goals = {"A": 3, "B": 2, "C": 1, "D": 0}
    matches = []
    index = 0
    for home in teams:
        for away in teams:
            if home == away:
                continue
            for _ in range(3):
                index += 1
                matches.append(
                    {
                        "id": f"f{index}",
                        "home": home,
                        "away": away,
                        "commence": NOW - timedelta(days=index * 3),
                        "home_score": goals[home],
                        "away_score": goals[away],
                        "completed": True,
                    }
                )
    return matches


def nba_matches():
    """A tiny league with a clear point-margin order: A > B > C > D."""
    teams = ["A", "B", "C", "D"]
    strength = {"A": 15, "B": 5, "C": -5, "D": -15}
    matches = []
    index = 0
    for home in teams:
        for away in teams:
            if home == away:
                continue
            for _ in range(3):
                index += 1
                matches.append(
                    {
                        "id": f"n{index}",
                        "home": home,
                        "away": away,
                        "commence": NOW - timedelta(days=index * 2),
                        "home_score": 110 + strength[home] // 2,
                        "away_score": 110 + strength[away] // 2,
                        "completed": True,
                    }
                )
    return matches


class FootballModelTests(unittest.TestCase):
    def test_score_matrix_is_a_distribution(self):
        matrix = score_matrix(1.6, 1.1)
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.0, places=12)
        self.assertEqual(len(matrix), 11)
        self.assertEqual(len(matrix[0]), 11)

    def test_stronger_team_has_higher_win_probability(self):
        model = fit_football(football_matches(), NOW, 365)
        probs = model.probabilities("A", "D")
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=9)
        self.assertGreater(probs["1"], 0.60)
        self.assertGreater(probs["1"], probs["2"])
        self.assertIsInstance(model, FootballModel)

    def test_unknown_team_gets_average_prior(self):
        model = fit_football(football_matches(), NOW, 365)
        probs = model.probabilities("A", "Newcomer")
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=9)
        self.assertGreater(probs["1"], 0.5)

    def test_validation_returns_metrics(self):
        matches = football_matches()
        spec = replace(COMPETITIONS["la_liga"], min_train=10, min_test=5)
        result = validate_football(matches, spec)
        self.assertIsNotNone(result)
        self.assertGreater(result["matches"], 0)
        self.assertGreaterEqual(result["accuracy"], 0.0)
        self.assertLessEqual(result["accuracy"], 1.0)


class NbaModelTests(unittest.TestCase):
    def test_normal_cdf(self):
        self.assertAlmostEqual(normal_cdf(0), 0.5)
        self.assertGreater(normal_cdf(1), 0.5)
        self.assertLess(normal_cdf(-1), 0.5)

    def test_stronger_home_team_wins_more_often(self):
        model = fit_nba(nba_matches(), NOW, 120)
        probs = model.probabilities("A", "D")
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=9)
        self.assertGreater(probs["1"], 0.65)
        self.assertNotIn("X", probs)
        self.assertIsInstance(model, NbaModel)

    def test_home_advantage_is_positive(self):
        model = fit_nba(nba_matches(), NOW, 120)
        self.assertGreater(model.home_court, 0.0)


class ParsingTests(unittest.TestCase):
    def test_serie_a_is_in_win_probabilities(self):
        self.assertIn("serie_a", ORDER)
        self.assertEqual(COMPETITIONS["serie_a"].espn, "soccer/ita.1")
        self.assertEqual(COMPETITIONS["serie_a"].name, "Serie A")

    def test_month_keys(self):
        keys = month_keys(datetime(2025, 11, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(keys, ["202511", "202512", "202601", "202602"])

    def test_parse_events_reads_home_away_and_scores(self):
        raw = [
            {
                "id": "1",
                "date": "2026-11-10T20:00Z",
                "season": {"slug": "league-phase"},
                "competitions": [
                    {
                        "status": {"type": {"state": "post", "completed": True}},
                        "competitors": [
                            {"homeAway": "home", "score": "2", "team": {"displayName": "Home FC"}},
                            {"homeAway": "away", "score": "1", "team": {"displayName": "Away FC"}},
                        ],
                    }
                ],
            }
        ]
        parsed = _parse_events(raw, COMPETITIONS["la_liga"])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["home"], "Home FC")
        self.assertEqual(parsed[0]["away"], "Away FC")
        self.assertEqual(parsed[0]["home_score"], 2)
        self.assertEqual(parsed[0]["away_score"], 1)
        self.assertTrue(parsed[0]["completed"])

    def test_parse_events_reads_corners_and_fouls(self):
        raw = [
            {
                "id": "1",
                "date": "2026-11-10T20:00Z",
                "season": {"slug": "league-phase"},
                "competitions": [
                    {
                        "status": {"type": {"state": "post", "completed": True}},
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "2",
                                "team": {"displayName": "Home FC"},
                                "statistics": [
                                    {"name": "wonCorners", "displayValue": "7"},
                                    {"name": "foulsCommitted", "displayValue": "11"},
                                    {"name": "threePointFieldGoalsMade", "displayValue": "5"},
                                ],
                            },
                            {
                                "homeAway": "away",
                                "score": "1",
                                "team": {"displayName": "Away FC"},
                                "statistics": [
                                    {"name": "wonCorners", "displayValue": "4"},
                                    {"name": "foulsCommitted", "displayValue": "14"},
                                    {"name": "threePointFieldGoalsMade", "displayValue": "3"},
                                ],
                            },
                        ],
                    }
                ],
            }
        ]
        parsed = _parse_events(raw, COMPETITIONS["la_liga"])
        self.assertEqual(parsed[0]["home_corners"], 7)
        self.assertEqual(parsed[0]["away_corners"], 4)
        self.assertEqual(parsed[0]["home_fouls"], 11)
        self.assertEqual(parsed[0]["away_fouls"], 14)
        self.assertEqual(parsed[0]["home_threes"], 5)
        self.assertEqual(parsed[0]["away_threes"], 3)

    def test_preseason_nba_is_ignored(self):
        raw = [
            {
                "id": "1",
                "date": "2026-10-01T20:00Z",
                "season": {"slug": "preseason"},
                "competitions": [
                    {
                        "status": {"type": {"state": "post", "completed": True}},
                        "competitors": [
                            {"homeAway": "home", "score": "100", "team": {"displayName": "Home"}},
                            {"homeAway": "away", "score": "90", "team": {"displayName": "Away"}},
                        ],
                    }
                ],
            }
        ]
        self.assertEqual(_parse_events(raw, COMPETITIONS["nba"]), [])


class FormTests(unittest.TestCase):
    def test_form_is_oldest_to_newest(self):
        matches = [
            {"home": "A", "away": "B", "home_score": 2, "away_score": 0},
            {"home": "B", "away": "A", "home_score": 1, "away_score": 1},
            {"home": "A", "away": "C", "home_score": 0, "away_score": 3},
        ]
        self.assertEqual(form_string("A", matches, "football"), "WDL")

    def test_recent_results_include_real_goals(self):
        matches = [
            {
                "home": "A",
                "away": "B",
                "home_score": 2,
                "away_score": 1,
                "commence": NOW,
            }
        ]
        rows = recent_results("B", matches)
        self.assertEqual(rows, [f"L 1-2 vs A ({NOW.date().isoformat()})"])

    def test_recent_results_include_corners_and_fouls(self):
        matches = [
            {
                "home": "A",
                "away": "B",
                "home_score": 2,
                "away_score": 1,
                "home_corners": 7,
                "home_fouls": 11,
                "away_corners": 4,
                "away_fouls": 14,
                "commence": NOW,
            }
        ]
        rows = recent_results("A", matches)
        self.assertEqual(rows, [f"W 2-1 vs B ({NOW.date().isoformat()}) · 7 corners, 11 fouls"])
        self.assertEqual(team_averages(matches, "A")["corners"], 7)
        projected = projected_stats(matches, "A", "B")
        self.assertEqual(projected["corners"]["total"], 11)
        self.assertEqual(projected["fouls"]["total"], 25)

    def test_probable_scorers_follow_goal_share(self):
        players = [
            {"name": "Striker", "goals": 8, "appearances": 10, "shots_on_target": 15},
            {"name": "Winger", "goals": 2, "appearances": 10, "shots_on_target": 6},
        ]
        scorers = probable_scorers(players, 1.5)
        self.assertEqual([row["name"] for row in scorers], ["Striker", "Winger"])
        self.assertAlmostEqual(scorers[0]["probability"], 0.6988, places=3)
        self.assertGreater(scorers[0]["probability"], scorers[1]["probability"])

    def test_nba_threes_are_projected(self):
        matches = [
            {
                "home": "A",
                "away": "B",
                "home_score": 110,
                "away_score": 100,
                "home_threes": 14,
                "away_threes": 10,
                "commence": NOW,
            }
        ]
        projected = projected_stats(matches, "A", "B", limit=5)
        self.assertEqual(projected["threes"]["total"], 24)
        self.assertEqual(team_averages(matches, "A", limit=5)["threes"], 14)


def event(state, completed, commence, home_score=None, away_score=None):
    return {
        "id": f"{state}-{commence.isoformat()}",
        "home": "A",
        "away": "B",
        "commence": commence,
        "home_score": home_score,
        "away_score": away_score,
        "completed": completed,
        "state": state,
        "season_type": "regular-season",
    }


class TrainingDataTests(unittest.TestCase):
    def test_only_finished_matches_enter_training(self):
        spec = COMPETITIONS["la_liga"]
        events = [
            event("post", True, NOW - timedelta(days=1), 2, 1),
            event("pre", False, NOW + timedelta(days=1)),
            event("in", False, NOW, 1, 0),
            event("post", True, NOW - timedelta(days=2)),  # no score
        ]
        matches = build_matches(events, spec, NOW)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["home_score"], 2)
        self.assertEqual(matches[0]["state"], "post")

    def test_fixtures_are_future_and_inside_horizon(self):
        spec = COMPETITIONS["nba"]
        events = [
            event("pre", False, NOW + timedelta(days=2)),
            event("pre", False, NOW + timedelta(days=1)),
            event("pre", False, NOW + timedelta(days=20)),
            event("pre", False, NOW - timedelta(minutes=10)),
            event("post", True, NOW + timedelta(days=1), 100, 90),
        ]
        fixtures = _fixtures_for(events, spec, NOW)
        self.assertEqual([item["commence"] for item in fixtures], [NOW + timedelta(days=1), NOW + timedelta(days=2)])



class OddsApiMappingTests(unittest.TestCase):
    def test_nba_sport_key_maps_to_two_way_event(self):
        provider = TheOddsAPIProvider("key", "eu", [], ["basketball_nba"])
        event = provider._parse_event(
            {
                "id": "nba-1",
                "sport_key": "basketball_nba",
                "sport_title": "NBA",
                "home_team": "Los Angeles Lakers",
                "away_team": "Boston Celtics",
                "commence_time": "2026-11-12T00:00:00Z",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Los Angeles Lakers", "price": 1.9},
                                    {"name": "Boston Celtics", "price": 1.95},
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.league, "NBA")
        self.assertEqual(event.codes(), ["1", "2"])
        self.assertEqual(len(event.books[0].odds), 2)

    def test_champions_league_key_maps_to_label(self):
        provider = TheOddsAPIProvider("key", "eu", [], ["soccer_uefa_champs_league"])
        event = provider._parse_event(
            {
                "id": "ucl-1",
                "sport_key": "soccer_uefa_champs_league",
                "sport_title": "UEFA Champions League",
                "home_team": "Arsenal",
                "away_team": "Internazionale",
                "commence_time": "2026-11-24T20:00:00Z",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Arsenal", "price": 2.0},
                                    {"name": "Draw", "price": 3.5},
                                    {"name": "Internazionale", "price": 3.8},
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.league, "Champions League")
        self.assertEqual(event.codes(), ["1", "X", "2"])


class DemoProviderTests(unittest.TestCase):
    def test_demo_includes_new_competitions_and_two_way_nba(self):
        events = DemoProvider()._events(None)
        leagues = {event.league for event in events}
        self.assertIn("Champions League", leagues)
        self.assertIn("La Liga", leagues)
        self.assertIn("NBA", leagues)
        nba = next(event for event in events if event.league == "NBA")
        self.assertEqual(nba.codes(), ["1", "2"])
        self.assertEqual(len(nba.labels()), 2)
        self.assertTrue(all(len(book.odds) == 2 for book in nba.books))


if __name__ == "__main__":
    unittest.main()
