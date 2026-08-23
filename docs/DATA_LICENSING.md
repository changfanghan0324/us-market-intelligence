# Data licensing and source policy

GitHub Pages is publicly reachable. `noindex` and `robots.txt` reduce indexing;
they are not authentication or a redistribution license.

## News and research

The default system publishes deterministic scenario/monitor analysis and links
to releases from the Federal Reserve, SEC, and BLS. RSS provides the discovery
boundary; selected links may be fetched only from allowlisted official agency
hosts, parsed within capped memory, and discarded after deterministic extraction.
The system publishes a source-grounded digest and source link, not the original
HTML or full page text. Specialized releases are summarized in Traditional
Chinese. A generic fallback may preserve at most three bounded complete sentences
from a U.S. government release so a free rules-based rewrite does not distort the
official meaning; the report labels that passage explicitly. It does not access
commercial articles, bypass paywalls, or archive raw publisher text. Publisher
labels come from an exact validated-host mapping.

## Quotes and consensus estimates

Official RSS is a release-discovery mechanism, not a licensed quote feed. It
cannot populate:

- current price;
- market capitalization;
- expected EPS;
- expected revenue.

Only a separately configured `MarketDataProvider` with field-level provenance and
confirmed public-display/redistribution rights may populate those values. With no
such provider, the HTML visibly labels them unavailable.

The same distinction applies to upcoming earnings coverage. The official-free
provider is not an authoritative upcoming-earnings calendar; it performs a
bounded search of the prior 90 days of SEC 8-K and 6-K filings and publishes only
next-session events explicitly confirmed by the issuer document. A scan with no
match is not an empty market calendar and not a no-qualifying-company conclusion.
This filing-only path does not publish consensus estimates, market prices, scored
candidates, or price-direction predictions. An authoritative complete mode still
requires a reviewed calendar feed with event-time provenance and suitable
public-display rights.

Financial Modeling Prep is a possible future provider, but its public display and
redistribution terms must be confirmed with the vendor before enabling it. Free
or personal-use plans from any vendor should not be assumed to permit publication
on Pages.

## Official filings

SEC press-release RSS, SEC full-text search, and allowlisted EDGAR filing pages are
part of the default source set. The latter two are used only for bounded earnings
event discovery under SEC fair-access limits and a declared User-Agent: search
requests, candidate documents, response sizes, and timeouts are capped, and raw
responses are never persisted. These sources are neither a complete
upcoming-earnings calendar nor a real-time quote or consensus feed.

## Private data

Holdings and journal data must never be placed in the public site, the public
canonical ledger, Actions artifacts, prompts, or logs. Future portfolio support
requires an authenticated private store and renderer.
