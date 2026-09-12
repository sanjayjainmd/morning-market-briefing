"""Paying for the hosting, and what happens when it cannot be paid.

Two rules, both from hard experience with unattended systems:

* Hosting money lives in a prepaid reserve, not in trading margin. The reserve
  is topped up out of *realized* profit only, and never drawn down to fund a
  trade.
* Running out of money pauses the system. It does not delete it. Dormancy
  cancels resting orders, stops new ones, and leaves the logs, the database
  and the code exactly where they are, because a system that destroys its own
  audit trail when the card declines is unauditable by design.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from .config import BotConfig
from .storage import AuditLog


class FundingStatus(str, Enum):
    HEALTHY = "healthy"
    LOW = "low"
    UNFUNDED = "unfunded"


@dataclass
class FundingState:
    reserve: float
    status: FundingStatus
    months_of_runway: float
    message: str


class Treasury:
    """Tracks the operating reserve and drives the dormant state."""

    def __init__(self, config: BotConfig, log: AuditLog) -> None:
        self.config = config
        self.log = log

    # ------------------------------------------------------------ accounting

    def reserve(self) -> float:
        row = self.log.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind IN ('deposit','sweep') THEN amount"
            " WHEN kind='hosting_payment' THEN -amount ELSE 0 END), 0) AS total"
            " FROM funding_events"
        ).fetchone()
        return float(row["total"] or 0.0)

    def state(self) -> FundingState:
        reserve = self.reserve()
        monthly = max(self.config.funding.monthly_cost, 1e-9)
        months = reserve / monthly
        if months <= 0:
            status = FundingStatus.UNFUNDED
            message = "reserve exhausted — trading pauses until it is topped up"
        elif months < self.config.funding.reserve_months_warn:
            status = FundingStatus.LOW
            message = f"{months:.1f} months of runway, below the {self.config.funding.reserve_months_warn:.0f}-month warning line"
        else:
            status = FundingStatus.HEALTHY
            message = f"{months:.1f} months of runway"
        return FundingState(reserve, status, months, message)

    def deposit(self, amount: float, note: str = "manual top-up") -> FundingState:
        self.log.record_funding_event("deposit", amount, None, note)
        state = self.state()
        self.log.record_funding_event("balance", None, state.reserve, state.message)
        return state

    def pay_hosting(self, amount: float | None = None, note: str = "") -> FundingState:
        amount = self.config.funding.monthly_cost if amount is None else amount
        self.log.record_funding_event("hosting_payment", amount, None, note)
        state = self.state()
        if state.status is FundingStatus.UNFUNDED:
            self.enter_dormancy("hosting reserve exhausted")
        return state

    def sweep_realized_profit(self, realized_pnl: float) -> float:
        """Move a slice of realized profit into the reserve. Never margin."""
        if realized_pnl <= 0:
            return 0.0
        target = self.config.funding.reserve_months_target * self.config.funding.monthly_cost
        room = max(0.0, target - self.reserve())
        amount = min(
            room, realized_pnl * self.config.funding.max_sweep_fraction_of_realized
        )
        if amount <= 0:
            return 0.0
        self.log.record_funding_event("sweep", amount, None, "swept from realized profit")
        return amount

    # -------------------------------------------------------------- dormancy

    @property
    def dormant_path(self) -> Path:
        return Path(self.config.dormant_path)

    def is_dormant(self) -> bool:
        return self.dormant_path.exists()

    def enter_dormancy(self, reason: str, broker=None) -> None:
        """Pause, recoverably: cancel orders, stop trading, keep everything."""
        cancelled = broker.cancel_all() if broker is not None else 0
        self.dormant_path.parent.mkdir(parents=True, exist_ok=True)
        self.dormant_path.write_text(
            f"{datetime.now().astimezone().isoformat()}\nreason: {reason}\n"
            f"cancelled_orders: {cancelled}\n"
            "Delete this file (or run `python -m elimination_bot.cli resume`) to "
            "re-enable trading. Logs and database are intentionally preserved.\n",
            encoding="utf-8",
        )
        self.log.record_funding_event("dormancy", None, self.reserve(), reason)

    def resume(self) -> bool:
        if not self.is_dormant():
            return False
        self.dormant_path.unlink()
        self.log.record_funding_event("resume", None, self.reserve(), "dormancy lifted")
        return True


def kill_switch_engaged(config: BotConfig) -> bool:
    """A file on disk stops all trading — the simplest switch that cannot fail."""
    return Path(config.kill_switch_path).exists()
