# Operations and maintenance

## Daily schedule

GitHub Actions runs at 08:00 in `America/New_York` using the workflow's IANA
timezone setting, so EST/EDT changes are automatic. The target is deployment
before 09:00.

GitHub scheduled workflows are best-effort: starts can be delayed or, under high
load, dropped. A strict delivery SLA requires an external scheduler or dedicated
runner invoking the same CLI. Pages can remain the delivery layer.

## Publication policy

- Three validated market-news items and one validated macro event are required.
- Earnings is required when tomorrow is an open NYSE session.
- A closed tomorrow uses the exact required closure sentence.
- An open session with no candidate scoring at least 7.0 uses a separate
  no-qualifying-candidate message.
- The default earnings universe is explicitly marked `bounded_research`; it must
  not be described as exhaustive without a reviewed licensed calendar adapter.
- Research and Knowledge Refresh may show an explicit unavailable panel after
  bounded retries.
- Required-section, schema, rendering, secret-scan, commit, deployment, or public
  verification failure marks the workflow failed.
- A failed run never overwrites the last-known-good `latest.html`.
- If artifacts were committed but Pages deployment failed, rerun without force:
  paid research is skipped and the validated existing artifact is redeployed.

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
- inspect warning/error counts and OpenAI usage;
- apply reviewed Dependabot updates;
- confirm `latest.html` shows the expected date.

Monthly:

- manually dispatch a non-forced health run if the schedule has been quiet;
- verify the Pages deployment environment and repository secret still exist;
- review model/tool-call ceilings and costs;
- update and test the exchange-calendar dependency.

The preflight reads published totals from `records/usage.json` and unreconciled
per-response observations from `records/usage_events.jsonl`. Each received API
response is journaled before content validation; failed-run observations are
committed to the reports branch by a failure-only workflow step. Request IDs
prevent successful responses from being counted twice. The system warns at 80%
and stops new calls when observed usage has reached 100% of a configured ceiling.

A connection failure or timeout that yields no response and no usage metadata
cannot be measured locally. Per-call output/tool limits, bounded retries, and the
section deadline contain that residual risk; provider billing remains the final
source of truth.

GitHub documents that scheduled workflows in a public repository may be disabled
after 60 days without repository activity. Re-enable the workflow from the
Actions UI if this occurs. Do not introduce a long-lived personal token only to
simulate activity.

## Secret rotation

Rotate the OpenAI key in GitHub Settings, then run the workflow manually. Do not
delete the old key until the new run passes public verification. If a key ever
appears in a commit or public artifact, revoke it immediately; deleting a file in
a later commit is insufficient.

## Future portfolio deployment

Portfolio holdings, transactions, tax lots, journal notes, and account data are
private. They are explicitly outside the public Pages projection. Before adding
Portfolio Tracking or private Historical Search, deploy an authenticated private
surface and conduct a new security review.
