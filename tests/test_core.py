import unittest

from core import (
    BookLine,
    Event,
    alert_key,
    evaluate_event,
    find_opportunities,
    implied_probabilities,
    implied_sum,
    italian_league,
    round_stake,
    tracked_league,
)


def event(odds_by_book, home="Inter", away="Milan", draw=True):
    books = []
    for key, odds in odds_by_book.items():
        books.append(odds if isinstance(odds, BookLine) else BookLine(key, key.title(), odds))
    return Event("evt-1", "Serie A", home, away, "2026-11-10T20:45:00Z", books)


class MathTests(unittest.TestCase):
    def test_implied_sum(self):
        self.assertAlmostEqual(implied_sum([2.0, 2.0]), 1.0)
        self.assertAlmostEqual(implied_sum([1.25, 5.0]), 1.0)

    def test_implied_probabilities_favour_the_shortest_price(self):
        probs = implied_probabilities([2.10, 3.70, 4.40])
        self.assertAlmostEqual(sum(probs), 1.0, places=12)
        self.assertEqual(max(range(3), key=probs.__getitem__), 0)
        self.assertAlmostEqual(probs[0], 0.48904, places=5)
        self.assertEqual(implied_probabilities([]), [])

    def test_known_arb(self):
        ev = evaluate_event(
            event({"a": {"Inter": 2.10, "Draw": 3.70, "Milan": 4.40}}),
            bankroll=1000,
        )
        self.assertIsNotNone(ev)
        self.assertAlmostEqual(ev.s, 0.9737335, places=6)
        self.assertAlmostEqual(ev.margin, 0.0269751, places=6)
        self.assertEqual(ev.stakes, [489.03, 277.56, 233.40])
        self.assertAlmostEqual(ev.total_stake, 999.99, places=2)
        self.assertGreater(ev.profit, 0)
        self.assertTrue(ev.is_arb)

    def test_no_arb_has_no_stakes(self):
        ev = evaluate_event(
            event({"a": {"Inter": 2.00, "Draw": 3.50, "Milan": 4.00}}),
            bankroll=1000,
        )
        self.assertIsNotNone(ev)
        self.assertGreater(ev.s, 1)
        self.assertEqual(ev.stakes, [])
        self.assertFalse(ev.is_arb)

    def test_best_price_per_outcome(self):
        ev = evaluate_event(
            event({
                "sisal": {"Inter": 2.12, "Draw": 3.50, "Milan": 4.10},
                "snai": {"Inter": 2.05, "Draw": 3.65, "Milan": 4.00},
                "eurobet": {"Inter": 2.00, "Draw": 3.40, "Milan": 4.25},
            }),
            bankroll=1000,
        )
        self.assertIsNotNone(ev)
        self.assertEqual([p.odds for p in ev.best], [2.12, 3.65, 4.25])
        self.assertEqual([p.book_key for p in ev.best], ["sisal", "snai", "eurobet"])

    def test_incomplete_market_is_skipped(self):
        ev = evaluate_event(
            event({"a": {"Inter": 2.10, "Draw": 3.70}}),
            bankroll=1000,
        )
        self.assertIsNone(ev)

    def test_two_way(self):
        ev = evaluate_event(
            event({"a": {"Sinner": 2.10, "Alcaraz": 2.10}}, home="Sinner", away="Alcaraz", draw=False),
            bankroll=1000,
        )
        self.assertIsNotNone(ev)
        self.assertEqual([p.code for p in ev.best], ["1", "2"])
        self.assertEqual(len(ev.stakes), 2)

    def test_max_stake_caps_the_whole_split(self):
        ev = evaluate_event(
            event({
                "a": BookLine("a", "A", {"Inter": 2.10, "Draw": 3.70, "Milan": 4.40}, max_stakes={"Inter": 100}),
                "b": BookLine("b", "B", {"Inter": 2.10, "Draw": 3.70, "Milan": 4.40}),
            }),
            bankroll=1000,
        )
        self.assertIsNotNone(ev)
        self.assertTrue(ev.capped)
        self.assertLessEqual(ev.stakes[0], 100)
        self.assertAlmostEqual(ev.stakes[0], 100.0, places=2)

    def test_round_stake_never_rounds_up(self):
        self.assertEqual(round_stake(10.999), 10.99)
        self.assertEqual(round_stake(10.001), 10.0)

    def test_alert_key_buckets(self):
        self.assertEqual(alert_key("1", "1X2", 0.0201), alert_key("1", "1X2", 0.0204))
        self.assertNotEqual(alert_key("1", "1X2", 0.020), alert_key("1", "1X2", 0.026))

    def test_find_opportunities_sorted(self):
        a = event({"a": {"Inter": 2.10, "Draw": 3.70, "Milan": 4.40}})
        a.id = "a"
        b = event({"a": {"Roma": 2.05, "Draw": 3.60, "Milan": 4.30}}, home="Roma")
        b.id = "b"
        opps = find_opportunities([b, a], bankroll=1000)
        self.assertEqual([o.event.id for o in opps], ["a", "b"])

    def test_italian_league_filter(self):
        self.assertTrue(italian_league("Italy - Serie A"))
        self.assertTrue(italian_league("Serie B"))
        self.assertTrue(italian_league("Coppa Italia"))
        self.assertTrue(italian_league("Italy - Serie C"))
        self.assertFalse(italian_league("Serie A Women"))
        self.assertFalse(italian_league("Premier League"))
        self.assertFalse(italian_league("Primavera 1"))
        self.assertFalse(italian_league("Brazil - Serie A"))
        self.assertFalse(italian_league("England - Championship"))

    def test_tracked_league_filter(self):
        self.assertTrue(tracked_league("Italy - Serie A"))
        self.assertTrue(tracked_league("UEFA Champions League"))
        self.assertTrue(tracked_league("Spain - LaLiga"))
        self.assertTrue(tracked_league("International - UEFA Champions League"))
        self.assertTrue(tracked_league("NBA"))
        self.assertFalse(tracked_league("Champions League Women"))
        self.assertFalse(tracked_league("NBA G League"))
        self.assertFalse(tracked_league("NBA All-Star"))
        self.assertFalse(tracked_league("Spain - LaLiga 2"))
        self.assertFalse(tracked_league("Premier League"))
        self.assertFalse(tracked_league("Brazil - Serie A"))


if __name__ == "__main__":
    unittest.main()
