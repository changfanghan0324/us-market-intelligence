# Security policy

## Secrets

The application accepts `OPENAI_API_KEY` only from the runtime environment. In
production, configure it as a GitHub Actions repository/environment secret. Never
commit an `.env` file, API key, personal access token, brokerage credential, or
encrypted secret blob to this repository.

The daily workflow does not persist its repository credential. Git access is
injected only into the reports-branch fetch and final push steps. Repository
rulesets should separately protect `main` and prohibit force-push/deletion of the
generated `reports` history.

Before publication, scanners reject known credential formats, private-data
markers, and the exact runtime API-key value. The exact-value check also covers
opaque high-entropy secrets that do not have a recognizable prefix.

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
