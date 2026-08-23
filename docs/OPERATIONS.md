# Operations and maintenance

## Daily schedule

GitHub Actions runs at 08:00 in `America/New_York` using the workflow's IANA
timezone setting, so EST/EDT changes are automatic. The target is deployment
before 09:00.

GitHub scheduled workflows are best-effort: starts can be delayed or, under high
load, dropped. A strict delivery SLA requires an external scheduler or dedicated
runner invoking the same CLI. Pages can remain the delivery layer.

## Publication policy

- Three distinct, genuine official-feed market-news releases and one validated
  macro event are required. No synthetic filler is permitted.
- Official-free Market News uses a visibly disclosed 14-calendar-day maximum
  lookback; every card retains its actual release date.
- Global Macro uses a visibly disclosed 30-calendar-day source window and rejects
  both older and future-dated events.
- One exhausted market feed may publish only when the remaining genuine releases
  satisfy the section contract; affected sections/provider runs and the report are
  marked degraded with a fixed-label coverage disclosure.
- In official-free mode, an open target session has no authoritative earnings
  calendar and uses the degraded `data_unavailable` state with no predictions.
- A calendar-capable mode still requires validated earnings coverage on an open
  target session.
- A closed tomorrow uses the exact required closure sentence.
- An open session with no candidate scoring at least 7.0 uses a separate
  no-qualifying-candidate message.
- `data_unavailable` must never be described as no earnings or no qualifying
  companies. `no_qualifying_candidates` is reserved for a universe that was
  actually observed.
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
- inspect warning/error counts, feed freshness, and distinct source links;
- apply reviewed Dependabot updates;
- confirm `latest.html` shows the expected date.

Monthly:

- manually dispatch a non-forced health run if the schedule has been quiet;
- verify the Pages deployment environment and official feed endpoints;
- review request-size, timeout, and concurrency ceilings;
- update and test the exchange-calendar dependency.

The official-free provider records zero model tokens and zero model web-search
calls. Network timeouts, response sizes, XML parsing, and concurrency remain
bounded. The existing usage ledger remains available for a deliberately selected
metered adapter, but no API billing setup is part of default operations.

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
