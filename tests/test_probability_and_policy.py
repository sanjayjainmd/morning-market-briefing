import unittest
from datetime import timedelta

from elimination_bot.config import BotConfig, ModelConfig, RiskPolicy
from elimination_bot.edge import FeeModel
from elimination_bot.models import (
    Access,
    Action,
    BookLevel,
    Market,
    OrderBook,
    Quote,
    Signal,
    utcnow,
)
from elimination_bot.policy import ExposureState, decide
from elimination_bot.probability import (
    calibration_table,
    estimate_probability,
    normalize_field,
    source_weight,
)
from elimination_bot.signals.base import InformationPolicyError, PublicInfoPolicy

PROVEN = {"example-spoiler-archive": {"observations": 60, "brier": 0.08}}


def signal(source="example-spoiler-archive", lean=2.0, access=Access.PUBLIC, url="https://e.test/x", confidence=0.9):
    return Signal(
        source_id=source,
        market_key="fixture:M",
        subject="Alex",
        observed_at=utcnow(),
        lean=lean,
        confidence=confidence,
        access=access,
        url=url,
    )


class InformationPolicyTests(unittest.TestCase):
    def test_nonpublic_signals_are_dropped_and_logged(self):
        policy = PublicInfoPolicy()
        kept = policy.filter([signal(), signal(access=Access.NONPUBLIC)])
        self.assertEqual(len(kept), 1)
        self.assertIn("not tradeable", policy.dropped[0]["reason"])

    def test_signals_without_a_citable_url_are_dropped(self):
        policy = PublicInfoPolicy()
        self.assertEqual(policy.filter([signal(url=None)]), [])

    def test_enforce_raises_on_nonpublic_material(self):
        with self.assertRaises(InformationPolicyError):
            PublicInfoPolicy().enforce([signal(access=Access.RESTRICTED)])

    def test_estimate_ignores_nonpublic_signals_entirely(self):
        cfg = ModelConfig()
        clean = estimate_probability(
            market_key="fixture:M", subject="Alex", market_prob=0.2,
            signals=[], config=cfg, source_scores=PROVEN,
        )
        leaked = estimate_probability(
            market_key="fixture:M", subject="Alex", market_prob=0.2,
            signals=[signal(access=Access.NONPUBLIC, lean=4.0)],
            config=cfg, source_scores=PROVEN,
        )
        self.assertAlmostEqual(clean.prob, leaked.prob)
        self.assertTrue(any(c.get("excluded") for c in leaked.components))


class ProbabilityTests(unittest.TestCase):
    def test_unproven_source_moves_the_prior_less_than_a_proven_one(self):
        cfg = ModelConfig()
        unproven = estimate_probability(
            market_key="fixture:M", subject="Alex", market_prob=0.2,
            signals=[signal()], config=cfg, source_scores={},
        )
        proven = estimate_probability(
            market_key="fixture:M", subject="Alex", market_prob=0.2,
            signals=[signal()], config=cfg, source_scores=PROVEN,
        )
        self.assertLess(unproven.prob, proven.prob)
        self.assertGreater(unproven.prob, 0.2)

    def test_a_source_worse_than_a_coin_flip_gets_no_weight(self):
        useless = {"example-spoiler-archive": {"observations": 100, "brier": 0.30}}
        estimate = estimate_probability(
            market_key="fixture:M", subject="Alex", market_prob=0.2,
            signals=[signal()], config=ModelConfig(), source_scores=useless,
        )
        self.assertAlmostEqual(estimate.prob, 0.2, places=6)

    def test_shift_is_capped(self):
        cfg = ModelConfig(max_logit_shift=0.1)
        estimate = estimate_probability(
            market_key="fixture:M", subject="Alex", market_prob=0.2,
            signals=[signal(lean=8.0)], config=cfg, source_scores=PROVEN,
        )
        self.assertLess(estimate.prob, 0.23)

    def test_field_normalization_sums_to_one_and_keeps_order(self):
        cfg = ModelConfig()
        estimates = [
            estimate_probability(
                market_key=f"fixture:{i}", subject=str(i), market_prob=p,
                signals=[], config=cfg,
            )
            for i, p in enumerate([0.50, 0.40, 0.35, 0.20])
        ]
        before = [e.prob for e in estimates]
        normalize_field(estimates, 1.0)
        after = [e.prob for e in estimates]
        self.assertAlmostEqual(sum(after), 1.0, places=6)
        self.assertEqual(sorted(after, reverse=True), after)
        self.assertTrue(all(0 < p < 1 for p in after))
        self.assertTrue(all(a < b for a, b in zip(after, before)))

    def test_source_weight_grows_with_observations(self):
        cfg = ModelConfig()
        few = source_weight({"observations": 5, "brier": 0.08}, cfg)
        many = source_weight({"observations": 200, "brier": 0.08}, cfg)
        self.assertLess(few, many)
        self.assertEqual(source_weight(None, cfg), 0.1)

    def test_calibration_table_buckets_outcomes(self):
        table = calibration_table([(0.75, True), (0.75, False), (0.15, False)])
        bucket = next(b for b in table if b["n"] == 2)
        self.assertAlmostEqual(bucket["observed"], 0.5)
        self.assertAlmostEqual(bucket["predicted"], 0.75)


def quote_for(bids, asks):
    return Quote(
        market_key="fixture:M",
        observed_at=utcnow(),
        book=OrderBook(
            bids=tuple(BookLevel(p, s) for p, s in bids),
            asks=tuple(BookLevel(p, s) for p, s in asks),
        ),
    )


def market_for(minutes_to_close=600):
    return Market(
        venue="fixture", market_id="M", group_id="EP7", title="Alex eliminated",
        subject="Alex", show="Example Show",
        close_time=utcnow() + timedelta(minutes=minutes_to_close),
    )


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = BotConfig(bankroll=10_000)
        self.fee_model = FeeModel()

    def run_decide(self, prob, quote=None, market=None, exposure=None, config=None):
        from elimination_bot.models import Estimate

        quote = quote or quote_for([(0.17, 400)], [(0.20, 500), (0.22, 900)])
        return decide(
            cycle_id="cyc-test",
            market=market or market_for(),
            quote=quote,
            estimate=Estimate(market_key="fixture:M", subject="Alex", prob=prob, prior_prob=0.185),
            config=config or self.config,
            fee_model=self.fee_model,
            exposure=exposure or ExposureState(),
        )

    def test_a_large_public_edge_becomes_a_trade(self):
        decision = self.run_decide(0.40)
        self.assertIs(decision.action, Action.BUY_YES)
        self.assertGreater(decision.contracts, 0)
        self.assertLess(decision.limit_price, 0.40)
        self.assertIn("net edge", decision.reasons[-1])

    def test_small_edge_is_declined_with_a_reason(self):
        decision = self.run_decide(0.25)
        self.assertIs(decision.action, Action.PASS)
        self.assertTrue(any("net edge" in r for r in decision.reasons))

    def test_wide_spread_is_declined(self):
        decision = self.run_decide(0.40, quote=quote_for([(0.05, 400)], [(0.20, 500)]))
        self.assertIs(decision.action, Action.PASS)
        self.assertIn("spread", decision.reasons[0])

    def test_market_about_to_close_is_declined(self):
        decision = self.run_decide(0.40, market=market_for(minutes_to_close=5))
        self.assertIs(decision.action, Action.PASS)
        self.assertIn("min to close", decision.reasons[0])

    def test_far_dated_market_is_declined(self):
        decision = self.run_decide(0.40, market=market_for(minutes_to_close=60 * 24 * 60))
        self.assertIs(decision.action, Action.PASS)
        self.assertIn("days to close", decision.reasons[0])

    def test_thin_top_of_book_is_declined(self):
        decision = self.run_decide(0.40, quote=quote_for([(0.17, 400)], [(0.20, 5), (0.21, 900)]))
        self.assertIs(decision.action, Action.PASS)
        self.assertIn("top of book", decision.reasons[0])

    def test_near_certainty_is_refused(self):
        decision = self.run_decide(0.99)
        self.assertIs(decision.action, Action.PASS)
        self.assertIn("outside", decision.reasons[0])

    def test_group_exposure_already_spent_blocks_the_trade(self):
        exposure = ExposureState()
        exposure.add("EP7", self.config.risk.max_fraction_per_group * self.config.bankroll)
        decision = self.run_decide(0.40, exposure=exposure)
        self.assertIs(decision.action, Action.PASS)
        self.assertIn("size 0", decision.reasons[0])

    def test_one_sided_book_is_declined(self):
        decision = self.run_decide(0.40, quote=quote_for([], [(0.20, 500)]))
        self.assertIs(decision.action, Action.PASS)

    def test_buying_no_when_the_market_is_too_high(self):
        decision = self.run_decide(0.05, quote=quote_for([(0.40, 600)], [(0.43, 600)]))
        self.assertIs(decision.action, Action.BUY_NO)
        self.assertGreater(decision.contracts, 0)


if __name__ == "__main__":
    unittest.main()
