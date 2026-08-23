# Setup and first deployment

## Prerequisites

- A GitHub repository with Actions and Pages available.
- Permission to configure Actions and Pages.
- For optional local development only: uv 0.12.2 and Python 3.12.

Claude Code is not a runtime dependency. It was used only for the independent
pre-implementation architecture review recorded in this repository.

## 1. Put the source on GitHub

The configured source repository is
[changfanghan0324/us-market-intelligence](https://github.com/changfanghan0324/us-market-intelligence).
Push source changes to its default `main` branch. Do not add an `.env` file or
any API key to the commit.

The workflow creates and maintains a separate `reports` branch automatically.
Generated reports are never committed to the source branch.

Create a repository ruleset that protects `main` from direct/force pushes and
deletion. Protect `reports` from force pushes and deletion while allowing the
repository's GitHub Actions bot to append report commits. The workflow disables
persisted checkout credentials and exposes the repository token only to the
read/final-push steps.

## 2. Keep the free provider selected

The committed production configuration contains:

```yaml
research:
  provider: official_free
```

No OpenAI account, OpenAI SDK, `OPENAI_API_KEY`, or API credit balance is
required. The production job explicitly verifies that the optional SDK is not
installed. It uses bounded Federal Reserve, SEC, and BLS RSS discovery, then temporarily reads
only allowlisted official HTML needed for source-grounded summaries. It also runs
a bounded SEC filing search for next-session earnings events. All source HTML is
parsed in memory and discarded; do not add tokens, cookies, commercial endpoints,
or unofficial mirrors to the free provider.

`OPENAI_API_KEY` in `.env.example` is an optional compatibility seam only. If a
future operator deliberately selects the separate OpenAI adapter, the key must
be supplied through the process environment and never placed in YAML, source,
issues, logs, or generated reports. The committed workflow deliberately has no
OpenAI secret mapping: production enablement requires a separate security/cost
review, a minimal step-level GitHub Secrets mapping, and renewed tests. Changing
the provider name alone fails closed and has no effect on `official_free`.

For local development of that separate adapter only, keep its optional dependency
selected on both installation and execution:

```bash
uv sync --frozen --extra openai
uv run --frozen --extra openai market-intelligence --help
```

## 3. Enable GitHub Pages

Open `Settings → Pages`. Under **Build and deployment**, choose **GitHub Actions**
as the source.

The workflow uses GitHub's official Pages artifact/deployment actions. Do not
select the `reports` branch as a legacy branch-based Pages source.

## 4. Run the first deployment

Open `Actions → Daily Market Intelligence → Run workflow`.

Leave `force` disabled for the first run. The workflow will:

1. validate the no-secret free configuration;
2. generate and validate the canonical report;
3. render a standalone HTML report;
4. update the `reports` branch;
5. retain the newest eight dated public reports;
6. deploy the site through GitHub Pages;
7. verify that `latest.html` contains the new immutable report ID.

An idempotent rerun does not repeat collection, but it does redeploy the
validated existing Pages artifact. This is the recovery path when a previous
Pages deployment failed after the reports-branch commit.

The deployment job displays and verifies the exact Pages URL. This deployment's
permanent report address is:

[https://changfanghan0324.github.io/us-market-intelligence/latest.html](https://changfanghan0324.github.io/us-market-intelligence/latest.html)

## 5. Local verification (development only)

The production delivery path is GitHub Pages. Local output is only for tests and
development.

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev pytest
```

The offline test suite uses synthetic fixtures and makes no network call. A live
`official_free` generation needs outbound HTTPS access to the configured official
feeds, allowlisted agency pages, SEC full-text search, and allowlisted EDGAR
filings, but no secret. Production dependencies are installed from the
hash-locked `uv.lock` file.

## Optional licensed market data

The default build does not republish price, market-cap, consensus EPS, or
consensus revenue data. Those fields show as unavailable until a provider with
public-display/redistribution rights is deliberately enabled. See
`DATA_LICENSING.md` before adding any vendor.

## Free-mode limitations

The free source set has no authoritative complete upcoming-earnings calendar. On
an open target session, the Earnings panel scans at most the prior 90 days of SEC
8-K and 6-K filings and lists only issuer documents that explicitly confirm the
target date. The result is bounded coverage: no match does not mean no company
reports, and the panel provides no consensus estimates, market prices, scored
candidates, or price-direction predictions. Market News may use genuine releases
from a visibly disclosed 14-day window. If fewer than three distinct releases
survive validation, publication stops and the previous `latest.html` remains
available.
