"""Position sizing: fractional Kelly, then every cap that can shrink it."""

from __future__ import annotations

from dataclasses import dataclass

from .config import RiskPolicy
from .models import Action, OrderBook

from .edge import available_depth


@dataclass
class SizingResult:
    contracts: int
    binding_constraint: str
    kelly_contracts: int
    notes: list[str]


def kelly_fraction(prob: float, cost: float) -> float:
    """Full-Kelly stake fraction for a $1-payout contract bought at ``cost``.

    f* = (p - c) / (1 - c). Zero when the model has no edge at that price.
    """
    if cost <= 0 or cost >= 1:
        return 0.0
    f = (prob - cost) / (1 - cost)
    return max(0.0, min(1.0, f))


def size_position(
    *,
    prob: float,
    all_in_cost: float,
    bankroll: float,
    policy: RiskPolicy,
    book: OrderBook,
    action: Action,
    limit_price: float,
    group_deployed: float = 0.0,
    cycle_deployed: float = 0.0,
    gross_exposure: float = 0.0,
) -> SizingResult:
    """Contracts to buy, and the name of whichever constraint bound hardest."""
    notes: list[str] = []
    if bankroll <= 0 or all_in_cost <= 0 or all_in_cost >= 1:
        return SizingResult(0, "unpriceable", 0, ["cost outside (0,1) or no bankroll"])

    f_full = kelly_fraction(prob, all_in_cost)
    f = f_full * policy.kelly_fraction
    kelly_dollars = f * bankroll
    kelly_contracts = int(kelly_dollars // all_in_cost)

    budgets: dict[str, float] = {
        "kelly": kelly_dollars,
        "per_market_cap": policy.max_fraction_per_market * bankroll,
        "per_group_cap": max(0.0, policy.max_fraction_per_group * bankroll - group_deployed),
        "per_cycle_cap": max(0.0, policy.max_fraction_per_cycle * bankroll - cycle_deployed),
        "gross_exposure_cap": max(0.0, policy.max_gross_exposure * bankroll - gross_exposure),
    }
    binding, budget = min(budgets.items(), key=lambda kv: kv[1])
    contracts = int(budget // all_in_cost)

    depth = available_depth(book, action, limit_price)
    liquidity_cap = int(depth * policy.max_book_participation)
    if liquidity_cap < contracts:
        contracts, binding = liquidity_cap, "liquidity"
        notes.append(f"depth {depth} at limit {limit_price:.2f}")

    if contracts > policy.max_contracts:
        contracts, binding = policy.max_contracts, "max_contracts"

    if contracts < policy.min_contracts:
        notes.append(f"{contracts} < min_contracts {policy.min_contracts}")
        return SizingResult(0, "min_contracts", kelly_contracts, notes)

    return SizingResult(contracts, binding, kelly_contracts, notes)
