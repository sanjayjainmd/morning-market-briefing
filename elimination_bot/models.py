"""Core value types. Everything the engine records is one of these."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses/datetimes/enums into JSON-safe values."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return _iso(obj)


def dumps(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), sort_keys=True)


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class Action(str, Enum):
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"
    PASS = "pass"


class Access(str, Enum):
    """How a piece of information was obtained. Only PUBLIC is tradeable."""

    PUBLIC = "public"          # freely available to any member of the public
    LICENSED = "licensed"      # paid feed we are licensed to use
    RESTRICTED = "restricted"  # paywalled without permission, confidential, NDA
    NONPUBLIC = "nonpublic"    # insider / leaked / hacked — never tradeable


TRADEABLE_ACCESS = (Access.PUBLIC, Access.LICENSED)


@dataclass(frozen=True)
class BookLevel:
    price: float   # probability in [0, 1]
    size: int      # contracts available at this price


@dataclass(frozen=True)
class OrderBook:
    """Resting liquidity for the YES contract, expressed in probability terms.

    ``bids`` are descending, ``asks`` ascending. A NO purchase is modelled as
    selling YES into the bid, i.e. paying ``1 - bid``.
    """

    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def depth(self, side: Side) -> int:
        levels = self.asks if side is Side.YES else self.bids
        return sum(level.size for level in levels)


@dataclass
class Market:
    """One tradeable elimination contract: 'contestant X is eliminated'."""

    venue: str
    market_id: str
    group_id: str            # the episode/event all mutually exclusive legs share
    title: str
    subject: str             # contestant name
    show: str
    close_time: datetime | None = None
    tick_size: float = 0.01
    volume: int = 0
    open_interest: int = 0
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.market_id}"


@dataclass
class Quote:
    market_key: str
    observed_at: datetime
    book: OrderBook
    last_price: float | None = None
    volume_24h: int = 0

    @property
    def market_prob(self) -> float | None:
        """Market-implied probability, the mid when two-sided."""
        return self.book.mid if self.book.mid is not None else self.last_price


@dataclass
class Signal:
    """One observation about one contestant, from one identified source."""

    source_id: str
    market_key: str
    subject: str
    observed_at: datetime
    lean: float                 # log-odds nudge; >0 means "more likely eliminated"
    confidence: float           # [0, 1] how strongly the source commits
    access: Access
    url: str | None = None
    note: str = ""
    published_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tradeable(self) -> bool:
        return self.access in TRADEABLE_ACCESS and bool(self.url)


@dataclass
class Estimate:
    market_key: str
    subject: str
    prob: float
    prior_prob: float                      # the market prior we shrank toward
    components: list[dict[str, Any]] = field(default_factory=list)
    normalized: bool = False

    @property
    def logit(self) -> float:
        p = min(max(self.prob, 1e-6), 1 - 1e-6)
        return math.log(p / (1 - p))


@dataclass
class EdgeBreakdown:
    """Everything standing between a model probability and a profitable fill."""

    action: Action
    model_prob: float           # model probability of the side being bought
    top_of_book: float          # best price for that side
    vwap: float                 # average fill price for the intended size
    slippage: float             # vwap - top_of_book
    fee_per_contract: float
    safety_margin: float
    contracts: int

    @property
    def all_in_cost(self) -> float:
        return self.vwap + self.fee_per_contract

    @property
    def gross_edge(self) -> float:
        return self.model_prob - self.top_of_book

    @property
    def net_edge(self) -> float:
        return self.model_prob - self.all_in_cost - self.safety_margin

    @property
    def expected_value(self) -> float:
        return self.net_edge * self.contracts


@dataclass
class Decision:
    cycle_id: str
    market_key: str
    subject: str
    action: Action
    contracts: int
    limit_price: float | None
    reasons: list[str]
    edge: EdgeBreakdown | None
    estimate: Estimate | None
    decided_at: datetime = field(default_factory=utcnow)

    @property
    def is_trade(self) -> bool:
        return self.action is not Action.PASS and self.contracts > 0


@dataclass
class Order:
    order_id: str
    cycle_id: str
    market_key: str
    action: Action
    contracts: int
    limit_price: float
    mode: str                   # "shadow" | "live"
    placed_at: datetime = field(default_factory=utcnow)
    status: str = "open"


@dataclass
class Fill:
    order_id: str
    market_key: str
    contracts: int
    price: float                # average fill price for the side bought
    fees: float
    filled_at: datetime = field(default_factory=utcnow)

    @property
    def cost(self) -> float:
        return self.contracts * self.price + self.fees


@dataclass
class Outcome:
    market_key: str
    subject: str
    eliminated: bool
    settled_at: datetime = field(default_factory=utcnow)
    note: str = ""

    def payout(self, action: Action, contracts: int) -> float:
        """Settlement value of a position, in dollars ($1 per winning contract)."""
        if action is Action.BUY_YES:
            return float(contracts) if self.eliminated else 0.0
        if action is Action.BUY_NO:
            return 0.0 if self.eliminated else float(contracts)
        return 0.0
