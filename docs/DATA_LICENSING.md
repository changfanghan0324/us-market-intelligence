# Data licensing and source policy

GitHub Pages is publicly reachable. `noindex` and `robots.txt` reduce indexing;
they are not authentication or a redistribution license.

## News and research

The default system publishes deterministic scenario/monitor analysis and links
to releases from the Federal Reserve, SEC, and BLS. It parses RSS metadata only;
it does not fetch or copy full articles, bypass paywalls, or archive raw publisher
text. Publisher labels come from an exact validated-host mapping.

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
feeds are not an authoritative upcoming-earnings calendar. The default report
therefore publishes `data_unavailable`, not an empty calendar and not a
no-qualifying-company conclusion. An authoritative mode requires a reviewed
calendar feed with event-time provenance and suitable public-display rights.

Financial Modeling Prep is a possible future provider, but its public display and
redistribution terms must be confirmed with the vendor before enabling it. Free
or personal-use plans from any vendor should not be assumed to permit publication
on Pages.

## Official filings

SEC press-release RSS is part of the default source set. SEC EDGAR APIs remain a
possible future companion for official filings and actual reported values,
subject to SEC fair-access limits and a declared User-Agent. Neither SEC source
is a complete upcoming-earnings calendar or real-time quote feed.

## Private data

Holdings and journal data must never be placed in the public site, the public
canonical ledger, Actions artifacts, prompts, or logs. Future portfolio support
requires an authenticated private store and renderer.
