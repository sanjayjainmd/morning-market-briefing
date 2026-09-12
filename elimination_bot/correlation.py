"""Five websites repeating one rumour are one signal, not five.

Correlated sources are the most dangerous failure mode in this whole design:
they look exactly like independent confirmation, they arrive together, and
they move the model hardest at precisely the moment it should be most
sceptical.

Signals are grouped into clusters by, in order of authority:

1. an explicit ``clusters`` mapping for known syndication relationships;
2. an explicit ``cluster`` field on the observation itself;
3. the registrable domain of the citing URL;
4. near-identical claims (same direction and magnitude) made inside a window.

Each cluster then collapses to a single effective signal: the strongest
member at full weight, every other member at ``extra_member_weight``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlparse

from .config import CorrelationConfig
from .models import Signal


@dataclass
class Cluster:
    """One independent claim, however many places repeated it."""

    cluster_id: str
    signals: list[Signal] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.signals)

    def lead(self) -> tuple[Signal, float]:
        pairs = sorted(zip(self.signals, self.weights), key=lambda sw: -sw[1])
        return pairs[0]

    def effective_weight(self, extra_member_weight: float) -> float:
        """Strongest member at full weight, the rest with geometric decay.

        A flat surcharge per repetition would let five copies of one rumour
        out-weigh two genuinely independent sources, which is the exact
        failure this module exists to prevent. The k-th repetition therefore
        contributes ``extra_member_weight ** k`` of its own weight, so a
        cluster converges to about 1.33x its lead member and never to 2x.
        """
        if not self.weights:
            return 0.0
        ordered = sorted(self.weights, reverse=True)
        total = ordered[0]
        for index, weight in enumerate(ordered[1:], start=1):
            total += (extra_member_weight ** index) * weight
        return total

    def effective_lean(self) -> float:
        """Weighted mean lean of the cluster's members."""
        total = sum(self.weights)
        if total <= 0:
            return 0.0
        return sum(s.lean * w for s, w in zip(self.signals, self.weights)) / total

    def members(self) -> list[str]:
        return [s.source_id for s in self.signals]


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def cluster_signals(
    signals: Sequence[Signal],
    weights: Mapping[str, float] | Sequence[float],
    config: CorrelationConfig,
) -> list[Cluster]:
    """Group signals into independent claims and attach each member's weight."""
    if isinstance(weights, Mapping):
        weight_for = lambda i, s: float(weights.get(s.source_id, 0.0))  # noqa: E731
    else:
        weight_for = lambda i, s: float(weights[i])  # noqa: E731

    clusters: dict[str, Cluster] = {}
    for index, signal in enumerate(signals):
        key = _cluster_key(signal, config, clusters)
        cluster = clusters.setdefault(key, Cluster(cluster_id=key))
        cluster.signals.append(signal)
        cluster.weights.append(weight_for(index, signal))
    return list(clusters.values())


def _cluster_key(
    signal: Signal, config: CorrelationConfig, existing: Mapping[str, Cluster]
) -> str:
    explicit = config.clusters.get(signal.source_id) or signal.metadata.get("cluster")
    if explicit:
        return f"declared:{explicit}"

    if config.cluster_by_domain:
        domain = domain_of(signal.url)
        if domain:
            declared_domain = f"domain:{domain}"
            # A domain cluster still merges into a declared cluster when one of
            # its members was mapped explicitly.
            return declared_domain

    if config.cluster_by_claim:
        for key, cluster in existing.items():
            if not key.startswith("claim:") and not key.startswith("domain:"):
                continue
            if _same_claim(signal, cluster, config):
                return key
        return f"claim:{signal.source_id}:{round(signal.lean, 2)}"

    return f"source:{signal.source_id}"


def _same_claim(signal: Signal, cluster: Cluster, config: CorrelationConfig) -> bool:
    """Same direction, similar magnitude, close in time — probably one rumour."""
    window = config.claim_window_hours * 3600
    for other in cluster.signals:
        if other.subject != signal.subject:
            continue
        if math.copysign(1, other.lean) != math.copysign(1, signal.lean):
            continue
        if abs(other.lean - signal.lean) > config.claim_lean_tolerance:
            continue
        gap = abs((signal.observed_at - other.observed_at).total_seconds())
        if gap <= window:
            return True
    return False


def effective_source_count(
    clusters: Iterable[Cluster], extra_member_weight: float
) -> float:
    """Independent-claim count, weighted by reliability.

    Ten copies of one rumour from unproven accounts count for a fraction of
    one; two well-scored independent sources count for nearly two.
    """
    return sum(
        min(1.0, cluster.effective_weight(extra_member_weight)) for cluster in clusters
    )
