import json
import tempfile
import unittest
from pathlib import Path

from elimination_bot.broker import PaperBroker
from elimination_bot.config import BotConfig
from elimination_bot.edge import FeeModel
from elimination_bot.engine import Engine
from elimination_bot.evaluate import (
    bootstrap_ci,
    calibration_error,
    concentration,
    readiness_report,
    split_holdout,
)
from elimination_bot.models import Action, Outcome
from elimination_bot.signals import MicrostructureSource, PublicResearchSource
from elimination_bot.signals.base import PublicInfoPolicy
from elimination_bot.storage import AuditLog
from elimination_bot.venues import FixtureVenue

FIXTURES = Path(__file__).parent / "fixtures"


def seed_reliability(log: AuditLog, source_id: str, n: int = 60, brier: float = 0.08) -> None:
    for _ in range(n):
        log.update_source_score(source_id, brier, 0.3)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = BotConfig(
            bankroll=10_000,
            db_path=str(root / "db.sqlite3"),
            research_path=str(FIXTURES / "example_research.json"),
            dormant_path=str(root / "DORMANT"),
            kill_switch_path=str(root / "KILL"),
        )
        self.log = AuditLog(self.config.db_path)
        self.log.record_funding_event("deposit", 200.0, None, "test float")

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def build(self) -> Engine:
        return Engine(
            self.config,
            self.log,
            [FixtureVenue(path=FIXTURES / "example_episode.json")],
            [PublicResearchSource(self.config.research_path), MicrostructureSource()],
            PaperBroker(log=self.log, fee_model=FeeModel(), haircut=0.005),
            PublicInfoPolicy(),
        )

    def test_cycle_with_an_unproven_source_declines_everything(self):
        report = self.build().run_cycle()
        self.assertEqual(report.markets_seen, 4)
        self.assertEqual(report.quotes_captured, 4)
        self.assertEqual(len(report.trades), 0, report.summary())
        self.assertTrue(all(d.reasons for d in report.decisions))

    def test_cycle_trades_once_a_source_has_earned_its_weight(self):
        seed_reliability(self.log, "example-spoiler-archive")
        report = self.build().run_cycle()
        trades = report.trades
        self.assertEqual(len(trades), 1, report.summary())
        trade = trades[0]
        self.assertEqual(trade.subject, "Alex")
        self.assertIs(trade.action, Action.BUY_YES)
        self.assertEqual(len(report.fills), 1)
        self.assertGreater(report.fills[0].contracts, 0)

    def test_leaked_signal_is_dropped_every_cycle(self):
        report = self.build().run_cycle()
        dropped = [d for d in report.signals_dropped if d["source_id"] == "anonymous-production-tip"]
        self.assertEqual(len(dropped), 1)
        self.assertNotIn(
            "anonymous-production-tip",
            [row["source_id"] for row in self.log.conn.execute("SELECT source_id FROM signals")],
        )

    def test_field_probabilities_are_normalized_within_the_episode(self):
        seed_reliability(self.log, "example-spoiler-archive")
        self.build().run_cycle()
        probs = [
            row["prob"]
            for row in self.log.conn.execute("SELECT prob FROM estimates")
        ]
        self.assertEqual(len(probs), 4)
        self.assertAlmostEqual(sum(probs), 1.0, places=5)

    def test_kill_switch_halts_the_cycle_before_any_market_is_touched(self):
        Path(self.config.kill_switch_path).write_text("stop")
        report = self.build().run_cycle()
        self.assertIsNotNone(report.halted)
        self.assertEqual(report.quotes_captured, 0)

    def test_dormancy_halts_the_cycle(self):
        Path(self.config.dormant_path).write_text("paused")
        self.assertIsNotNone(self.build().run_cycle().halted)

    def test_unfunded_reserve_halts_the_cycle(self):
        self.log.record_funding_event("hosting_payment", 200.0, None, "drain")
        self.assertIn("unfunded", self.build().run_cycle().halted)

    def test_settlement_grades_sources_and_sweeps_profit(self):
        seed_reliability(self.log, "example-spoiler-archive")
        engine = self.build()
        report = engine.run_cycle()
        traded = report.trades[0].market_key
        result = engine.settle([
            Outcome(market_key=traded, subject="Alex", eliminated=True),
        ])
        self.assertEqual(result["trades"], 1)
        self.assertGreater(result["realized_pnl"], 0)
        self.assertGreater(result["swept_to_reserve"], 0)
        scores = self.log.source_scores()
        self.assertIn("example-spoiler-archive", scores)
        self.assertGreater(scores["example-spoiler-archive"]["observations"], 0)

    def test_drawdown_stops_trading(self):
        seed_reliability(self.log, "example-spoiler-archive")
        engine = self.build()
        report = engine.run_cycle()
        traded = report.trades[0].market_key
        engine.settle([Outcome(market_key=traded, subject="Alex", eliminated=False)])
        self.config.bankroll = 100.0  # make the loss large relative to the bankroll
        self.assertIn("drawdown", engine.halt_reason() or "")


class EvaluateTests(unittest.TestCase):
    def test_bootstrap_ci_brackets_the_mean_and_is_deterministic(self):
        data = [0.1, -0.05, 0.2, 0.15, -0.02, 0.3, 0.05, 0.08]
        lo, hi = bootstrap_ci(data, iterations=2000)
        self.assertEqual((lo, hi), bootstrap_ci(data, iterations=2000))
        self.assertLess(lo, sum(data) / len(data) < hi, True)

    def test_bootstrap_ci_of_noise_includes_zero(self):
        lo, hi = bootstrap_ci([1.0, -1.0] * 40, iterations=2000)
        self.assertLess(lo, 0)
        self.assertGreater(hi, 0)

    def test_calibration_error_is_zero_for_a_calibrated_forecaster(self):
        pairs = [(0.5, i % 2 == 0) for i in range(100)]
        self.assertAlmostEqual(calibration_error(pairs), 0.0, places=6)

    def test_calibration_error_catches_overconfidence(self):
        pairs = [(0.9, i % 2 == 0) for i in range(100)]
        self.assertAlmostEqual(calibration_error(pairs), 0.4, places=6)

    def test_concentration_finds_the_dominant_bucket(self):
        trades = [
            {"show": "A", "pnl": 90.0},
            {"show": "B", "pnl": 10.0},
        ]
        result = concentration(trades, "show")
        self.assertEqual(result["top"], "A")
        self.assertAlmostEqual(result["share"], 0.9)

    def test_holdout_split_is_chronological(self):
        trades = [{"filled_at": f"2026-09-{d:02d}"} for d in range(1, 11)]
        train, holdout = split_holdout(trades, 0.3)
        self.assertEqual(len(train), 7)
        self.assertEqual(holdout[0]["filled_at"], "2026-09-08")

    def test_readiness_fails_loudly_on_an_empty_log(self):
        with AuditLog(":memory:") as log:
            report = readiness_report(log)
            self.assertFalse(report.ready)
            failed = [c.name for c in report.criteria if not c.passed]
            self.assertIn("independent opportunities", failed)
            self.assertIn("holdout period profitable", failed)
            self.assertIn("stay in shadow mode", report.render())

    def test_readiness_report_serializes(self):
        with AuditLog(":memory:") as log:
            payload = json.loads(json.dumps(readiness_report(log).to_dict(), default=str))
            self.assertIn("criteria", payload)
            self.assertFalse(payload["ready"])


if __name__ == "__main__":
    unittest.main()


class SourceGradingTests(unittest.TestCase):
    """A source's track record counts claims, not how often we re-read them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = BotConfig(
            bankroll=10_000,
            db_path=str(root / "db.sqlite3"),
            research_path=str(FIXTURES / "example_research.json"),
            dormant_path=str(root / "DORMANT"),
            kill_switch_path=str(root / "KILL"),
        )
        self.log = AuditLog(self.config.db_path)
        self.log.record_funding_event("deposit", 200.0, None, "test float")

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def build(self) -> Engine:
        return Engine(
            self.config,
            self.log,
            [FixtureVenue(path=FIXTURES / "example_episode.json")],
            [PublicResearchSource(self.config.research_path), MicrostructureSource()],
            PaperBroker(log=self.log, fee_model=FeeModel(), haircut=0.005),
            PublicInfoPolicy(),
        )

    def test_repeated_cycles_count_as_one_observation_per_market(self):
        engine = self.build()
        for _ in range(5):
            engine.run_cycle()
        signals = self.log.counts()["signals"]
        engine.settle([
            Outcome(market_key="fixture:ELIM-EP7-ALEX", subject="Alex", eliminated=True),
        ])
        score = self.log.source_scores()["example-spoiler-archive"]
        self.assertGreaterEqual(signals, 5)
        self.assertEqual(score["observations"], 1)

    def test_a_source_that_was_right_scores_better_than_one_that_was_wrong(self):
        engine = self.build()
        engine.run_cycle()
        engine.settle([
            Outcome(market_key="fixture:ELIM-EP7-ALEX", subject="Alex", eliminated=True),
            Outcome(market_key="fixture:ELIM-EP7-BRETT", subject="Brett", eliminated=False),
        ])
        scores = self.log.source_scores()
        self.assertLess(
            scores["example-spoiler-archive"]["brier"], 0.25
        )
