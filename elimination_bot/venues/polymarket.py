"""Polymarket public market data (read-only, no credentials).

Endpoints used (documented at https://docs.polymarket.com/):

* ``GET https://gamma-api.polymarket.com/markets`` — market metadata
* ``GET https://clob.polymarket.com/book?token_id=`` — CLOB depth

Gamma returns prices as decimal strings in [0, 1] and several fields as
JSON-encoded strings, so parsing has to be defensive.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from ..models import BookLevel, Market, OrderBook, Quote, utcnow
from .base import (
    HttpClient,
    MarketDataSource,
    VenueError,
    looks_like_elimination,
    parse_time,
    to_prob,
)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class PolymarketPublicData(MarketDataSource):
    name = "polymarket"

    def __init__(
        self, gamma_url: str = GAMMA_URL, clob_url: str = CLOB_URL, timeout: float = 15.0
    ) -> None:
        self.gamma = HttpClient(gamma_url, timeout=timeout)
        self.clob = HttpClient(clob_url, timeout=timeout)

    # ------------------------------------------------------------ discovery

    def discover(self, shows: Sequence[str] = (), limit: int = 250) -> list[Market]:
        markets: list[Market] = []
        offset = 0
        page = min(100, limit)
        while len(markets) < limit:
            payload = self.gamma.get(
                "/markets",
                {"closed": "false", "active": "true", "limit": page, "offset": offset},
            )
            batch = payload if isinstance(payload, list) else payload.get("data") or []
            for raw in batch:
                market = self.parse_market(raw)
                if market is None or not self.matches_shows(market, shows):
                    continue
                markets.append(market)
            if len(batch) < page:
                break
            offset += page
        return markets[:limit]

    @staticmethod
    def parse_market(raw: dict[str, Any]) -> Market | None:
        market_id = raw.get("conditionId") or raw.get("id")
        if not market_id:
            return None
        question = raw.get("question") or raw.get("title") or ""
        group_item = raw.get("groupItemTitle") or ""
        event = raw.get("events") or []
        event_title = ""
        event_id = ""
        if isinstance(event, list) and event and isinstance(event[0], dict):
            event_title = event[0].get("title") or ""
            event_id = str(event[0].get("id") or "")
        if not looks_like_elimination(question, event_title, group_item):
            return None
        tokens = _maybe_json(raw.get("clobTokenIds")) or []
        yes_token = tokens[0] if isinstance(tokens, list) and tokens else None
        return Market(
            venue="polymarket",
            market_id=str(market_id),
            group_id=event_id or str(market_id),
            title=question,
            subject=group_item or question,
            show=event_title,
            close_time=parse_time(raw.get("endDate") or raw.get("end_date_iso")),
            tick_size=float(raw.get("orderPriceMinTickSize") or 0.01),
            volume=int(float(raw.get("volumeNum") or raw.get("volume") or 0)),
            open_interest=int(float(raw.get("openInterest") or 0)),
            url=f"https://polymarket.com/event/{raw.get('slug', '')}",
            metadata={"yes_token_id": yes_token, "event_title": event_title},
        )

    # --------------------------------------------------------------- quotes

    def fetch_quotes(self, markets: Iterable[Market]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        for market in markets:
            token = market.metadata.get("yes_token_id")
            if not token:
                continue
            try:
                payload = self.clob.get("/book", {"token_id": token})
            except VenueError:
                continue
            book = self.parse_book(payload)
            if book.best_bid is None and book.best_ask is None:
                continue
            quotes[market.key] = Quote(
                market_key=market.key,
                observed_at=utcnow(),
                book=book,
                volume_24h=market.volume,
            )
        return quotes

    @staticmethod
    def parse_book(raw: dict[str, Any]) -> OrderBook:
        def levels(entries: Any) -> list[BookLevel]:
            out: list[BookLevel] = []
            for entry in entries or []:
                if isinstance(entry, dict):
                    price, size = entry.get("price"), entry.get("size")
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    price, size = entry[0], entry[1]
                else:
                    continue
                prob = to_prob(price)
                try:
                    shares = int(float(size))
                except (TypeError, ValueError):
                    continue
                if prob is None or shares <= 0:
                    continue
                out.append(BookLevel(prob, shares))
            return out

        bids = sorted(levels(raw.get("bids")), key=lambda l: -l.price)
        asks = sorted(levels(raw.get("asks")), key=lambda l: l.price)
        return OrderBook(bids=tuple(bids), asks=tuple(asks))
