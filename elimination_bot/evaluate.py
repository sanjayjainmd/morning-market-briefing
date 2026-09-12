"""Did any of this work?

The shadow log is only useful if it is graded honestly. These functions
implement the go-live bar: enough independent opportunities, profit after
fees and slippage, profit across more than one show, calibrated probabilities,
no dependence on a single source, contestant or spectacular trade, a bootstrap
interval that excludes zero, and a holdout period nobody tuned against.

``readiness_report`` returns every criterion with its measured value, so a
failure says which one failed and by how much.
"""

from __future__ import annotations

import json
import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Sequence

from .probability import brier, calibration_table, log_loss
from .storage import AuditLog


@dataclass
class Criterion:
    name: str
    passed: bool
    value: Any
    threshold: Any
    detail: str = ""


@dataclass
class ReadinessReport:
    criteria: list[Criterion] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.criteria) and all(c.passed for c in self.criteria)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "criteria": [c.__dict__ for c in self.criteria],
            "stats": self.stats,
        }

    def render(self) -> str:
        lines = ["Go-live readiness", "=" * 40]
        for c in self.criteria:
            mark = "PASS" if c.passed else "FAIL"
            lines.append(f"[{mark}] {c.name}: {c.value} (need {c.threshold}) {c.detail}".rstrip())
        lines.append("-" * 40)
        lines.append(
            "VERDICT: ready for a funded pilot"
            if self.ready
            else "VERDICT: stay in shadow mode"
        )
        return "\n".join(lines)


def bootstrap_ci(
    values: Sequence[float],
    iterations: int = 10_000,
    alpha: float = 0.05,
    seed: int = 12345,
) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean. Returns (low, high)."""
    data = list(values)
    if len(data) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(data)
    means = []
    for _ in range(iterations):
        sample = [data[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int((alpha / 2) * iterations)]
    hi = means[min(iterations - 1, int((1 - alpha / 2) * iterations))]
    return (lo, hi)


def scoring(pairs: Sequence[tuple[float, bool]]) -> dict[str, Any]:
    if not pairs:
        return {"n": 0}
    briers = [brier(p, o) for p, o in pairs]
    losses = [log_loss(p, o) for p, o in pairs]
    base_rate = sum(1 for _, o in pairs if o) / len(pairs)
    baseline = statistics.fmean([brier(base_rate, o) for _, o in pairs])
    mean_brier = statistics.fmean(briers)
    return {
        "n": len(pairs),
        "brier": mean_brier,
        "log_loss": statistics.fmean(losses),
        "base_rate": base_rate,
        "baseline_brier": baseline,
        "brier_skill_score": 1 - mean_brier / baseline if baseline else 0.0,
        "calibration": calibration_table(pairs),
    }


def calibration_error(pairs: Sequence[tuple[float, bool]], bins: int = 10) -> float:
    """Expected calibration error: n-weighted |observed - predicted|."""
    table = calibration_table(pairs, bins)
    total = sum(b["n"] for b in table)
    if not total:
        return float("nan")
    return sum(
        b["n"] * abs(b["observed"] - b["predicted"]) for b in table if b["n"]
    ) / total


def concentration(trades: Sequence[dict], key: str) -> dict[str, Any]:
    """Share of total positive PnL coming from the single largest bucket."""
    totals: dict[str, float] = {}
    for trade in trades:
        totals[str(trade.get(key))] = totals.get(str(trade.get(key)), 0.0) + trade["pnl"]
    gross_positive = sum(v for v in totals.values() if v > 0)
    if not gross_positive:
        return {"top": None, "share": 1.0, "buckets": totals}
    top_key, top_value = max(totals.items(), key=lambda kv: kv[1])
    return {"top": top_key, "share": top_value / gross_positive, "buckets": totals}


def split_holdout(trades: Sequence[dict], holdout_fraction: float = 0.3) -> tuple[list, list]:
    """Chronological split: the tail is the untouched period."""
    ordered = sorted(trades, key=lambda t: t["filled_at"] or "")
    cut = int(len(ordered) * (1 - holdout_fraction))
    return ordered[:cut], ordered[cut:]


def readiness_report(
    log: AuditLog,
    *,
    min_opportunities: int = 150,
    min_shows: int = 2,
    max_calibration_error: float = 0.10,
    max_single_source_share: float = 0.5,
    max_single_trade_share: float = 0.35,
    holdout_fraction: float = 0.3,
) -> ReadinessReport:
    report = ReadinessReport()
    trades = log.settled_trades()
    graded = log.graded_estimates()
    pairs = [(row["prob"], bool(row["eliminated"])) for row in graded]

    pnls = [t["pnl"] for t in trades]
    total_pnl = sum(pnls)
    stake = sum(t["contracts"] * t["price"] + t["fees"] for t in trades)
    lo, hi = bootstrap_ci(pnls) if len(pnls) > 1 else (float("nan"), float("nan"))
    train, holdout = split_holdout(trades, holdout_fraction)
    by_source = _source_contribution(log, trades)
    show_conc = concentration(trades, "show")
    subject_conc = concentration(trades, "subject")
    top_trade_share = (
        max((t["pnl"] for t in trades), default=0.0) / total_pnl
        if total_pnl > 0
        else 1.0
    )

    report.stats = {
        "trades": len(trades),
        "graded_estimates": len(pairs),
        "total_pnl": total_pnl,
        "total_staked": stake,
        "roi": (total_pnl / stake) if stake else None,
        "mean_pnl_per_trade": statistics.fmean(pnls) if pnls else 0.0,
        "bootstrap_ci_mean_pnl": [lo, hi],
        "holdout_pnl": sum(t["pnl"] for t in holdout),
        "holdout_trades": len(holdout),
        "scoring": scoring(pairs),
        "calibration_error": calibration_error(pairs) if pairs else None,
        "show_concentration": show_conc,
        "subject_concentration": subject_conc,
        "source_contribution": by_source,
        "counts": log.counts(),
    }

    add = report.criteria.append
    add(Criterion(
        "independent opportunities", len(trades) >= min_opportunities,
        len(trades), f">= {min_opportunities}",
    ))
    add(Criterion(
        "profitable after fees and slippage", total_pnl > 0,
        round(total_pnl, 2), "> 0",
        f"ROI {report.stats['roi']:.1%}" if report.stats["roi"] is not None else "",
    ))
    shows = {t["show"] for t in trades if t["show"]}
    add(Criterion(
        "spread across shows/seasons", len(shows) >= min_shows,
        len(shows), f">= {min_shows}", ", ".join(sorted(shows)[:5]),
    ))
    cal_err = report.stats["calibration_error"]
    add(Criterion(
        "probabilities calibrated",
        cal_err is not None and cal_err <= max_calibration_error,
        None if cal_err is None else round(cal_err, 4),
        f"<= {max_calibration_error}",
        "expected calibration error",
    ))
    add(Criterion(
        "not one lucky trade", total_pnl > 0 and top_trade_share <= max_single_trade_share,
        round(top_trade_share, 3), f"<= {max_single_trade_share}",
        "largest trade's share of total PnL",
    ))
    add(Criterion(
        "not one contestant", subject_conc["share"] <= 0.5,
        round(subject_conc["share"], 3), "<= 0.5", f"top: {subject_conc['top']}",
    ))
    top_source_share = max((v["share"] for v in by_source.values()), default=0.0)
    add(Criterion(
        "not one source", top_source_share <= max_single_source_share,
        round(top_source_share, 3), f"<= {max_single_source_share}",
        "share of traded markets touched by the most-used source",
    ))
    add(Criterion(
        "bootstrap CI excludes zero", lo == lo and lo > 0,
        None if lo != lo else round(lo, 4), "> 0",
        "lower bound of 95% CI on mean PnL per trade",
    ))
    add(Criterion(
        "holdout period profitable",
        len(holdout) >= 20 and sum(t["pnl"] for t in holdout) > 0,
        round(sum(t["pnl"] for t in holdout), 2),
        "> 0 with >= 20 trades", f"{len(holdout)} holdout trades",
    ))
    return report


def _source_contribution(log: AuditLog, trades: Sequence[dict]) -> dict[str, dict[str, Any]]:
    """How much of the traded universe each source actually touched."""
    keys = {t["market_key"] for t in trades}
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = log.conn.execute(
        f"SELECT source_id, COUNT(DISTINCT market_key) AS markets"
        f" FROM signals WHERE market_key IN ({placeholders}) GROUP BY source_id",
        tuple(keys),
    ).fetchall()
    scores = log.source_scores()
    return {
        row["source_id"]: {
            "markets": row["markets"],
            "share": row["markets"] / len(keys),
            "brier": (scores.get(row["source_id"]) or {}).get("brier"),
            "observations": (scores.get(row["source_id"]) or {}).get("observations", 0),
        }
        for row in rows
    }


def render_json(report: ReadinessReport) -> str:
    return json.dumps(report.to_dict(), indent=2, default=str)
