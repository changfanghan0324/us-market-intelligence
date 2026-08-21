# Future upgrade roadmap

The current implementation preserves stable point-in-time data rather than
prematurely defining engines whose real requirements are not known yet. Future
modules consume canonical records and predictions; they never parse HTML.

## 1. Portfolio Tracking

Add a private, authenticated store for transactions, tax lots, cash, currencies,
corporate actions, and point-in-time holdings. Portfolio analytics can then map
each daily market event to user exposures, contribution, concentration, and risk.

Privacy boundary: no holdings, positions, accounts, or user identifiers may enter
the public Pages report, public record ledger, prompts, or logs.

## 2. Factor Analysis

Add versioned, point-in-time factor definitions for:

- Momentum
- Value
- Quality
- Growth
- Volatility

Inputs require data-vintage timestamps, corporate-action adjustment, universe
membership, winsorization/normalization rules, and protections against look-ahead
and survivorship bias. Store exposure and factor-definition versions with every
result.

## 3. AI Investment Journal

The current prediction records already preserve direction, horizon, confidence,
evidence, and falsification conditions. A journal module can add immutable thesis
events, automatically attribute why tracked stocks moved, and later link each
prediction to a deterministic correct/incorrect outcome.

The model may explain outcomes; it must not grade itself. Outcome rules and market
windows remain code-defined.

## 4. Historical Database and similarity search

Move the append-only sanitized records to a private object store/database when
volume or privacy requires it. Build searchable indexes over event type, sector,
macro regime, volatility bucket, factor state, and source evidence. Semantic
embeddings may be added behind the search interface, but structured filters should
remain first-class.

Target question: "What happened six months ago before similar market events?"
Answers must enforce an as-of cutoff so future knowledge cannot leak backward.

## 5. Backtesting Module

Join frozen pre-earnings predictions to actual reactions using declared event
windows and benchmarks. Measure:

- direction accuracy and confusion matrix;
- Brier score and probability calibration;
- raw and market/sector-adjusted returns;
- coverage, sample size, and missing-data rate;
- results by score bucket, sector, horizon, and market regime.

Consensus snapshots must have an `available_at` timestamp. Revised estimates or
actuals published after the prediction cannot be substituted into the ex-ante
record.

## Suggested delivery order

1. Private historical store and integrity migration.
2. Deterministic outcome collection and journal grading.
3. Earnings/event backtest and calibration dashboard.
4. Private portfolio ingestion and exposure analysis.
5. Point-in-time factor engine.
6. Authenticated portfolio/journal UI separate from public Pages.
