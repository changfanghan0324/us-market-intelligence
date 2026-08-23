# Security policy

## Secrets and runtime modes

The production `official_free` mode has no research-provider secret. The
committed GitHub workflow intentionally does not inject `OPENAI_API_KEY`, and the
default configuration does not read it. Never commit an `.env` file, API key,
personal access token, brokerage credential, or encrypted secret blob.

The code retains an optional OpenAI adapter, but changing `research.provider`
alone is not a supported production switch. Enabling it requires a separate
reviewed workflow change that maps `secrets.OPENAI_API_KEY` only into the generate
step, plus renewed security/cost tests and a repository secret. The runtime then
accepts the key only from that environment variable and fails closed if missing.

The daily workflow does not persist its repository credential. Git access is
injected only into the reports-branch fetch and final push steps. Repository
rulesets should separately protect `main` and prohibit force-push/deletion of the
generated `reports` history.

Before publication, scanners reject known credential formats and private-data
markers. In a separately wired OpenAI mode they also reject the exact runtime
API-key value, covering opaque high-entropy secrets without a recognizable prefix.

If a credential appears in a commit, workflow log, artifact, or public report:

1. revoke it immediately at the provider;
2. stop the production workflow;
3. inspect Pages, Actions artifacts, and Git history;
4. rotate the replacement secret;
5. complete an incident review before re-enabling publication.

Deleting it in a later commit is not sufficient.

## Public-data boundary

The GitHub Pages output and generated report branch must contain only public
market research. Holdings, transactions, tax lots, account identifiers, journal
entries, and user data are prohibited. A future portfolio module requires a
separate authenticated private surface and security review.

## Reporting a vulnerability

Do not open a public issue containing credentials or exploitable private-data
details. Use the repository owner's private security-reporting channel or GitHub
private vulnerability reporting when enabled.
