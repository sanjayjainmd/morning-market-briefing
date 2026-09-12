"""Command line: ``python -m elimination_bot.cli <command>``.

    discover    list the elimination markets the venues are showing
    cycle       run one full shadow cycle (the thing a cron calls)
    settle      record outcomes from a JSON file and grade the sources
    report      recent decisions, fills and open positions
    readiness   the go-live criteria, measured against the log
    sources     the source-reliability table
    status      funding, dormancy, kill switch, drawdown
    fund        deposit / pay hosting
    pause       enter dormancy (cancel orders, stop trading, keep everything)
    resume      leave dormancy
    export      dump the audit log as JSONL
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .broker import build_broker
from .config import BotConfig
from .edge import fee_model_for
from .engine import Engine
from .evaluate import readiness_report, render_json
from .funding import Treasury, kill_switch_engaged
from .models import Outcome, utcnow
from .signals import MicrostructureSource, PublicResearchSource
from .signals.base import PublicInfoPolicy
from .signals.registry import update_source_scores
from .storage import AuditLog
from .venues import FixtureVenue, build_sources


def build_engine(config: BotConfig, log: AuditLog, fixture: str | None = None) -> Engine:
    if fixture:
        sources = [FixtureVenue(path=fixture)]
    else:
        sources = build_sources(config.execution.venues, config.execution.request_timeout)
    signal_sources = [
        PublicResearchSource(config.research_path),
        MicrostructureSource(),
    ]
    broker = build_broker(config, log, fee_model_for("kalshi", config.fees))
    return Engine(config, log, sources, signal_sources, broker, PublicInfoPolicy())


def cmd_discover(args, config: BotConfig, log: AuditLog) -> int:
    engine = build_engine(config, log, args.fixture)
    markets = []
    for source in engine.sources:
        markets.extend(source.discover(config.shows, limit=config.execution.max_markets_per_cycle))
    for market in markets:
        close = market.close_time.isoformat() if market.close_time else "?"
        print(f"{market.key:<48} {market.show[:22]:<24} {market.subject[:28]:<30} closes {close}")
    print(f"\n{len(markets)} elimination markets")
    return 0


def cmd_cycle(args, config: BotConfig, log: AuditLog) -> int:
    engine = build_engine(config, log, args.fixture)
    report = engine.run_cycle()
    print(report.summary())
    for error in report.errors:
        print(f"  error: {error}", file=sys.stderr)
    for dropped in report.signals_dropped:
        print(f"  dropped {dropped['source_id']}: {dropped['reason']}")
    for decision in report.decisions:
        if decision.is_trade or args.verbose:
            mark = "TRADE" if decision.is_trade else "pass "
            print(f"  {mark} {decision.subject[:24]:<26} {decision.action.value:<8} "
                  f"{decision.contracts:>5} @ {decision.limit_price or 0:.2f}  "
                  f"{'; '.join(decision.reasons)}")
    if report.halted:
        return 2
    return 0


def cmd_settle(args, config: BotConfig, log: AuditLog) -> int:
    payload = json.loads(Path(args.outcomes).read_text(encoding="utf-8"))
    entries = payload.get("outcomes", payload) if isinstance(payload, dict) else payload
    outcomes = [
        Outcome(
            market_key=entry["market_key"],
            subject=entry.get("subject", ""),
            eliminated=bool(entry["eliminated"]),
            note=entry.get("note", ""),
        )
        for entry in entries
    ]
    engine = build_engine(config, log, args.fixture)
    result = engine.settle(outcomes)
    print(json.dumps(result, indent=2))
    return 0


def cmd_report(args, config: BotConfig, log: AuditLog) -> int:
    rows = log.conn.execute(
        "SELECT decided_at, market_key, subject, action, contracts, limit_price,"
        " net_edge, model_prob, market_prob, reasons FROM decisions"
        " ORDER BY id DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    for row in rows:
        reasons = "; ".join(json.loads(row["reasons"]))
        print(
            f"{row['decided_at'][:19]}  {row['subject'] or row['market_key']:<28}"
            f" {row['action']:<8} {row['contracts']:>5}  {reasons}"
        )
    positions = log.open_positions()
    if positions:
        print("\nOpen positions (unsettled):")
        for key, pos in positions.items():
            print(f"  {key:<44} {pos['contracts']:>5} contracts  cost ${pos['cost']:.2f}")
    print("\n" + json.dumps(log.counts(), indent=2))
    return 0


def cmd_readiness(args, config: BotConfig, log: AuditLog) -> int:
    report = readiness_report(log, min_opportunities=args.min_opportunities)
    print(render_json(report) if args.json else report.render())
    return 0 if report.ready else 1


def cmd_sources(args, config: BotConfig, log: AuditLog) -> int:
    scores = update_source_scores(log) if args.regrade else log.source_scores()
    if not scores:
        print("no graded sources yet — settle some markets first")
        return 0
    print(f"{'source':<32} {'n':>5} {'brier':>8} {'log loss':>9}")
    for source_id, score in sorted(scores.items(), key=lambda kv: kv[1]["brier"] or 1):
        print(
            f"{source_id:<32} {score['observations']:>5}"
            f" {score['brier']:>8.4f} {score['log_loss']:>9.4f}"
        )
    return 0


def cmd_status(args, config: BotConfig, log: AuditLog) -> int:
    treasury = Treasury(config, log)
    state = treasury.state()
    engine = None
    drawdown = None
    try:
        engine = build_engine(config, log, args.fixture)
        drawdown = engine.drawdown()
    except Exception as exc:  # venue construction can fail offline
        print(f"(engine unavailable: {exc})")
    print(json.dumps(
        {
            "mode": config.execution.mode,
            "bankroll": config.bankroll,
            "reserve": round(state.reserve, 2),
            "funding_status": state.status.value,
            "months_of_runway": round(state.months_of_runway, 2),
            "funding_note": state.message,
            "dormant": treasury.is_dormant(),
            "kill_switch": kill_switch_engaged(config),
            "drawdown": None if drawdown is None else round(drawdown, 4),
            "halt_reason": engine.halt_reason() if engine else None,
            "counts": log.counts(),
        },
        indent=2,
    ))
    return 0


def cmd_fund(args, config: BotConfig, log: AuditLog) -> int:
    treasury = Treasury(config, log)
    if args.deposit:
        state = treasury.deposit(args.deposit, args.note or "manual top-up")
    elif args.pay_hosting is not None:
        state = treasury.pay_hosting(args.pay_hosting or None, args.note or "")
    else:
        state = treasury.state()
    print(f"reserve ${state.reserve:.2f} — {state.status.value}: {state.message}")
    return 0


def cmd_pause(args, config: BotConfig, log: AuditLog) -> int:
    treasury = Treasury(config, log)
    treasury.enter_dormancy(args.reason or "manual pause")
    print(f"dormant. Logs preserved. Marker: {config.dormant_path}")
    return 0


def cmd_resume(args, config: BotConfig, log: AuditLog) -> int:
    treasury = Treasury(config, log)
    print("resumed" if treasury.resume() else "not dormant")
    return 0


def cmd_export(args, config: BotConfig, log: AuditLog) -> int:
    written = log.export_jsonl(args.out)
    for path in written:
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="elimination_bot", description=__doc__)
    parser.add_argument("--config", help="path to a JSON config file")
    parser.add_argument("--db", help="override the audit log path")
    parser.add_argument("--fixture", help="use a JSON fixture venue instead of the network")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="list open elimination markets")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("cycle", help="run one shadow cycle")
    p.add_argument("-v", "--verbose", action="store_true", help="show passes too")
    p.set_defaults(func=cmd_cycle)

    p = sub.add_parser("settle", help="record outcomes and regrade sources")
    p.add_argument("outcomes", help="JSON file of {market_key, eliminated} entries")
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser("report", help="recent decisions and positions")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("readiness", help="measure the go-live criteria")
    p.add_argument("--json", action="store_true")
    p.add_argument("--min-opportunities", type=int, default=150)
    p.set_defaults(func=cmd_readiness)

    p = sub.add_parser("sources", help="source reliability table")
    p.add_argument("--regrade", action="store_true", help="rescore from settled outcomes")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("status", help="funding, dormancy, drawdown")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("fund", help="deposit to or pay from the operating reserve")
    p.add_argument("--deposit", type=float)
    p.add_argument("--pay-hosting", type=float, nargs="?", const=0.0)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_fund)

    p = sub.add_parser("pause", help="enter dormancy (recoverable)")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="leave dormancy")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("export", help="dump the audit log as JSONL")
    p.add_argument("--out", default="data/export")
    p.set_defaults(func=cmd_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = BotConfig.load(args.config)
    if args.db:
        config.db_path = args.db
    with AuditLog(config.db_path) as log:
        return args.func(args, config, log)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
