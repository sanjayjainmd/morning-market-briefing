"""Read the contract before pricing it.

An elimination market is only tradeable if its terms are unambiguous. The
question the model answers — "is this contestant eliminated in this episode?"
— has to be the same question the exchange will settle. Where it might not
be, the bot does not trade; it records why and moves on.

The checks:

* which episode the contract covers, and that it has not already aired;
* what "eliminated" means, and whether withdrawal, disqualification, medical
  evacuation, a double elimination or a non-elimination week are addressed;
* when trading closes, and that the market is open now;
* which source settles it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import VerificationConfig
from .models import Market, utcnow

EPISODE_PATTERNS = (
    re.compile(r"\bepisode\s*#?\s*(\d+)\b", re.I),
    re.compile(r"\bep\.?\s*(\d+)\b", re.I),
    re.compile(r"\bweek\s*#?\s*(\d+)\b", re.I),
    re.compile(r"\bday\s*#?\s*(\d+)\b", re.I),
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
)


@dataclass
class Verification:
    market_key: str
    ok: bool
    issues: list[str] = field(default_factory=list)
    episode: str | None = None
    resolution_source: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)

    def reason(self) -> str:
        return "; ".join(self.issues) if self.issues else "contract terms verified"


def _text(market: Market, *keys: str) -> str:
    parts = [market.title, market.metadata.get("subtitle") or ""]
    parts.extend(str(market.metadata.get(key) or "") for key in keys)
    return " ".join(p for p in parts if p)


def find_episode(market: Market) -> str | None:
    """Which episode the contract covers.

    The title is authoritative: rules text is often boilerplate shared across
    a series and can name a different episode than the contract itself.
    """
    for haystack in (
        market.title or "",
        market.subject or "",
        market.group_id or "",
        str(market.metadata.get("rules") or ""),
    ):
        for pattern in EPISODE_PATTERNS:
            match = pattern.search(haystack)
            if match:
                return match.group(0).strip()
    return None


def verify(
    market: Market,
    config: VerificationConfig,
    now: datetime | None = None,
) -> Verification:
    """Check one contract's terms. Anything unclear fails closed."""
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    issues: list[str] = []
    checks: dict[str, bool] = {}
    rules = str(market.metadata.get("rules") or "").strip()
    resolution_source = (
        market.metadata.get("resolution_source")
        or market.metadata.get("settlement_source")
        or None
    )

    episode = find_episode(market)
    checks["episode_identified"] = episode is not None
    if episode is None:
        issues.append("cannot tell which episode the contract covers")

    checks["rules_present"] = len(rules) >= config.min_rules_chars
    if config.require_rules and not checks["rules_present"]:
        issues.append("no usable rules text to read")

    checks["resolution_source"] = bool(resolution_source)
    if config.require_resolution_source and not resolution_source:
        issues.append("no official resolution source named")

    checks["close_time"] = market.close_time is not None
    if config.require_close_time and market.close_time is None:
        issues.append("no closing time published")
    elif market.close_time is not None and market.close_time <= now:
        checks["close_time"] = False
        issues.append("closing time has already passed")

    status = str(market.metadata.get("status") or "open").lower()
    checks["open"] = status in ("open", "active", "")
    if config.require_open_status and not checks["open"]:
        issues.append(f"market status is {status!r}")

    aired = _already_aired(market, now)
    checks["not_yet_aired"] = not aired
    if config.block_if_already_aired and aired:
        issues.append("episode appears to have already aired somewhere")

    unaddressed = unaddressed_edge_cases(market, rules, config)
    checks["edge_cases_addressed"] = not unaddressed
    if unaddressed:
        issues.append(
            "rules do not say how these resolve: " + ", ".join(sorted(unaddressed))
        )

    return Verification(
        market_key=market.key,
        ok=not issues,
        issues=issues,
        episode=episode,
        resolution_source=str(resolution_source) if resolution_source else None,
        checks=checks,
    )


def unaddressed_edge_cases(
    market: Market, rules: str, config: VerificationConfig
) -> set[str]:
    """Edge cases the contract raises but never resolves.

    A term counts as addressed when the rules both mention it and say what
    happens — the bot is looking for "a withdrawal counts as an elimination",
    not merely the word "withdrawal".
    """
    if not config.require_ambiguity_resolution:
        return set()
    rules_lower = rules.lower()
    title_lower = f"{market.title} {market.subject}".lower()
    unaddressed: set[str] = set()
    for term in config.ambiguity_terms:
        raised_in_title = term in title_lower
        mentioned_in_rules = term in rules_lower
        if not (raised_in_title or mentioned_in_rules):
            continue
        if mentioned_in_rules and any(word in rules_lower for word in config.resolution_terms):
            continue
        unaddressed.add(term)
    return unaddressed


def _already_aired(market: Market, now: datetime) -> bool:
    """Has the deciding episode already been shown somewhere?

    Two public tells: the venue publishes an air date in the past, or trading
    has been closed/settled while the market is still listed. Neither is
    perfect, which is exactly why an unclear answer blocks the trade.
    """
    for key in ("aired_at", "air_date", "episode_air_time"):
        value = market.metadata.get(key)
        if not value:
            continue
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp <= now:
            return True
    status = str(market.metadata.get("status") or "").lower()
    return status in ("closed", "settled", "finalized", "determined")


def summarize(verifications: dict[str, Verification]) -> dict[str, Any]:
    failed = [v for v in verifications.values() if not v.ok]
    reasons: dict[str, int] = {}
    for verification in failed:
        for issue in verification.issues:
            key = issue.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
    return {
        "checked": len(verifications),
        "verified": len(verifications) - len(failed),
        "rejected": len(failed),
        "reasons": reasons,
    }
