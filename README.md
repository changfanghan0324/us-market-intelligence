# US Market Intelligence Morning Report

A production-oriented, daily US market briefing pipeline for one professional
investor. Its default `official_free` mode collects public US-government RSS
release metadata, applies deterministic analysis rules, validates a typed
canonical report, renders a standalone mobile HTML dashboard, commits it to
GitHub, and deploys it through GitHub Pages. It needs no OpenAI API key and
incurs no model-API charge.

The permanent iPhone Shortcut target is `latest.html`. Public Pages retains the
newest successful report plus seven prior successful dated reports.

- Live report: [latest.html](https://changfanghan0324.github.io/us-market-intelligence/latest.html)
- Source repository: [changfanghan0324/us-market-intelligence](https://github.com/changfanghan0324/us-market-intelligence)

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
08:07 America/New_York primary schedule
  + 08:37 and 09:07 safe retry schedules
  → secure configuration preflight
  → bounded official .gov RSS collection
  → deterministic ranking and validation
  → standalone HTML + canonical record
  → dedicated reports-branch commit
  → GitHub Pages deployment
  → public latest.html report-ID verification
```

Reports are generated only in the ephemeral GitHub runner and stored in GitHub;
the production workflow does not depend on a local computer or iPhone file.

## Free official-source mode

The production configuration uses only these no-key public feeds:

- Federal Reserve press releases;
- SEC press releases;
- BLS latest numbers;
- Federal Reserve FEDS working papers.

The collector reads RSS metadata and links only. It does not fetch article
bodies, scrape commercial sites, call an AI model, or download prices and
consensus estimates. Actual feed entries always rank ahead; it never invents
events to fill the layout. A bounded 14-calendar-day window is visibly disclosed
and a run fails safely if it cannot produce three distinct genuine releases.

The six answers are deterministic scenario and monitoring statements, not
personalized advice or claims about investor intent. Optional FEDS research may
degrade transparently if its feed is unavailable, while Knowledge Refresh is a
static educational explainer.

If one official market feed remains unavailable after two bounded attempts but
the other feeds still provide enough genuine content, the affected News/Macro
section and provider record are visibly marked `degraded`. Public copy names only
fixed feed labels; it never exposes raw exception text or internal warning codes.

## Security defaults

- The default mode has no API secret and does not require `OPENAI_API_KEY`.
- The committed workflow does not inject an OpenAI secret. Enabling the optional
  adapter requires a separate reviewed workflow change and GitHub Secret; changing
  the config alone is intentionally insufficient.
- Network calls, response sizes, concurrency, and deadlines are bounded.
- Secrets, raw provider payloads, article bodies, holdings, and account data are
  prohibited from reports and logs.
- The public renderer uses an explicit field projection and pre-publication scan.
- HTML contains embedded CSS, no JavaScript, and no external assets.
- Generated content is committed to a dedicated `reports` branch with scoped
  workflow permissions.

## Data-quality and licensing defaults

Official RSS is a release-discovery source, not a licensed market-data feed.
Current price, market cap, consensus EPS, and consensus revenue remain visibly
unavailable unless an operator adds a provider with public display/redistribution
rights.

News scores and the exact 30/20/20/15/15 earnings score are calculated by code.
Unknown facts stay unknown; the system does not invent filler to satisfy a layout.
Unconfirmed earnings times remain labeled as unconfirmed; any backtest evaluation
window based on a market-open/close proxy is labeled as a proxy.

There is no authoritative, no-key upcoming-earnings calendar in the official
source set. On an open target session the report therefore publishes the explicit
state `data_unavailable`, with no earnings candidates or predictions, and is
marked degraded. This never means "no companies report." A complete-universe
claim requires a reviewed earnings-calendar adapter with suitable rights.

## Quick start

1. Use the configured [GitHub repository](https://github.com/changfanghan0324/us-market-intelligence).
2. Keep `research.provider: official_free` in `config/config.yaml`; no API key or
   billing setup is needed.
3. Select **GitHub Actions** as the Pages publishing source.
4. Run **Daily Market Intelligence** manually once.
5. Use the permanent [latest report URL](https://changfanghan0324.github.io/us-market-intelligence/latest.html)
   in the iPhone Shortcut.

See [Setup](docs/SETUP.md) for the exact steps and
[iPhone Shortcut](docs/IPHONE_SHORTCUT.md) for the one-tap configuration.

## Development

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev pytest
uv run --frozen --extra dev ruff check .
```

Tests are offline and use synthetic/provider-mocked data. They make no live
network or OpenAI request. The production workflow pins uv and installs from the
hash-locked `uv.lock` file.

## Architecture and review trail

- [Final architecture](docs/ARCHITECTURE.md)
- [Full Claude Code Opus review](docs/CLAUDE_OPUS_REVIEW.md)
- [Claude Code Opus reconciliation](docs/ADR-001-OPUS-RECONCILIATION.md)
- [Claude Opus free-mode review reconciliation](docs/CLAUDE_OPUS_FREE_MODE_REVIEW.md)
- [Data licensing policy](docs/DATA_LICENSING.md)
- [Operations and maintenance](docs/OPERATIONS.md)
- [Future upgrades](docs/FUTURE_UPGRADES.md)

Implementation began only after an actual Claude Code Opus review covering
security, architecture, cost, and reliability returned a conditional proceed and
the mandatory findings were reconciled.

## Important limitations

GitHub scheduled workflows are best-effort. The workflow uses timezone-aware,
non-top-of-hour attempts at 08:07, 08:37, and 09:07 New York time so a delayed,
dropped, or failed first attempt has two independent fallbacks. A later attempt
validates and safely redeploys an existing same-day report without calling the
provider again. If a strict delivery deadline becomes contractual, invoke the
same CLI from a scheduler with an SLA and retain GitHub Pages as the publication
surface.

The free provider has no token or model-search usage to bill. Its remaining costs
are ordinary GitHub Actions/Pages usage and network access, subject to GitHub's
current plan limits and terms. If the optional OpenAI adapter is enabled, the
usage ledger and bounded request limits apply as an additional cost-control layer.

This project is for personal research and education, not investment advice.
