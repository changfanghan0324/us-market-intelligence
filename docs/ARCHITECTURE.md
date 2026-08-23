# Production architecture

Status: Final architecture after independent Claude Code Opus review
Decision record: `ADR-001-OPUS-RECONCILIATION.md`

## System flow

```text
GitHub Actions — 08:07 primary + 08:37/09:07 fallbacks, America/New_York
        |
        v
fixed configuration preflight (no secret in default mode)
        |
        v
NYSE calendar context
        |
        v
official Federal Reserve / SEC / BLS RSS discovery
        + bounded allowlisted HTML and SEC filing research
        |
        v
deterministic mapping + strict source/schema validation + scoring
        |
        v
canonical DailyReport + explicit availability states
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

- Default `official_free` mode has no API credential, model-API call, or installed
  OpenAI SDK. The SDK is isolated in an optional package extra.
- The committed workflow does not expose `OPENAI_API_KEY`. The optional OpenAI
  adapter requires separate reviewed workflow wiring before production use.
- Missing required configuration exits before provider calls or publication.
- Required-section failure keeps the previous public report unchanged.
- Every market-bearing item has all six explicit analytical answers:
  1. what changed;
  2. why it matters;
  3. who benefits;
  4. who loses;
  5. likely professional-investor reaction;
  6. indicators to monitor next.
- Official-feed analysis components and totals are calculated by deterministic
  code. The optional OpenAI adapter may propose bounded components, but code
  still calculates its totals and selection.
- Neither official-source extraction nor an AI schema can provide licensed
  numeric quote or consensus fields. Only a separately licensed market-data
  adapter can populate them.
- Source publishers are derived from validated hosts; model text cannot create
  filenames, IDs, paths, or publisher identities.
- Known source publication times must be at or before the immutable report
  cutoff. News and macro summaries use bounded paragraph-length digests derived
  from official documents; macro analysis covers Stocks, Bonds, USD, and
  Commodities; research covers four explicit application categories and a 90-day
  recency window.
- The official-free source set has no authoritative complete upcoming-earnings
  calendar. For an open target session it searches the prior 90 days of SEC 8-K
  and 6-K filings and publishes only issuer-confirmed next-session events under
  `bounded_research`. An empty scan is explicitly not a no-earnings or
  complete-universe conclusion; filing-only events receive no consensus data,
  market-price data, score, or price-direction prediction.
- In a future calendar-enabled adapter, unknown earnings release times stay
  unknown and any market-open/close evaluation anchor is labeled as a proxy.
- Public rendering is an explicit allowlist projection. Holdings and private
  journal data have no route into the public schema.
- Generated HTML contains no scripts or external assets.
- A successful deployment is not declared until the public alias exposes the new
  immutable report ID.

## Report and scoring model

The canonical report is a strict, versioned model containing source evidence,
timestamps, scores, explicit impact analyses, predictions, warnings, and provider
usage metadata.

In `official_free`, Market News is selected only from genuine releases in the
Federal Reserve press-release, SEC press-release, and BLS latest-numbers feeds.
RSS supplies the discovery link and timestamp. A narrower second hop may fetch
only allowlisted Federal Reserve, SEC, or BLS HTML, parse a capped agency-specific
content region in memory, and discard the raw document after the run; original
page text is not persisted. The provider uses a bounded 14-calendar-day lookback
so three distinct releases can be ranked during quiet windows; the window is
disclosed in the public report and actual dates remain visible. The run fails
closed rather than create filler when fewer than three releases survive
validation. Global Macro is a deterministic scenario view over a bounded
30-calendar-day window; both past and future date bounds are enforced and
disclosed. FEDS working papers supply optional Research Discovery; Knowledge
Refresh is a static educational explainer.

Earnings Watch is a separate bounded research path. It submits a fixed small set
of date-specific queries to SEC full-text search, restricts results to 8-K/6-K
filings submitted during the prior 90 days, and reads at most the configured
number of allowlisted EDGAR HTML documents in memory. A filing is shown only when
its own text explicitly ties the target date to a results release, results-related
board meeting, or earnings call; a call time is never inferred to be the release
time. Search responses and filing HTML are discarded rather than placed in the
canonical ledger or public site.

Provider capabilities and lookback windows come from the selected provider
instance/settings rather than duplicated mode-name conditions. If one market feed
fails after bounded attempts but the remaining feeds still satisfy the content
contract, safe warning codes map to fixed public feed labels. The required section,
canonical provider run, and overall report become `degraded`; unknown feed codes
fail closed.

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

Only an evidence-complete calendar provider may create scored earnings candidates.
Candidates must score at least 7.0 and have meaningful attention. The default SEC
filing scan creates schedule-only confirmed events instead, with no score,
consensus estimate, market-price input, or direction prediction. A successful
bounded scan with no explicit confirmation, an unavailable/incomplete SEC search,
a checked complete universe with no qualifying company, and a market-closed
tomorrow remain distinct and visibly disclosed states.

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
can be honest. They contain no holdings, accounts, secrets, fetched source HTML,
raw articles, or unlicensed vendor datasets.

`usage_events.jsonl` records sanitized usage metadata immediately for every
received provider response. Published request IDs reconcile those events into
`usage.json`; failed-run events remain observable for the next budget preflight.

## Scheduling and deployment

The workflow uses GitHub's current IANA-timezone schedule support:

```yaml
- cron: "7 8 * * *"
  timezone: "America/New_York"
- cron: "37 8 * * *"
  timezone: "America/New_York"
- cron: "7 9 * * *"
  timezone: "America/New_York"
```

This syntax is supported on GitHub.com: GitHub announced IANA timezone support on
[March 19, 2026](https://github.blog/changelog/2026-03-19-github-actions-late-march-2026-updates/),
and the linked current GitHub.com workflow-syntax reference documents schedules.
Older GitHub Enterprise Server documentation may still describe UTC-only cron.

Manual dispatch supports a `force` input. Report date is the idempotency key;
scheduled reruns for an already valid date validate and redeploy the existing
artifact without constructing a provider. The non-top-of-hour primary and retry
slots reduce the chance that GitHub scheduler congestion prevents publication.

The build job has only the repository write permission needed for the dedicated
reports branch. The deployment job has `pages: write` and `id-token: write`, uses
the `github-pages` environment, and deploys the exact validated site artifact.
All external Actions are pinned to immutable commit SHAs.

GitHub schedules are best-effort and can be delayed. The architecture records
start/deploy times and exposes staleness prominently. A hard 09:00 SLA can move
scheduling to a stronger external service without changing the generation CLI or
Pages delivery.

## Failure policy

- Configuration failure: stop immediately; fixed safe message.
- Official feed reads use two bounded attempts with a fixed exponential delay;
  official HTML and SEC research additionally enforce host/path allowlists,
  request/response caps, MIME checks, and fixed timeouts. The optional OpenAI
  adapter uses bounded exponential retry with jitter.
- Malformed XML/JSON/HTML, unsafe links or redirects, and insufficient genuine
  releases fail safely; the system never synthesizes an event.
- A single exhausted market feed with sufficient remaining genuine content is a
  visible degraded-coverage result, not silent success and not total failure.
- Invalid evidence/item: discard the item.
- Missing required news/macro section: fail closed.
- A successful open-day SEC scan with no explicit next-day confirmation publishes
  `no_confirmed_events_in_bounded_scan`, never "no companies report." Confirmed
  filing events publish as `confirmed_events_available`; missing SEC search or
  filing material adds a degraded-coverage warning. Failure of a configured
  authoritative calendar provider still fails closed.
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
