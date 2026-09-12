import unittest

from elimination_bot.config import BotConfig
from elimination_bot.edge import FeeModel
from elimination_bot.exits import ExitContext, assess_exit
from elimination_bot.models import (
    Action,
    BookLevel,
    Estimate,
    OrderBook,
    Position,
    ProbabilityInterval,
    Quote,
    utcnow,
)

FEES = FeeModel()


def quote(bids, asks=((0.99, 1000),)):
    return Quote(
        market_key="fixture:M",
        observed_at=utcnow(),
        book=OrderBook(
            bids=tuple(BookLevel(p, s) for p, s in bids),
            asks=tuple(BookLevel(p, s) for p, s in asks),
        ),
    )


def estimate(point, half_width=0.0):
    return Estimate(
        market_key="fixture:M",
        subject="Alex",
        prob=point,
        prior_prob=point,
        interval=ProbabilityInterval(
            point=point, low=point - half_width, high=point + half_width
        ),
    )


def position(contracts=42, entry=0.47, side=Action.BUY_YES):
    cost = contracts * entry
    return Position(
        market_key="fixture:M",
        subject="Alex",
        show="Example Show",
        group_id="EP7",
        yes_open=contracts if side is Action.BUY_YES else 0,
        no_open=contracts if side is Action.BUY_NO else 0,
        contracts_bought=contracts,
        cost=cost,
        entry_vwap=entry,
    )


class ExitValueTests(unittest.TestCase):
    """The two worked examples, plus the cases they are contrasted with."""

    def setUp(self):
        self.config = BotConfig()

    def assess(self, prob, bid, half_width=0.0, context=None, config=None, pos=None):
        return assess_exit(
            position=pos or position(),
            quote=quote([(bid, 500)]),
            estimate=estimate(prob, half_width),
            config=config or self.config,
            fee_model=FEES,
            context=context or ExitContext(),
        )

    def test_sells_when_the_market_pays_more_than_the_updated_estimate(self):
        # bought at 0.47; estimate has fallen to 0.49 and the bid is 0.56
        result = self.assess(0.49, 0.56)
        self.assertIs(result.action, Action.SELL_YES)
        self.assertEqual(result.trigger, "overpriced")
        self.assertGreater(result.exit_edge, self.config.exits.min_exit_edge)

    def test_holds_a_profitable_position_that_is_still_undervalued(self):
        # bid 0.65 against a fair value of 0.72: profitable, but not a reason
        result = self.assess(0.72, 0.65)
        self.assertIs(result.action, Action.PASS)
        self.assertEqual(result.trigger, "hold")

    def test_a_price_drop_alone_is_not_a_sell(self):
        # the bid fell from 0.47 to 0.38 but nothing about the world changed
        result = self.assess(0.65, 0.38, context=ExitContext(entry_prob_eliminated=0.65))
        self.assertIs(result.action, Action.PASS)
        self.assertEqual(result.trigger, "hold")

    def test_uncertainty_makes_the_bot_slower_to_sell(self):
        certain = self.assess(0.49, 0.56, half_width=0.0)
        uncertain = self.assess(0.49, 0.56, half_width=0.06)
        self.assertIs(certain.action, Action.SELL_YES)
        self.assertIs(uncertain.action, Action.PASS)

    def test_exit_costs_are_subtracted_from_the_bid(self):
        result = self.assess(0.49, 0.56)
        self.assertLess(result.net_proceeds, 0.56)

    def test_no_resting_bid_means_no_exit(self):
        result = assess_exit(
            position=position(),
            quote=quote([], asks=((0.6, 100),)),
            estimate=estimate(0.4),
            config=self.config,
            fee_model=FEES,
            context=ExitContext(),
        )
        self.assertIs(result.action, Action.PASS)
        self.assertEqual(result.trigger, "no_bid")

    def test_a_no_position_is_exited_against_the_ask(self):
        # Selling NO means buying YES back from the ask, so 42 NO contracts
        # realise 1 - 0.10 each, less fees and the exit buffer.
        result = assess_exit(
            position=position(side=Action.BUY_NO, entry=0.30),
            quote=quote([(0.05, 500)], asks=((0.10, 500),)),
            estimate=estimate(0.20),
            config=self.config,
            fee_model=FEES,
            context=ExitContext(),
        )
        self.assertIs(result.action, Action.SELL_NO)
        self.assertEqual(result.trigger, "overpriced")
        self.assertAlmostEqual(result.net_proceeds, 0.90 - FEES.per_contract(42, 0.90) - 0.01)
        self.assertAlmostEqual(result.hold_value, 0.80)

    def test_a_no_position_worth_more_than_the_ask_is_held(self):
        result = assess_exit(
            position=position(side=Action.BUY_NO, entry=0.30),
            quote=quote([(0.05, 500)], asks=((0.10, 500),)),
            estimate=estimate(0.05),
            config=self.config,
            fee_model=FEES,
            context=ExitContext(),
        )
        self.assertIs(result.action, Action.PASS)


class ForcedExitTests(unittest.TestCase):
    def setUp(self):
        self.config = BotConfig()

    def assess(self, context, prob=0.65, bid=0.45, pos=None):
        return assess_exit(
            position=pos or position(),
            quote=quote([(bid, 500)]),
            estimate=estimate(prob),
            config=self.config,
            fee_model=FEES,
            context=context,
        )

    def test_emergency_loss_limit_closes_the_position(self):
        result = self.assess(ExitContext(), prob=0.10, bid=0.10)
        self.assertIs(result.action, Action.SELL_YES)
        self.assertEqual(result.trigger, "emergency_loss_limit")
        self.assertEqual(result.contracts, 42)

    def test_unverifiable_contract_closes_the_position(self):
        result = self.assess(
            ExitContext(verification_ok=False, verification_reason="rules changed")
        )
        self.assertEqual(result.trigger, "contract_ambiguity")
        self.assertEqual(result.contracts, 42)

    def test_a_retracted_source_closes_the_position(self):
        result = self.assess(ExitContext(retracted_sources=["spoiler-archive"]))
        self.assertEqual(result.trigger, "source_invalidated")
        self.assertIn("spoiler-archive", result.reasons[0])

    def test_operational_failure_closes_the_position(self):
        result = self.assess(ExitContext(operational_ok=False, operational_note="clock skew"))
        self.assertEqual(result.trigger, "operational_failure")

    def test_approaching_close_flattens(self):
        result = self.assess(ExitContext(minutes_to_close=10))
        self.assertEqual(result.trigger, "approaching_close")

    def test_stale_evidence_reduces_rather_than_holding_blind(self):
        result = self.assess(ExitContext(stalest_signal_hours=200))
        self.assertEqual(result.trigger, "stale_evidence")
        self.assertEqual(result.contracts, 21)

    def test_episode_exposure_over_cap_reduces(self):
        result = self.assess(ExitContext(group_over_cap=True))
        self.assertEqual(result.trigger, "risk_limit")
        self.assertEqual(result.contracts, 21)

    def test_risk_off_reduces_only_near_fair_value(self):
        near = self.assess(ExitContext(risk_off=True), prob=0.45, bid=0.45)
        far = self.assess(ExitContext(risk_off=True), prob=0.90, bid=0.45)
        self.assertEqual(near.trigger, "drawdown_circuit_breaker")
        self.assertEqual(far.trigger, "hold")

    def test_signal_reversal_reduces_even_when_the_bid_is_poor(self):
        result = self.assess(
            ExitContext(entry_prob_eliminated=0.65), prob=0.40, bid=0.42
        )
        self.assertEqual(result.trigger, "signal_reversal")
        self.assertEqual(result.contracts, 21)

    def test_a_reversal_below_the_exit_price_closes_out(self):
        result = self.assess(
            ExitContext(entry_prob_eliminated=0.65), prob=0.30, bid=0.40
        )
        self.assertEqual(result.trigger, "signal_reversal")
        self.assertEqual(result.contracts, 42)

    def test_a_small_drift_is_not_a_reversal(self):
        result = self.assess(ExitContext(entry_prob_eliminated=0.65), prob=0.60, bid=0.40)
        self.assertEqual(result.trigger, "hold")

    def test_rotation_is_off_by_default_and_opt_in(self):
        context = ExitContext(capital_constrained=True, best_alternative_edge=0.25)
        self.assertEqual(self.assess(context).trigger, "hold")
        self.config.exits.rotate_for_better_opportunity = True
        self.assertEqual(self.assess(context).trigger, "better_opportunity")

    def test_forced_exits_still_refuse_to_dump_below_a_floor(self):
        result = self.assess(ExitContext(minutes_to_close=5), bid=0.45)
        self.assertGreaterEqual(
            result.limit_price, 0.45 - self.config.exits.max_forced_exit_slippage - 1e-9
        )


if __name__ == "__main__":
    unittest.main()
