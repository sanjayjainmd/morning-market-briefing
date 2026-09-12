import unittest

from elimination_bot.venues.base import looks_like_elimination, parse_time, to_prob
from elimination_bot.venues.kalshi import KalshiPublicData
from elimination_bot.venues.polymarket import PolymarketPublicData


class ClassificationTests(unittest.TestCase):
    def test_elimination_phrasings_are_recognised(self):
        for title in (
            "Who will be eliminated in week 4?",
            "Will Alex be voted off this week?",
            "Next houseguest evicted",
            "Who gets sent home on Sunday?",
        ):
            self.assertTrue(looks_like_elimination(title), title)

    def test_outright_winner_markets_are_excluded(self):
        self.assertFalse(looks_like_elimination("Who will win the season?"))
        self.assertFalse(looks_like_elimination("Winner of the final"))

    def test_a_winner_market_that_is_also_an_elimination_still_counts(self):
        self.assertTrue(
            looks_like_elimination("Will the winner of last week be eliminated?")
        )

    def test_price_coercion_rejects_out_of_range_values(self):
        self.assertAlmostEqual(to_prob(35, scale=0.01), 0.35)
        self.assertIsNone(to_prob(150, scale=0.01))
        self.assertIsNone(to_prob("abc"))
        self.assertIsNone(to_prob(None))

    def test_time_parsing_handles_z_suffix_and_epochs(self):
        self.assertIsNotNone(parse_time("2026-09-13T01:00:00Z"))
        self.assertIsNotNone(parse_time(1_789_000_000))
        self.assertIsNone(parse_time("not a date"))


class KalshiParsingTests(unittest.TestCase):
    def test_market_parsing_extracts_subject_and_group(self):
        market = KalshiPublicData.parse_market(
            {
                "ticker": "ELIM-25SEP12-ALEX",
                "event_ticker": "ELIM-25SEP12",
                "series_ticker": "EXAMPLESHOW",
                "title": "Who will be eliminated in episode 7?",
                "yes_sub_title": "Alex",
                "close_time": "2026-09-13T01:00:00Z",
                "volume": 1200,
                "tick_size": 1,
            }
        )
        self.assertEqual(market.subject, "Alex")
        self.assertEqual(market.group_id, "ELIM-25SEP12")
        self.assertEqual(market.venue, "kalshi")
        self.assertAlmostEqual(market.tick_size, 0.01)

    def test_unrelated_markets_are_skipped(self):
        self.assertIsNone(
            KalshiPublicData.parse_market({"ticker": "CPI", "title": "CPI above 3%?"})
        )
        self.assertIsNone(KalshiPublicData.parse_market({"title": "eliminated?"}))

    def test_no_side_is_mirrored_into_the_yes_ask(self):
        book = KalshiPublicData.parse_book({"yes": [[30, 100], [29, 50]], "no": [[64, 80], [62, 40]]})
        self.assertAlmostEqual(book.best_bid, 0.30)
        self.assertAlmostEqual(book.best_ask, 0.36)  # 100 - 64 cents
        self.assertEqual(book.asks[0].size, 80)

    def test_malformed_levels_are_ignored(self):
        book = KalshiPublicData.parse_book(
            {"yes": [[30, 100], ["bad"], [999, 5], [40, 0]], "no": []}
        )
        self.assertEqual(len(book.bids), 1)
        self.assertEqual(book.asks, ())


class PolymarketParsingTests(unittest.TestCase):
    def test_market_parsing_reads_event_and_token(self):
        market = PolymarketPublicData.parse_market(
            {
                "conditionId": "0xabc",
                "question": "Will Alex be eliminated in week 3?",
                "groupItemTitle": "Alex",
                "events": [{"id": "77", "title": "Example Show Week 3"}],
                "clobTokenIds": '["111", "222"]',
                "endDate": "2026-09-13T01:00:00Z",
                "volumeNum": 5000,
                "slug": "example-show-week-3",
            }
        )
        self.assertEqual(market.subject, "Alex")
        self.assertEqual(market.group_id, "77")
        self.assertEqual(market.metadata["yes_token_id"], "111")
        self.assertIn("polymarket.com", market.url)

    def test_book_parsing_accepts_dicts_and_pairs(self):
        book = PolymarketPublicData.parse_book(
            {
                "bids": [{"price": "0.31", "size": "120"}, ["0.30", "50"]],
                "asks": [{"price": "0.35", "size": "90"}],
            }
        )
        self.assertAlmostEqual(book.best_bid, 0.31)
        self.assertAlmostEqual(book.best_ask, 0.35)
        self.assertEqual(len(book.bids), 2)

    def test_market_without_a_clob_token_is_marked_unquotable(self):
        market = PolymarketPublicData.parse_market(
            {
                "conditionId": "0xdef",
                "question": "Who is eliminated next?",
                "events": [{"id": "1", "title": "Show"}],
            }
        )
        self.assertIsNone(market.metadata["yes_token_id"])


if __name__ == "__main__":
    unittest.main()
