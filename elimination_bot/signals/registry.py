"""The source-reliability database.

This is the part of the system most likely to be worth anything: a record of
which public sources have actually predicted eliminations, scored the same way
every time. Weights in ``probability.source_weight`` read directly from it.
"""

from __future__ import annotations

import json
import math
from typing import Any

from ..probability import brier, log_loss, sigmoid
from ..storage import AuditLog


def _implied_prob(lean: float, prior: float) -> float:
    """The probability a source implied, given the prior it was nudging."""
    prior = min(max(prior, 1e-4), 1 - 1e-4)
    return sigmoid(math.log(prior / (1 - prior)) + lean)


def update_source_scores(log: AuditLog) -> dict[str, dict[str, float]]:
    """Grade every source's claim on each settled market. Idempotent per run.

    One observation per (source, market), not per cycle: the engine re-reads
    the same public post every 25 minutes, and counting each re-read would
    inflate a source's track record roughly fifty-fold for a single claim.
    The last signal before settlement is the one graded.

    Each claim is scored on the probability it implied relative to the market
    prior at the time, so a source is judged on its own contribution rather
    than on whether the market happened to be right.
    """
    rows = log.conn.execute(
        "SELECT s.source_id, s.market_key, s.payload, o.eliminated,"
        " (SELECT e.prior_prob FROM estimates e WHERE e.market_key = s.market_key"
        "   ORDER BY e.id LIMIT 1) AS prior"
        " FROM signals s JOIN outcomes o ON o.market_key = s.market_key"
        " WHERE s.id IN ("
        "   SELECT MAX(id) FROM signals GROUP BY source_id, market_key)"
    ).fetchall()

    log.conn.execute("DELETE FROM source_scores")
    log.conn.commit()

    for row in rows:
        payload: dict[str, Any] = json.loads(row["payload"])
        prior = row["prior"]
        if prior is None:
            prior = 0.5
        implied = _implied_prob(float(payload.get("lean", 0.0)), float(prior))
        outcome = bool(row["eliminated"])
        log.update_source_score(
            row["source_id"], brier(implied, outcome), log_loss(implied, outcome)
        )
    return log.source_scores()
