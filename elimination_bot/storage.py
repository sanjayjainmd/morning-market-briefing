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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import (
    Decision,
    Estimate,
    Fill,
    Market,
    Order,
    Outcome,
    Quote,
    Signal,
    dumps,
    to_jsonable,
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

    def open_positions(self) -> dict[str, dict[str, Any]]:
        """Net filled position per market, keyed by market, with cost basis."""
        rows = self.conn.execute(
            "SELECT f.market_key, o.action, SUM(f.contracts) AS contracts,"
            " SUM(f.contracts * f.price + f.fees) AS cost"
            " FROM fills f JOIN orders o ON o.order_id = f.order_id"
            " LEFT JOIN outcomes s ON s.market_key = f.market_key"
            " WHERE s.market_key IS NULL"
            " GROUP BY f.market_key, o.action",
            (),
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = out.setdefault(row["market_key"], {"contracts": 0, "cost": 0.0, "legs": {}})
            entry["contracts"] += row["contracts"]
            entry["cost"] += row["cost"]
            entry["legs"][row["action"]] = row["contracts"]
        return out

    def settled_trades(self) -> list[dict[str, Any]]:
        """Every filled trade that has a settled outcome, with realized PnL."""
        rows = self.conn.execute(
            "SELECT f.market_key, f.contracts, f.price, f.fees, f.filled_at,"
            " o.action, o.cycle_id, m.show, m.group_id, m.subject,"
            " s.eliminated, d.model_prob, d.market_prob, d.net_edge"
            " FROM fills f"
            " JOIN orders o ON o.order_id = f.order_id"
            " JOIN outcomes s ON s.market_key = f.market_key"
            " LEFT JOIN markets m ON m.market_key = f.market_key"
            " LEFT JOIN decisions d ON d.cycle_id = o.cycle_id"
            "   AND d.market_key = f.market_key"
            " ORDER BY f.filled_at"
        ).fetchall()
        trades = []
        for row in rows:
            won = bool(row["eliminated"]) if row["action"] == "buy_yes" else not bool(row["eliminated"])
            payout = float(row["contracts"]) if won else 0.0
            cost = row["contracts"] * row["price"] + row["fees"]
            trades.append(
                {
                    "market_key": row["market_key"],
                    "show": row["show"],
                    "group_id": row["group_id"],
                    "subject": row["subject"],
                    "action": row["action"],
                    "contracts": row["contracts"],
                    "price": row["price"],
                    "fees": row["fees"],
                    "filled_at": row["filled_at"],
                    "won": won,
                    "pnl": payout - cost,
                    "model_prob": row["model_prob"],
                    "market_prob": row["market_prob"],
                    "net_edge": row["net_edge"],
                }
            )
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
