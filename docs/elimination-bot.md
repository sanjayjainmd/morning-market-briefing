# Elimination-market mispricing engine

An autonomous, shadow-mode-first trading system for reality-TV elimination
markets on Kalshi and Polymarket. It runs unattended on a timer, forms its own
probability estimates from **public information only**, compares them to live
prices net of every cost, and records everything it saw and did.

It does not place real orders. That is deliberate, and it is the single most
important design decision in the package — see [Live trading](#live-trading).

## What one cycle does

```
discover -> quote -> collect public signals -> estimate -> price the edge
         -> apply the risk policy -> shadow-execute -> record
```

| Stage | Module | What happens |
|---|---|---|
| Discover | `venues/kalshi.py`, `venues/polymarket.py` | Open markets whose titles read as "who goes home", not "who wins" |
| Quote | same | Full order book per market, normalised to probabilities |
| Signals | `signals/` | Public research + cross-venue divergence, each with a citable URL |
| Estimate | `probability.py` | Market price is the prior; weighted signals shift it in log-odds; the episode's legs are renormalised to sum to one elimination |
| Edge | `edge.py` | Walk the book for the intended size, add exchange fees, subtract a safety margin |
| Policy | `policy.py` | Twelve gates; a pass is recorded with its reason |
| Size | `sizing.py` | Quarter Kelly, then per-market / per-episode / per-cycle / gross / liquidity caps |
| Execute | `broker.py` | Paper fills against the captured book, with an adverse-selection haircut |
| Record | `storage.py` | SQLite, append-only, exportable as JSONL |

## Information policy

Only lawful public information reaches the model. This is enforced in code,
not in a comment: every signal carries an `access` field, and
`signals/base.py:PublicInfoPolicy` drops anything that is not `public` or
`licensed`, or that arrives without a citable URL. Dropped signals are
recorded as dropped — the audit log shows both that the information was
offered and that it was refused.

What that rules out: insider information, confidential or NDA-covered
material, hacked or stolen material, and paywalled material used without
permission. Kalshi prohibits trading on material non-public information and
screens for it. What it leaves in is the interesting part anyway: publicly
posted spoilers, open fan polls, social sentiment, press interviews, previews,
edit analysis, and the venues' own public order flow.

Public research is hand-entered as structured JSON (see
`tests/fixtures/example_research.json`) rather than scraped indiscriminately,
so the provenance of every input is legible and a collector cannot quietly
start ingesting something it should not.

## The source-reliability database

`signals/registry.py` grades every signal against the outcome of the market it
spoke about, scoring the probability the source *implied* relative to the
market prior at the time. Those Brier scores set the weights in
`probability.source_weight`:

* an unproven source carries weight 0.1 and barely moves a price;
* a source no better than a coin flip (Brier ≥ 0.25) is driven to zero;
* weight grows with a shrinkage factor `n / (n + 20)`, so it has to earn trust
  across many episodes rather than one lucky call.

This is the asset worth building. A measured record of which public spoiler
sources actually predict, and how fast markets absorb them, is more durable
than any single model.

## Risk policy

Defaults in `config/elimination_bot.example.json`:

| Gate | Default | Why |
|---|---|---|
| `min_net_edge` | 0.10 | Ten points after fees, spread, slippage and margin |
| `safety_margin` | 0.02 | The model is an estimate, not the truth |
| `max_spread` | 0.06 | No wide, illiquid books |
| `min_top_of_book_size` / `min_depth_contracts` | 25 / 100 | Real liquidity only |
| `max_book_participation` | 0.25 | Never take more than a quarter of visible depth |
| `kelly_fraction` | 0.25 | Quarter Kelly or less |
| `max_fraction_per_market` | 0.02 | 2% of bankroll per contestant |
| `max_fraction_per_group` | 0.05 | 5% per episode |
| `max_fraction_per_cycle` | 0.05 | 5% per 25-minute cycle |
| `max_gross_exposure` | 0.30 | 30% at risk in total |
| `min_minutes_to_close` | 20 | No lottery tickets at the bell |
| `max_days_to_close` | 21 | No capital parked for a month |
| `max_drawdown` | 0.20 | Stop trading after −20% |

Two kill paths, both files on disk because that is the mechanism least likely
to fail: `data/KILL_SWITCH` stops trading immediately, `data/DORMANT` marks the
system paused.

## Funding, and what happens when the bill goes unpaid

Hosting money lives in a prepaid operating reserve — twelve months by default,
warning at six — and is topped up **only from realized profit**, never from
trading margin (`funding.py`). Settlement and withdrawal delays make week-to-
week self-funding unreliable, so the reserve absorbs the timing mismatch.

If the reserve runs out the system goes **dormant**: cancel resting orders,
stop placing new ones, write a marker file explaining why, and preserve the
database, the logs and the code exactly as they are. Nothing is deleted.
A system that destroys its own audit trail when a card declines is unauditable
by design, and creates security, accounting and debugging problems precisely
when you most need the records. `cli.py resume` lifts dormancy.

## Live trading

`broker.LiveBroker` raises `LiveTradingDisabled` on every order. Wiring a
venue's signed order endpoint is a small amount of code and a large amount of
responsibility; it belongs to whoever has read a full shadow season's results,
and it should be written and tested against a funded account, not shipped
ahead of the evidence.

Both venues do support programmatic order placement, and the interface it
would implement is already defined (`broker.Broker`). Nothing about the rest
of the system changes when that day comes: the same decisions, the same caps,
the same log.

Note also what autonomy does and does not mean here. The exchange account, the
billing relationship and the legal responsibility remain the operator's. The
bot is autonomous operationally, not legally independent, and eligibility and
tax treatment are the operator's to check in their own jurisdiction.

## Shadow protocol and the go-live bar

Run at least one complete season in shadow mode. `evaluate.readiness_report`
measures the bar and reports each criterion with its measured value:

1. ≥ 150 independent trade opportunities (100–200 is the working range)
2. Positive total PnL after fees and realistic slippage
3. Profitable across at least two shows or seasons
4. Expected calibration error ≤ 0.10 — "70%" predictions win about 70% of the time
5. The largest single trade is ≤ 35% of total PnL
6. No single contestant is > 50% of positive PnL
7. No single source touches > 50% of traded markets
8. Bootstrap 95% CI on mean PnL per trade excludes zero
9. The chronological holdout tail (last 30%, ≥ 20 trades) is profitable

`python -m elimination_bot.cli readiness` exits non-zero until all nine pass.

## Running it

```bash
pip install -r requirements-elimination-bot.txt
cp config/elimination_bot.example.json config/elimination_bot.json

# what is trading right now
python -m elimination_bot.cli --config config/elimination_bot.json discover

# one shadow cycle — this is what the timer calls
python -m elimination_bot.cli --config config/elimination_bot.json cycle -v

# after an episode airs
python -m elimination_bot.cli --config config/elimination_bot.json settle outcomes.json
python -m elimination_bot.cli --config config/elimination_bot.json sources --regrade
python -m elimination_bot.cli --config config/elimination_bot.json readiness

# operations
python -m elimination_bot.cli --config config/elimination_bot.json status
python -m elimination_bot.cli --config config/elimination_bot.json fund --deposit 480
python -m elimination_bot.cli --config config/elimination_bot.json pause --reason "manual"
python -m elimination_bot.cli --config config/elimination_bot.json export --out shadow/export
```

Offline, add `--fixture tests/fixtures/example_episode.json` to any command to
run the whole pipeline against a recorded episode with no network access.

`outcomes.json` is `{"outcomes": [{"market_key": "kalshi:ELIM-...", "subject":
"Alex", "eliminated": true}]}`.

Every 25 minutes, via `.github/workflows/elimination-shadow.yml` (scheduling
commented out by default — a 25-minute cron commits to the repo ~57 times a
day) or a cron line on any small host:

```
*/25 * * * * cd /srv/bot && python -m elimination_bot.cli --config config/elimination_bot.json cycle >> logs/cycle.log 2>&1
```

## Tests

```bash
python -m unittest discover -s tests
```

No third-party test dependency; `requests` is the only runtime one, and the
fixture venue means the suite never touches the network.

## Honest expectations

The engine is straightforward to operate. Whether it makes money is a
different question, and the honest prior before seeing data is not encouraging:
occasional profitable trades are likely, net profitability over one season is
maybe a coin flip, sustained profitability across several seasons is unlikely,
and reliably paying its own expenses from weekly profits is less likely still.

The reasons are structural, not fixable by better code:

* Fan predictions and public spoilers are often already in the price.
* Public spoilers are frequently wrong, deliberately misleading, or the same
  single unreliable claim echoed by a dozen accounts — correlated sources look
  like confirmation and are not.
* Elimination markets are small: wide spreads, thin books, and not enough
  capacity to turn even a real edge into real money.
* A good probability model still loses through timing and sizing.
* One genuine spoiler can make a season look successful without a repeatable
  strategy behind it.
* Any public source that proves predictive gets priced in by others quickly.

Treat this as a measurement instrument first and a trading system second. The
readiness report exists to make "it worked" a claim with evidence behind it,
and to make staying in shadow mode the default outcome rather than a failure.
