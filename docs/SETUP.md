# Setup and first deployment

## Prerequisites

- A GitHub repository with Actions and Pages available.
- An OpenAI Platform API key with billing enabled.
- Permission to add repository Actions secrets and configure Pages.
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

## 2. Configure the required secret

In the repository, open:

`Settings → Secrets and variables → Actions → New repository secret`

Create exactly this secret:

- Name: `OPENAI_API_KEY`
- Value: your OpenAI Platform API key

Never paste the key into `config.yaml`, workflow YAML, source, an issue, a log,
or a generated report.

If the secret is missing, generation exits before research or publication. The
previous `latest.html` remains intact and the workflow log prints fixed setup
instructions without revealing environment contents.

The API organization must also have an available API credit balance. A ChatGPT
subscription does not fund API requests. If the workflow reports that the credit
balance is exhausted, use the official [API billing portal](https://platform.openai.com/account/billing),
add credits, wait a few minutes for the balance to update, and rerun the workflow.

## 3. Enable GitHub Pages

Open `Settings → Pages`. Under **Build and deployment**, choose **GitHub Actions**
as the source.

The workflow uses GitHub's official Pages artifact/deployment actions. Do not
select the `reports` branch as a legacy branch-based Pages source.

## 4. Run the first deployment

Open `Actions → Daily Market Intelligence → Run workflow`.

Leave `force` disabled for the first run. The workflow will:

1. validate configuration and the required secret;
2. generate and validate the canonical report;
3. render a standalone HTML report;
4. update the `reports` branch;
5. retain the newest eight dated public reports;
6. deploy the site through GitHub Pages;
7. verify that `latest.html` contains the new immutable report ID.

An idempotent rerun does not repeat paid research, but it does redeploy the
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

The offline test suite uses synthetic fixtures and does not call OpenAI. A live
generation requires `OPENAI_API_KEY` in the process environment; never pass it as
a command-line argument. Production dependencies are installed from the
hash-locked `uv.lock` file.

## Optional licensed market data

The default build does not republish price, market-cap, consensus EPS, or
consensus revenue data. Those fields show as unavailable until a provider with
public-display/redistribution rights is deliberately enabled. See
`DATA_LICENSING.md` before adding any vendor.

## Optional company investor-relations sources

To let the research step use a reviewed issuer IR host, add its hostname (without
`https://` or a path) to both `openai.allowed_domains` and
`openai.company_ir_domains` in `config/config.yaml`. The latter is injected only
into News and Earnings searches. Review each host before committing it.
