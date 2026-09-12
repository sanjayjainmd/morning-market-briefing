"""Append-only audit log in SQLite.

Every signal, estimate, decision, order, fill and settlement is written here
before anything else happens. The log is the deliverable: without it there is
no calibration, no source-reliability database, and no way to tell a real edge
from one lucky spoiler.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Action,
    Decision,
    Estimate,
    Fill,
    Market,
    Order,
    Outcome,
    Position,
    Quote,
    Signal,
    dumps,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    bankroll REAL NOT NULL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS markets (
    market_key TEXT PRIMARY KEY,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    show TEXT,
    subject TEXT,
    title TEXT,
    close_time TEXT,
    url TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    market_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    best_bid REAL, best_ask REAL, mid REAL,
    spread REAL, depth_yes INTEGER, depth_no INTEGER,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    source_id TEXT NOT NULL,
    market_key TEXT NOT NULL,
    subject TEXT,
    observed_at TEXT NOT NULL,
    lean REAL NOT NULL,
    confidence REAL NOT NULL,
    access TEXT NOT NULL,
    url TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS estimates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    market_key TEXT NOT NULL,
    subject TEXT,
    prob REAL NOT NULL,
    prior_prob REAL NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    market_key TEXT NOT NULL,
    subject TEXT,
    action TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    limit_price REAL,
    net_edge REAL,
    model_prob REAL,
    market_prob REAL,
    decided_at TEXT NOT NULL,
    reasons TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    cycle_id TEXT,
    market_key TEXT NOT NULL,
    action TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    limit_price REAL NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    placed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    market_key TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL,
    filled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    market_key TEXT PRIMARY KEY,
    subject TEXT,
    eliminated INTEGER NOT NULL,
    settled_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT,
    market_key TEXT NOT NULL,
    ok INTEGER NOT NULL,
    episode TEXT,
    resolution_source TEXT,
    issues TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_scores (
    source_id TEXT PRIMARY KEY,
    observations INTEGER NOT NULL DEFAULT 0,
    brier_sum REAL NOT NULL DEFAULT 0.0,
    log_loss_sum REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS funding_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount REAL,
    reserve_after REAL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_quotes_market ON quotes(market_key, observed_at);
CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_key, observed_at);
CREATE INDEX IF NOT EXISTS idx_decisions_cycle ON decisions(cycle_id);
CREATE INDEX IF NOT EXISTS idx_fills_market ON fills(market_key);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class AuditLog:
    """Thin, synchronous SQLite wrapper. One writer, many readers."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- writes

    def start_cycle(self, mode: str, bankroll: float, notes: str = "") -> str:
        cycle_id = new_id("cyc")
        self.conn.execute(
            "INSERT INTO cycles (cycle_id, started_at, mode, bankroll, notes)"
            " VALUES (?,?,?,?,?)",
            (cycle_id, utcnow().isoformat(), mode, bankroll, notes),
        )
        self.conn.commit()
        return cycle_id

    def finish_cycle(self, cycle_id: str, notes: str = "") -> None:
        self.conn.execute(
            "UPDATE cycles SET finished_at=?, notes=COALESCE(NULLIF(?,''), notes)"
            " WHERE cycle_id=?",
            (utcnow().isoformat(), notes, cycle_id),
        )
        self.conn.commit()

    def record_market(self, market: Market) -> None:
        now = utcnow().isoformat()
        close = market.close_time.isoformat() if market.close_time else None
        self.conn.execute(
            "INSERT INTO markets (market_key, venue, market_id, group_id, show,"
            " subject, title, close_time, url, first_seen, last_seen, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(market_key) DO UPDATE SET last_seen=excluded.last_seen,"
            " payload=excluded.payload, close_time=excluded.close_time",
            (
                market.key, market.venue, market.market_id, market.group_id,
                market.show, market.subject, market.title, close, market.url,
                now, now, dumps(market),
            ),
        )
        self.conn.commit()

    def record_quote(self, quote: Quote, cycle_id: str | None = None) -> None:
        book = quote.book
        self.conn.execute(
            "INSERT INTO quotes (cycle_id, market_key, observed_at, best_bid,"
            " best_ask, mid, spread, depth_yes, depth_no, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                cycle_id, quote.market_key, quote.observed_at.isoformat(),
                book.best_bid, book.best_ask, book.mid, book.spread,
                sum(l.size for l in book.asks), sum(l.size for l in book.bids),
                dumps(quote),
            ),
        )
        self.conn.commit()

    def record_signals(self, signals: Iterable[Signal], cycle_id: str | None = None) -> int:
        rows = [
            (
                cycle_id, s.source_id, s.market_key, s.subject,
                s.observed_at.isoformat(), s.lean, s.confidence, s.access.value,
                s.url, dumps(s),
            )
            for s in signals
        ]
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO signals (cycle_id, source_id, market_key, subject,"
            " observed_at, lean, confidence, access, url, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def record_estimate(self, estimate: Estimate, cycle_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO estimates (cycle_id, market_key, subject, prob,"
            " prior_prob, created_at, payload) VALUES (?,?,?,?,?,?,?)",
            (
                cycle_id, estimate.market_key, estimate.subject, estimate.prob,
                estimate.prior_prob, utcnow().isoformat(), dumps(estimate),
            ),
        )
        self.conn.commit()

    def record_decision(self, decision: Decision, market_prob: float | None = None) -> None:
        edge = decision.edge
        self.conn.execute(
            "INSERT INTO decisions (cycle_id, market_key, subject, action,"
            " contracts, limit_price, net_edge, model_prob, market_prob,"
            " decided_at, reasons, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision.cycle_id, decision.market_key, decision.subject,
                decision.action.value, decision.contracts, decision.limit_price,
                edge.net_edge if edge else None,
                decision.estimate.prob if decision.estimate else None,
                market_prob, decision.decided_at.isoformat(),
                json.dumps(decision.reasons), dumps(decision),
            ),
        )
        self.conn.commit()

    def record_order(self, order: Order) -> None:
        self.conn.execute(
            "INSERT INTO orders (order_id, cycle_id, market_key, action,"
            " contracts, limit_price, mode, status, placed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(order_id) DO UPDATE SET status=excluded.status",
            (
                order.order_id, order.cycle_id, order.market_key,
                order.action.value, order.contracts, order.limit_price,
                order.mode, order.status, order.placed_at.isoformat(),
            ),
        )
        self.conn.commit()

    def record_fill(self, fill: Fill) -> None:
        self.conn.execute(
            "INSERT INTO fills (order_id, market_key, contracts, price, fees,"
            " filled_at) VALUES (?,?,?,?,?,?)",
            (
                fill.order_id, fill.market_key, fill.contracts, fill.price,
                fill.fees, fill.filled_at.isoformat(),
            ),
        )
        self.conn.commit()

    def record_outcome(self, outcome: Outcome) -> None:
        self.conn.execute(
            "INSERT INTO outcomes (market_key, subject, eliminated, settled_at, note)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(market_key) DO UPDATE SET eliminated=excluded.eliminated,"
            " settled_at=excluded.settled_at, note=excluded.note",
            (
                outcome.market_key, outcome.subject, int(outcome.eliminated),
                outcome.settled_at.isoformat(), outcome.note,
            ),
        )
        self.conn.commit()

    def record_verification(self, verification, cycle_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO verifications (cycle_id, market_key, ok, episode,"
            " resolution_source, issues, checked_at) VALUES (?,?,?,?,?,?,?)",
            (
                cycle_id, verification.market_key, int(verification.ok),
                verification.episode, verification.resolution_source,
                json.dumps(verification.issues), utcnow().isoformat(),
            ),
        )
        self.conn.commit()

    def record_funding_event(
        self, kind: str, amount: float | None, reserve_after: float | None, note: str = ""
    ) -> None:
        self.conn.execute(
            "INSERT INTO funding_events (at, kind, amount, reserve_after, note)"
            " VALUES (?,?,?,?,?)",
            (utcnow().isoformat(), kind, amount, reserve_after, note),
        )
        self.conn.commit()

    def update_source_score(self, source_id: str, brier: float, log_loss: float) -> None:
        self.conn.execute(
            "INSERT INTO source_scores (source_id, observations, brier_sum,"
            " log_loss_sum, updated_at) VALUES (?,1,?,?,?)"
            " ON CONFLICT(source_id) DO UPDATE SET"
            " observations=source_scores.observations+1,"
            " brier_sum=source_scores.brier_sum+excluded.brier_sum,"
            " log_loss_sum=source_scores.log_loss_sum+excluded.log_loss_sum,"
            " updated_at=excluded.updated_at",
            (source_id, brier, log_loss, utcnow().isoformat()),
        )
        self.conn.commit()

    # ----------------------------------------------------------------- reads

    def entry_model_prob(self, market_key: str) -> float | None:
        """The probability that justified opening this position."""
        row = self.conn.execute(
            "SELECT model_prob FROM decisions WHERE market_key=?"
            " AND action IN ('buy_yes','buy_no') AND model_prob IS NOT NULL"
            " ORDER BY id LIMIT 1",
            (market_key,),
        ).fetchone()
        return float(row["model_prob"]) if row else None

    def source_scores(self) -> dict[str, dict[str, float]]:
        rows = self.conn.execute("SELECT * FROM source_scores").fetchall()
        out: dict[str, dict[str, float]] = {}
        for row in rows:
            n = row["observations"] or 0
            out[row["source_id"]] = {
                "observations": n,
                "brier": (row["brier_sum"] / n) if n else None,
                "log_loss": (row["log_loss_sum"] / n) if n else None,
            }
        return out

    def signals_for(self, market_key: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM signals WHERE market_key=? ORDER BY observed_at",
            (market_key,),
        ).fetchall()
        return [dict(r) for r in rows]

    def rounds(self) -> list[Position]:
        """Every round of exposure, in order, one per market entry-to-flat.

        A market can be entered, exited and entered again. Each of those is a
        separate opportunity with its own cost basis, so the ledger closes a
        round the moment the position goes flat rather than blending the next
        entry into the last one's average price.
        """
        rows = self.conn.execute(
            "SELECT f.market_key, f.contracts, f.price, f.fees, f.filled_at,"
            " o.action, m.show, m.subject, m.group_id"
            " FROM fills f JOIN orders o ON o.order_id = f.order_id"
            " LEFT JOIN markets m ON m.market_key = f.market_key"
            " ORDER BY f.filled_at, f.id"
        ).fetchall()

        completed: list[Position] = []
        current: dict[str, Position] = {}
        counts: dict[str, int] = {}

        for row in rows:
            key = row["market_key"]
            position = current.get(key)
            if position is None:
                position = Position(
                    market_key=key,
                    subject=row["subject"] or "",
                    show=row["show"] or "",
                    group_id=row["group_id"] or "",
                    round_index=counts.get(key, 0),
                    first_filled_at=row["filled_at"],
                )
                current[key] = position
            position.last_filled_at = row["filled_at"]
            position.fees += row["fees"]
            action = Action(row["action"])
            contracts = int(row["contracts"])
            if action.is_buy:
                if action is Action.BUY_YES:
                    position.yes_open += contracts
                else:
                    position.no_open += contracts
                position.contracts_bought += contracts
                position.cost += contracts * row["price"] + row["fees"]
            else:
                if action is Action.SELL_YES:
                    position.yes_open -= contracts
                else:
                    position.no_open -= contracts
                position.proceeds += contracts * row["price"] - row["fees"]

            position.entry_vwap = (
                position.cost / position.contracts_bought
                if position.contracts_bought
                else 0.0
            )
            if position.contracts_bought and not position.is_open:
                completed.append(position)
                counts[key] = position.round_index + 1
                del current[key]

        return completed + [p for p in current.values() if p.contracts_bought]

    def ledger(self) -> dict[str, Position]:
        """The current open round per market, keyed by market."""
        return {p.market_key: p for p in self.rounds() if p.is_open}

    def open_positions(self) -> dict[str, dict[str, Any]]:
        """Unsettled markets where contracts are still held."""
        settled = {
            row["market_key"]
            for row in self.conn.execute("SELECT market_key FROM outcomes")
        }
        out: dict[str, dict[str, Any]] = {}
        for key, position in self.ledger().items():
            if key in settled or not position.is_open:
                continue
            out[key] = {
                "contracts": position.open_contracts,
                "cost": position.open_cost,
                "legs": {
                    "buy_yes": position.yes_open,
                    "buy_no": position.no_open,
                },
                "entry_vwap": position.entry_vwap,
                "position": position,
            }
        return out

    def settled_trades(self) -> list[dict[str, Any]]:
        """One row per completed market position — the unit of independence.

        A position counts as complete when the market has settled or when
        every contract has been sold back. Realized PnL is
        ``proceeds + settlement payout - cost``, so a position exited early
        is graded on what it actually earned, not on how the episode ended.
        """
        outcomes = {
            row["market_key"]: bool(row["eliminated"])
            for row in self.conn.execute("SELECT market_key, eliminated FROM outcomes")
        }
        decisions = {
            row["market_key"]: row
            for row in self.conn.execute(
                "SELECT market_key, model_prob, market_prob, net_edge FROM decisions"
                " WHERE action IN ('buy_yes','buy_no') ORDER BY id"
            )
        }

        trades: list[dict[str, Any]] = []
        for position in self.rounds():
            key = position.market_key
            eliminated = outcomes.get(key)
            if eliminated is None and position.is_open:
                continue  # still live: not yet a graded opportunity
            payout = 0.0
            if eliminated is not None:
                payout = position.yes_open * (1.0 if eliminated else 0.0)
                payout += position.no_open * (0.0 if eliminated else 1.0)
            pnl = position.proceeds + payout - position.cost
            decision = decisions.get(key)
            trades.append(
                {
                    "market_key": key,
                    "round": position.round_index,
                    "show": position.show,
                    "group_id": position.group_id,
                    "subject": position.subject,
                    "action": (position.action.value if position.action else "closed"),
                    "contracts": position.contracts_bought,
                    "price": position.entry_vwap,
                    "fees": position.fees,
                    "filled_at": position.first_filled_at,
                    "closed_at": position.last_filled_at,
                    "exited_early": not position.is_open and eliminated is None,
                    "settled": eliminated is not None,
                    "won": pnl > 0,
                    "pnl": pnl,
                    "model_prob": decision["model_prob"] if decision else None,
                    "market_prob": decision["market_prob"] if decision else None,
                    "net_edge": decision["net_edge"] if decision else None,
                }
            )
        trades.sort(key=lambda t: t["filled_at"] or "")
        return trades

    def graded_estimates(self) -> list[dict[str, Any]]:
        """Latest estimate per market for markets that have settled."""
        rows = self.conn.execute(
            "SELECT e.market_key, e.subject, e.prob, e.prior_prob, e.created_at,"
            " s.eliminated, m.show FROM estimates e"
            " JOIN outcomes s ON s.market_key = e.market_key"
            " LEFT JOIN markets m ON m.market_key = e.market_key"
            " WHERE e.id IN (SELECT MAX(id) FROM estimates GROUP BY market_key)"
            " ORDER BY e.created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def export_jsonl(self, out_dir: str | Path) -> list[Path]:
        """Dump every table as JSONL — the portable form of the audit log."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        tables = [r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        written = []
        for table in sorted(tables):
            path = out / f"{table}.jsonl"
            with path.open("w", encoding="utf-8") as fh:
                for row in self.conn.execute(f"SELECT * FROM {table}"):
                    fh.write(json.dumps(dict(row), sort_keys=True) + "\n")
            written.append(path)
        return written

    def counts(self) -> dict[str, int]:
        tables = [r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        return {
            t: self.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in sorted(tables)
        }
