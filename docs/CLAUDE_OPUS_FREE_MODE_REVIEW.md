# Claude Opus free-mode review reconciliation

Date: 2026-08-22
Reviewer: Claude Code, Claude Opus (terminal review)
Reconciliation owner: Codex

## Scope and disposition

Claude Opus reviewed the no-key official-feed upgrade for security,
architecture, cost, and reliability. This document records how Codex reconciled
that review. It does not claim a second Claude verification after the fixes.

| Finding | Decision | Reconciliation |
|---|---|---|
| GitHub schedule `timezone` was considered unsupported | Rejected as stale | GitHub.com shipped IANA timezone schedules on [2026-03-19](https://github.blog/changelog/2026-03-19-github-actions-late-march-2026-updates/) and links to its current [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax). The existing `timezone: "America/New_York"` schedule remains. |
| Global Macro could select an arbitrarily old release | Accepted | A provider-derived, bounded 30-day policy now rejects older and future-dated events and publishes the source-window disclosure. Older eligible official events are restricted to medium/low impact by deterministic provider rules. |
| Capabilities and lookbacks were inferred from the config mode string | Accepted | The CLI now reads earnings-calendar capability and source windows from the constructed provider and its settings, removing duplicated `14`-day wiring. |
| Official feeds lacked bounded recovery and partial-health propagation | Accepted | Each feed has two bounded attempts. Safe fixed warning codes propagate into degraded provider metadata, required-section state, report state, and fixed-label public disclosures. Unknown codes fail closed. |
| Free/OpenAI security documentation had drifted | Accepted | The default is explicitly no-secret. The committed workflow does not inject OpenAI credentials; optional OpenAI production use requires separate reviewed workflow wiring and cost/security validation. |

## Nonblocking limitations retained

- RSS title/link metadata cannot replace full document research or a licensed
  market-data service.
- No authoritative free upcoming-earnings calendar is claimed; the report uses
  the explicit `data_unavailable` state.
- GitHub scheduled workflows remain best-effort, and Pages remains public despite
  `noindex`/`robots.txt` indexing controls.
- Deterministic scenario text is educational analysis, not personalized advice
  or a factual claim about how every professional investor will react.

## Codex validation boundary

The reconciliation is enforced through canonical-model invariants, source-date
bounds, provider metadata propagation, public-projection checks, renderer tests,
and the repository's offline test/lint suite. A future material provider or
workflow change requires another review; this record is not evergreen approval.
