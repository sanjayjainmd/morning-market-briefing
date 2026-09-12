"""Fees, slippage and the net edge that survives them.

The only number that matters is what remains after you have crossed the
spread, walked the book, paid the exchange, and subtracted a haircut for the
fact that the model is an estimate, not the truth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import FeeConfig
from .models import Action, BookLevel, EdgeBreakdown, OrderBook, Side


def _ceil_cent(value: float) -> float:
    return math.ceil(round(value * 100, 9)) / 100


@dataclass(frozen=True)
class FeeModel:
    """Exchange fees, quoted per contract at a given price."""

    rate: float = 0.07
    fixed_per_contract: float = 0.0

    def total(self, contracts: int, price: float) -> float:
        """Kalshi-style: ceil(rate * C * P * (1-P)) dollars, rounded up a cent."""
        if contracts <= 0:
            return 0.0
        price = min(max(price, 0.0), 1.0)
        variable = _ceil_cent(self.rate * contracts * price * (1 - price))
        return variable + self.fixed_per_contract * contracts

    def per_contract(self, contracts: int, price: float) -> float:
        if contracts <= 0:
            return 0.0
        return self.total(contracts, price) / contracts


ZERO_FEES = FeeModel(rate=0.0)


def fee_model_for(venue: str, fees: FeeConfig) -> FeeModel:
    if venue == "kalshi":
        return FeeModel(rate=fees.kalshi_fee_rate)
    if venue == "polymarket":
        return FeeModel(rate=0.0, fixed_per_contract=fees.polymarket_taker_fee)
    return FeeModel(rate=fees.default_fee_rate)


def side_levels(book: OrderBook, action: Action) -> tuple[BookLevel, ...]:
    """Levels you must lift, priced in the currency of the side you are buying.

    Buying NO is selling YES into the bid: you pay ``1 - bid`` per contract,
    and the deepest bid is the cheapest NO.
    """
    if action is Action.BUY_YES:
        return tuple(sorted(book.asks, key=lambda l: l.price))
    if action is Action.BUY_NO:
        return tuple(
            BookLevel(price=round(1 - l.price, 10), size=l.size)
            for l in sorted(book.bids, key=lambda l: -l.price)
        )
    return ()


def walk_book(
    book: OrderBook, action: Action, contracts: int, limit_price: float | None = None
) -> tuple[int, float]:
    """Fill up to ``contracts`` against the book. Returns (filled, vwap)."""
    levels = side_levels(book, action)
    remaining = contracts
    notional = 0.0
    filled = 0
    for level in levels:
        if remaining <= 0:
            break
        if limit_price is not None and level.price > limit_price + 1e-12:
            break
        take = min(remaining, level.size)
        notional += take * level.price
        filled += take
        remaining -= take
    if filled == 0:
        return 0, 0.0
    return filled, notional / filled


def available_depth(book: OrderBook, action: Action, limit_price: float) -> int:
    return sum(
        level.size
        for level in side_levels(book, action)
        if level.price <= limit_price + 1e-12
    )


def model_prob_for_side(prob_eliminated: float, action: Action) -> float:
    return prob_eliminated if action is Action.BUY_YES else 1 - prob_eliminated


def build_edge(
    *,
    book: OrderBook,
    action: Action,
    prob_eliminated: float,
    contracts: int,
    fee_model: FeeModel,
    safety_margin: float,
    limit_price: float | None = None,
) -> EdgeBreakdown | None:
    """Price a candidate trade of ``contracts`` on ``action``. None if unfillable."""
    if action is Action.PASS or contracts <= 0:
        return None
    levels = side_levels(book, action)
    if not levels:
        return None
    filled, vwap = walk_book(book, action, contracts, limit_price)
    if filled == 0:
        return None
    return EdgeBreakdown(
        action=action,
        model_prob=model_prob_for_side(prob_eliminated, action),
        top_of_book=levels[0].price,
        vwap=vwap,
        slippage=vwap - levels[0].price,
        fee_per_contract=fee_model.per_contract(filled, vwap),
        safety_margin=safety_margin,
        contracts=filled,
    )


def best_action(book: OrderBook, prob_eliminated: float) -> Action:
    """Which side the model disagrees with the market on, if either."""
    ask, bid = book.best_ask, book.best_bid
    yes_gap = (prob_eliminated - ask) if ask is not None else -math.inf
    no_gap = ((1 - prob_eliminated) - (1 - bid)) if bid is not None else -math.inf
    if yes_gap <= 0 and no_gap <= 0:
        return Action.PASS
    return Action.BUY_YES if yes_gap >= no_gap else Action.BUY_NO
