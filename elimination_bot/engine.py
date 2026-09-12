"""One 25-minute cycle, start to finish.

    discover -> quote -> collect public signals -> estimate -> price the edge
    -> apply the risk policy -> (shadow) execute -> record everything

Nothing here asks for approval, and nothing here can place a real order: the
autonomy is in the loop, the restraint is in ``broker.LiveBroker``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .config import BotConfig
from .edge import fee_model_for
from .funding import FundingStatus, Treasury, kill_switch_engaged
from .models import Action, Decision, Fill, Market, Outcome, Quote, utcnow
from .policy import ExposureState, decide
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
    signals_kept: int = 0
    signals_dropped: list[dict] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    halted: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def trades(self) -> list[Decision]:
        return [d for d in self.decisions if d.is_trade]

    def summary(self) -> str:
        if self.halted:
            return f"cycle {self.cycle_id}: HALTED — {self.halted}"
        return (
            f"cycle {self.cycle_id}: {self.markets_seen} markets, "
            f"{self.quotes_captured} books, {self.signals_kept} signals "
            f"({len(self.signals_dropped)} dropped), {len(self.trades)} trades, "
            f"{len(self.fills)} fills"
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
    ) -> None:
        self.config = config
        self.log = log
        self.sources = list(sources)
        self.signal_sources = list(signal_sources)
        self.broker = broker
        self.info_policy = info_policy or PublicInfoPolicy()
        self.treasury = Treasury(config, log)

    # ------------------------------------------------------------- guards

    def halt_reason(self) -> str | None:
        if kill_switch_engaged(self.config):
            return f"kill switch present at {self.config.kill_switch_path}"
        if self.treasury.is_dormant():
            return f"dormant: see {self.config.dormant_path}"
        funding = self.treasury.state()
        if funding.status is FundingStatus.UNFUNDED:
            return f"unfunded: {funding.message}"
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

        markets = self._discover(report)
        quotes = self._quote(markets, report, cycle_id)
        tradeable = [m for m in markets if m.key in quotes]
        for market in tradeable:
            self.log.record_market(market)

        signals = self._collect_signals(tradeable, quotes, report, cycle_id)
        estimates = self._estimate(tradeable, quotes, signals, cycle_id)
        self._decide_and_execute(tradeable, quotes, estimates, report, cycle_id)

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
    ) -> dict[str, object]:
        scores = self.log.source_scores()
        estimates: dict[str, object] = {}
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
                source_scores=scores,
            )
            estimates[market.key] = estimate
            groups.setdefault(f"{market.venue}:{market.group_id}", []).append(estimate)

        if self.config.model.normalize_field:
            for legs in groups.values():
                normalize_field(legs, self.config.model.eliminations_per_group)

        for estimate in estimates.values():
            self.log.record_estimate(estimate, cycle_id)  # type: ignore[arg-type]
        return estimates

    def _decide_and_execute(
        self,
        markets: Sequence[Market],
        quotes: dict[str, Quote],
        estimates: dict[str, object],
        report: CycleReport,
        cycle_id: str,
    ) -> None:
        exposure = ExposureState()
        for market_key, position in self.log.open_positions().items():
            exposure.gross += float(position["cost"])
        exposure.cycle = 0.0

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
                estimate=estimate,  # type: ignore[arg-type]
                config=self.config,
                fee_model=fee_model_for(market.venue, self.config.fees),
                exposure=exposure,
            )
            self.log.record_decision(decision, quotes[market.key].market_prob)
            report.decisions.append(decision)
            if not decision.is_trade:
                continue
            order, fill = self.broker.place(decision, quotes[market.key])
            if fill is not None:
                report.fills.append(fill)
                exposure.add(market.group_id, fill.cost)

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
