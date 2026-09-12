"""Human-curated public research, read from a JSON file.

Fan polls, public spoiler posts, press interviews, preview clips and edit
analysis do not arrive through a clean API, and scraping them indiscriminately
is how a system ends up ingesting something it should not. This source takes
structured, hand-entered observations — each with a source id, a public URL, a
timestamp and an explicit lean — so the provenance of every input is legible.

File format (``data/public_research.json``)::

    {
      "observations": [
        {
          "source_id": "example-fan-poll",
          "show": "Example Show",
          "subject": "Contestant A",
          "market_key": "kalshi:ELIM-A",   # optional; subject match is enough
          "observed_at": "2026-09-12T14:00:00Z",
          "published_at": "2026-09-12T13:30:00Z",
          "access": "public",
          "url": "https://example.com/poll",
          "lean": 0.8,                      # log-odds nudge toward elimination
          "prob": 0.55,                     # alternative to lean: stated probability
          "confidence": 0.6,
          "note": "fan poll, n=4,120"
        }
      ]
    }
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from ..models import Access, Market, Quote, Signal, utcnow
from .base import SignalSource


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize(text: str | None) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


class PublicResearchSource(SignalSource):
    source_id = "public_research"

    def __init__(self, path: str | Path, max_age_hours: float = 240.0) -> None:
        self.path = Path(path)
        self.max_age = timedelta(hours=max_age_hours)

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        observations = data.get("observations") if isinstance(data, dict) else data
        return [o for o in (observations or []) if isinstance(o, dict)]

    def collect(
        self, markets: Sequence[Market], quotes: dict[str, Quote]
    ) -> list[Signal]:
        observations = self._load()
        if not observations:
            return []
        now = utcnow()
        by_subject: dict[tuple[str, str], list[Market]] = {}
        by_key: dict[str, Market] = {}
        for market in markets:
            by_key[market.key] = market
            by_subject.setdefault((_normalize(market.show), _normalize(market.subject)), []).append(market)

        signals: list[Signal] = []
        for obs in observations:
            observed_at = _parse_time(obs.get("observed_at")) or now
            if now - observed_at > self.max_age:
                continue
            targets: list[Market] = []
            key = obs.get("market_key")
            if key and key in by_key:
                targets = [by_key[key]]
            else:
                targets = by_subject.get(
                    (_normalize(obs.get("show")), _normalize(obs.get("subject"))), []
                )
            if not targets:
                continue

            lean = self._lean(obs, targets[0], quotes)
            if lean is None:
                continue
            access = self._access(obs)
            for market in targets:
                signals.append(
                    Signal(
                        source_id=str(obs.get("source_id") or self.source_id),
                        market_key=market.key,
                        subject=market.subject,
                        observed_at=observed_at,
                        published_at=_parse_time(obs.get("published_at")),
                        lean=lean,
                        confidence=float(obs.get("confidence", 0.5)),
                        access=access,
                        url=obs.get("url"),
                        note=str(obs.get("note", "")),
                        metadata={"kind": obs.get("kind", "research")},
                    )
                )
        return signals

    @staticmethod
    def _access(obs: dict[str, Any]) -> Access:
        raw = str(obs.get("access", "public")).lower()
        try:
            return Access(raw)
        except ValueError:
            # Unknown provenance is treated as untradeable, not as public.
            return Access.RESTRICTED

    @staticmethod
    def _lean(
        obs: dict[str, Any], market: Market, quotes: dict[str, Quote]
    ) -> float | None:
        """A stated probability becomes a lean relative to the current market."""
        if obs.get("lean") is not None:
            try:
                return float(obs["lean"])
            except (TypeError, ValueError):
                return None
        if obs.get("prob") is None:
            return None
        try:
            prob = float(obs["prob"])
        except (TypeError, ValueError):
            return None
        prob = min(max(prob, 1e-4), 1 - 1e-4)
        quote = quotes.get(market.key)
        reference = quote.market_prob if quote and quote.market_prob else 0.5
        reference = min(max(reference, 1e-4), 1 - 1e-4)
        return math.log(prob / (1 - prob)) - math.log(reference / (1 - reference))
