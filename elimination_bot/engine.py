"""One 25-minute cycle, start to finish.

    discover -> quote -> verify the contract -> collect public signals
    -> estimate with an uncertainty interval -> manage open positions
    -> price new entries -> re-check at the last moment -> record

Nothing here asks for approval, and nothing here can place a real order: the
autonomy is in the loop, the restraint is in ``broker.LiveBroker``.

Order matters. Exits run before entries so that freed capital and freed
exposure caps are available to the best new idea in the same cycle, and so a
position whose contract has become unverifiable is closed before the engine
considers adding to that episode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import BotConfig
from .contract import Verification, summarize, verify
from .exits import ExitAssessment, ExitContext, assess_exit
from .fees import FeeBook, load_fee_book
from .funding import FundingStatus, Treasury, kill_switch_engaged
from .models import (
    Decision,
    Estimate,
    Fill,
    Market,
    Outcome,
    Position,
    Quote,
    utcnow,
)
from .policy import ExposureState, decide, _minutes_to_close
from .probability import estimate_probability, normalize_field
from .signals.base import PublicInfoPolicy, SignalSource
from .storage import AuditLog
from .venues.base import MarketDataSource, VenueError


@dataclass
class CycleReport:
    cycle_id: str
    started_at: str
    markets_seen: int = 0
    quotes_captured: int = 0
    contracts_verified: int = 0
    verification: dict[str, Any] = field(default_factory=dict)
    signals_kept: int = 0
    signals_dropped: list[dict] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    exits: list[ExitAssessment] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    preflight_rejections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    halted: str | None = None
    risk_off: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def trades(self) -> list[Decision]:
        return [d for d in self.decisions if d.is_trade and d.action.is_buy]

    @property
    def sells(self) -> list[Decision]:
        return [d for d in self.decisions if d.is_trade and d.action.is_sell]

    def summary(self) -> str:
        if self.halted:
            return f"cycle {self.cycle_id}: HALTED — {self.halted}"
        return (
            f"cycle {self.cycle_id}: {self.markets_seen} markets, "
            f"{self.quotes_captured} books, {self.contracts_verified} verified, "
            f"{self.signals_kept} signals ({len(self.signals_dropped)} dropped), "
            f"{len(self.trades)} entries, {len(self.sells)} exits, "
            f"{len(self.fills)} fills"
            + (" [risk-off]" if self.risk_off else "")
        )


class Engine:
    def __init__(
        self,
        config: BotConfig,
        log: AuditLog,
        sources: Sequence[MarketDataSource],
        signal_sources: Sequence[SignalSource],
        broker,
        info_policy: PublicInfoPolicy | None = None,
        fee_book: FeeBook | None = None,
    ) -> None:
        self.config = config
        self.log = log
        self.sources = list(sources)
        self.signal_sources = list(signal_sources)
        self.broker = broker
        self.info_policy = info_policy or PublicInfoPolicy()
        self.treasury = Treasury(config, log)
        self.fee_book = fee_book or load_fee_book(
            config.fees, fee_schedule_path(config.db_path)
        )

    # ------------------------------------------------------------- guards

    def halt_reason(self) -> str | None:
        """Conditions that stop the cycle dead, exits included."""
        if kill_switch_engaged(self.config):
            return f"kill switch present at {self.config.kill_switch_path}"
        if self.treasury.is_dormant():
            return f"dormant: see {self.config.dormant_path}"
        funding = self.treasury.state()
        if funding.status is FundingStatus.UNFUNDED:
            return f"unfunded: {funding.message}"
        if self.config.execution.is_live:
            blocked = self.fee_book.blocks_live_trading(self.config.execution.venues)
            if blocked:
                return blocked
        return None

    def risk_off_reason(self) -> str | None:
        """Conditions that stop new entries but leave exits available."""
        drawdown = self.drawdown()
        if drawdown > self.config.risk.max_drawdown:
            return f"drawdown {drawdown:.1%} exceeds max {self.config.risk.max_drawdown:.1%}"
        return None

    def drawdown(self) -> float:
        """Peak-to-current drawdown of realized PnL, as a fraction of bankroll."""
        trades = self.log.settled_trades()
        if not trades:
            return 0.0
        equity = 0.0
        peak = 0.0
        trough = 0.0
        for trade in trades:
            equity += trade["pnl"]
            peak = max(peak, equity)
            trough = min(trough, equity - peak)
        return abs(trough) / self.config.bankroll if self.config.bankroll else 0.0

    # -------------------------------------------------------------- cycle

    def run_cycle(self) -> CycleReport:
        cycle_id = self.log.start_cycle(self.config.execution.mode, self.config.bankroll)
        report = CycleReport(cycle_id=cycle_id, started_at=utcnow().isoformat())

        halt = self.halt_reason()
        if halt:
            report.halted = halt
            self.log.finish_cycle(cycle_id, f"halted: {halt}")
            return report

        fee_warning = self.fee_book.warning(self.config.execution.venues)
        if fee_warning:
            report.warnings.append(fee_warning)

        risk_off = self.risk_off_reason()
        report.risk_off = risk_off is not None
        if risk_off:
            report.warnings.append(f"risk-off: {risk_off}")

        markets = self._discover(report)
        quotes = self._quote(markets, report, cycle_id)
        quoted = [m for m in markets if m.key in quotes]
        for market in quoted:
            self.log.record_market(market)

        verifications = self._verify(quoted, report, cycle_id)
        tradeable = [m for m in quoted if verifications[m.key].ok]

        signals = self._collect_signals(quoted, quotes, report, cycle_id)
        estimates = self._estimate(quoted, quotes, signals, cycle_id)

        by_key = {m.key: m for m in quoted}
        exited = self._manage_exits(
            by_key, quotes, estimates, verifications, signals, report, cycle_id
        )

        if not report.risk_off:
            self._open_positions(
                tradeable, quotes, estimates, verifications, report, cycle_id, exited
            )

        self.log.finish_cycle(cycle_id, report.summary())
        return report

    # ------------------------------------------------------------- stages

    def _discover(self, report: CycleReport) -> list[Market]:
        markets: list[Market] = []
        budget = self.config.execution.max_markets_per_cycle
        for source in self.sources:
            try:
                found = source.discover(self.config.shows, limit=budget)
            except VenueError as exc:
                report.errors.append(f"{source.name} discovery failed: {exc}")
                continue
            markets.extend(found)
        report.markets_seen = len(markets)
        return markets[:budget]

    def _quote(
        self, markets: Sequence[Market], report: CycleReport, cycle_id: str
    ) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        by_venue: dict[str, list[Market]] = {}
        for market in markets:
            by_venue.setdefault(market.venue, []).append(market)
        for source in self.sources:
            batch = by_venue.get(source.name, [])
            if not batch:
                continue
            try:
                quotes.update(source.fetch_quotes(batch))
            except VenueError as exc:
                report.errors.append(f"{source.name} quotes failed: {exc}")
        for quote in quotes.values():
            self.log.record_quote(quote, cycle_id)
        report.quotes_captured = len(quotes)
        return quotes

    def _verify(
        self, markets: Sequence[Market], report: CycleReport, cycle_id: str
    ) -> dict[str, Verification]:
        verifications = {
            market.key: verify(market, self.config.verification) for market in markets
        }
        for verification in verifications.values():
            self.log.record_verification(verification, cycle_id)
        report.verification = summarize(verifications)
        report.contracts_verified = report.verification["verified"]
        return verifications

    def _collect_signals(
        self,
        markets: Sequence[Market],
        quotes: dict[str, Quote],
        report: CycleReport,
        cycle_id: str,
    ) -> dict[str, list]:
        collected = []
        for source in self.signal_sources:
            try:
                collected.extend(source.collect(markets, quotes))
            except Exception as exc:  # a bad source must not kill the cycle
                report.errors.append(f"signal source {source.source_id} failed: {exc}")
        kept = self.info_policy.filter(collected)
        report.signals_kept = len(kept)
        report.signals_dropped = list(self.info_policy.dropped)
        self.info_policy.dropped.clear()
        self.log.record_signals(kept, cycle_id)

        by_market: dict[str, list] = {}
        for signal in kept:
            by_market.setdefault(signal.market_key, []).append(signal)
        return by_market

    def _estimate(
        self,
        markets: Sequence[Market],
        quotes: dict[str, Quote],
        signals: dict[str, list],
        cycle_id: str,
    ) -> dict[str, Estimate]:
        scores = self.log.source_scores()
        estimates: dict[str, Estimate] = {}
        groups: dict[str, list] = {}
        for market in markets:
            quote = quotes[market.key]
            market_prob = quote.market_prob
            if market_prob is None:
                continue
            estimate = estimate_probability(
                market_key=market.key,
                subject=market.subject,
                market_prob=market_prob,
                signals=signals.get(market.key, []),
                config=self.config.model,
                correlation=self.config.correlation,
                uncertainty=self.config.uncertainty,
                source_scores=scores,
            )
            estimates[market.key] = estimate
            groups.setdefault(f"{market.venue}:{market.group_id}", []).append(estimate)

        if self.config.model.normalize_field:
            for legs in groups.values():
                normalize_field(legs, self.config.model.eliminations_per_group)

        for estimate in estimates.values():
            self.log.record_estimate(estimate, cycle_id)
        return estimates

    # --------------------------------------------------------------- exits

    def _manage_exits(
        self,
        markets: dict[str, Market],
        quotes: dict[str, Quote],
        estimates: dict[str, Estimate],
        verifications: dict[str, Verification],
        signals: dict[str, list],
        report: CycleReport,
        cycle_id: str,
    ) -> set[str]:
        """Returns the markets exited this cycle, which are then off-limits."""
        exited: set[str] = set()
        open_positions = self.log.open_positions()
        if not open_positions:
            return exited
        exposure_by_group = self._group_exposure(open_positions, markets)

        for market_key, entry in open_positions.items():
            position: Position = entry["position"]
            market = markets.get(market_key)
            quote = quotes.get(market_key)
            if market is None or quote is None:
                report.errors.append(
                    f"holding {market_key} but it was not quoted this cycle;"
                    " cannot manage the position"
                )
                exited.add(market_key)  # unquotable: certainly not a place to add
                continue

            verification = verifications.get(market_key)
            group_exposure = exposure_by_group.get(market.group_id, 0.0)
            context = ExitContext(
                minutes_to_close=_minutes_to_close(market),
                verification_ok=verification.ok if verification else True,
                verification_reason=verification.reason() if verification else "",
                stalest_signal_hours=(
                    estimates[market_key].stalest_signal_hours
                    if market_key in estimates
                    else None
                ),
                retracted_sources=[
                    s.source_id
                    for s in signals.get(market_key, [])
                    if s.metadata.get("retracted")
                ],
                group_over_cap=group_exposure
                > self.config.risk.max_fraction_per_group * self.config.bankroll,
                risk_off=report.risk_off,
                operational_ok=not report.errors,
                operational_note="; ".join(report.errors[:2]),
                entry_prob_eliminated=self.log.entry_model_prob(market_key),
            )
            assessment = assess_exit(
                position=position,
                quote=quote,
                estimate=estimates.get(market_key),
                config=self.config,
                fee_model=self.fee_book.model(market.venue),
                context=context,
            )
            report.exits.append(assessment)
            decision = self._exit_decision(cycle_id, assessment, position)
            self.log.record_decision(decision, quote.market_prob)
            report.decisions.append(decision)
            if not decision.is_trade:
                continue
            _order, fill = self.broker.place(decision, quote)
            if fill is not None:
                report.fills.append(fill)
                exited.add(market_key)
        return exited

    @staticmethod
    def _exit_decision(
        cycle_id: str, assessment: ExitAssessment, position: Position
    ) -> Decision:
        reasons = [f"[{assessment.trigger}] {r}" for r in assessment.reasons]
        return Decision(
            cycle_id=cycle_id,
            market_key=assessment.market_key,
            subject=assessment.subject or position.subject,
            action=assessment.action,
            contracts=assessment.contracts,
            limit_price=assessment.limit_price,
            reasons=reasons,
            edge=None,
            estimate=None,
        )

    def _group_exposure(
        self, open_positions: dict[str, dict], markets: dict[str, Market]
    ) -> dict[str, float]:
        totals: dict[str, float] = {}
        for key, entry in open_positions.items():
            market = markets.get(key)
            group = market.group_id if market else entry["position"].group_id
            totals[group] = totals.get(group, 0.0) + float(entry["cost"])
        return totals

    # ------------------------------------------------------------- entries

    def _open_positions(
        self,
        markets: Sequence[Market],
        quotes: dict[str, Quote],
        estimates: dict[str, Estimate],
        verifications: dict[str, Verification],
        report: CycleReport,
        cycle_id: str,
        exited: set[str] | None = None,
    ) -> None:
        exposure = ExposureState()
        # A market exited earlier in this cycle is not re-entered now, in
        # either direction. The evidence that closed the position is the same
        # evidence that would open the new one, and paying the spread twice in
        # 25 minutes to flip sides is churn, not conviction.
        held = set(exited or ())
        for market_key, position in self.log.open_positions().items():
            exposure.gross += float(position["cost"])
            held.add(market_key)

        # Trade the biggest disagreements first: the caps are finite, so order
        # matters, and a cycle should spend its budget on its best ideas.
        ranked = sorted(
            markets,
            key=lambda m: -abs(
                getattr(estimates.get(m.key), "prob", 0.0) - (quotes[m.key].market_prob or 0.0)
            ),
        )
        for market in ranked:
            estimate = estimates.get(market.key)
            if estimate is None:
                continue
            decision = decide(
                cycle_id=cycle_id,
                market=market,
                quote=quotes[market.key],
                estimate=estimate,
                config=self.config,
                fee_model=self.fee_book.model(market.venue),
                exposure=exposure,
                verification=verifications.get(market.key),
                held=market.key in held,
            )
            self.log.record_decision(decision, quotes[market.key].market_prob)
            report.decisions.append(decision)
            if not decision.is_trade:
                continue

            checked, quote, rejection = self._preflight(
                decision, market, estimate, exposure, held
            )
            if checked is None:
                report.preflight_rejections.append(f"{market.subject}: {rejection}")
                continue
            _order, fill = self.broker.place(checked, quote)
            if fill is not None:
                report.fills.append(fill)
                exposure.add(market.group_id, fill.cost)
                held.add(market.key)

    def _preflight(
        self,
        decision: Decision,
        market: Market,
        estimate: Estimate,
        exposure: ExposureState,
        held: set[str],
    ) -> tuple[Decision | None, Quote, str]:
        """Re-check everything against a fresh book immediately before sending.

        Between deciding and submitting, the ask can move, the size can
        vanish, the market can close, or the kill switch can be thrown. If the
        book has moved the order is re-priced at the smaller size that still
        clears the edge bar — the engine never chases a price it has already
        rejected.
        """
        source = next((s for s in self.sources if s.name == market.venue), None)
        quote = None
        if source is not None:
            try:
                quote = source.fetch_quotes([market]).get(market.key)
            except VenueError as exc:
                return None, quote, f"could not re-quote before sending: {exc}"
        if quote is None:
            return None, quote, "no fresh book at submission time"

        if kill_switch_engaged(self.config):
            return None, quote, "kill switch engaged during the cycle"
        if self.treasury.is_dormant():
            return None, quote, "system went dormant during the cycle"
        if market.key in held:
            return None, quote, "already holding this market"

        verification = verify(market, self.config.verification)
        if not verification.ok:
            return None, quote, f"contract no longer verified: {verification.reason()}"

        minutes = _minutes_to_close(market)
        if minutes is not None and minutes < self.config.risk.min_minutes_to_close:
            return None, quote, "market too close to closing by submission time"

        revised = decide(
            cycle_id=decision.cycle_id,
            market=market,
            quote=quote,
            estimate=estimate,
            config=self.config,
            fee_model=self.fee_book.model(market.venue),
            exposure=exposure,
            verification=verification,
        )
        if not revised.is_trade:
            return None, quote, f"edge gone on the fresh book: {revised.reasons[-1]}"
        if revised.contracts < decision.contracts:
            revised.reasons.append(
                f"resized {decision.contracts} -> {revised.contracts} on the fresh book"
            )
        if revised.limit_price is not None and decision.limit_price is not None:
            # Never pay more than the price the original decision cleared.
            revised.limit_price = min(revised.limit_price, decision.limit_price)
        return revised, quote, ""

    # ------------------------------------------------------------ settling

    def settle(self, outcomes: Sequence[Outcome]) -> dict[str, float]:
        """Record results, then grade the sources that spoke about them."""
        from .signals.registry import update_source_scores

        for outcome in outcomes:
            self.log.record_outcome(outcome)
        update_source_scores(self.log)
        trades = self.log.settled_trades()
        realized = sum(t["pnl"] for t in trades)
        swept = self.treasury.sweep_realized_profit(realized)
        return {"realized_pnl": realized, "swept_to_reserve": swept, "trades": len(trades)}


def fee_schedule_path(db_path: str) -> str:
    """Fee-schedule overrides live beside the audit database."""
    from pathlib import Path

    return str(Path(db_path).parent / "fee_schedule.json")
