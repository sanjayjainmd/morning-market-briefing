import unittest

from elimination_bot.config import RiskPolicy
from elimination_bot.edge import (
    FeeModel,
    available_depth,
    best_action,
    build_edge,
    fee_model_for,
    walk_book,
)
from elimination_bot.config import FeeConfig
from elimination_bot.models import Action, BookLevel, OrderBook
from elimination_bot.sizing import kelly_fraction, size_position


def book(bids=((0.30, 100), (0.29, 400)), asks=((0.34, 50), (0.36, 400))):
    return OrderBook(
        bids=tuple(BookLevel(p, s) for p, s in bids),
        asks=tuple(BookLevel(p, s) for p, s in asks),
    )


class FeeTests(unittest.TestCase):
    def test_kalshi_formula_rounds_up_to_the_cent(self):
        # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> $0.02
        self.assertAlmostEqual(FeeModel().total(1, 0.50), 0.02)

    def test_fee_is_largest_at_fifty_cents(self):
        mid = FeeModel().total(1000, 0.50)
        edge_ = FeeModel().total(1000, 0.05)
        self.assertGreater(mid, edge_)

    def test_zero_contracts_is_free(self):
        self.assertEqual(FeeModel().total(0, 0.5), 0.0)

    def test_venue_fee_models(self):
        fees = FeeConfig()
        self.assertGreater(fee_model_for("kalshi", fees).total(100, 0.5), 0)
        self.assertEqual(fee_model_for("polymarket", fees).total(100, 0.5), 0.0)


class BookWalkTests(unittest.TestCase):
    def test_yes_walk_crosses_levels_in_price_order(self):
        filled, vwap = walk_book(book(), Action.BUY_YES, 120)
        self.assertEqual(filled, 120)
        self.assertAlmostEqual(vwap, (50 * 0.34 + 70 * 0.36) / 120)

    def test_no_side_is_priced_as_one_minus_the_bid(self):
        filled, vwap = walk_book(book(), Action.BUY_NO, 150)
        self.assertEqual(filled, 150)
        self.assertAlmostEqual(vwap, (100 * 0.70 + 50 * 0.71) / 150)

    def test_limit_price_stops_the_walk(self):
        filled, vwap = walk_book(book(), Action.BUY_YES, 500, limit_price=0.34)
        self.assertEqual(filled, 50)
        self.assertAlmostEqual(vwap, 0.34)

    def test_empty_side_fills_nothing(self):
        empty = OrderBook(bids=(BookLevel(0.3, 10),), asks=())
        self.assertEqual(walk_book(empty, Action.BUY_YES, 10), (0, 0.0))

    def test_available_depth_respects_the_limit(self):
        self.assertEqual(available_depth(book(), Action.BUY_YES, 0.34), 50)
        self.assertEqual(available_depth(book(), Action.BUY_YES, 0.36), 450)


class EdgeTests(unittest.TestCase):
    def test_net_edge_subtracts_slippage_fees_and_margin(self):
        edge = build_edge(
            book=book(),
            action=Action.BUY_YES,
            prob_eliminated=0.55,
            contracts=120,
            fee_model=FeeModel(),
            safety_margin=0.02,
        )
        self.assertAlmostEqual(edge.gross_edge, 0.55 - 0.34)
        self.assertLess(edge.net_edge, edge.gross_edge)
        self.assertAlmostEqual(
            edge.net_edge, 0.55 - edge.vwap - edge.fee_per_contract - 0.02
        )
        self.assertGreater(edge.slippage, 0)

    def test_no_side_edge_uses_the_complement_probability(self):
        edge = build_edge(
            book=book(),
            action=Action.BUY_NO,
            prob_eliminated=0.10,
            contracts=50,
            fee_model=FeeModel(),
            safety_margin=0.0,
        )
        self.assertAlmostEqual(edge.model_prob, 0.90)
        self.assertAlmostEqual(edge.top_of_book, 0.70)

    def test_best_action_passes_when_the_model_sits_inside_the_spread(self):
        self.assertIs(best_action(book(), 0.32), Action.PASS)
        self.assertIs(best_action(book(), 0.60), Action.BUY_YES)
        self.assertIs(best_action(book(), 0.05), Action.BUY_NO)


class SizingTests(unittest.TestCase):
    def test_kelly_is_zero_without_edge(self):
        self.assertEqual(kelly_fraction(0.30, 0.35), 0.0)
        self.assertAlmostEqual(kelly_fraction(0.60, 0.40), (0.60 - 0.40) / 0.60)

    def test_per_market_cap_binds_before_kelly_on_a_big_edge(self):
        result = size_position(
            prob=0.80,
            all_in_cost=0.40,
            bankroll=10_000,
            policy=RiskPolicy(),
            book=book(asks=((0.40, 100_000),)),
            action=Action.BUY_YES,
            limit_price=0.45,
        )
        self.assertEqual(result.binding_constraint, "per_market_cap")
        self.assertEqual(result.contracts, int(0.02 * 10_000 // 0.40))

    def test_liquidity_cap_overrides_dollar_caps(self):
        result = size_position(
            prob=0.80,
            all_in_cost=0.40,
            bankroll=10_000,
            policy=RiskPolicy(),
            book=book(asks=((0.40, 200),)),
            action=Action.BUY_YES,
            limit_price=0.45,
        )
        self.assertEqual(result.binding_constraint, "liquidity")
        self.assertEqual(result.contracts, 50)  # 25% of 200 resting contracts

    def test_group_budget_already_spent_blocks_further_size(self):
        policy = RiskPolicy()
        result = size_position(
            prob=0.80,
            all_in_cost=0.40,
            bankroll=1_000,
            policy=policy,
            book=book(asks=((0.40, 10_000),)),
            action=Action.BUY_YES,
            limit_price=0.45,
            group_deployed=policy.max_fraction_per_group * 1_000,
        )
        self.assertEqual(result.contracts, 0)
        self.assertEqual(result.binding_constraint, "min_contracts")

    def test_tiny_bankroll_falls_below_the_minimum_ticket(self):
        result = size_position(
            prob=0.80,
            all_in_cost=0.40,
            bankroll=50,
            policy=RiskPolicy(),
            book=book(asks=((0.40, 10_000),)),
            action=Action.BUY_YES,
            limit_price=0.45,
        )
        self.assertEqual(result.contracts, 0)


if __name__ == "__main__":
    unittest.main()


class ConfigTests(unittest.TestCase):
    def test_typos_are_rejected_rather_than_silently_ignored(self):
        from elimination_bot.config import BotConfig

        with self.assertRaises(ValueError):
            BotConfig.from_dict({"bankrol": 500})
        with self.assertRaises(ValueError):
            BotConfig.from_dict({"risk": {"min_net_edg": 0.1}})
        self.assertEqual(BotConfig.from_dict({"_comment": "ok"}).bankroll, 1000.0)

    def test_example_config_matches_the_dataclasses(self):
        from elimination_bot.config import BotConfig

        config = BotConfig.load("config/elimination_bot.example.json")
        self.assertEqual(config.execution.mode, "shadow")
        self.assertAlmostEqual(config.risk.kelly_fraction, 0.25)

    def test_impossible_policies_are_refused(self):
        from elimination_bot.config import BotConfig

        with self.assertRaises(ValueError):
            BotConfig.from_dict({"risk": {"kelly_fraction": 0}})
        with self.assertRaises(ValueError):
            BotConfig.from_dict({"risk": {"max_fraction_per_market": 0.9}})
        with self.assertRaises(ValueError):
            BotConfig.from_dict({"execution": {"mode": "yolo"}})
