# US Market Intelligence Morning Report

A production-oriented, daily US market briefing pipeline for one professional
investor. It researches and analyzes market news, tomorrow's earnings, global
macro, new research, and a classic knowledge concept; validates a typed canonical
report; renders a standalone mobile HTML dashboard; commits it to GitHub; and
deploys it through GitHub Pages.

The permanent iPhone Shortcut target is `latest.html`. Public Pages retains the
newest successful report plus seven prior successful dated reports.

## What the report always answers

Every market analysis explicitly states:

1. What changed?
2. Why does it matter?
3. Who benefits?
4. Who loses?
5. How would professional investors react?
6. What indicators should be monitored next?

These are validated schema fields, not optional prompt suggestions.

## Deployment model

```text
08:00 America/New_York GitHub schedule
  → secure configuration preflight
  → cited section research
  → deterministic ranking and validation
  → standalone HTML + canonical record
  → dedicated reports-branch commit
  → GitHub Pages deployment
  → public latest.html report-ID verification
```

Reports are generated only in the ephemeral GitHub runner and stored in GitHub;
the production workflow does not depend on a local computer or iPhone file.

## Security defaults

- `OPENAI_API_KEY` is read only from the runtime environment/GitHub Secrets.
- A missing key stops before collection or publication and leaves the prior site
  unchanged.
- The default API model is the configurable `gpt-5.6-terra`; per-section calls,
  retries, output tokens, web searches, and observed rolling monthly usage are bounded.
- Secrets, raw provider payloads, article bodies, holdings, and account data are
  prohibited from reports and logs.
- The public renderer uses an explicit field projection and pre-publication scan.
- HTML contains embedded CSS, no JavaScript, and no external assets.
- Generated content is committed to a dedicated `reports` branch with scoped
  workflow permissions.

## Data-quality and licensing defaults

OpenAI web search supports cited research and analysis. It is not treated as a
licensed market-data feed. Current price, market cap, consensus EPS, and consensus
revenue remain visibly unavailable unless an operator adds a provider with public
display/redistribution rights. The AI schema cannot populate those fields.

News scores and the exact 30/20/20/15/15 earnings score are calculated by code.
Unknown facts stay unknown; the system does not invent filler to satisfy a layout.
Unconfirmed earnings times remain labeled as unconfirmed; any backtest evaluation
window based on a market-open/close proxy is labeled as a proxy.

The default earnings discovery mode is a bounded, cited-research universe, not an
authoritative list of every company reporting. That limitation is displayed in
the HTML. A complete-universe claim requires a licensed earnings-calendar adapter;
the coverage field can then be switched to `authoritative_full_calendar` after review.

## Quick start

1. Push this project to a GitHub repository.
2. Add `OPENAI_API_KEY` under repository Actions secrets.
3. Select **GitHub Actions** as the Pages publishing source.
4. Run **Daily Market Intelligence** manually once.
5. Use the deployed `latest.html` URL in the iPhone Shortcut.

See [Setup](docs/SETUP.md) for the exact steps and
[iPhone Shortcut](docs/IPHONE_SHORTCUT.md) for the one-tap configuration.

## Development

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev pytest
uv run --frozen --extra dev ruff check .
```

Tests are offline and use synthetic/provider-mocked data. No live OpenAI request
is made by the test suite. The production workflow pins uv and installs from the
hash-locked `uv.lock` file.

## Architecture and review trail

- [Final architecture](docs/ARCHITECTURE.md)
- [Full Claude Code Opus review](docs/CLAUDE_OPUS_REVIEW.md)
- [Claude Code Opus reconciliation](docs/ADR-001-OPUS-RECONCILIATION.md)
- [Data licensing policy](docs/DATA_LICENSING.md)
- [Operations and maintenance](docs/OPERATIONS.md)
- [Future upgrades](docs/FUTURE_UPGRADES.md)

Implementation began only after an actual Claude Code Opus review covering
security, architecture, cost, and reliability returned a conditional proceed and
the mandatory findings were reconciled.

## Important limitations

GitHub scheduled workflows are best-effort. The workflow is timezone-aware and
targets 08:00 New York time, but GitHub can delay or drop scheduled runs under
load. If the before-09:00 deadline becomes contractual, invoke the same CLI from
a scheduler with an SLA and retain GitHub Pages as the publication surface.

The workflow records every provider response's reported usage immediately, even
when later validation fails, and reconciles it with successful report usage.
Network failures that return no response usage are inherently unobservable, so
the configured per-call tool, token, retry, and deadline caps remain the final
cost containment layer rather than a billing guarantee.

This project is for personal research and education, not investment advice.
