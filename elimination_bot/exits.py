"""When to sell.

The rule is the entry rule run backwards: sell when the executable exit value
beats the updated value of continuing to hold, by more than a margin.

    net proceeds (bid, walked, minus fees and exit slippage)
        - fair value of holding
        > min_exit_edge

Fair value for an open position is taken from the *optimistic* end of the
uncertainty interval — the opposite end from the one used to buy — so exiting
requires the market to beat even the generous case for holding. Both rules are
pessimistic about the action being taken, which is what makes "do nothing" the
usual answer.

What this module deliberately does not do is sell on a price move. A 20% drop
in a thin prediction market is often one small order, not new information. A
price move triggers reassessment; only a change in fair value, a risk limit,
or a genuinely better exit price triggers a sale. The same applies upward: a
contract bought at $0.47 and now bid $0.56 is held if it is still worth $0.72.

The one exception is the emergency loss limit, which exists to bound damage
when the model is simply wrong, and is not an expected-value judgement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from .config import BotConfig
from .edge import FeeModel, walk_book
from .models import Action, Estimate, Position, Quote


@dataclass
class ExitContext:
    """Everything outside the price that can force a position out."""

    minutes_to_close: float | None = None
    verification_ok: bool = True
    verification_reason: str = ""
    stalest_signal_hours: float | None = None
    retracted_sources: Sequence[str] = ()
    group_over_cap: bool = False
    risk_off: bool = False              # drawdown circuit breaker tripped
    operational_ok: bool = True
    operational_note: str = ""
    best_alternative_edge: float | None = None
    capital_constrained: bool = False
    entry_prob_eliminated: float | None = None


@dataclass
class ExitAssessment:
    market_key: str
    subject: str
    action: Action
    contracts: int
    limit_price: float | None
    trigger: str
    reasons: list[str] = field(default_factory=list)
    net_proceeds: float | None = None
    hold_value: float | None = None

    @property
    def is_exit(self) -> bool:
        return self.action is not Action.PASS and self.contracts > 0

    @property
    def exit_edge(self) -> float | None:
        if self.net_proceeds is None or self.hold_value is None:
            return None
        return self.net_proceeds - self.hold_value


def _reversal(
    position: Position, estimate: Estimate | None, context: ExitContext
) -> tuple[float, float] | None:
    """How far the central estimate for the held side has fallen since entry."""
    if estimate is None or context.entry_prob_eliminated is None:
        return None
    if position.action is Action.BUY_YES:
        entry_point = context.entry_prob_eliminated
        current_point = estimate.prob
    else:
        entry_point = 1 - context.entry_prob_eliminated
        current_point = 1 - estimate.prob
    return entry_point - current_point, current_point


def assess_exit(
    *,
    position: Position,
    quote: Quote,
    estimate: Estimate | None,
    config: BotConfig,
    fee_model: FeeModel,
    context: ExitContext | None = None,
) -> ExitAssessment:
    """Decide whether to sell, reduce, or hold one open position."""
    context = context or ExitContext()
    exits = config.exits
    action = position.action.closing if position.action else None
    open_contracts = position.open_contracts

    def result(act, contracts, trigger, reason, limit=None, proceeds=None, hold=None):
        return ExitAssessment(
            market_key=position.market_key,
            subject=position.subject,
            action=act,
            contracts=contracts,
            limit_price=limit,
            trigger=trigger,
            reasons=[reason],
            net_proceeds=proceeds,
            hold_value=hold,
        )

    if action is None or open_contracts <= 0:
        return result(Action.PASS, 0, "flat", "no open position")

    filled, vwap = walk_book(quote.book, action, open_contracts)
    if filled <= 0:
        return result(Action.PASS, 0, "no_bid", "nothing resting to sell into")

    fee_per_contract = fee_model.per_contract(filled, vwap)
    net_proceeds = vwap - fee_per_contract - exits.exit_cost_buffer
    hold_value = (
        estimate.conservative(action) if estimate is not None else position.entry_vwap
    )
    touch = walk_book(quote.book, action, 1)[1]
    floor = max(0.01, touch - exits.max_forced_exit_slippage)

    # --- forced exits, in descending severity --------------------------------
    basis = position.open_cost
    loss_limit = basis * (1 - exits.emergency_loss_fraction)
    mark = net_proceeds * open_contracts
    if basis > 0 and mark <= loss_limit:
        return result(
            action, filled, "emergency_loss_limit",
            f"mark ${mark:.2f} at or below the hard loss limit ${loss_limit:.2f}"
            f" on a ${basis:.2f} cost basis",
            limit=floor, proceeds=net_proceeds, hold=hold_value,
        )

    if not context.verification_ok:
        return result(
            action, filled, "contract_ambiguity",
            f"contract no longer verifiable: {context.verification_reason}",
            limit=floor, proceeds=net_proceeds, hold=hold_value,
        )

    retracted = [s for s in context.retracted_sources]
    if retracted:
        return result(
            action, filled, "source_invalidated",
            f"source retracted or invalidated: {', '.join(sorted(retracted))}",
            limit=floor, proceeds=net_proceeds, hold=hold_value,
        )

    if not context.operational_ok:
        return result(
            action, filled, "operational_failure",
            f"operational check failed: {context.operational_note or 'unspecified'}",
            limit=floor, proceeds=net_proceeds, hold=hold_value,
        )

    if (
        context.minutes_to_close is not None
        and context.minutes_to_close <= exits.flatten_minutes_to_close
    ):
        return result(
            action, filled, "approaching_close",
            f"{context.minutes_to_close:.0f} min to close: data and execution"
            " can no longer be verified",
            limit=floor, proceeds=net_proceeds, hold=hold_value,
        )

    # --- reductions ----------------------------------------------------------
    reduce_size = max(1, math.ceil(open_contracts * exits.reduce_fraction))
    reduce_size = min(reduce_size, filled)

    # Signal reversal. The expected-value rule alone would not catch this: it
    # values holding at the optimistic end of the interval, so evidence can
    # turn against a position without the bid ever looking generous. A
    # material fall in the central estimate is its own reason to cut.
    reversal = _reversal(position, estimate, context)
    if reversal is not None:
        drop, current_point = reversal
        if drop > exits.reversal_drop:
            full = current_point < net_proceeds
            return result(
                action, filled if full else reduce_size, "signal_reversal",
                f"central estimate fell {drop:.3f} since entry"
                f" ({current_point:.3f} now)"
                + ("; below the exit price, closing out" if full else "; reducing"),
                limit=floor, proceeds=net_proceeds, hold=hold_value,
            )

    if (
        exits.allow_stale_exit
        and context.stalest_signal_hours is not None
        and context.stalest_signal_hours > exits.max_signal_age_hours
    ):
        return result(
            action, reduce_size, "stale_evidence",
            f"evidence {context.stalest_signal_hours:.0f}h old >"
            f" {exits.max_signal_age_hours:.0f}h: reducing rather than holding blind",
            limit=floor, proceeds=net_proceeds, hold=hold_value,
        )

    if context.group_over_cap:
        return result(
            action, reduce_size, "risk_limit",
            "episode exposure above its cap: reducing correlated risk",
            limit=floor, proceeds=net_proceeds, hold=hold_value,
        )

    if context.risk_off and net_proceeds >= hold_value - exits.min_exit_edge:
        return result(
            action, reduce_size, "drawdown_circuit_breaker",
            f"risk-off and the bid ({net_proceeds:.3f}) is near fair value"
            f" ({hold_value:.3f})",
            limit=floor, proceeds=net_proceeds, hold=hold_value,
        )

    # --- the ordinary, expected-value reason ---------------------------------
    edge = net_proceeds - hold_value
    if edge > exits.min_exit_edge:
        return result(
            action, filled, "overpriced",
            f"net proceeds {net_proceeds:.3f} exceed fair value {hold_value:.3f}"
            f" by {edge:+.3f} > {exits.min_exit_edge:.3f}",
            limit=hold_value, proceeds=net_proceeds, hold=hold_value,
        )

    if (
        exits.rotate_for_better_opportunity
        and context.capital_constrained
        and context.best_alternative_edge is not None
        and context.best_alternative_edge - max(edge, 0.0) > exits.rotation_edge_advantage
    ):
        return result(
            action, reduce_size, "better_opportunity",
            f"capital constrained and an independent opportunity offers"
            f" {context.best_alternative_edge:+.3f} against this position's {edge:+.3f}",
            limit=hold_value, proceeds=net_proceeds, hold=hold_value,
        )

    return result(
        Action.PASS, 0, "hold",
        f"net proceeds {net_proceeds:.3f} do not beat fair value {hold_value:.3f}"
        f" by {exits.min_exit_edge:.3f} (edge {edge:+.3f})",
        proceeds=net_proceeds, hold=hold_value,
    )
