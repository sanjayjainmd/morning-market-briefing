"""Elimination-market mispricing engine.

A shadow-mode-first research system for reality-TV elimination markets on
Kalshi and Polymarket. It estimates elimination probabilities from *public*
information only, compares them to market prices net of fees/spread/slippage,
and records every signal, decision, order, fill and outcome for calibration.

Live order placement is disabled by design: see ``elimination_bot.broker``.
"""

__version__ = "0.1.0"
