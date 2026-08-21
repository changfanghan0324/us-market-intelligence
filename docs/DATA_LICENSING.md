# Data licensing and source policy

GitHub Pages is publicly reachable. `noindex` and `robots.txt` reduce indexing;
they are not authentication or a redistribution license.

## News and research

The system publishes short, original analysis and links to sources. It does not
copy full articles, bypass paywalls, or archive raw publisher text. Preferred
sources follow the Tier 1/Tier 2 order in the report specification. Publisher
labels are derived from validated hostnames rather than trusted model claims.

## Quotes and consensus estimates

OpenAI web search is an analysis and discovery tool, not a licensed quote feed.
The OpenAI output schema therefore cannot populate:

- current price;
- market capitalization;
- expected EPS;
- expected revenue.

Only a separately configured `MarketDataProvider` with field-level provenance and
confirmed public-display/redistribution rights may populate those values. With no
such provider, the HTML visibly labels them unavailable.

The same distinction applies to upcoming earnings coverage. Web research can
produce a bounded, cited candidate set, but it cannot establish that every listed
issuer was checked. The default report therefore displays a coverage limitation.
An authoritative full-universe mode requires a licensed earnings-calendar feed
with event-time provenance and public-display rights.

Financial Modeling Prep is a possible future provider, but its public display and
redistribution terms must be confirmed with the vendor before enabling it. Free
or personal-use plans from any vendor should not be assumed to permit publication
on Pages.

## Official filings

SEC EDGAR APIs are appropriate as a future companion source for official filings
and actual reported values, subject to SEC fair-access limits and a declared
User-Agent. EDGAR is not a complete upcoming earnings calendar or real-time quote
feed.

Reviewed issuer investor-relations hosts can be added under
`openai.company_ir_domains`; each must also appear in the global `allowed_domains`
list. This expands only News and Earnings web search and does not turn IR pages
into a complete calendar.

## Private data

Holdings and journal data must never be placed in the public site, the public
canonical ledger, Actions artifacts, prompts, or logs. Future portfolio support
requires an authenticated private store and renderer.
