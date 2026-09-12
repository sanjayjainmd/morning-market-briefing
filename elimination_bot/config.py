"""Configuration. JSON on disk, dataclasses in memory, conservative defaults.

Every default here is deliberately restrictive: the system is meant to start
in shadow mode and stay there until the evidence in ``evaluate.py`` says
otherwise.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class RiskPolicy:
    """Hard gates applied to every candidate trade."""

    min_net_edge: float = 0.10             # 10 percentage points after all costs
    safety_margin: float = 0.0             # extra reserve on top of the uncertainty interval
    max_spread: float = 0.06               # skip illiquid, wide two-sided markets
    min_top_of_book_size: int = 25         # contracts at the touch
    min_depth_contracts: int = 100         # contracts within the limit price
    max_book_participation: float = 0.25   # never take more than this share of depth
    kelly_fraction: float = 0.25           # quarter Kelly or less
    max_fraction_per_market: float = 0.02  # 2% of bankroll on one contestant
    max_fraction_per_group: float = 0.05   # 5% of bankroll on one episode
    max_fraction_per_cycle: float = 0.05   # 5% of bankroll deployed per 25-min cycle
    max_gross_exposure: float = 0.30       # 30% of bankroll at risk in total
    min_contracts: int = 5                 # below this the ticket isn't worth it
    max_contracts: int = 2500
    max_signal_age_hours: float = 96.0     # evidence older than this cannot open a position
    min_minutes_to_close: float = 20.0     # no lottery tickets at the bell
    max_days_to_close: float = 21.0        # no capital parked for a month
    max_drawdown: float = 0.20             # stop trading after -20% from peak
    max_model_prob: float = 0.97           # refuse to claim near-certainty
    min_model_prob: float = 0.03

    def validate(self) -> None:
        if not 0 < self.kelly_fraction <= 1:
            raise ValueError("kelly_fraction must be in (0, 1]")
        if self.min_net_edge <= 0:
            raise ValueError("min_net_edge must be positive")
        if self.max_fraction_per_market > self.max_fraction_per_group:
            raise ValueError("per-market cap cannot exceed per-group cap")


@dataclass
class ModelConfig:
    """How signals are folded into a probability."""

    prior_weight: float = 3.0        # market price counts as this many unit signals
    max_logit_shift: float = 2.0     # cap on how far signals may move the prior
    min_source_observations: int = 10  # below this a source is shrunk hard
    shrinkage_k: float = 20.0        # reliability shrinkage constant
    normalize_field: bool = True     # exactly-one-elimination constraint per group
    eliminations_per_group: float = 1.0
    recency_half_life_hours: float = 72.0  # a claim's weight halves this often


@dataclass
class CorrelationConfig:
    """Five sites repeating one rumour are one signal, not five."""

    #: explicit source_id -> cluster id, for known syndication relationships
    clusters: dict[str, str] = field(default_factory=dict)
    #: fall back to grouping by the URL's registrable domain
    cluster_by_domain: bool = True
    #: fall back to grouping near-identical claims made within the window
    cluster_by_claim: bool = True
    claim_window_hours: float = 36.0
    claim_lean_tolerance: float = 0.25
    #: each additional member of a cluster contributes only this fraction
    extra_member_weight: float = 0.25


@dataclass
class UncertaintyConfig:
    """How wide the probability interval is, and therefore how rarely we trade."""

    base_half_width: float = 0.08      # one proven, independent source
    min_half_width: float = 0.03
    max_half_width: float = 0.30
    independence_exponent: float = 0.5  # width shrinks as 1/sqrt(effective sources)
    dispersion_weight: float = 0.6      # disagreement between clusters widens it
    staleness_penalty_per_day: float = 0.01
    unproven_source_penalty: float = 0.04
    prior_only_half_width: float = 0.05  # market price alone: narrow but not zero


@dataclass
class VerificationConfig:
    """A contract is not analysed until its rules are unambiguous."""

    require_rules: bool = True
    require_resolution_source: bool = True
    require_close_time: bool = True
    require_open_status: bool = True
    block_if_already_aired: bool = True
    require_ambiguity_resolution: bool = True
    #: edge cases that must be addressed in the rules text if the title raises them
    ambiguity_terms: list[str] = field(
        default_factory=lambda: [
            "withdraw", "withdrawal", "quit", "quits", "disqualif", "medical",
            "evacuat", "forfeit", "double elimination", "no elimination",
            "non-elimination", "tie", "postpone",
        ]
    )
    #: words whose presence in the rules shows the edge case was addressed
    resolution_terms: list[str] = field(
        default_factory=lambda: ["count", "counts", "deemed", "treated", "considered",
                                 "shall", "will resolve", "resolves", "resolution"]
    )
    min_rules_chars: int = 40


@dataclass
class ExitConfig:
    """Selling is an expected-value decision, not a profit or loss trigger."""

    min_exit_edge: float = 0.04          # M_exit: how far the bid must beat fair value
    exit_cost_buffer: float = 0.01       # slippage paid on the way out
    reduce_fraction: float = 0.5         # size of a partial reduction
    max_signal_age_hours: float = 96.0   # stale evidence -> reduce, do not hold blind
    flatten_minutes_to_close: float = 30.0
    emergency_loss_fraction: float = 0.60  # hard stop: fraction of cost basis lost
    allow_stale_exit: bool = True
    reversal_drop: float = 0.10          # point estimate falling this far is a reversal
    max_forced_exit_slippage: float = 0.05  # never dump below this much under the touch
    rotate_for_better_opportunity: bool = False  # off by default: churn costs money
    rotation_edge_advantage: float = 0.10


@dataclass
class FeeConfig:
    """Fee rates are an input to be verified, not a constant to be trusted.

    ``verified_at`` records when a human last checked these against the
    venue's published schedule. Once the check goes stale the engine warns in
    shadow mode and refuses to trade live: a fee schedule that changed under a
    hard-coded rate silently turns a positive edge negative.
    """

    kalshi_fee_rate: float = 0.07    # ceil(rate * C * P * (1-P)) dollars
    polymarket_taker_fee: float = 0.0
    default_fee_rate: float = 0.07
    verified_at: str | None = None   # ISO date the schedule was last checked
    max_schedule_age_days: float = 90.0
    schedule_url: str = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
    require_verified_schedule_for_live: bool = True


@dataclass
class ExecutionConfig:
    mode: str = "shadow"             # "shadow" only; "live" is guarded and unimplemented
    venues: list[str] = field(default_factory=lambda: ["kalshi", "polymarket"])
    cycle_minutes: int = 25
    request_timeout: float = 15.0
    max_markets_per_cycle: int = 250
    adverse_selection_haircut: float = 0.005  # shadow fills pay this much worse

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


@dataclass
class FundingConfig:
    """Hosting money is kept apart from trading margin, and running out pauses."""

    monthly_cost: float = 40.0
    reserve_months_target: float = 12.0
    reserve_months_warn: float = 6.0
    sweep_from_realized_only: bool = True
    max_sweep_fraction_of_realized: float = 0.5


@dataclass
class BotConfig:
    bankroll: float = 1000.0
    db_path: str = "data/elimination_bot.sqlite3"
    research_path: str = "data/public_research.json"
    kill_switch_path: str = "data/KILL_SWITCH"
    dormant_path: str = "data/DORMANT"
    shows: list[str] = field(default_factory=list)   # empty = any elimination market
    risk: RiskPolicy = field(default_factory=RiskPolicy)
    model: ModelConfig = field(default_factory=ModelConfig)
    correlation: CorrelationConfig = field(default_factory=CorrelationConfig)
    uncertainty: UncertaintyConfig = field(default_factory=UncertaintyConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    funding: FundingConfig = field(default_factory=FundingConfig)

    def validate(self) -> None:
        if self.bankroll <= 0:
            raise ValueError("bankroll must be positive")
        self.risk.validate()
        if self.execution.mode not in ("shadow", "live"):
            raise ValueError("execution.mode must be 'shadow' or 'live'")
        if self.uncertainty.min_half_width > self.uncertainty.max_half_width:
            raise ValueError("uncertainty.min_half_width exceeds max_half_width")
        if self.exits.min_exit_edge <= 0:
            raise ValueError("exits.min_exit_edge must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotConfig":
        sections = {
            "risk": RiskPolicy,
            "model": ModelConfig,
            "correlation": CorrelationConfig,
            "uncertainty": UncertaintyConfig,
            "verification": VerificationConfig,
            "exits": ExitConfig,
            "fees": FeeConfig,
            "execution": ExecutionConfig,
            "funding": FundingConfig,
        }
        kwargs: dict[str, Any] = {}
        known = set(cls.__dataclass_fields__)
        unknown_top = {k for k in data if not k.startswith("_") and k not in known}
        if unknown_top:
            raise ValueError(f"unknown config option(s): {sorted(unknown_top)}")
        for key, value in data.items():
            if key.startswith("_"):
                continue
            if key in sections:
                section_cls = sections[key]
                allowed = set(section_cls.__dataclass_fields__)
                unknown = set(value) - allowed
                if unknown:
                    raise ValueError(f"unknown {key} option(s): {sorted(unknown)}")
                kwargs[key] = section_cls(**value)
            else:
                kwargs[key] = value
        cfg = cls(**kwargs)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None) -> "BotConfig":
        if path is None:
            cfg = cls()
            cfg.validate()
            return cfg
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    def save(self, path: str | os.PathLike[str]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
