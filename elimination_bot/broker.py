"""Execution.

``PaperBroker`` is the only implementation that places anything. It fills
against the order book that was captured at decision time, charges the same
fees the venue would, and applies an adverse-selection haircut — because in
the real world the resting size you were aiming at is the size most likely to
disappear when you are right.

``LiveBroker`` deliberately refuses to trade. Wiring a real venue's signed
order endpoint is a small amount of code and a large amount of responsibility;
it should be written and tested against a funded account by someone who has
first read a shadow season's results, not shipped ahead of the evidence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .edge import FeeModel, walk_book
from .models import Decision, Fill, Order, Quote
from .storage import AuditLog, new_id


class LiveTradingDisabled(RuntimeError):
    """Raised whenever anything tries to send a real order."""


class Broker(ABC):
    mode: str = "shadow"

    @abstractmethod
    def place(self, decision: Decision, quote: Quote) -> tuple[Order, Fill | None]:
        """Submit an order for a decision and return it with any fill."""

    @abstractmethod
    def cancel_all(self) -> int:
        """Cancel every resting order. Returns how many were cancelled."""


@dataclass
class PaperBroker(Broker):
    """Shadow execution against the captured book."""

    log: AuditLog
    fee_model: FeeModel = field(default_factory=FeeModel)
    haircut: float = 0.005
    mode: str = "shadow"
    open_orders: dict[str, Order] = field(default_factory=dict)

    def place(self, decision: Decision, quote: Quote) -> tuple[Order, Fill | None]:
        order = Order(
            order_id=new_id("ord"),
            cycle_id=decision.cycle_id,
            market_key=decision.market_key,
            action=decision.action,
            contracts=decision.contracts,
            limit_price=decision.limit_price or 1.0,
            mode=self.mode,
        )
        self.log.record_order(order)

        filled, vwap = walk_book(
            quote.book, decision.action, decision.contracts, order.limit_price
        )
        if filled <= 0:
            order.status = "unfilled"
            self.log.record_order(order)
            return order, None

        # Fill slightly worse than the book showed: queue position, latency,
        # and the fact that liquidity leans away from informed flow. Worse
        # means paying more when buying and receiving less when selling.
        if decision.action.is_sell:
            price = max(order.limit_price, vwap - self.haircut)
        else:
            price = min(order.limit_price, vwap + self.haircut)
        fill = Fill(
            order_id=order.order_id,
            market_key=decision.market_key,
            contracts=filled,
            price=price,
            fees=self.fee_model.total(filled, price),
        )
        order.status = "filled" if filled == decision.contracts else "partial"
        self.log.record_order(order)
        self.log.record_fill(fill)
        return order, fill

    def cancel_all(self) -> int:
        count = 0
        for order in list(self.open_orders.values()):
            order.status = "cancelled"
            self.log.record_order(order)
            count += 1
        self.open_orders.clear()
        return count


@dataclass
class LiveBroker(Broker):
    """Guard rail. Refuses to place orders; never silently no-ops."""

    venue: str = "kalshi"
    mode: str = "live"

    def place(self, decision: Decision, quote: Quote) -> tuple[Order, Fill | None]:
        raise LiveTradingDisabled(
            "Live order placement is not implemented. Run the engine in shadow "
            "mode, collect a full season of decisions, and clear the criteria in "
            "elimination_bot.evaluate.readiness_report() before implementing and "
            "testing a signed order path against a funded account."
        )

    def cancel_all(self) -> int:
        # Cancelling is the one live action that must always be available, but
        # there is nothing to cancel while placement is disabled.
        return 0


def build_broker(config, log: AuditLog, fee_model: FeeModel) -> Broker:
    if config.execution.is_live:
        return LiveBroker()
    return PaperBroker(
        log=log,
        fee_model=fee_model,
        haircut=config.execution.adverse_selection_haircut,
    )
