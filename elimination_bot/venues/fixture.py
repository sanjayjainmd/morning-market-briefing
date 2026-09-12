"""A venue backed by a JSON file.

Used for tests, for replaying a recorded season, and for running the engine
end to end without touching the network.

File format::

    {
      "venue": "fixture",
      "markets": [
        {"market_id": "A", "group_id": "ep1", "show": "Example Show",
         "subject": "Contestant A", "title": "Contestant A eliminated in ep 1",
         "close_time": "2026-09-13T01:00:00Z",   # or "close_in_minutes": 600
         "book": {"bids": [[0.30, 100]], "asks": [[0.34, 80]]}}
      ]
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from datetime import timedelta

from ..models import BookLevel, Market, OrderBook, Quote, utcnow
from .base import MarketDataSource, parse_time


class FixtureVenue(MarketDataSource):
    name = "fixture"

    def __init__(self, path: str | Path | None = None, payload: dict[str, Any] | None = None) -> None:
        if payload is None:
            if path is None:
                raise ValueError("FixtureVenue needs a path or a payload")
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.payload = payload
        self.name = payload.get("venue", "fixture")

    def discover(self, shows: Sequence[str] = (), limit: int = 250) -> list[Market]:
        markets = []
        for raw in self.payload.get("markets", [])[:limit]:
            market = Market(
                venue=self.name,
                market_id=str(raw["market_id"]),
                group_id=str(raw.get("group_id", raw["market_id"])),
                title=raw.get("title", ""),
                subject=raw.get("subject", ""),
                show=raw.get("show", ""),
                close_time=self._close_time(raw),
                volume=int(raw.get("volume", 0)),
                open_interest=int(raw.get("open_interest", 0)),
                url=raw.get("url"),
                metadata=raw.get("metadata", {}),
            )
            if self.matches_shows(market, shows):
                markets.append(market)
        return markets

    @staticmethod
    def _close_time(raw: dict[str, Any]):
        """Absolute ``close_time``, or ``close_in_minutes`` relative to now.

        Relative closes keep recorded fixtures from expiring out from under
        the tests that use them.
        """
        if raw.get("close_in_minutes") is not None:
            return utcnow() + timedelta(minutes=float(raw["close_in_minutes"]))
        return parse_time(raw.get("close_time"))

    def fetch_quotes(self, markets: Iterable[Market]) -> dict[str, Quote]:
        books = {
            str(raw["market_id"]): raw.get("book", {})
            for raw in self.payload.get("markets", [])
        }
        quotes: dict[str, Quote] = {}
        for market in markets:
            raw = books.get(market.market_id)
            if not raw:
                continue
            quotes[market.key] = Quote(
                market_key=market.key,
                observed_at=utcnow(),
                book=OrderBook(
                    bids=tuple(BookLevel(float(p), int(s)) for p, s in raw.get("bids", [])),
                    asks=tuple(BookLevel(float(p), int(s)) for p, s in raw.get("asks", [])),
                ),
                last_price=raw.get("last_price"),
                volume_24h=market.volume,
            )
        return quotes
