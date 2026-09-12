"""Shared venue plumbing: HTTP with retries, and the adapter interface."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

try:  # requests is in requirements.txt; keep import failure legible
    import requests
except ImportError:  # pragma: no cover - exercised only without the dep
    requests = None  # type: ignore[assignment]

from ..models import Market, Quote

#: Words that mark a market as "who leaves next" rather than "who wins".
ELIMINATION_TERMS = (
    "eliminat", "voted off", "voted out", "sent home", "leave", "leaves",
    "exit", "evicted", "eviction", "bottom two", "bottom 2", "next out",
    "next to go", "cut from", "dismissed", "fired",
)

#: Words that mark a market we do not want even if it mentions elimination.
EXCLUSION_TERMS = ("win the", "winner of", "season winner", "finalist")


class VenueError(RuntimeError):
    pass


def looks_like_elimination(title: str, *extra: str) -> bool:
    text = " ".join([title or "", *[e or "" for e in extra]]).lower()
    if any(term in text for term in EXCLUSION_TERMS) and not any(
        term in text for term in ("eliminat", "evict", "voted o")
    ):
        return False
    return any(term in text for term in ELIMINATION_TERMS)


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_prob(value: Any, scale: float = 1.0) -> float | None:
    """Coerce a venue price (cents or dollars) into a probability."""
    if value is None or value == "":
        return None
    try:
        price = float(value) * scale
    except (TypeError, ValueError):
        return None
    if not 0.0 <= price <= 1.0:
        return None
    return price


class HttpClient:
    """Minimal GET client with timeout, retries and a descriptive user agent."""

    def __init__(self, base_url: str, timeout: float = 15.0, retries: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        if requests is None:
            raise VenueError("the 'requests' package is required for live venue data")
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "elimination-bot/0.1 (research; read-only)"}
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    raise VenueError(f"{response.status_code} from {url}")
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # network, JSON, or the raise above
                last = exc
                if attempt == self.retries - 1:
                    break
                time.sleep(2 ** attempt)
        raise VenueError(f"GET {url} failed: {last}")


class MarketDataSource(ABC):
    """Read-only access to one venue's elimination markets."""

    name: str = "venue"

    @abstractmethod
    def discover(self, shows: Sequence[str] = (), limit: int = 250) -> list[Market]:
        """Open elimination markets, optionally filtered to named shows."""

    @abstractmethod
    def fetch_quotes(self, markets: Iterable[Market]) -> dict[str, Quote]:
        """Current order books, keyed by ``Market.key``. Missing books are skipped."""

    def matches_shows(self, market: Market, shows: Sequence[str]) -> bool:
        if not shows:
            return True
        haystack = f"{market.show} {market.title}".lower()
        return any(show.lower() in haystack for show in shows)
