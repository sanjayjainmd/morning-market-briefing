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


PROVEN_SOURCES = ("example-spoiler-archive", "example-editing-model")


def seed_reliability(log: AuditLog, *source_ids: str, n: int = 60, brier: float = 0.08) -> None:
    for source_id in (source_ids or PROVEN_SOURCES):
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
        seed_reliability(self.log)
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
        seed_reliability(self.log)
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
        seed_reliability(self.log)
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

    def test_drawdown_stops_new_entries_but_not_exits(self):
        seed_reliability(self.log)
        engine = self.build()
        report = engine.run_cycle()
        traded = report.trades[0].market_key
        engine.settle([Outcome(market_key=traded, subject="Alex", eliminated=False)])
        self.config.bankroll = 100.0  # make the loss large relative to the bankroll

        self.assertIn("drawdown", engine.risk_off_reason() or "")
        self.assertIsNone(engine.halt_reason())  # exits must still be reachable
        follow_up = engine.run_cycle()
        self.assertTrue(follow_up.risk_off)
        self.assertEqual(len(follow_up.trades), 0)
        self.assertTrue(any("risk-off" in w for w in follow_up.warnings))


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


def research_file(path: Path, subject: str, prob: float, sources=("example-spoiler-archive",)) -> Path:
    """Write a public-research file that says what a test needs it to say."""
    payload = {
        "observations": [
            {
                "source_id": source,
                "show": "Example Elimination Show",
                "subject": subject,
                "access": "public",
                "url": f"https://{source}.test/ep7",
                "prob": prob,
                "confidence": 0.9,
            }
            for source in sources
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def shifted_fixture(bid: float, ask: float, subject: str = "Alex") -> dict:
    """The example episode with one contestant's book moved."""
    payload = json.loads((FIXTURES / "example_episode.json").read_text())
    for market in payload["markets"]:
        if market["subject"] == subject:
            market["book"] = {"bids": [[bid, 600]], "asks": [[ask, 600]]}
    return payload


class EngineExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = BotConfig(
            bankroll=10_000,
            db_path=str(self.root / "db.sqlite3"),
            research_path=str(FIXTURES / "example_research.json"),
            dormant_path=str(self.root / "DORMANT"),
            kill_switch_path=str(self.root / "KILL"),
        )
        self.log = AuditLog(self.config.db_path)
        self.log.record_funding_event("deposit", 200.0, None, "test float")
        seed_reliability(self.log)

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def build(self, payload=None) -> Engine:
        venue = (
            FixtureVenue(payload=payload)
            if payload is not None
            else FixtureVenue(path=FIXTURES / "example_episode.json")
        )
        return Engine(
            self.config,
            self.log,
            [venue],
            [PublicResearchSource(self.config.research_path), MicrostructureSource()],
            PaperBroker(log=self.log, fee_model=FeeModel(), haircut=0.005),
            PublicInfoPolicy(),
        )

    def open_a_position(self) -> str:
        report = self.build().run_cycle()
        self.assertEqual(len(report.trades), 1, report.summary())
        return report.trades[0].market_key

    def test_a_position_is_held_while_it_is_still_undervalued(self):
        self.open_a_position()
        report = self.build().run_cycle()
        self.assertEqual(len(report.sells), 0)
        self.assertEqual(report.exits[0].trigger, "hold")
        self.assertEqual(len(report.trades), 0)  # and never doubles up

    def test_a_position_is_sold_when_evidence_reverses(self):
        market_key = self.open_a_position()
        self.config.research_path = str(
            research_file(self.root / "reversed.json", "Alex", 0.15)
        )
        report = self.build(shifted_fixture(0.55, 0.58)).run_cycle()

        self.assertEqual(len(report.sells), 1, report.summary())
        sale = report.sells[0]
        self.assertIs(sale.action, Action.SELL_YES)
        self.assertEqual(sale.market_key, market_key)
        self.assertIn(report.exits[0].trigger, {"signal_reversal", "overpriced"})
        self.assertEqual(len(report.fills), 1)

    def test_selling_realizes_profit_without_waiting_for_settlement(self):
        self.open_a_position()
        self.config.research_path = str(
            research_file(self.root / "reversed.json", "Alex", 0.15)
        )
        self.build(shifted_fixture(0.55, 0.58)).run_cycle()

        trades = self.log.settled_trades()
        self.assertEqual(len(trades), 1)
        self.assertTrue(trades[0]["exited_early"])
        self.assertFalse(trades[0]["settled"])
        self.assertGreater(trades[0]["pnl"], 0)  # bought near 0.20, sold near 0.55
        self.assertEqual(self.log.open_positions(), {})

    def test_an_unverifiable_contract_forces_an_exit(self):
        self.open_a_position()
        broken = shifted_fixture(0.20, 0.23)
        for market in broken["markets"]:
            market["metadata"] = {"status": "open"}  # rules and source gone
        report = self.build(broken).run_cycle()
        self.assertEqual(len(report.sells), 1)
        self.assertEqual(report.exits[0].trigger, "contract_ambiguity")

    def _record_past_loss(self, market_key: str = "fixture:ELIM-EP7-DEV") -> None:
        """A settled losing position, so the drawdown breaker has something to see."""
        from elimination_bot.models import Fill, Order

        order = Order(
            order_id="ord-prior-loss", cycle_id="cyc-prior", market_key=market_key,
            action=Action.BUY_YES, contracts=100, limit_price=0.40, mode="shadow",
        )
        self.log.record_order(order)
        self.log.record_fill(
            Fill(order_id=order.order_id, market_key=market_key, contracts=100,
                 price=0.40, fees=1.0)
        )
        self.log.record_outcome(
            Outcome(market_key=market_key, subject="Dev", eliminated=False)
        )

    def test_exits_still_run_when_the_drawdown_breaker_has_tripped(self):
        self.open_a_position()
        self._record_past_loss()
        self.config.bankroll = 100.0  # the $41 loss is now a >20% drawdown

        engine = self.build(shifted_fixture(0.55, 0.58))
        self.assertIn("drawdown", engine.risk_off_reason() or "")
        report = engine.run_cycle()
        self.assertTrue(report.risk_off)
        self.assertEqual(len(report.trades), 0)
        self.assertGreaterEqual(len(report.exits), 1)
        self.assertTrue(any("risk-off" in w for w in report.warnings))

    def test_a_held_market_is_never_entered_twice(self):
        self.open_a_position()
        report = self.build().run_cycle()
        entries = [d for d in report.decisions if d.action.is_buy]
        self.assertEqual(entries, [])
        passes = [d for d in report.decisions if not d.is_trade]
        self.assertTrue(any("already holding" in r for d in passes for r in d.reasons))

    def test_a_market_exited_this_cycle_is_not_re_entered_immediately(self):
        self.open_a_position()
        self.config.research_path = str(
            research_file(self.root / "reversed.json", "Alex", 0.15)
        )
        report = self.build(shifted_fixture(0.55, 0.58)).run_cycle()
        self.assertEqual(len(report.sells), 1)
        self.assertEqual(len(report.trades), 0, "flipped sides in the same cycle")
        self.assertTrue(
            any("just exited" in r for d in report.decisions for r in d.reasons)
        )


class MovingVenue(FixtureVenue):
    """A fixture venue whose book deteriorates after the first quote."""

    def __init__(self, payload, after):
        super().__init__(payload=payload)
        self.after = after
        self.calls = 0

    def fetch_quotes(self, markets):
        self.calls += 1
        if self.calls > 1:
            self.payload = self.after
        return super().fetch_quotes(markets)


class EnginePreflightTests(unittest.TestCase):
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
        seed_reliability(self.log)

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def build(self, venue) -> Engine:
        return Engine(
            self.config,
            self.log,
            [venue],
            [PublicResearchSource(self.config.research_path), MicrostructureSource()],
            PaperBroker(log=self.log, fee_model=FeeModel(), haircut=0.005),
            PublicInfoPolicy(),
        )

    def test_a_price_that_runs_away_before_submission_is_not_chased(self):
        good = json.loads((FIXTURES / "example_episode.json").read_text())
        gone = shifted_fixture(0.45, 0.48)
        report = self.build(MovingVenue(good, gone)).run_cycle()
        self.assertEqual(len(report.fills), 0)
        self.assertEqual(len(report.preflight_rejections), 1)
        self.assertIn("edge gone", report.preflight_rejections[0])

    def test_a_thinner_book_resizes_the_order_downwards(self):
        good = json.loads((FIXTURES / "example_episode.json").read_text())
        thinner = json.loads(json.dumps(good))
        for market in thinner["markets"]:
            if market["subject"] == "Alex":
                market["book"]["asks"] = [[0.20, 120]]
        report = self.build(MovingVenue(good, thinner)).run_cycle()
        self.assertEqual(len(report.fills), 1)
        self.assertLessEqual(report.fills[0].contracts, 30)  # 25% of 120

    def test_the_kill_switch_stops_an_order_mid_cycle(self):
        good = json.loads((FIXTURES / "example_episode.json").read_text())

        class KillingVenue(MovingVenue):
            def __init__(inner, payload, kill_path):
                super().__init__(payload, payload)
                inner.kill_path = kill_path

            def fetch_quotes(inner, markets):
                if inner.calls >= 1:
                    Path(inner.kill_path).write_text("stop")
                return super().fetch_quotes(markets)

        report = self.build(KillingVenue(good, self.config.kill_switch_path)).run_cycle()
        self.assertEqual(len(report.fills), 0)
        self.assertTrue(
            any("kill switch" in r for r in report.preflight_rejections), report.preflight_rejections
        )


class EngineVerificationTests(unittest.TestCase):
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
        seed_reliability(self.log)

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def test_unverifiable_contracts_are_never_traded(self):
        engine = Engine(
            self.config,
            self.log,
            [FixtureVenue(path=FIXTURES / "unverifiable_episode.json")],
            [PublicResearchSource(self.config.research_path), MicrostructureSource()],
            PaperBroker(log=self.log, fee_model=FeeModel()),
            PublicInfoPolicy(),
        )
        report = engine.run_cycle()
        self.assertEqual(report.contracts_verified, 0)
        self.assertEqual(len(report.trades), 0)
        self.assertGreater(report.verification["rejected"], 0)
        rows = self.log.conn.execute("SELECT ok FROM verifications").fetchall()
        self.assertTrue(rows and all(row["ok"] == 0 for row in rows))
