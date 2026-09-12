"""The gauntlet every candidate trade walks before it becomes an order.

Each gate records why it passed or failed. A pass is as auditable as a trade:
when the season is over you want to know what the bot declined, not only what
it bought.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from typing import TYPE_CHECKING

from .config import BotConfig
from .edge import FeeModel, best_action, build_edge
from .models import Action, Decision, Estimate, Market, Quote, Side, utcnow
from .sizing import size_position

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for typing
    from .contract import Verification


@dataclass
class ExposureState:
    """What is already committed, so the caps can be applied cumulatively."""

    gross: float = 0.0
    per_group: dict[str, float] = None  # type: ignore[assignment]
    cycle: float = 0.0

    def __post_init__(self) -> None:
        if self.per_group is None:
            self.per_group = {}

    def add(self, group_id: str, dollars: float) -> None:
        self.gross += dollars
        self.cycle += dollars
        self.per_group[group_id] = self.per_group.get(group_id, 0.0) + dollars

    def group(self, group_id: str) -> float:
        return self.per_group.get(group_id, 0.0)


def _minutes_to_close(market: Market, now: datetime | None = None) -> float | None:
    if market.close_time is None:
        return None
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (market.close_time - now).total_seconds() / 60.0


def decide(
    *,
    cycle_id: str,
    market: Market,
    quote: Quote,
    estimate: Estimate,
    config: BotConfig,
    fee_model: FeeModel,
    exposure: ExposureState,
    bankroll: float | None = None,
    now: datetime | None = None,
    verification: "Verification | None" = None,
    held: bool = False,
) -> Decision:
    """Return a trade or an explained pass for one market.

    The entry rule, stated once:

        conservative probability > executable price + fees + slippage + minimum edge

    where the conservative probability is the pessimistic end of the estimate's
    interval for the side being bought, the executable price is the VWAP of
    actually walking the book for the intended size (never the midpoint), and
    the minimum edge is ``risk.min_net_edge``.
    """
    policy = config.risk
    bankroll = config.bankroll if bankroll is None else bankroll
    reasons: list[str] = []
    book = quote.book

    def passed(reason: str) -> Decision:
        reasons.append(reason)
        return Decision(
            cycle_id=cycle_id,
            market_key=market.key,
            subject=market.subject,
            action=Action.PASS,
            contracts=0,
            limit_price=None,
            reasons=reasons,
            edge=None,
            estimate=estimate,
        )

    # --- the contract itself -------------------------------------------------
    if verification is not None and not verification.ok:
        return passed(f"contract not verified: {verification.reason()}")

    # --- sanity gates --------------------------------------------------------
    if book.best_bid is None or book.best_ask is None:
        return passed("one-sided or empty book")
    if book.spread is not None and book.spread > policy.max_spread:
        return passed(f"spread {book.spread:.3f} > max {policy.max_spread:.3f}")
    if not (policy.min_model_prob <= estimate.prob <= policy.max_model_prob):
        return passed(
            f"model prob {estimate.prob:.3f} outside [{policy.min_model_prob}, {policy.max_model_prob}]"
        )
    if (
        estimate.stalest_signal_hours is not None
        and estimate.stalest_signal_hours > policy.max_signal_age_hours
    ):
        return passed(
            f"evidence {estimate.stalest_signal_hours:.0f}h old >"
            f" {policy.max_signal_age_hours:.0f}h"
        )

    minutes = _minutes_to_close(market, now)
    if minutes is not None:
        if minutes < policy.min_minutes_to_close:
            return passed(f"{minutes:.0f} min to close < {policy.min_minutes_to_close:.0f}")
        if minutes > policy.max_days_to_close * 24 * 60:
            return passed(f"{minutes / 1440:.1f} days to close > {policy.max_days_to_close}")

    # --- which side, judged against the pessimistic end of the interval ------
    conservative_yes = estimate.conservative(Action.BUY_YES)
    conservative_no = estimate.conservative(Action.BUY_NO)
    action = best_action(
        book, estimate.prob, yes_prob=conservative_yes, no_prob=conservative_no
    )
    if action is Action.PASS:
        band = estimate.interval
        detail = (
            f" (interval {band.low:.3f}-{band.high:.3f})" if band is not None else ""
        )
        return passed(f"no side clears its conservative bound{detail}")
    side_prob = conservative_yes if action is Action.BUY_YES else conservative_no

    if held:
        return passed(
            "already holding or just exited this market;"
            " exits are managed separately and re-entry waits for the next cycle"
        )

    touch = book.best_ask if action is Action.BUY_YES else round(1 - book.best_bid, 10)
    touch_size = (
        book.asks[0].size if action is Action.BUY_YES else book.bids[0].size
    )
    if touch_size < policy.min_top_of_book_size:
        return passed(f"top of book {touch_size} < {policy.min_top_of_book_size}")

    # --- price the trade at the touch, then size it --------------------------
    probe = build_edge(
        book=book,
        action=action,
        prob_eliminated=estimate.prob,
        side_prob=side_prob,
        contracts=max(policy.min_contracts, 1),
        fee_model=fee_model,
        safety_margin=policy.safety_margin,
    )
    if probe is None:
        return passed("no fillable liquidity on the signalled side")
    if probe.net_edge < policy.min_net_edge:
        return passed(
            f"net edge {probe.net_edge:+.3f} < min {policy.min_net_edge:.3f}"
            f" (conservative {side_prob:.3f} vs ask {probe.top_of_book:.3f},"
            f" fee {probe.fee_per_contract:.3f})"
        )

    # Walk no further than the price at which the edge disappears.
    limit_price = min(
        1 - 1e-6,
        side_prob - policy.min_net_edge - policy.safety_margin - probe.fee_per_contract,
    )
    if limit_price < touch:
        return passed("edge gone before the touch price")

    sizing = size_position(
        prob=side_prob,
        all_in_cost=probe.all_in_cost,
        bankroll=bankroll,
        policy=policy,
        book=book,
        action=action,
        limit_price=limit_price,
        group_deployed=exposure.group(market.group_id),
        cycle_deployed=exposure.cycle,
        gross_exposure=exposure.gross,
    )
    if sizing.contracts <= 0:
        return passed(f"size 0 ({sizing.binding_constraint}): {'; '.join(sizing.notes) or 'capped'}")

    side = Side.YES if action is Action.BUY_YES else Side.NO
    if book.depth(side) < policy.min_depth_contracts:
        return passed(f"{side.value} depth {book.depth(side)} < {policy.min_depth_contracts}")

    edge = build_edge(
        book=book,
        action=action,
        prob_eliminated=estimate.prob,
        side_prob=side_prob,
        contracts=sizing.contracts,
        fee_model=fee_model,
        safety_margin=policy.safety_margin,
        limit_price=limit_price,
    )
    if edge is None or edge.contracts < policy.min_contracts:
        return passed("size shrank below minimum once the book was walked")
    if edge.net_edge < policy.min_net_edge:
        return passed(
            f"net edge after slippage {edge.net_edge:+.3f} < min {policy.min_net_edge:.3f}"
        )

    reasons.append(
        f"net edge {edge.net_edge:+.3f} on {edge.contracts} contracts"
        f" (conservative {side_prob:.3f} of point {estimate.prob:.3f}"
        f" vs market {quote.market_prob:.3f}, vwap {edge.vwap:.3f},"
        f" fee {edge.fee_per_contract:.3f}, bound by {sizing.binding_constraint})"
    )
    return Decision(
        cycle_id=cycle_id,
        market_key=market.key,
        subject=market.subject,
        action=action,
        contracts=edge.contracts,
        limit_price=limit_price,
        reasons=reasons,
        edge=edge,
        estimate=estimate,
    )


def estimate_limit(
    prob_eliminated: float,
    action: Action,
    min_net_edge: float,
    safety_margin: float,
    fee_per_contract: float,
) -> float:
    """Highest price that still clears the edge bar, for the side being bought."""
    side_prob = prob_eliminated if action is Action.BUY_YES else 1 - prob_eliminated
    return side_prob - min_net_edge - safety_margin - fee_per_contract
