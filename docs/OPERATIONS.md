# Operations and maintenance

## Daily schedule

GitHub Actions makes a primary attempt at 08:07 and safe fallback attempts at
08:37 and 09:07 in `America/New_York` using the workflow's IANA timezone setting,
so EST/EDT changes are automatic. A fallback validates and redeploys an existing
same-day report without collecting sources again; otherwise it generates the
missing report. The production concurrency group prevents simultaneous writes.

GitHub scheduled workflows are best-effort: starts can still be delayed or,
under high load, dropped. The three non-top-of-hour attempts materially reduce
that risk but are not a delivery SLA. A strict SLA requires an external scheduler
or dedicated runner invoking the same CLI. Pages can remain the delivery layer.

## Publication policy

- Three distinct, genuine official-feed market-news releases and one validated
  macro event are required. No synthetic filler is permitted.
- Official-free Market News uses a visibly disclosed 14-calendar-day maximum
  lookback; every card retains its actual release date.
- News and Macro may temporarily fetch allowlisted Federal Reserve, SEC, or BLS
  HTML linked by the discovery feed. Parsing is bounded and in memory; raw HTML
  is not written to reports, records, artifacts, or logs.
- Global Macro uses a visibly disclosed 30-calendar-day source window and rejects
  both older and future-dated events.
- One exhausted market feed may publish only when the remaining genuine releases
  satisfy the section contract; affected sections/provider runs and the report are
  marked degraded with a fixed-label coverage disclosure.
- In official-free mode, an open target session performs a bounded search over
  the prior 90 days of SEC 8-K and 6-K filings. Only issuer documents explicitly
  confirming the target date appear as schedule-only events.
- This SEC path is not a complete calendar and supplies no consensus estimate,
  market price, candidate score, or price-direction prediction. A calendar-capable
  mode still requires validated complete coverage on an open target session.
- A closed tomorrow uses the exact required closure sentence.
- A bounded scan with confirmed documents uses `confirmed_events_available`; a
  successful scan with no confirmation uses
  `no_confirmed_events_in_bounded_scan` and never means no company reports.
- Unavailable SEC search or filing material is disclosed as degraded bounded
  coverage; raw upstream errors and documents remain private.
- An open session with no candidate scoring at least 7.0 uses a separate
  no-qualifying-candidate message only for an evidence-complete scored universe.
- `no_qualifying_candidates` is never used for the bounded SEC filing scan.
- Research and Knowledge Refresh may show an explicit unavailable panel after
  bounded retries.
- Required-section, schema, rendering, secret-scan, commit, deployment, or public
  verification failure marks the workflow failed.
- A failed run never overwrites the last-known-good `latest.html`.
- If artifacts were committed but Pages deployment failed, rerun without force:
  collection is skipped and the validated existing artifact is redeployed.

## Retention

The public Pages site contains:

- `latest.html` (a full copy of the newest valid dated report);
- the newest valid dated report;
- seven prior successful dated reports;
- an index and manifest.

Older dated HTML is removed from the current published tree. Ordinary Git history
is not secure erasure.

Sanitized canonical records, provider-usage observations, and ex-ante predictions
are append-only so a future
investment journal and backtesting module can evaluate what was known and
predicted at the time. They must never contain API keys, account data, holdings,
raw licensed articles, or unlicensed numeric market datasets.

## Routine checks

Weekly:

- review failed or delayed workflow runs;
- inspect warning/error counts, feed freshness, official-document enrichment,
  bounded SEC filing coverage, and distinct source links;
- apply reviewed Dependabot updates;
- confirm `latest.html` shows the expected date.

Monthly:

- manually dispatch a non-forced health run if the schedule has been quiet;
- verify the Pages deployment environment, official feed/page endpoints, and SEC
  full-text search/EDGAR access;
- review request-size, timeout, and concurrency ceilings;
- update and test the exchange-calendar dependency.

The official-free provider records zero model tokens and zero model web-search
calls, does not require `OPENAI_API_KEY`, and runs without the optional OpenAI
SDK installed. Network timeouts, response sizes,
request counts, XML/JSON/HTML parsing, redirects, and concurrency remain bounded.
The existing usage ledger remains available for a deliberately selected metered
adapter, but no API billing setup is part of default operations.

GitHub documents that scheduled workflows in a public repository may be disabled
after 60 days without repository activity. Re-enable the workflow from the
Actions UI if this occurs. Do not introduce a long-lived personal token only to
simulate activity.

## Optional adapter secret rotation

Official-free mode has no provider secret to rotate. If the optional OpenAI
adapter is deliberately enabled, rotate its key in GitHub Settings and run the
workflow manually before revoking the old key. If any key appears in a commit or
public artifact, revoke it immediately; a later deletion is insufficient.

## Future portfolio deployment

Portfolio holdings, transactions, tax lots, journal notes, and account data are
private. They are explicitly outside the public Pages projection. Before adding
Portfolio Tracking or private Historical Search, deploy an authenticated private
surface and conduct a new security review.
