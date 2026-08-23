# ADR-001: Claude Code Opus review reconciliation

Status: Accepted
Date: 2026-08-21

Claude Code Opus independently reviewed the Codex architecture under four lenses:
Security, Architecture, Cost, and Reliability. Its verdict was **conditional
proceed**. Implementation began only after the following decisions were accepted.

1. Numeric price/market-cap/EPS/revenue data is excluded from OpenAI schemas and
   may come only from a licensed provider with field provenance.
2. The eight-report rule applies to public HTML; sanitized predictions/records
   remain append-only for future journal and backtest integrity.
3. Public output uses an explicit projection allowlist plus secret/private-marker
   scanning and leakage tests.
4. Source hosts are allowlisted and normalized; control/bidirectional characters
   are stripped; IDs, dates, and paths are code-generated.
5. Generated artifacts are committed to a dedicated reports branch.
6. Publication thresholds distinguish required market sections from degradable
   educational sections.
7. Market-closed, no-qualifying-earnings, provider-failure, and unavailable-data
   states remain distinct.
8. Date-keyed idempotency replaces content-hash idempotency; manual dispatch can
   force a replacement.
9. Canonical point-in-time data seams are implemented now; speculative future
   engine interfaces are deferred.
10. Calendar coverage, DST behavior, injection, data leakage, market-data
    provenance, retention, workflow permissions, and HTML invariants receive
    dedicated tests.
11. Pages status is followed by a bounded public report-ID poll.
12. HTML uses no scripts/external assets and includes `noindex`, `robots.txt`, and
    a personal-research disclaimer.
13. Usage and tool/token ceilings are bounded and recorded without storing raw
    provider payloads or credentials.

One review assumption was updated from current official GitHub documentation:
Actions now supports an IANA `timezone` on scheduled workflows. The final design
therefore uses one 08:00 `America/New_York` schedule instead of duplicate UTC
crons, while retaining timezone tests.

That single-slot decision was superseded after the 2026-08-23 production
reliability review. The workflow now uses independent 08:07, 08:37, and 09:07
`America/New_York` attempts. They are not DST workarounds: they reduce documented
GitHub scheduler delay/drop risk, while date-keyed idempotency makes later
same-day attempts safe.

The default output language is `zh-TW`, inferred from the Traditional Chinese
brief and kept configurable. Implementation was authorized after this record.
