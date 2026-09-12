"""How wrong the point estimate might be, expressed as an interval.

A single number like "65%" invites the bot to trade on a precision it does
not have. Every estimate therefore carries a range, and each side is bought
only against the pessimistic end of that range: YES against the low bound,
NO against ``1 - high``. Widening the interval and raising the required edge
have the same effect — fewer trades — but the interval says *why*.

The half-width grows with:

* few independent claims (correlated sources barely narrow it);
* unproven sources, which could be anything;
* disagreement between clusters;
* stale evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .config import UncertaintyConfig
from .correlation import Cluster
from .models import ProbabilityInterval
from .probability import clamp, logit, sigmoid

#: Beyond this the interval spans almost the whole unit line and says nothing
#: useful; it only ever means "do not trade", which the caller already knows.
MAX_LOGIT_WIDTH = 2.5


@dataclass
class UncertaintyInputs:
    effective_sources: float
    dispersion: float          # spread of cluster leans, in log-odds
    unproven_share: float      # fraction of cluster weight from unproven sources
    stalest_hours: float


def gather(
    clusters: Sequence[Cluster],
    config: UncertaintyConfig,
    extra_member_weight: float,
    now_hours: float = 0.0,
    unproven_sources: Sequence[str] = (),
) -> UncertaintyInputs:
    weights = [c.effective_weight(extra_member_weight) for c in clusters]
    total = sum(weights)
    effective = sum(min(1.0, w) for w in weights)

    if total > 0 and len(clusters) > 1:
        leans = [c.effective_lean() for c in clusters]
        mean = sum(l * w for l, w in zip(leans, weights)) / total
        dispersion = (
            sum(w * (l - mean) ** 2 for l, w in zip(leans, weights)) / total
        ) ** 0.5
    else:
        dispersion = 0.0

    unproven = set(unproven_sources)
    unproven_weight = sum(
        w for c, w in zip(clusters, weights)
        if all(s.source_id in unproven for s in c.signals)
    )
    unproven_share = (unproven_weight / total) if total else 1.0

    return UncertaintyInputs(
        effective_sources=effective,
        dispersion=dispersion,
        unproven_share=unproven_share,
        stalest_hours=now_hours,
    )


def half_width(inputs: UncertaintyInputs, config: UncertaintyConfig) -> tuple[float, dict[str, float]]:
    """Half-width of the probability interval, with its components itemised."""
    if inputs.effective_sources <= 0:
        base = config.prior_only_half_width
        components = {"prior_only": base}
    else:
        base = config.base_half_width / (
            max(inputs.effective_sources, 1e-9) ** config.independence_exponent
        )
        components = {"independence": base}

    dispersion_term = config.dispersion_weight * inputs.dispersion * 0.25
    unproven_term = config.unproven_source_penalty * inputs.unproven_share
    staleness_term = config.staleness_penalty_per_day * (inputs.stalest_hours / 24.0)
    components.update(
        {
            "dispersion": dispersion_term,
            "unproven": unproven_term,
            "staleness": staleness_term,
        }
    )
    total = base + dispersion_term + unproven_term + staleness_term
    bounded = min(config.max_half_width, max(config.min_half_width, total))
    components["applied"] = bounded
    return bounded, components


def interval(
    point: float,
    inputs: UncertaintyInputs,
    config: UncertaintyConfig,
) -> ProbabilityInterval:
    """Build the interval in log-odds space so it never leaves (0, 1).

    Half-widths are specified in probability terms at the point estimate and
    converted, which keeps a 6-point interval around 0.50 from becoming a
    6-point interval around 0.03.
    """
    width, components = half_width(inputs, config)
    point = clamp(point)
    scale = max(point * (1 - point), 1e-4)
    logit_width = min(MAX_LOGIT_WIDTH, width / scale)
    low = sigmoid(logit(point) - logit_width)
    high = sigmoid(logit(point) + logit_width)
    components = dict(components)
    components["effective_sources"] = inputs.effective_sources
    return ProbabilityInterval(point=point, low=low, high=high, components=components)
