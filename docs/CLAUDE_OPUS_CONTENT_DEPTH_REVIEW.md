# Claude Code Opus content-depth review

Date: 2026-08-23

Reviewer: Claude Code 2.1.223, Opus, high effort

Review mode: read-only architecture, security, cost, reliability, and content-accuracy review

## Outcome

The first review returned **BLOCK** with five release blockers. After remediation, the
second full review returned **APPROVE** with no blockers. Two focused reconciliation
reviews also returned **APPROVE** with no blockers; their non-blocking parser observations
were fixed before deployment.

Claude performed static review only and did not execute the test suite. Codex ran all
automated and live in-memory validation described below.

## Initial blockers

1. The renderer rejected correctly degraded SEC earnings output.
2. Earnings dates were validated inconsistently across UTC and New York time.
3. Long issuer names could create overlong evidence titles.
4. Federal Reserve enforcement respondents could be paired with the wrong institution.
5. Non-BLS labor material could be attributed to BLS.

## Required remediation completed

1. Added fail-closed, section-specific degraded-state invariants in the renderer.
2. Correlated public warnings, provider status, warning codes, and warning details.
3. Standardized earnings validation on `America/New_York` dates.
4. Bounded SEC EFTS searches to exact dates, supported forms, and fixed result limits.
5. Separated earnings-release evidence from conference-call evidence.
6. Prevented conference-call times from being inferred as earnings-release times.
7. Truncated evidence labels without losing form and accession identifiers.
8. Fixed HTML parsing around void elements and omitted sibling closing tags.
9. Paired each Fed respondent only with the institution in the same official record.
10. Limited labor-statistics attribution to BLS material.
11. Expanded official-source passages into complete, bounded multi-sentence digests.
12. Added explicit macro transmission, beneficiaries, losers, investor reaction, and
    next-indicator analysis.
13. Replaced generic knowledge text with a rotating 24-card Traditional Chinese catalog,
    including formula, worked example, common trap, and application.
14. Removed the OpenAI SDK from the default production dependency set and added a CI
    assertion that the free runtime does not contain it.

## Focused reconciliation

The post-approval Fed parser review confirmed that the two official consent-prohibition
phrasings create separate record boundaries and that incomplete records cannot borrow a
later institution. A follow-up observation about names such as `Example Bank, N.A.` was
also fixed. Tests now cover initialed respondents, bank abbreviations, incomplete records,
alternate order wording, and control-phrase rejection.

## Independent verification

- 333 automated tests passed.
- Ruff formatting and lint checks passed.
- Python bytecode compilation and diff whitespace checks passed.
- An isolated, frozen, production-only environment imported the application with no
  `openai` package installed (`FREE_RUNTIME_OK`).
- Live official-provider data was exercised entirely in memory; no local HTML report was
  persisted during validation.

No API keys, authentication data, or other secrets were supplied to the reviewer.
