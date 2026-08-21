# Adversarial Architecture Review — US Market Intelligence Morning Report

**Reviewer:** Claude Code, Claude Opus (Critical Reviewer, PART 10 Agent 2)
**Subject:** `work/ARCHITECTURE_DRAFT.md` (Codex, 2026-08-21) vs. original brief + superseding requirements
**Verdict:** **PROCEED, conditional** on the P0/P1 items in the checklist.

The Codex draft is strong: it correctly resolves the local-file vs. Pages
contradiction, refuses to trust AI-computed scores, and fails closed on
validation. What follows is where it is wrong, internally inconsistent, or
speculative.

## Security

**S1 — Model output can populate licensed market-data fields (P0).** The draft
forbids unlicensed quote/estimate redistribution, but the research call asks for
sourced quote/fundamentals/consensus values and `EarningsCandidate` carries them.
Web search becomes a laundering path around the licensing control. Remove
`current_price`, `market_cap`, `expected_eps`, and `expected_revenue` from every
JSON Schema sent to OpenAI. Those fields are writable only by the
`MarketDataProvider` adapter; a validator rejects numeric fields whose provenance
is `model`. Unset values render `Unavailable (no licensed provider configured)`.

**S2 — Private-data isolation is type-hint-only (P1).** Python annotations are
unenforced at runtime. The renderer needs an explicit field-projection allowlist,
a pre-write staging scan for holdings/journal markers, and a test that attaches a
synthetic holdings payload and asserts those bytes never appear in public output.

**S3 — Prompt injection from web-search content is unaddressed (P1).** Add a
domain allowlist and publisher/host consistency checks, strip control and bidi
characters, cap retrieved text, generate IDs/filenames/dates in code, and expose
no side-effecting model tool.

**S4 — Git history defeats the retention policy (P1).** Deletions in ordinary Git
history are cosmetic and make a future private-data mistake durable. Publish
generated artifacts to an orphan `reports` branch, run a pre-push secret-pattern
scan, and document that ordinary history is not secure erasure.

**S5 — CSP claim is overstated on Pages (P2).** Pages cannot set custom response
headers. Use a CSP meta tag where supported and make the real control no scripts
and no external assets; test that invariant.

**S6 — `workflow_dispatch` plus `contents: write` on the default branch (P2).**
Restrict writes to the reports branch, default to read permissions with per-job
escalation, use the `github-pages` environment, and never use
`pull_request_target`.

**S7 — Secret plausibility checks (P3).** Check only length/prefix booleans and
emit fixed error strings; never echo key material.

**S8 — Disclaimer/indexing posture (P3).** Add a personal-research/not-investment-
advice footer, `noindex`, and `robots.txt`.

## Architecture

**A1 — Retention destroys the backtest corpus (P0).** Pruning canonical records
on the same eight-report window deletes predictions before most horizons resolve.
Retention must apply to `site/` only. `records/` is append-only, never pruned, and
includes `records/predictions.jsonl`. This satisfies the public presentation rule
and preserves the future backtesting seam.

**A2 — Over-engineered extension ports (P2).** Do not create speculative engine
interfaces for systems with no data or user requirements. Preserve the expensive
data seams: stable report IDs, schema version, source evidence, impact analysis,
prediction, append-only ledger, and at most a historical-record read port. Keep
the other future capabilities in architecture documentation until implemented.

**A3 — Content-hash idempotency is unimplementable (P2).** LLM output is
nondeterministic. Use report date as the idempotency key. Scheduled runs exit
successfully when a validated report already exists; manual dispatch supports
`force: true`.

**A4 — Standard-library shadowing (P3).** Rename `logging.py` to `log.py` and
`calendar/` to `mktcal/`.

**A5 — `latest.html` integrity (P3).** Record SHA-256 for the dated report and
`latest.html` in the manifest and assert equality before commit.

## Cost

**C1 — Daily budget has no persistence (P3).** Persist safe usage totals in
`records/usage.json`; enforce a rolling monthly threshold and warn at 80%.

**C2 — Expected budget (informational, P3).** Five section calls per day are
likely in the low tens of dollars monthly. No cost-driven redesign is warranted;
bounded tool calls, retries, and deadlines are the important controls.

**C3 — Cost regression guard (P3).** Test per-run tool-call and output-token caps
so a prompt change cannot silently multiply cost.

## Reliability

**R1 — Scheduled workflows can auto-disable after prolonged repository
inactivity (P1).** Document the risk and provide an operational keepalive or
reminder rather than assuming the schedule is permanent.

**R2 — DST gate (P1 to verify).** The two UTC schedules plus a gate on the
scheduled slot are correct. Pin timezone data and test the 2026 DST transitions,
including exactly one eligible slot and concurrency behavior.

**R3 — Atomicity and degradation are underspecified (P2).** Make News (three
valid items) and Global Macro required. Earnings is required only when tomorrow
is open. Research and Knowledge may render explicit unavailable panels. All
other incomplete required content fails closed.

**R4 — Empty earnings watchlist needs separate copy (P2).** An open day with no
candidate scoring at least 7.0 is distinct from a closed market and from failure.
Render a dedicated no-qualifying-candidate message. Preserve the exact closed-
market sentence.

**R5 — Calendar staleness (P2).** Pin the exchange calendar dependency, fail
closed if its coverage ends before the target date, and test holidays plus an
early close.

**R6 — Pages CDN staleness (P2).** Treat the Pages deployment status as the first
gate, then poll `latest.html` for the immutable report ID for about ten minutes.
Verification timeout must fail the workflow, not become a warning.

**R7 — Stale-success visibility (P2).** Display report date and generated time
prominently in the mobile header.

**R8 — Six-question padding (P2).** Non-empty checks are insufficient. Enforce
minimum lengths, a typed `none identified` sentinel instead of fabricated actors,
named entities for beneficiaries/losers, and an anti-duplication check between the
summary and `why_it_matters`.

**R9 — Missing tests (P2).** Add DST transitions, prompt injection,
private-data leakage, model-supplied numeric rejection, non-pruned records,
workflow permissions, and `actionlint` coverage.

## Data licensing conclusion

Original summaries with attributed links and very short quotations are suitable
for a personal research page. Republished quote, market-cap, consensus EPS, or
revenue values from a vendor without display rights are not. The control must be
enforced at the schema/provenance boundary, not left to a prompt.

## Overengineering conclusion

The speculative extension engines and content-hash idempotency should be removed.
Strict schemas, deterministic scoring, atomic staging, exact-filename retention,
and deployment verification remain proportionate controls.

## Mandatory-change checklist

1. Strip numeric market-data fields from OpenAI schemas; only a licensed provider
   may populate them, with provenance validation and an unavailable render path.
2. Apply retention only to `site/`; keep `records/` append-only and add a
   prediction ledger.
3. Use an explicit renderer allowlist, private-marker scan, and leakage test.
4. Add publisher/host validation, text sanitization, code-generated IDs, and a
   prompt-injection test.
5. Publish to a dedicated orphan reports branch and run a secret scan before push.
6. Document scheduled-workflow inactivity risk and an operational response.
7. Pin timezone data and test both 2026 DST transitions.
8. Define required/degradable section thresholds explicitly.
9. Add the no-qualifying-earnings panel.
10. Use date-keyed idempotency with a manual force option.
11. Keep canonical data seams rather than speculative future-engine ports.
12. Pin/check exchange-calendar coverage and test holidays/early close.
13. Verify deployment followed by a bounded report-ID poll.
14. Strengthen six-question semantic validation.
15. Correct the CSP claim and scope workflow permissions.
16. Make date/time prominent and add the missing test categories.
17. Persist usage totals and guard tool/token ceilings.
18. Avoid standard-library-shadowing names, hash-check `latest.html`, use fixed
    secret errors, and add the disclaimer/indexing controls.
