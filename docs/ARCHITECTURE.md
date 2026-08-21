# Production architecture

Status: Final architecture after independent Claude Code Opus review
Decision record: `ADR-001-OPUS-RECONCILIATION.md`

## System flow

```text
GitHub Actions — 08:00 America/New_York
        |
        v
fixed secret/config preflight
        |
        v
NYSE calendar context
        |
        v
section-scoped OpenAI Responses API research
        |
        v
strict schemas + source validation + deterministic scoring
        |
        v
canonical DailyReport + ex-ante predictions
        |
        v
allowlisted public projection + standalone HTML
        |
        v
secret/private-data scan + reports-branch commit
        |
        v
official GitHub Pages deployment
        |
        v
bounded public latest.html report-ID verification
```

The codebase is a modular Python monolith. HTML is a rendering target, never the
source of truth. Provider, calendar, analysis, validation, rendering, retention,
and deployment responsibilities stay separate without introducing services that
a single daily job does not need.

## Publication invariants

- `OPENAI_API_KEY` comes only from the process environment.
- Missing required configuration exits before provider calls or publication.
- Required-section failure keeps the previous public report unchanged.
- Every market-bearing item has all six explicit analytical answers:
  1. what changed;
  2. why it matters;
  3. who benefits;
  4. who loses;
  5. likely professional-investor reaction;
  6. indicators to monitor next.
- AI-proposed scoring components are bounded, but totals and selection are
  calculated by code.
- OpenAI schemas cannot provide numeric quote or consensus fields. Only a
  separately licensed market-data adapter can populate those values.
- Source publishers are derived from validated hosts; model text cannot create
  filenames, IDs, paths, or publisher identities.
- Known source publication times must be at or before the immutable report
  cutoff. News summaries are capped at 100 characters; macro analysis covers
  Stocks, Bonds, USD, and Commodities; research covers four explicit application
  categories and a 90-day recency window.
- Unknown earnings release times stay unknown. Market-open/close anchors used for
  later evaluation are separately identified as proxies.
- Public rendering is an explicit allowlist projection. Holdings and private
  journal data have no route into the public schema.
- Generated HTML contains no scripts or external assets.
- A successful deployment is not declared until the public alias exposes the new
  immutable report ID.

## Report and scoring model

The canonical report is a strict, versioned model containing source evidence,
timestamps, scores, explicit impact analyses, predictions, warnings, and provider
usage metadata.

News attention is the arithmetic mean of:

- market-size impact;
- earnings impact;
- macro importance;
- sector influence;
- short-term trading relevance.

Earnings selection is computed exactly as:

```text
0.30 × business quality
+ 0.20 × revenue growth
+ 0.20 × earnings-surprise potential
+ 0.15 × market-expectation gap
+ 0.15 × risk/reward asymmetry
```

Only candidates scoring at least 7.0 with meaningful attention are included. An
open day with no qualifying company is distinct from a market-closed tomorrow.

## Storage and retention

The source branch contains application code and workflow definitions. The
workflow creates a separate `reports` branch containing:

```text
site/
  latest.html
  index.html
  manifest.json
  robots.txt
  reports/daily_market_report_YYYY-MM-DD.html
records/
  daily_market_report_YYYY-MM-DD.json
  predictions.jsonl
  usage.json
  usage_events.jsonl
```

The public `site/reports/` directory keeps the newest eight successful dated
reports. `latest.html` is a byte-identical copy of the newest dated report, and
the manifest records matching SHA-256 values.

Sanitized records and prediction events are append-only. This preserves what the
system knew and predicted at publication time so future journaling and backtests
can be honest. They contain no holdings, accounts, secrets, raw articles, or
unlicensed vendor datasets.

`usage_events.jsonl` records sanitized usage metadata immediately for every
received provider response. Published request IDs reconcile those events into
`usage.json`; failed-run events remain observable for the next budget preflight.

## Scheduling and deployment

The workflow uses GitHub's current IANA-timezone schedule support:

```yaml
- cron: "0 8 * * *"
  timezone: "America/New_York"
```

Manual dispatch supports a `force` input. Report date is the idempotency key;
scheduled reruns for an already valid date are no-ops.

The build job has only the repository write permission needed for the dedicated
reports branch. The deployment job has `pages: write` and `id-token: write`, uses
the `github-pages` environment, and deploys the exact validated site artifact.
All external Actions are pinned to immutable commit SHAs.

GitHub schedules are best-effort and can be delayed. The architecture records
start/deploy times and exposes staleness prominently. A hard 09:00 SLA can move
scheduling to a stronger external service without changing the generation CLI or
Pages delivery.

## Failure policy

- Configuration/authentication failure: stop immediately; fixed safe message.
- Transient provider failure: bounded exponential retry with jitter.
- Invalid evidence/item: discard the item.
- Missing required news/macro/open-day earnings section: fail closed.
- Research or Knowledge failure: explicit unavailable panel is permitted.
- Rendering/private-data/secret scan failure: no commit.
- Git push failure: no deployment.
- Pages or public-verification failure: workflow fails; committed validated
  artifacts can be redeployed idempotently.

## Future upgrades

The costly-to-retrofit seams exist now: stable IDs, schema versions, point-in-time
evidence, explicit predictions, impact analysis, and append-only records. Future
modules consume those records rather than parse HTML.

- Portfolio Tracking requires a private holdings/transactions repository and an
  authenticated renderer.
- Factor Analysis adds versioned point-in-time momentum, value, quality, growth,
  and volatility features with anti-look-ahead controls.
- AI Investment Journal links immutable predictions to later evidence and
  deterministic outcomes.
- Historical Database indexes all private canonical records for event/regime
  similarity search.
- Backtesting joins frozen predictions and consensus snapshots to actual,
  benchmark-adjusted reactions and reports accuracy/calibration/coverage.

These engines are documented rather than prematurely stubbed. The stable data
contracts and ledger are the current compatibility boundary.
