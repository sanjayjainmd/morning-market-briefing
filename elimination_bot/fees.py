"""Fee schedules, treated as data that expires.

A hard-coded fee rate is a silent time bomb: the venue changes its schedule,
the constant does not, and every edge calculation is quietly wrong in the
direction that loses money. So rates live in a file an operator updates,
each carrying the date it was last checked against the venue's published
schedule, and the engine reacts when that check goes stale — a warning in
shadow mode, a refusal to trade live.

Override file (``data/fee_schedule.json``)::

    {
      "verified_at": "2026-09-01",
      "source": "https://kalshi.com/docs/kalshi-fee-schedule.pdf",
      "venues": {
        "kalshi": {"rate": 0.07, "fixed_per_contract": 0.0},
        "polymarket": {"rate": 0.0, "fixed_per_contract": 0.0}
      }
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .config import FeeConfig
from .edge import FeeModel


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


@dataclass
class FeeSchedule:
    venue: str
    rate: float
    fixed_per_contract: float = 0.0
    verified_at: date | None = None
    source: str = ""

    def model(self) -> FeeModel:
        return FeeModel(rate=self.rate, fixed_per_contract=self.fixed_per_contract)

    def age_days(self, today: date | None = None) -> float | None:
        if self.verified_at is None:
            return None
        today = today or datetime.now(timezone.utc).date()
        return (today - self.verified_at).days

    def is_stale(self, max_age_days: float, today: date | None = None) -> bool:
        age = self.age_days(today)
        return age is None or age > max_age_days

    def describe(self, max_age_days: float) -> str:
        if self.verified_at is None:
            return f"{self.venue}: fee schedule never verified against {self.source or 'the venue'}"
        age = self.age_days()
        state = "stale" if self.is_stale(max_age_days) else "current"
        return (
            f"{self.venue}: rate {self.rate:.4f}, verified {self.verified_at.isoformat()}"
            f" ({age} days ago, {state})"
        )


class FeeBook:
    """Every venue's schedule, plus whether any of them need re-checking."""

    def __init__(self, config: FeeConfig, schedules: dict[str, FeeSchedule]) -> None:
        self.config = config
        self.schedules = schedules

    def schedule(self, venue: str) -> FeeSchedule:
        if venue in self.schedules:
            return self.schedules[venue]
        return FeeSchedule(
            venue=venue,
            rate=self.config.default_fee_rate,
            verified_at=_parse_date(self.config.verified_at),
            source=self.config.schedule_url,
        )

    def model(self, venue: str) -> FeeModel:
        return self.schedule(venue).model()

    def stale_venues(self, venues: list[str] | None = None) -> list[str]:
        names = venues if venues is not None else list(self.schedules)
        return [
            name
            for name in names
            if self.schedule(name).is_stale(self.config.max_schedule_age_days)
        ]

    def warning(self, venues: list[str] | None = None) -> str | None:
        stale = self.stale_venues(venues)
        if not stale:
            return None
        return (
            "fee schedule not verified within "
            f"{self.config.max_schedule_age_days:.0f} days for: {', '.join(stale)}."
            f" Re-check {self.config.schedule_url} and update the schedule file."
        )

    def blocks_live_trading(self, venues: list[str] | None = None) -> str | None:
        if not self.config.require_verified_schedule_for_live:
            return None
        return self.warning(venues)

    def describe(self) -> list[str]:
        return [
            self.schedule(v).describe(self.config.max_schedule_age_days)
            for v in sorted(set(list(self.schedules) + ["kalshi", "polymarket"]))
        ]


def load_fee_book(config: FeeConfig, path: str | Path | None = None) -> FeeBook:
    """Read the override file if present, else fall back to configured rates."""
    defaults = {
        "kalshi": FeeSchedule(
            venue="kalshi",
            rate=config.kalshi_fee_rate,
            verified_at=_parse_date(config.verified_at),
            source=config.schedule_url,
        ),
        "polymarket": FeeSchedule(
            venue="polymarket",
            rate=0.0,
            fixed_per_contract=config.polymarket_taker_fee,
            verified_at=_parse_date(config.verified_at),
            source=config.schedule_url,
        ),
    }

    target = Path(path) if path else None
    if target and target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        verified_at = _parse_date(payload.get("verified_at"))
        source = payload.get("source") or config.schedule_url
        for venue, entry in (payload.get("venues") or {}).items():
            if not isinstance(entry, dict):
                continue
            defaults[venue] = FeeSchedule(
                venue=venue,
                rate=float(entry.get("rate", config.default_fee_rate)),
                fixed_per_contract=float(entry.get("fixed_per_contract", 0.0)),
                verified_at=_parse_date(entry.get("verified_at")) or verified_at,
                source=str(entry.get("source") or source),
            )
    return FeeBook(config, defaults)
