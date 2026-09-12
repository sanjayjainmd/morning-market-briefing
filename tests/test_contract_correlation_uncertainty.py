import unittest
from datetime import timedelta

from elimination_bot.config import (
    CorrelationConfig,
    FeeConfig,
    ModelConfig,
    UncertaintyConfig,
    VerificationConfig,
)
from elimination_bot.contract import find_episode, summarize, verify
from elimination_bot.correlation import (
    cluster_signals,
    domain_of,
    effective_source_count,
)
from elimination_bot.fees import FeeSchedule, load_fee_book
from elimination_bot.models import Access, Market, Signal, utcnow
from elimination_bot.probability import estimate_probability, recency_decay
from elimination_bot.uncertainty import UncertaintyInputs, interval

FULL_RULES = (
    "Resolves YES if the contestant is eliminated in episode 7 as shown in the "
    "original broadcast. A withdrawal, quit, disqualification or medical "
    "evacuation shall count as an elimination. A non-elimination week resolves "
    "NO for every contestant."
)


def market(**overrides):
    metadata = {
        "rules": FULL_RULES,
        "resolution_source": "Original network broadcast",
        "status": "open",
    }
    metadata.update(overrides.pop("metadata", {}))
    defaults = dict(
        venue="fixture",
        market_id="M",
        group_id="EP7",
        title="Will Alex be eliminated in episode 7?",
        subject="Alex",
        show="Example Show",
        close_time=utcnow() + timedelta(hours=8),
        metadata=metadata,
    )
    defaults.update(overrides)
    return Market(**defaults)


class ContractVerificationTests(unittest.TestCase):
    def setUp(self):
        self.config = VerificationConfig()

    def test_a_complete_contract_verifies(self):
        result = verify(market(), self.config)
        self.assertTrue(result.ok, result.issues)
        self.assertEqual(result.episode, "episode 7")
        self.assertIn("broadcast", result.resolution_source)

    def test_episode_is_found_in_several_phrasings(self):
        for title, expected in (
            ("Eliminated in Episode 12?", "Episode 12"),
            ("Voted off in week 3", "week 3"),
            ("Sent home on Day 40", "Day 40"),
            ("Eliminated on 2026-10-01", "2026-10-01"),
        ):
            self.assertEqual(find_episode(market(title=title)), expected)

    def test_missing_rules_block_the_trade(self):
        result = verify(market(metadata={"rules": ""}), self.config)
        self.assertFalse(result.ok)
        self.assertTrue(any("rules" in issue for issue in result.issues))

    def test_missing_resolution_source_blocks_the_trade(self):
        result = verify(market(metadata={"resolution_source": None}), self.config)
        self.assertFalse(result.ok)
        self.assertTrue(any("resolution source" in issue for issue in result.issues))

    def test_unaddressed_edge_case_blocks_the_trade(self):
        thin = "Resolves YES if the contestant is eliminated in episode 7."
        result = verify(
            market(
                title="Will Alex withdraw or be eliminated in episode 7?",
                metadata={"rules": thin},
            ),
            self.config,
        )
        self.assertFalse(result.ok)
        self.assertIn("withdraw", result.reason())

    def test_an_addressed_edge_case_is_fine(self):
        self.assertTrue(
            verify(
                market(title="Will Alex withdraw or be eliminated in episode 7?"),
                self.config,
            ).ok
        )

    def test_already_closed_market_is_rejected(self):
        result = verify(market(close_time=utcnow() - timedelta(minutes=1)), self.config)
        self.assertFalse(result.ok)
        self.assertIn("already passed", result.reason())

    def test_an_episode_that_already_aired_is_rejected(self):
        result = verify(
            market(metadata={"aired_at": (utcnow() - timedelta(hours=2)).isoformat()}),
            self.config,
        )
        self.assertFalse(result.ok)
        self.assertIn("already aired", result.reason())

    def test_settled_status_counts_as_aired(self):
        self.assertFalse(verify(market(metadata={"status": "settled"}), self.config).ok)

    def test_summary_counts_rejections(self):
        results = {
            "a": verify(market(), self.config),
            "b": verify(market(metadata={"rules": ""}), self.config),
        }
        summary = summarize(results)
        self.assertEqual(summary, {**summary, "checked": 2, "verified": 1, "rejected": 1})


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.config = CorrelationConfig()

    def signal(self, source, url, lean=1.5, minutes_ago=0):
        return Signal(
            source_id=source,
            market_key="fixture:M",
            subject="Alex",
            observed_at=utcnow() - timedelta(minutes=minutes_ago),
            lean=lean,
            confidence=0.9,
            access=Access.PUBLIC,
            url=url,
        )

    def test_domain_extraction(self):
        self.assertEqual(domain_of("https://news.example.com/a/b"), "example.com")
        self.assertIsNone(domain_of(None))

    def test_same_domain_repeats_collapse_into_one_claim(self):
        signals = [
            self.signal("archive", "https://spoilers.test/a"),
            self.signal("archive-mirror", "https://spoilers.test/b"),
            self.signal("archive-amp", "https://spoilers.test/c"),
        ]
        clusters = cluster_signals(signals, [0.5, 0.5, 0.5], self.config)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].size, 3)
        # one full-weight member, then geometrically less for each repetition
        self.assertAlmostEqual(clusters[0].effective_weight(0.25), 0.5 * (1 + 0.25 + 0.0625))
        self.assertLess(clusters[0].effective_weight(0.25), 2 * 0.5)

    def test_declared_clusters_beat_domains(self):
        config = CorrelationConfig(clusters={"blog-a": "wire", "blog-b": "wire"})
        clusters = cluster_signals(
            [
                self.signal("blog-a", "https://a.test/x"),
                self.signal("blog-b", "https://b.test/x"),
            ],
            [0.5, 0.5],
            config,
        )
        self.assertEqual(len(clusters), 1)

    def test_independent_domains_stay_independent(self):
        clusters = cluster_signals(
            [
                self.signal("a", "https://a.test/x"),
                self.signal("b", "https://b.test/x"),
            ],
            [0.5, 0.5],
            self.config,
        )
        self.assertEqual(len(clusters), 2)
        self.assertAlmostEqual(effective_source_count(clusters, 0.25), 1.0)

    def test_five_echoes_move_the_price_far_less_than_two_sources(self):
        scores = {f"s{i}": {"observations": 60, "brier": 0.08} for i in range(6)}
        echoes = [self.signal(f"s{i}", "https://one.test/x") for i in range(5)]
        independent = [
            self.signal("s0", "https://one.test/x"),
            self.signal("s1", "https://two.test/y"),
        ]
        echo_estimate = estimate_probability(
            market_key="fixture:M", subject="Alex", market_prob=0.2,
            signals=echoes, config=ModelConfig(), source_scores=scores,
        )
        independent_estimate = estimate_probability(
            market_key="fixture:M", subject="Alex", market_prob=0.2,
            signals=independent, config=ModelConfig(), source_scores=scores,
        )
        self.assertLess(echo_estimate.prob, independent_estimate.prob)
        self.assertLess(echo_estimate.effective_sources, independent_estimate.effective_sources)


class RecencyTests(unittest.TestCase):
    def test_weight_halves_every_half_life(self):
        signal = Signal(
            source_id="s", market_key="k", subject="A",
            observed_at=utcnow() - timedelta(hours=72),
            lean=1.0, confidence=1.0, access=Access.PUBLIC, url="https://a.test",
        )
        self.assertAlmostEqual(recency_decay(signal, 72.0), 0.5, places=3)
        self.assertAlmostEqual(recency_decay(signal, 0.0), 1.0)

    def test_old_evidence_moves_the_price_less(self):
        scores = {"s": {"observations": 60, "brier": 0.08}}
        def at(hours):
            return Signal(
                source_id="s", market_key="k", subject="A",
                observed_at=utcnow() - timedelta(hours=hours),
                lean=2.0, confidence=0.9, access=Access.PUBLIC, url="https://a.test",
            )
        fresh = estimate_probability(
            market_key="k", subject="A", market_prob=0.2, signals=[at(1)],
            config=ModelConfig(), source_scores=scores,
        )
        old = estimate_probability(
            market_key="k", subject="A", market_prob=0.2, signals=[at(240)],
            config=ModelConfig(), source_scores=scores,
        )
        self.assertGreater(fresh.prob, old.prob)
        self.assertGreater(old.stalest_signal_hours, 200)


class UncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.config = UncertaintyConfig()

    def test_more_independent_sources_narrow_the_interval(self):
        one = interval(0.65, UncertaintyInputs(1, 0, 0, 0), self.config)
        many = interval(0.65, UncertaintyInputs(4, 0, 0, 0), self.config)
        self.assertLess(many.high - many.low, one.high - one.low)

    def test_disagreement_widens_the_interval(self):
        agree = interval(0.65, UncertaintyInputs(2, 0.0, 0, 0), self.config)
        disagree = interval(0.65, UncertaintyInputs(2, 1.5, 0, 0), self.config)
        self.assertGreater(disagree.high - disagree.low, agree.high - agree.low)

    def test_unproven_sources_and_staleness_widen_it(self):
        clean = interval(0.65, UncertaintyInputs(2, 0, 0.0, 0), self.config)
        unproven = interval(0.65, UncertaintyInputs(2, 0, 1.0, 0), self.config)
        stale = interval(0.65, UncertaintyInputs(2, 0, 0.0, 240), self.config)
        self.assertGreater(unproven.high - unproven.low, clean.high - clean.low)
        self.assertGreater(stale.high - stale.low, clean.high - clean.low)

    def test_interval_stays_inside_the_unit_line(self):
        band = interval(0.02, UncertaintyInputs(0.2, 2.0, 1.0, 500), self.config)
        self.assertGreater(band.low, 0.0)
        self.assertLess(band.high, 1.0)
        self.assertLessEqual(band.low, band.point)
        self.assertLessEqual(band.point, band.high)

    def test_conservative_bounds_are_pessimistic_for_both_sides(self):
        from elimination_bot.models import Action

        band = interval(0.65, UncertaintyInputs(2, 0, 0, 0), self.config)
        self.assertLess(band.conservative(Action.BUY_YES), band.point)
        self.assertLess(band.conservative(Action.BUY_NO), 1 - band.point)


class FeeScheduleTests(unittest.TestCase):
    def test_unverified_schedule_is_stale(self):
        book = load_fee_book(FeeConfig())
        self.assertIsNotNone(book.warning(["kalshi"]))
        self.assertIn("kalshi", book.stale_venues(["kalshi"]))

    def test_recently_verified_schedule_is_current(self):
        import datetime as dt

        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        book = load_fee_book(FeeConfig(verified_at=today))
        self.assertIsNone(book.warning(["kalshi", "polymarket"]))

    def test_old_verification_goes_stale_again(self):
        book = load_fee_book(FeeConfig(verified_at="2020-01-01"))
        self.assertIsNotNone(book.warning(["kalshi"]))

    def test_schedule_file_overrides_configured_rates(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fee_schedule.json"
            path.write_text(json.dumps({
                "verified_at": "2026-09-01",
                "venues": {"kalshi": {"rate": 0.035}},
            }))
            book = load_fee_book(FeeConfig(), path)
            self.assertAlmostEqual(book.schedule("kalshi").rate, 0.035)
            self.assertAlmostEqual(book.model("kalshi").total(100, 0.5), 0.88)

    def test_live_trading_is_blocked_by_a_stale_schedule(self):
        book = load_fee_book(FeeConfig())
        self.assertIsNotNone(book.blocks_live_trading(["kalshi"]))
        relaxed = load_fee_book(FeeConfig(require_verified_schedule_for_live=False))
        self.assertIsNone(relaxed.blocks_live_trading(["kalshi"]))

    def test_schedule_age_reporting(self):
        import datetime as dt

        schedule = FeeSchedule("kalshi", 0.07, verified_at=dt.date(2026, 9, 1))
        self.assertGreaterEqual(schedule.age_days(dt.date(2026, 9, 12)), 11)
        self.assertIn("verified", schedule.describe(90))


if __name__ == "__main__":
    unittest.main()
