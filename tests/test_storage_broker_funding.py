import json
import tempfile
import unittest
from pathlib import Path

from elimination_bot.broker import LiveBroker, LiveTradingDisabled, PaperBroker
from elimination_bot.config import BotConfig
from elimination_bot.edge import FeeModel
from elimination_bot.funding import FundingStatus, Treasury, kill_switch_engaged
from elimination_bot.models import (
    Action,
    BookLevel,
    Decision,
    Market,
    OrderBook,
    Outcome,
    Quote,
    utcnow,
)
from elimination_bot.storage import AuditLog


def quote(bids=((0.30, 100),), asks=((0.34, 60), (0.36, 500))):
    return Quote(
        market_key="fixture:M",
        observed_at=utcnow(),
        book=OrderBook(
            bids=tuple(BookLevel(p, s) for p, s in bids),
            asks=tuple(BookLevel(p, s) for p, s in asks),
        ),
    )


def decision(contracts=100, limit=0.40, action=Action.BUY_YES):
    return Decision(
        cycle_id="cyc-1", market_key="fixture:M", subject="Alex", action=action,
        contracts=contracts, limit_price=limit, reasons=["test"], edge=None, estimate=None,
    )


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.log = AuditLog(":memory:")

    def tearDown(self):
        self.log.close()

    def test_market_upsert_is_idempotent(self):
        market = Market(venue="fixture", market_id="M", group_id="EP7",
                        title="t", subject="Alex", show="Show")
        self.log.record_market(market)
        self.log.record_market(market)
        self.assertEqual(self.log.counts()["markets"], 1)

    def test_settled_trades_compute_realized_pnl(self):
        broker = PaperBroker(log=self.log, fee_model=FeeModel(), haircut=0.0)
        broker.place(decision(contracts=60, limit=0.34), quote())
        self.log.record_outcome(Outcome(market_key="fixture:M", subject="Alex", eliminated=True))
        trades = self.log.settled_trades()
        self.assertEqual(len(trades), 1)
        self.assertTrue(trades[0]["won"])
        self.assertAlmostEqual(trades[0]["pnl"], 60 - (60 * 0.34) - trades[0]["fees"])
        self.assertEqual(trades[0]["contracts"], 60)
        self.assertTrue(trades[0]["settled"])

    def test_losing_trade_loses_the_whole_stake(self):
        broker = PaperBroker(log=self.log, fee_model=FeeModel(), haircut=0.0)
        broker.place(decision(contracts=60, limit=0.34), quote())
        self.log.record_outcome(Outcome(market_key="fixture:M", subject="Alex", eliminated=False))
        trade = self.log.settled_trades()[0]
        self.assertLess(trade["pnl"], 0)
        self.assertAlmostEqual(trade["pnl"], -(60 * 0.34) - trade["fees"])

    def test_open_positions_exclude_settled_markets(self):
        broker = PaperBroker(log=self.log, fee_model=FeeModel(), haircut=0.0)
        broker.place(decision(contracts=60, limit=0.34), quote())
        self.assertIn("fixture:M", self.log.open_positions())
        self.log.record_outcome(Outcome(market_key="fixture:M", subject="Alex", eliminated=True))
        self.assertEqual(self.log.open_positions(), {})

    def test_export_writes_one_jsonl_per_table(self):
        self.log.start_cycle("shadow", 1000)
        with tempfile.TemporaryDirectory() as tmp:
            written = self.log.export_jsonl(tmp)
            names = {p.name for p in written}
            self.assertIn("cycles.jsonl", names)
            rows = (Path(tmp) / "cycles.jsonl").read_text().strip().splitlines()
            self.assertEqual(json.loads(rows[0])["mode"], "shadow")


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.log = AuditLog(":memory:")

    def tearDown(self):
        self.log.close()

    def test_paper_fill_pays_the_haircut(self):
        broker = PaperBroker(log=self.log, fee_model=FeeModel(), haircut=0.005)
        _order, fill = broker.place(decision(contracts=60, limit=0.40), quote())
        self.assertAlmostEqual(fill.price, 0.345)
        self.assertGreater(fill.fees, 0)

    def test_paper_fill_never_exceeds_the_limit_price(self):
        broker = PaperBroker(log=self.log, fee_model=FeeModel(), haircut=0.05)
        _order, fill = broker.place(decision(contracts=60, limit=0.34), quote())
        self.assertLessEqual(fill.price, 0.34)

    def test_partial_fill_is_recorded_as_partial(self):
        broker = PaperBroker(log=self.log, fee_model=FeeModel(), haircut=0.0)
        order, fill = broker.place(decision(contracts=200, limit=0.34), quote())
        self.assertEqual(fill.contracts, 60)
        self.assertEqual(order.status, "partial")

    def test_unfillable_order_produces_no_fill(self):
        broker = PaperBroker(log=self.log, fee_model=FeeModel())
        order, fill = broker.place(decision(contracts=10, limit=0.20), quote())
        self.assertIsNone(fill)
        self.assertEqual(order.status, "unfilled")

    def test_live_broker_refuses_to_trade(self):
        with self.assertRaises(LiveTradingDisabled):
            LiveBroker().place(decision(), quote())


class FundingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = BotConfig(
            db_path=str(root / "db.sqlite3"),
            dormant_path=str(root / "DORMANT"),
            kill_switch_path=str(root / "KILL"),
        )
        self.log = AuditLog(self.config.db_path)
        self.treasury = Treasury(self.config, self.log)

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def test_empty_reserve_is_unfunded(self):
        self.assertIs(self.treasury.state().status, FundingStatus.UNFUNDED)

    def test_deposit_creates_runway(self):
        state = self.treasury.deposit(480.0)
        self.assertIs(state.status, FundingStatus.HEALTHY)
        self.assertAlmostEqual(state.months_of_runway, 12.0)

    def test_low_reserve_warns_before_it_runs_out(self):
        self.treasury.deposit(120.0)
        self.assertIs(self.treasury.state().status, FundingStatus.LOW)

    def test_running_out_pauses_instead_of_destroying(self):
        self.treasury.deposit(40.0)
        self.treasury.pay_hosting(40.0)
        self.assertTrue(self.treasury.is_dormant())
        marker = Path(self.config.dormant_path).read_text()
        self.assertIn("reason:", marker)
        # the audit log survives dormancy — that is the whole point
        self.assertGreater(self.log.counts()["funding_events"], 0)

    def test_resume_lifts_dormancy(self):
        self.treasury.enter_dormancy("test")
        self.assertTrue(self.treasury.resume())
        self.assertFalse(self.treasury.is_dormant())
        self.assertFalse(self.treasury.resume())

    def test_sweep_only_takes_from_realized_profit(self):
        self.assertEqual(self.treasury.sweep_realized_profit(-50.0), 0.0)
        swept = self.treasury.sweep_realized_profit(100.0)
        self.assertAlmostEqual(swept, 50.0)
        self.assertAlmostEqual(self.treasury.reserve(), 50.0)

    def test_sweep_stops_at_the_target_reserve(self):
        self.treasury.deposit(480.0)
        self.assertEqual(self.treasury.sweep_realized_profit(1000.0), 0.0)

    def test_kill_switch_file_is_detected(self):
        self.assertFalse(kill_switch_engaged(self.config))
        Path(self.config.kill_switch_path).write_text("stop")
        self.assertTrue(kill_switch_engaged(self.config))


if __name__ == "__main__":
    unittest.main()
