"""Turning signals into a probability.

The market price is the prior. Public signals move it in log-odds space, each
weighted by how well that source has actually predicted in the past, and the
total move is capped. Within an episode the legs are mutually exclusive, so
the field is renormalised to the number of eliminations expected.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from .config import CorrelationConfig, ModelConfig, UncertaintyConfig
from .models import Estimate, Signal, utcnow

EPS = 1e-6


def clamp(p: float, lo: float = EPS, hi: float = 1 - EPS) -> float:
    return min(max(p, lo), hi)


def logit(p: float) -> float:
    p = clamp(p)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


def source_weight(
    score: Mapping[str, float] | None, config: ModelConfig
) -> float:
    """Reliability weight in [0, 1] from a source's historical Brier score.

    An unproven source gets a small weight, not zero — it still has to earn
    its way up, and a source worse than a coin flip is driven to zero.
    """
    if not score or not score.get("observations"):
        return 0.1
    n = float(score["observations"])
    brier = score.get("brier")
    if brier is None:
        return 0.1
    # Brier 0.25 is the uninformed baseline; 0.0 is perfect.
    skill = max(0.0, 1 - float(brier) / 0.25)
    shrink = n / (n + config.shrinkage_k)
    weight = skill * shrink
    if n < config.min_source_observations:
        weight *= 0.5
    return max(0.0, min(1.0, weight))


def recency_decay(signal: Signal, half_life_hours: float, now: datetime | None = None) -> float:
    """Weight decay for a claim's age. A week-old spoiler is not fresh news."""
    if half_life_hours <= 0:
        return 1.0
    now = now or utcnow()
    stamp = signal.published_at or signal.observed_at
    age_hours = max(0.0, (now - stamp).total_seconds() / 3600.0)
    return 0.5 ** (age_hours / half_life_hours)


def signal_age_hours(signal: Signal, now: datetime | None = None) -> float:
    now = now or utcnow()
    stamp = signal.published_at or signal.observed_at
    return max(0.0, (now - stamp).total_seconds() / 3600.0)


def is_unproven(source_id: str, scores: Mapping[str, Mapping[str, float]], config: ModelConfig) -> bool:
    score = scores.get(source_id)
    if not score:
        return True
    return float(score.get("observations") or 0) < config.min_source_observations


def estimate_probability(
    *,
    market_key: str,
    subject: str,
    market_prob: float,
    signals: Sequence[Signal],
    config: ModelConfig,
    correlation: CorrelationConfig | None = None,
    uncertainty: UncertaintyConfig | None = None,
    source_scores: Mapping[str, Mapping[str, float]] | None = None,
    now: datetime | None = None,
) -> Estimate:
    """Blend the market prior with weighted, lawful, de-duplicated public signals.

    Correlated repetitions of one rumour are collapsed into a single claim
    before anything moves, and the result carries an uncertainty interval
    rather than pretending to be a point.
    """
    from .correlation import cluster_signals, effective_source_count
    from .uncertainty import UncertaintyInputs, interval as build_interval

    correlation = correlation or CorrelationConfig()
    uncertainty_config = uncertainty or UncertaintyConfig()
    scores = source_scores or {}
    now = now or utcnow()
    prior = clamp(market_prob)
    prior_logit = logit(prior)

    components: list[dict] = [
        {"source_id": "market_prior", "lean": prior_logit, "weight": config.prior_weight}
    ]

    usable: list[Signal] = []
    weights: list[float] = []
    for signal in signals:
        if not signal.tradeable:
            components.append(
                {
                    "source_id": signal.source_id,
                    "excluded": True,
                    "reason": f"access={signal.access.value} url={'yes' if signal.url else 'no'}",
                }
            )
            continue
        reliability = source_weight(scores.get(signal.source_id), config)
        decay = recency_decay(signal, config.recency_half_life_hours, now)
        weight = reliability * clamp(signal.confidence, 0.0, 1.0) * decay
        usable.append(signal)
        weights.append(weight)

    clusters = cluster_signals(usable, weights, correlation)
    shift = 0.0
    weight_sum = 0.0
    for cluster in clusters:
        cluster_weight = cluster.effective_weight(correlation.extra_member_weight)
        cluster_lean = cluster.effective_lean()
        shift += cluster_weight * cluster_lean
        weight_sum += cluster_weight
        lead, _ = cluster.lead()
        components.append(
            {
                "cluster_id": cluster.cluster_id,
                "source_id": lead.source_id,
                "members": cluster.members(),
                "repetitions": cluster.size,
                "lean": cluster_lean,
                "weight": cluster_weight,
                "url": lead.url,
            }
        )

    # The prior's weight competes with the signals': a pile of weak blog posts
    # cannot outvote a liquid market.
    denom = config.prior_weight + weight_sum
    raw_shift = (shift / denom) * config.prior_weight if denom else 0.0
    capped = max(-config.max_logit_shift, min(config.max_logit_shift, raw_shift))
    if capped != raw_shift:
        components.append({"source_id": "cap", "raw_shift": raw_shift, "applied": capped})

    prob = sigmoid(prior_logit + capped)

    unproven = [s.source_id for s in usable if is_unproven(s.source_id, scores, config)]
    stalest = max((signal_age_hours(s, now) for s in usable), default=0.0)
    inputs = UncertaintyInputs(
        effective_sources=effective_source_count(clusters, correlation.extra_member_weight),
        dispersion=_dispersion(clusters, correlation.extra_member_weight),
        unproven_share=_unproven_share(clusters, correlation.extra_member_weight, set(unproven)),
        stalest_hours=stalest,
    )
    band = build_interval(prob, inputs, uncertainty_config)

    return Estimate(
        market_key=market_key,
        subject=subject,
        prob=prob,
        prior_prob=prior,
        components=components,
        interval=band,
        effective_sources=inputs.effective_sources,
        stalest_signal_hours=stalest if usable else None,
    )


def _dispersion(clusters, extra_member_weight: float) -> float:
    weights = [c.effective_weight(extra_member_weight) for c in clusters]
    total = sum(weights)
    if total <= 0 or len(clusters) < 2:
        return 0.0
    leans = [c.effective_lean() for c in clusters]
    mean = sum(l * w for l, w in zip(leans, weights)) / total
    return (sum(w * (l - mean) ** 2 for l, w in zip(leans, weights)) / total) ** 0.5


def _unproven_share(clusters, extra_member_weight: float, unproven: set[str]) -> float:
    weights = [c.effective_weight(extra_member_weight) for c in clusters]
    total = sum(weights)
    if total <= 0:
        return 1.0 if clusters else 0.0
    unproven_weight = sum(
        w for c, w in zip(clusters, weights)
        if all(s.source_id in unproven for s in c.signals)
    )
    return unproven_weight / total


def normalize_field(
    estimates: Sequence[Estimate], eliminations: float = 1.0, max_iter: int = 64
) -> list[Estimate]:
    """Scale a group's probabilities so they sum to ``eliminations``.

    Uses a log-odds shift solved by bisection, which preserves the ordering
    and never pushes a probability outside (0, 1) — unlike naive division.
    """
    live = [e for e in estimates if e is not None]
    if len(live) < 2:
        return list(estimates)
    total = sum(e.prob for e in live)
    if abs(total - eliminations) < 1e-9:
        for e in live:
            e.normalized = True
        return list(estimates)

    def total_at(shift: float) -> float:
        return sum(sigmoid(logit(e.prob) + shift) for e in live)

    lo, hi = -20.0, 20.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        if total_at(mid) > eliminations:
            hi = mid
        else:
            lo = mid
    shift = (lo + hi) / 2
    for e in live:
        e.components.append({"source_id": "field_normalization", "shift": shift})
        e.prob = sigmoid(logit(e.prob) + shift)
        if e.interval is not None:
            e.interval.point = e.prob
            e.interval.low = sigmoid(logit(e.interval.low) + shift)
            e.interval.high = sigmoid(logit(e.interval.high) + shift)
        e.normalized = True
    return list(estimates)


def brier(prob: float, outcome: bool) -> float:
    return (prob - (1.0 if outcome else 0.0)) ** 2


def log_loss(prob: float, outcome: bool) -> float:
    p = clamp(prob)
    return -math.log(p) if outcome else -math.log(1 - p)


def calibration_table(
    pairs: Iterable[tuple[float, bool]], bins: int = 10
) -> list[dict]:
    """Bucket (probability, outcome) pairs to see whether 70% means 70%."""
    buckets: list[dict] = [
        {
            "bin": i,
            "low": i / bins,
            "high": (i + 1) / bins,
            "n": 0,
            "predicted": 0.0,
            "observed": 0.0,
        }
        for i in range(bins)
    ]
    for prob, outcome in pairs:
        idx = min(bins - 1, max(0, int(prob * bins)))
        b = buckets[idx]
        b["n"] += 1
        b["predicted"] += prob
        b["observed"] += 1.0 if outcome else 0.0
    for b in buckets:
        if b["n"]:
            b["predicted"] /= b["n"]
            b["observed"] /= b["n"]
        else:
            b["predicted"] = None
            b["observed"] = None
    return buckets
