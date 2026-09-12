"""Kalshi public market data (read-only, no credentials).

Endpoints used (documented at https://trading-api.readme.io/):

* ``GET /trade-api/v2/markets``            — open markets, paginated by cursor
* ``GET /trade-api/v2/markets/{ticker}/orderbook`` — resting depth

Prices come back in cents; everything downstream works in probabilities.
Parsing is deliberately forgiving: one malformed market must not stop a cycle.
"""

from __future__ import annotations

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

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _resolution_source(raw: dict[str, Any]) -> str | None:
    """Whoever the venue says settles the market, however it spells the field."""
    sources = raw.get("settlement_sources")
    if isinstance(sources, list) and sources:
        first = sources[0]
        if isinstance(first, dict):
            return first.get("name") or first.get("url")
        return str(first)
    return raw.get("settlement_source") or raw.get("source") or None


class KalshiPublicData(MarketDataSource):
    name = "kalshi"

    def __init__(self, base_url: str = BASE_URL, timeout: float = 15.0) -> None:
        self.http = HttpClient(base_url, timeout=timeout)

    # ------------------------------------------------------------ discovery

    def discover(self, shows: Sequence[str] = (), limit: int = 250) -> list[Market]:
        markets: list[Market] = []
        cursor: str | None = None
        while len(markets) < limit:
            params: dict[str, Any] = {"status": "open", "limit": min(200, limit)}
            if cursor:
                params["cursor"] = cursor
            payload = self.http.get("/markets", params)
            batch = payload.get("markets") or []
            for raw in batch:
                market = self.parse_market(raw)
                if market is None:
                    continue
                if not self.matches_shows(market, shows):
                    continue
                markets.append(market)
            cursor = payload.get("cursor")
            if not cursor or not batch:
                break
        return markets[:limit]

    @staticmethod
    def parse_market(raw: dict[str, Any]) -> Market | None:
        ticker = raw.get("ticker")
        if not ticker:
            return None
        title = raw.get("title") or raw.get("subtitle") or ""
        subtitle = raw.get("yes_sub_title") or raw.get("subtitle") or ""
        category = raw.get("category") or ""
        if not looks_like_elimination(title, subtitle, raw.get("event_ticker") or ""):
            return None
        return Market(
            venue="kalshi",
            market_id=str(ticker),
            group_id=str(raw.get("event_ticker") or ticker),
            title=title,
            subject=subtitle or title,
            show=str(raw.get("series_ticker") or category or "").strip(),
            close_time=parse_time(raw.get("close_time") or raw.get("expiration_time")),
            tick_size=(raw.get("tick_size") or 1) / 100,
            volume=int(raw.get("volume") or 0),
            open_interest=int(raw.get("open_interest") or 0),
            url=f"https://kalshi.com/markets/{ticker}",
            metadata={
                "status": raw.get("status"),
                "category": category,
                "event_ticker": raw.get("event_ticker"),
                "subtitle": subtitle,
                # Contract terms, read by elimination_bot.contract before the
                # market is priced at all.
                "rules": " ".join(
                    str(raw.get(key) or "")
                    for key in ("rules_primary", "rules_secondary")
                ).strip(),
                "resolution_source": _resolution_source(raw),
            },
        )

    # --------------------------------------------------------------- quotes

    def fetch_quotes(self, markets: Iterable[Market]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        for market in markets:
            try:
                payload = self.http.get(f"/markets/{market.market_id}/orderbook")
            except VenueError:
                continue
            book = self.parse_book(payload.get("orderbook") or payload)
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
        """Kalshi publishes resting YES bids and NO bids, both in cents.

        A resting NO bid at price ``n`` is an offer to sell YES at ``100 - n``,
        so the ask side of the YES book is the mirrored NO book.
        """
        def levels(entries: Any) -> list[tuple[float, int]]:
            out: list[tuple[float, int]] = []
            for entry in entries or []:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                price = to_prob(entry[0], scale=0.01)
                try:
                    size = int(entry[1])
                except (TypeError, ValueError):
                    continue
                if price is None or size <= 0:
                    continue
                out.append((price, size))
            return out

        bids = [BookLevel(p, s) for p, s in levels(raw.get("yes"))]
        asks = [
            BookLevel(round(1 - p, 10), s) for p, s in levels(raw.get("no"))
        ]
        bids.sort(key=lambda l: -l.price)
        asks.sort(key=lambda l: l.price)
        return OrderBook(bids=tuple(bids), asks=tuple(asks))
