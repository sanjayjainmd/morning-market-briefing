"""The information policy, enforced in code rather than in a README.

A signal may only reach the model if it is public (or licensed) and carries a
citable URL. Anything marked confidential, paywalled-without-permission,
insider or leaked is dropped at the boundary and recorded as dropped, so the
audit log shows both that it was offered and that it was refused.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..models import Access, Market, Quote, Signal


class InformationPolicyError(ValueError):
    """Raised when a source tries to feed the model non-public information."""


@dataclass
class PublicInfoPolicy:
    """Filter every signal through this before it can influence a price."""

    allow_licensed: bool = True
    require_url: bool = True
    dropped: list[dict] = field(default_factory=list)

    def allowed_access(self) -> tuple[Access, ...]:
        return (Access.PUBLIC, Access.LICENSED) if self.allow_licensed else (Access.PUBLIC,)

    def check(self, signal: Signal) -> bool:
        if signal.access not in self.allowed_access():
            self.dropped.append(
                {
                    "source_id": signal.source_id,
                    "market_key": signal.market_key,
                    "reason": f"access={signal.access.value} is not tradeable",
                }
            )
            return False
        if self.require_url and not signal.url:
            self.dropped.append(
                {
                    "source_id": signal.source_id,
                    "market_key": signal.market_key,
                    "reason": "no citable public URL",
                }
            )
            return False
        return True

    def filter(self, signals: Iterable[Signal]) -> list[Signal]:
        return [s for s in signals if self.check(s)]

    def enforce(self, signals: Iterable[Signal]) -> list[Signal]:
        """Like :meth:`filter` but raises on non-public material.

        Use in tests and in any path where silently dropping would hide a bug
        in a collector.
        """
        out = []
        for signal in signals:
            if signal.access not in self.allowed_access():
                raise InformationPolicyError(
                    f"{signal.source_id} supplied {signal.access.value} information"
                )
            if self.check(signal):
                out.append(signal)
        return out


class SignalSource(ABC):
    """A named producer of public observations about elimination odds."""

    #: stable identifier used as the key in the source-reliability database
    source_id: str = "unnamed"

    @abstractmethod
    def collect(
        self, markets: Sequence[Market], quotes: dict[str, Quote]
    ) -> list[Signal]:
        """Return signals for the given markets. Must never raise on bad input."""

    def describe(self) -> str:
        return self.__class__.__name__
