"""Venue adapters: read-only public market data."""

from .base import MarketDataSource, VenueError
from .kalshi import KalshiPublicData
from .polymarket import PolymarketPublicData
from .fixture import FixtureVenue


def build_sources(venues, timeout: float = 15.0) -> list[MarketDataSource]:
    """Instantiate the configured venue adapters by name."""
    registry = {
        "kalshi": lambda: KalshiPublicData(timeout=timeout),
        "polymarket": lambda: PolymarketPublicData(timeout=timeout),
    }
    sources = []
    for name in venues:
        factory = registry.get(name)
        if factory is None:
            raise VenueError(f"unknown venue: {name}")
        sources.append(factory())
    return sources


__all__ = [
    "MarketDataSource",
    "VenueError",
    "KalshiPublicData",
    "PolymarketPublicData",
    "FixtureVenue",
    "build_sources",
]
