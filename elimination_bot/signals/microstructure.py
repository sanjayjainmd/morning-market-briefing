"""Signals derived from public market data itself.

Two effects worth measuring, both computed from quotes anyone can pull:

* **Cross-venue divergence** — the same contestant priced differently on
  Kalshi and Polymarket. The cheaper venue is the tradeable one, and the gap
  is information about where the consensus is heading.
* **Drift** — where a price has moved over the recent quote history. Money
  moving one way ahead of an episode is public order flow, not a leak.

These are weak signals by construction. They are here so the model has a
market-derived component that never depends on anyone's spoiler blog.
"""

from __future__ import annotations

import math
from typing import Sequence

from ..models import Access, Market, Quote, Signal, utcnow
from .base import SignalSource


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _normalize(text: str | None) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


class MicrostructureSource(SignalSource):
    source_id = "microstructure"

    def __init__(self, max_lean: float = 0.6, min_divergence: float = 0.03) -> None:
        self.max_lean = max_lean
        self.min_divergence = min_divergence

    def collect(
        self, markets: Sequence[Market], quotes: dict[str, Quote]
    ) -> list[Signal]:
        signals: list[Signal] = []
        signals.extend(self._cross_venue(markets, quotes))
        return signals

    def _cross_venue(
        self, markets: Sequence[Market], quotes: dict[str, Quote]
    ) -> list[Signal]:
        groups: dict[tuple[str, str], list[Market]] = {}
        for market in markets:
            groups.setdefault((_normalize(market.show), _normalize(market.subject)), []).append(market)

        out: list[Signal] = []
        now = utcnow()
        for (_show, _subject), legs in groups.items():
            priced = [
                (m, quotes[m.key].market_prob)
                for m in legs
                if m.key in quotes and quotes[m.key].market_prob is not None
            ]
            if len({m.venue for m, _ in priced}) < 2:
                continue
            consensus = sum(p for _, p in priced) / len(priced)
            for market, prob in priced:
                gap = consensus - prob
                if abs(gap) < self.min_divergence:
                    continue
                lean = _logit(consensus) - _logit(prob)
                lean = max(-self.max_lean, min(self.max_lean, lean))
                other = ", ".join(
                    f"{m.venue}={p:.2f}" for m, p in priced if m.key != market.key
                )
                out.append(
                    Signal(
                        source_id=f"{self.source_id}.cross_venue",
                        market_key=market.key,
                        subject=market.subject,
                        observed_at=now,
                        lean=lean,
                        confidence=min(1.0, abs(gap) / 0.10),
                        access=Access.PUBLIC,
                        url=market.url or f"https://{market.venue}.example/{market.market_id}",
                        note=f"cross-venue divergence vs {other}",
                        metadata={"consensus": consensus, "gap": gap},
                    )
                )
        return out

    def drift(
        self, market: Market, history: Sequence[tuple[float, float]]
    ) -> Signal | None:
        """Signal from a market's own recent price path.

        ``history`` is ``[(epoch_seconds, probability), ...]`` oldest first,
        as stored in the quotes table.
        """
        if len(history) < 3:
            return None
        first_prob = history[0][1]
        last_prob = history[-1][1]
        move = _logit(last_prob) - _logit(first_prob)
        if abs(move) < 0.15:
            return None
        lean = max(-self.max_lean, min(self.max_lean, move * 0.5))
        return Signal(
            source_id=f"{self.source_id}.drift",
            market_key=market.key,
            subject=market.subject,
            observed_at=utcnow(),
            lean=lean,
            confidence=min(1.0, abs(move)),
            access=Access.PUBLIC,
            url=market.url or f"https://{market.venue}.example/{market.market_id}",
            note=f"price moved {first_prob:.2f} -> {last_prob:.2f}",
            metadata={"observations": len(history)},
        )
