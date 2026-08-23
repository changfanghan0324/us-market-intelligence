from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Self
from urllib.request import Request

import pytest

from market_intelligence.providers import official_research
from market_intelligence.providers.base import (
    PermanentProviderError,
    TransientProviderError,
)
from market_intelligence.providers.official_research import (
    BLS_LATEST_NUMBERS_URL,
    DEFAULT_OFFICIAL_USER_AGENT,
    FED_PRESS_RELEASES_URL,
    FED_RESEARCH_URL,
    OFFICIAL_ALLOWED_DOMAINS,
    SEC_PRESS_RELEASES_URL,
    OfficialFeedsResearchProvider,
    OfficialFeedsResearchSettings,
    _canonical_official_url,
    _macro_impact,
    _OfficialRedirectHandler,
    _theme,
)
from market_intelligence.providers.openai_research import ResearchRequest
from market_intelligence.providers.sec_earnings import (
    SECConfirmedEarningsEvent,
    SECEarningsResearchResult,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "official"
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _fixtures() -> dict[str, bytes]:
    return {
        FED_PRESS_RELEASES_URL: (FIXTURE_ROOT / "fed_press.xml").read_bytes(),
        BLS_LATEST_NUMBERS_URL: (FIXTURE_ROOT / "bls_latest.rss").read_bytes(),
        SEC_PRESS_RELEASES_URL: (FIXTURE_ROOT / "sec_press.rss").read_bytes(),
        FED_RESEARCH_URL: (FIXTURE_ROOT / "feds.xml").read_bytes(),
    }


def _request() -> ResearchRequest:
    return ResearchRequest(
        report_date=date(2026, 8, 21),
        previous_session_date=date(2026, 8, 20),
        target_date=date(2026, 8, 22),
        generated_at=NOW,
        timezone_name="America/New_York",
    )


def _sec_event(
    *,
    ticker: str,
    serial: int,
    company_name: str = "Example Holdings",
    scheduled_release_at: datetime | None = None,
    scheduled_release_window: Any = "unspecified",
    conference_call_at: datetime | None = None,
    confirmation_basis: Any = "board_meeting_results",
) -> SECConfirmedEarningsEvent:
    accession_number = f"0001234567-26-{serial:06d}"
    return SECConfirmedEarningsEvent(
        company_name=company_name,
        ticker=ticker,
        cik="0001234567",
        form="8-K",
        filing_date=date(2026, 8, 21),
        target_date=date(2026, 8, 22),
        accession_number=accession_number,
        filing_url=(
            "https://www.sec.gov/Archives/edgar/data/1234567/"
            f"{accession_number.replace('-', '')}/event{serial}.htm"
        ),
        confirmation_basis=confirmation_basis,
        scheduled_release_at=scheduled_release_at,
        scheduled_release_window=scheduled_release_window,
        conference_call_at=conference_call_at,
        evidence_excerpt="Issuer filing confirms the scheduled event date.",
    )


class FixtureFetcher:
    def __init__(
        self,
        values: dict[str, bytes],
        failures: set[str] | None = None,
        failures_before_success: dict[str, int] | None = None,
    ) -> None:
        self.values = values
        self.failures = failures or set()
        self.failures_before_success = dict(failures_before_success or {})
        self.calls: list[tuple[str, float, dict[str, str]]] = []

    def __call__(self, url: str, timeout: float, headers: Any) -> bytes:
        self.calls.append((url, timeout, dict(headers)))
        remaining_failures = self.failures_before_success.get(url, 0)
        if remaining_failures:
            self.failures_before_success[url] = remaining_failures - 1
            raise OSError("raw transient failure must stay private")
        if url in self.failures:
            raise OSError("raw network failure must stay private")
        return self.values[url]

    def call_count(self, url: str) -> int:
        return sum(call_url == url for call_url, _, _ in self.calls)


def _provider(fetcher: FixtureFetcher) -> OfficialFeedsResearchProvider:
    return OfficialFeedsResearchProvider(
        fetcher=fetcher,
        now=lambda: NOW,
        sleep=lambda _: None,
    )


def test_market_news_uses_only_genuine_fixture_entries_and_zero_usage() -> None:
    fetcher = FixtureFetcher(_fixtures())
    result = _provider(fetcher).market_news(_request())

    assert len(result.data.candidates) >= 3
    titles = {item.title for item in result.data.candidates}
    assert "Future labor release must not cross the source cutoff" not in titles
    assert all("monitor" not in title.casefold() for title in titles)
    assert all(
        item.sources[0].quoted_fragment is None for item in result.data.candidates
    )
    assert all(
        any(
            (item.sources[0].url.split("/", 3)[2]).endswith(domain)
            for domain in OFFICIAL_ALLOWED_DOMAINS
        )
        for item in result.data.candidates
    )
    assert result.metadata.provider == "official_public_sources"
    assert result.metadata.model == "deterministic_official_sources_v2"
    assert result.metadata.usage.input_tokens == 0
    assert result.metadata.usage.output_tokens == 0
    assert result.metadata.usage.web_search_calls == 0
    assert result.metadata.attempts == 1
    assert result.metadata.warning_codes == ()


def test_market_news_ranks_all_eligible_items_before_candidate_cap() -> None:
    newer_low_impact_items = "".join(
        f"""<item>
          <title>General administrative release number {index}</title>
          <link>https://www.federalreserve.gov/example-general-{index}.htm</link>
          <pubDate>Fri, 21 Aug 2026 {11 - index:02d}:00:00 GMT</pubDate>
        </item>"""
        for index in range(8)
    )
    older_high_impact_item = """<item>
      <title>FOMC interest rate policy update</title>
      <link>https://www.federalreserve.gov/example-fomc.htm</link>
      <pubDate>Wed, 19 Aug 2026 15:00:00 GMT</pubDate>
    </item>"""
    feed = (
        "<?xml version='1.0'?><rss><channel>"
        f"{newer_low_impact_items}{older_high_impact_item}"
        "</channel></rss>"
    ).encode()
    empty_feed = b"<?xml version='1.0'?><rss><channel></channel></rss>"
    fetcher = FixtureFetcher(
        {
            FED_PRESS_RELEASES_URL: feed,
            BLS_LATEST_NUMBERS_URL: empty_feed,
            SEC_PRESS_RELEASES_URL: empty_feed,
            FED_RESEARCH_URL: empty_feed,
        }
    )

    result = _provider(fetcher).market_news(_request())

    titles = [candidate.title for candidate in result.data.candidates]
    assert len(titles) == 8
    assert titles[0] == "FOMC interest rate policy update"
    assert "General administrative release number 7" not in titles


def test_market_news_skips_oversized_official_url_when_valid_entries_remain() -> None:
    oversized_url = "https://www.federalreserve.gov/" + ("a" * 3_000)
    feed = f"""<?xml version='1.0'?><rss><channel>
      <item>
        <title>FOMC interest rate policy update</title>
        <link>{oversized_url}</link>
        <pubDate>Fri, 21 Aug 2026 11:30:00 GMT</pubDate>
      </item>
      <item>
        <title>General official release one</title>
        <link>https://www.federalreserve.gov/one.htm</link>
        <pubDate>Fri, 21 Aug 2026 11:00:00 GMT</pubDate>
      </item>
      <item>
        <title>General official release two</title>
        <link>https://www.federalreserve.gov/two.htm</link>
        <pubDate>Fri, 21 Aug 2026 10:00:00 GMT</pubDate>
      </item>
      <item>
        <title>General official release three</title>
        <link>https://www.federalreserve.gov/three.htm</link>
        <pubDate>Fri, 21 Aug 2026 09:00:00 GMT</pubDate>
      </item>
    </channel></rss>""".encode()
    empty_feed = b"<?xml version='1.0'?><rss><channel></channel></rss>"
    fetcher = FixtureFetcher(
        {
            FED_PRESS_RELEASES_URL: feed,
            BLS_LATEST_NUMBERS_URL: empty_feed,
            SEC_PRESS_RELEASES_URL: empty_feed,
            FED_RESEARCH_URL: empty_feed,
        }
    )

    result = _provider(fetcher).market_news(_request())

    assert [candidate.title for candidate in result.data.candidates] == [
        "General official release one",
        "General official release two",
        "General official release three",
    ]


def test_market_news_contains_per_item_model_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_canonicalizer = official_research._canonical_official_url

    def allow_oversized_test_url(value: str) -> str:
        if len(value) > 2_048:
            return value
        return original_canonicalizer(value)

    monkeypatch.setattr(
        official_research,
        "_canonical_official_url",
        allow_oversized_test_url,
    )

    oversized_url = "https://www.federalreserve.gov/" + ("a" * 3_000)
    feed = f"""<?xml version='1.0'?><rss><channel>
      <item>
        <title>FOMC interest rate policy update</title>
        <link>{oversized_url}</link>
        <pubDate>Fri, 21 Aug 2026 11:30:00 GMT</pubDate>
      </item>
      <item>
        <title>General official release one</title>
        <link>https://www.federalreserve.gov/one.htm</link>
        <pubDate>Fri, 21 Aug 2026 11:00:00 GMT</pubDate>
      </item>
      <item>
        <title>General official release two</title>
        <link>https://www.federalreserve.gov/two.htm</link>
        <pubDate>Fri, 21 Aug 2026 10:00:00 GMT</pubDate>
      </item>
      <item>
        <title>General official release three</title>
        <link>https://www.federalreserve.gov/three.htm</link>
        <pubDate>Fri, 21 Aug 2026 09:00:00 GMT</pubDate>
      </item>
    </channel></rss>""".encode()
    empty_feed = b"<?xml version='1.0'?><rss><channel></channel></rss>"
    fetcher = FixtureFetcher(
        {
            FED_PRESS_RELEASES_URL: feed,
            BLS_LATEST_NUMBERS_URL: empty_feed,
            SEC_PRESS_RELEASES_URL: empty_feed,
            FED_RESEARCH_URL: empty_feed,
        }
    )

    result = _provider(fetcher).market_news(_request())

    assert len(result.data.candidates) == 3
    assert all(
        candidate.title != "FOMC interest rate policy update"
        for candidate in result.data.candidates
    )


def test_typed_url_validation_runs_before_cross_feed_title_deduplication() -> None:
    healthy_items = "".join(
        f"""<item>
          <title>Healthy official release {index}</title>
          <link>https://www.bls.gov/healthy-{index}.htm</link>
          <pubDate>Thu, 20 Aug 2026 {11 - index:02d}:00:00 GMT</pubDate>
        </item>"""
        for index in range(1, 4)
    )
    malformed_newer_duplicates = "".join(
        f"""<item>
          <title>Healthy official release {index}</title>
          <link>https://bad%host.sec.gov/shadow-{index}.htm</link>
          <pubDate>Fri, 21 Aug 2026 {11 - index:02d}:00:00 GMT</pubDate>
        </item>"""
        for index in range(1, 4)
    )
    empty_feed = b"<?xml version='1.0'?><rss><channel></channel></rss>"
    fetcher = FixtureFetcher(
        {
            FED_PRESS_RELEASES_URL: (
                "<?xml version='1.0'?><rss><channel>"
                f"{malformed_newer_duplicates}</channel></rss>"
            ).encode(),
            BLS_LATEST_NUMBERS_URL: (
                f"<?xml version='1.0'?><rss><channel>{healthy_items}</channel></rss>"
            ).encode(),
            SEC_PRESS_RELEASES_URL: empty_feed,
            FED_RESEARCH_URL: empty_feed,
        }
    )

    result = _provider(fetcher).market_news(_request())

    assert [candidate.title for candidate in result.data.candidates] == [
        "Healthy official release 1",
        "Healthy official release 2",
        "Healthy official release 3",
    ]
    assert all(
        candidate.sources[0].url.startswith("https://www.bls.gov/")
        for candidate in result.data.candidates
    )


def test_macro_and_research_fall_back_after_per_item_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_canonicalizer = official_research._canonical_official_url

    def allow_malformed_test_url(value: str) -> str:
        if "bad%host.sec.gov" in value:
            return value
        return original_canonicalizer(value)

    monkeypatch.setattr(
        official_research,
        "_canonical_official_url",
        allow_malformed_test_url,
    )
    empty_feed = b"<?xml version='1.0'?><rss><channel></channel></rss>"
    market_feed = b"""<?xml version='1.0'?><rss><channel>
      <item>
        <title>FOMC malformed interest rate update</title>
        <link>https://bad%host.sec.gov/macro.htm</link>
        <pubDate>Fri, 21 Aug 2026 11:00:00 GMT</pubDate>
      </item>
      <item>
        <title>Employment and labor market update</title>
        <link>https://www.federalreserve.gov/healthy-macro.htm</link>
        <pubDate>Thu, 20 Aug 2026 11:00:00 GMT</pubDate>
      </item>
    </channel></rss>"""
    research_feed = b"""<?xml version='1.0'?><rss><channel>
      <item>
        <title>FEDS malformed research discovery</title>
        <link>https://bad%host.sec.gov/research.htm</link>
        <pubDate>Fri, 21 Aug 2026 10:00:00 GMT</pubDate>
      </item>
      <item>
        <title>FEDS valid research discovery</title>
        <link>https://www.federalreserve.gov/econres/feds/valid.htm</link>
        <pubDate>Thu, 20 Aug 2026 10:00:00 GMT</pubDate>
      </item>
    </channel></rss>"""
    fetcher = FixtureFetcher(
        {
            FED_PRESS_RELEASES_URL: market_feed,
            BLS_LATEST_NUMBERS_URL: empty_feed,
            SEC_PRESS_RELEASES_URL: empty_feed,
            FED_RESEARCH_URL: research_feed,
        }
    )
    provider = _provider(fetcher)

    macro = provider.global_macro(_request())
    research = provider.research_discovery(_request())

    assert macro.data.event.global_event == "Employment and labor market update"
    assert research.data.discovery.research_title == "FEDS valid research discovery"


def test_typed_url_boundary_rejects_allowlist_looking_invalid_hostname() -> None:
    with pytest.raises(ValueError, match="valid HTTPS URL"):
        _canonical_official_url("https://bad%host.sec.gov/release.htm")


def test_publication_dates_use_report_timezone_not_utc() -> None:
    result = _provider(FixtureFetcher(_fixtures())).market_news(_request())

    crossed = next(
        item
        for item in result.data.candidates
        if item.title == "Federal Reserve issues a late UTC policy operations notice"
    )
    assert crossed.sources[0].published_at == datetime(2026, 8, 21, 0, 30, tzinfo=UTC)
    assert crossed.event_date == date(2026, 8, 20)


def test_global_macro_and_research_use_real_official_entries() -> None:
    fetcher = FixtureFetcher(_fixtures())
    provider = _provider(fetcher)

    macro = provider.global_macro(_request())
    research = provider.research_discovery(_request())

    assert macro.data.event.global_event == (
        "Minutes of the Federal Open Market Committee meeting"
    )
    assert set(macro.data.event.affected_assets) == {
        "stocks",
        "bonds",
        "usd",
        "commodities",
    }
    assert macro.data.event.market_impact == "high"
    assert research.data.discovery.research_title == (
        "FEDS Paper: A Structural Labor Market Indicator"
    )
    assert "DSGE" in research.data.discovery.simple_explanation
    assert "樣本外驗證" in research.data.discovery.simple_explanation
    assert len(research.data.discovery.simple_explanation) >= 180
    assert research.data.discovery.has_current_market_implication is False
    assert research.data.discovery.impact_analysis is None


def test_global_macro_caps_document_attempts_when_candidates_are_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = "".join(
        f"""<item>
          <title>FOMC monetary policy update {index}</title>
          <link>https://www.federalreserve.gov/macro-{index}.htm</link>
          <pubDate>Fri, 21 Aug 2026 {11 - index:02d}:00:00 GMT</pubDate>
        </item>"""
        for index in range(5)
    )
    market_feed = (
        f"<?xml version='1.0'?><rss><channel>{items}</channel></rss>"
    ).encode()
    empty_feed = b"<?xml version='1.0'?><rss><channel></channel></rss>"

    class RecordingDocumentClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def fetch(self, url: str) -> official_research.OfficialDocument:
            self.calls.append(url)
            return official_research.OfficialDocument(
                requested_url=url,
                final_url=url,
                agency="federal_reserve",
                mime_type="text/html",
                paragraphs=("A bounded official policy paragraph.",),
                links=(),
            )

    documents = RecordingDocumentClient()
    fetcher = FixtureFetcher(
        {
            FED_PRESS_RELEASES_URL: market_feed,
            BLS_LATEST_NUMBERS_URL: empty_feed,
            SEC_PRESS_RELEASES_URL: empty_feed,
            FED_RESEARCH_URL: empty_feed,
        }
    )
    provider = OfficialFeedsResearchProvider(
        fetcher=fetcher,
        document_client=documents,  # type: ignore[arg-type]
        now=lambda: NOW,
        sleep=lambda _: None,
    )

    def reject_candidate(*_: Any, **__: Any) -> None:
        raise ValueError("fixture validation failure")

    monkeypatch.setattr(official_research, "_macro_event", reject_candidate)

    with pytest.raises(PermanentProviderError, match="valid qualifying macro"):
        provider.global_macro(_request())

    assert len(documents.calls) == 3


def test_boiler_room_is_regulation_not_an_oil_or_macro_event() -> None:
    assert _theme("SEC Charges Boiler Room Operator in Investment Scam") == "regulation"
    assert _theme("OIL-PRICE policy update") == "energy"
    assert _theme("INTEREST-RATE policy update") == "monetary"
    assert _theme("Minutes of the Federal Open Market Committee") == "monetary"

    result = _provider(FixtureFetcher(_fixtures())).global_macro(_request())

    assert result.data.event.global_event == (
        "Minutes of the Federal Open Market Committee meeting"
    )
    assert "Boiler Room" not in result.data.event.global_event


def test_fixture_sec_charge_is_never_rendered_as_fed_bank_enforcement() -> None:
    result = _provider(FixtureFetcher(_fixtures())).market_news(_request())

    item = next(
        candidate
        for candidate in result.data.candidates
        if candidate.title == "SEC Charges Boiler Room Operator in Investment Scam"
    )

    assert "證券交易委員會（SEC）" in item.summary
    assert "不是法院終局認定" in item.summary
    assert "聯準會" not in item.summary
    assert "consent prohibition" not in item.summary
    assert "前銀行員工" not in item.summary
    actor_names = {
        actor.name
        for actor in (
            *item.impact_analysis.beneficiaries,
            *item.impact_analysis.losers,
        )
        if actor.kind == "named_entity"
    }
    assert all("銀行" not in name and "存款人" not in name for name in actor_names)
    assert "SEC complaint" in item.impact_analysis.professional_investor_reaction


def test_sec_charge_digest_keeps_substantive_source_facts_and_allegation_status() -> (
    None
):
    item = official_research._FeedItem(
        title="SEC Charges Adviser in Alleged Private Fund Fraud",
        url="https://www.sec.gov/newsroom/press-releases/example-sec-charge",
        published_at=NOW,
        publisher="U.S. Securities and Exchange Commission",
    )
    source = (
        "The Securities and Exchange Commission today charged Example Adviser LLC "
        "and its chief executive for allegedly defrauding private-fund investors "
        "through undisclosed fees. According to the SEC's complaint, the defendants "
        "raised more than $74 million from 800 investors between 2020 and 2025. "
        "The complaint seeks injunctions, disgorgement, and civil penalties."
    )

    candidate = official_research._news_item(
        item,
        request=_request(),
        source_text=source,
    )

    assert "undisclosed fees" in candidate.summary
    assert "$74 million" in candidate.summary
    assert "指控" in candidate.summary
    assert "不是法院終局認定" in candidate.summary
    assert "聯準會" not in candidate.summary
    assert "consent prohibition" not in candidate.summary


def test_fed_enforcement_claims_consent_prohibition_only_when_source_confirms() -> None:
    item = official_research._FeedItem(
        title="Federal Reserve announces enforcement action",
        url="https://www.federalreserve.gov/newsevents/pressreleases/example.htm",
        published_at=NOW,
        publisher="Federal Reserve",
    )

    without_order = official_research._news_digest(
        item,
        publication_date=date(2026, 8, 21),
        theme="regulation",
        source_text=(
            "The Federal Reserve Board today announced an enforcement action. "
            "The public document describes the action and the affected institution."
        ),
    )
    with_order = official_research._news_digest(
        item,
        publication_date=date(2026, 8, 21),
        theme="regulation",
        source_text=(
            "Consent prohibition against Jane Example Former employee of Example "
            "Bank, Misappropriation of customer funds."
        ),
    )

    assert "未能從可用文字逐筆配對" in without_order
    assert "措施為 consent prohibition" not in without_order
    assert "措施為 consent prohibition" in with_order
    assert "Jane Example" in with_order
    assert "Example Bank" in with_order
    assert "不等於法院或完整對抗程序" in with_order


def test_fed_enforcement_keeps_each_respondent_with_its_own_institution() -> None:
    item = official_research._FeedItem(
        title="Federal Reserve announces enforcement actions",
        url="https://www.federalreserve.gov/newsevents/pressreleases/example.htm",
        published_at=NOW,
        publisher="Federal Reserve",
    )
    source = (
        "Consent prohibition against Alice Alpha Former employee of First "
        "National Bank, Misappropriation of customer funds. "
        "Consent prohibition against Bob Beta Former employee of Second Trust "
        "Company, Unsafe or unsound banking practices."
    )

    summary = official_research._news_digest(
        item,
        publication_date=date(2026, 8, 21),
        theme="regulation",
        source_text=source,
    )

    assert "Alice Alpha（前任職機構：First National Bank）" in summary
    assert "Bob Beta（前任職機構：Second Trust Company）" in summary
    assert "Alice Alpha（前任職機構：Second Trust Company）" not in summary
    assert "本頁至少一項記錄提及挪用客戶資金" in summary
    assert "文件所述對象" not in summary
    assert "文件所述機構" not in summary


def test_fed_enforcement_does_not_invent_placeholders_when_pair_is_incomplete() -> None:
    item = official_research._FeedItem(
        title="Federal Reserve announces enforcement action",
        url="https://www.federalreserve.gov/newsevents/pressreleases/example.htm",
        published_at=NOW,
        publisher="Federal Reserve",
    )

    summary = official_research._news_digest(
        item,
        publication_date=date(2026, 8, 21),
        theme="regulation",
        source_text=(
            "The Federal Reserve Board today announced a consent prohibition. "
            "The available excerpt does not identify a former employer."
        ),
    )

    assert "未能從可用文字逐筆配對" in summary
    assert "文件所述對象" not in summary
    assert "文件所述機構" not in summary


def test_fed_enforcement_never_pairs_across_an_incomplete_record_boundary() -> None:
    item = official_research._FeedItem(
        title="Federal Reserve announces enforcement actions",
        url="https://www.federalreserve.gov/newsevents/pressreleases/example.htm",
        published_at=NOW,
        publisher="Federal Reserve",
    )
    source = (
        "Consent prohibition against Alice Alpha. "
        "Consent prohibition against Bob Beta Former employee of Second Trust "
        "Company, Unsafe or unsound banking practices."
    )

    summary = official_research._news_digest(
        item,
        publication_date=date(2026, 8, 21),
        theme="regulation",
        source_text=source,
    )

    assert "Bob Beta（前任職機構：Second Trust Company）" in summary
    assert "Alice Alpha" not in summary
    assert "Alice Alpha（前任職機構：Second Trust Company）" not in summary


def test_fed_enforcement_handles_order_of_prohibition_record_boundary() -> None:
    item = official_research._FeedItem(
        title="Federal Reserve announces enforcement actions",
        url="https://www.federalreserve.gov/newsevents/pressreleases/example.htm",
        published_at=NOW,
        publisher="Federal Reserve",
    )
    source = (
        "Consent prohibition against Alice Alpha. "
        "Consent order of prohibition against Bob Beta Former employee of "
        "Second Trust Company, Unsafe or unsound banking practices."
    )

    summary = official_research._news_digest(
        item,
        publication_date=date(2026, 8, 21),
        theme="regulation",
        source_text=source,
    )

    assert "Bob Beta（前任職機構：Second Trust Company）" in summary
    assert "Alice Alpha" not in summary


def test_fed_enforcement_preserves_initials_and_bank_abbreviation() -> None:
    records = official_research._fed_enforcement_records(
        "Consent prohibition against Stephanie R. Kilbert Former employee of "
        "Example Bank, N.A., Misappropriation of customer funds."
    )

    assert records == (("Stephanie R. Kilbert", "Example Bank, N.A."),)


def test_fed_enforcement_rejects_unmarked_control_phrase_in_respondent() -> None:
    records = official_research._fed_enforcement_records(
        "Consent prohibition against Alice Alpha. Order of prohibition against "
        "Bob Beta Former employee of Second Trust Company, Unsafe or unsound "
        "banking practices."
    )

    assert records == ()


def test_generic_news_digest_uses_multiple_complete_official_sentences() -> None:
    item = official_research._FeedItem(
        title="Agency updates a market infrastructure program",
        url="https://www.federalreserve.gov/newsevents/pressreleases/example.htm",
        published_at=NOW,
        publisher="Federal Reserve",
    )
    source = (
        "The agency expanded the program to include two additional settlement windows. "
        "The change begins on September 1 and applies to participating institutions. "
        "Officials will publish implementation metrics after the first full month."
    )

    summary = official_research._news_digest(
        item,
        publication_date=date(2026, 8, 21),
        theme="general",
        source_text=source,
    )

    assert "two additional settlement windows" in summary
    assert "September 1" in summary
    assert "implementation metrics" in summary
    assert "可核對的開頭" not in summary
    assert len(summary) >= 300


def test_non_bls_labor_item_keeps_its_actual_publisher() -> None:
    item = official_research._FeedItem(
        title="Employment and labor market update",
        url="https://www.federalreserve.gov/newsevents/pressreleases/labor.htm",
        published_at=NOW,
        publisher="Federal Reserve",
    )

    summary = official_research._news_digest(
        item,
        publication_date=date(2026, 8, 21),
        theme="labor",
        source_text=(
            "The Federal Reserve Board today published an assessment of labor "
            "market conditions and the policy transmission channels it monitors."
        ),
    )

    assert "Federal Reserve" in summary
    assert "美國勞工統計局" not in summary


def test_bounded_passage_skips_oversized_sentence_and_uses_later_facts() -> None:
    oversized = "The agency " + ("expanded its explanation " * 40) + "."
    source = (
        f"{oversized}\n"
        "The program now includes two additional settlement windows.\n"
        "Implementation begins on September 1 for participating institutions."
    )

    passage = official_research._bounded_source_passage(
        source,
        max_sentences=2,
        max_chars=180,
    )

    assert passage is not None
    assert "expanded its explanation" not in passage
    assert "two additional settlement windows" in passage
    assert "September 1" in passage
    assert len(passage) <= 180


def test_bounded_passage_rejects_truncated_feed_fragments() -> None:
    passage = official_research._bounded_source_passage(
        "The official feed description ends before the material allegation..."
    )

    assert passage is None


def test_bounded_passage_does_not_join_heading_fragments_to_body_text() -> None:
    source = (
        "The Federal Reserve Board today announced a new supervisory action.\n"
        "Related Materials\n"
        "The order takes effect immediately and applies to the named respondent."
    )

    passage = official_research._bounded_source_passage(source, max_sentences=2)

    assert passage is not None
    assert "new supervisory action" in passage
    assert "order takes effect immediately" in passage
    assert "Related Materials" not in passage


def test_generic_news_without_source_is_explicitly_limited() -> None:
    item = official_research._FeedItem(
        title="Agency publishes an administrative update",
        url="https://www.federalreserve.gov/newsevents/pressreleases/example.htm",
        published_at=NOW,
        publisher="Federal Reserve",
    )

    summary = official_research._news_digest(
        item,
        publication_date=date(2026, 8, 21),
        theme="general",
        source_text=None,
    )

    assert "未能提供足夠內文" in summary
    assert "不會自行補齊" in summary


def test_bls_mixed_inflation_and_jobs_summary_remains_labor_context() -> None:
    item = official_research._FeedItem(
        title="Major Economic Indicators Latest Numbers",
        url="https://www.bls.gov/bls/newsrels.htm",
        published_at=NOW,
        publisher="U.S. Bureau of Labor Statistics",
        description=(
            "Consumer Price Index (CPI): +0.2% in Jul 2026; "
            "Unemployment Rate: 4.1% in Jul 2026"
        ),
    )

    assert official_research._item_theme(item) == "labor"


def test_missing_bls_description_degrades_without_aborting_report_content() -> None:
    class UnavailableDocumentClient:
        def fetch(self, url: str) -> None:
            del url
            raise official_research.OfficialDocumentError(
                "fixture document is unavailable"
            )

    provider = OfficialFeedsResearchProvider(
        fetcher=FixtureFetcher(_fixtures()),
        document_client=UnavailableDocumentClient(),  # type: ignore[arg-type]
        now=lambda: NOW,
        sleep=lambda _: None,
    )

    result = provider.market_news(_request())

    assert "State Employment and Unemployment summary released" in {
        item.title for item in result.data.candidates
    }
    assert "official_document_unavailable" in result.metadata.warning_codes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3-1/2 to 3-3/4 percent", "3.50%–3.75%"),
        ("3-1/0 to 3-3/4 percent", "3-1/0 to 3-3/4 percent"),
        ("3-1/2 to 3-3/0 percent", "3-1/2 to 3-3/0 percent"),
    ],
)
def test_rate_range_formatter_rejects_zero_denominators(
    value: str,
    expected: str,
) -> None:
    assert official_research._format_rate_range(value) == expected


def test_concurrent_section_cache_avoids_duplicate_public_requests() -> None:
    fetcher = FixtureFetcher(_fixtures())
    provider = _provider(fetcher)

    provider.market_news(_request())
    provider.global_macro(_request())

    market_calls = [url for url, _, _ in fetcher.calls if url != FED_RESEARCH_URL]
    assert market_calls == [
        FED_PRESS_RELEASES_URL,
        BLS_LATEST_NUMBERS_URL,
        SEC_PRESS_RELEASES_URL,
    ]
    assert all(
        call[2]["User-Agent"] == DEFAULT_OFFICIAL_USER_AGENT for call in fetcher.calls
    )


def test_default_user_agent_has_fixed_declared_contact_without_control_lines() -> None:
    product, contact = DEFAULT_OFFICIAL_USER_AGENT.split(" ", maxsplit=1)

    assert product == "USMarketIntelligence/1.0"
    assert "@" in contact
    assert "." in contact.rsplit("@", maxsplit=1)[1]
    assert "\r" not in DEFAULT_OFFICIAL_USER_AGENT
    assert "\n" not in DEFAULT_OFFICIAL_USER_AGENT
    assert hashlib.sha256(DEFAULT_OFFICIAL_USER_AGENT.encode()).hexdigest() == (
        "04213c69c7e203a1f4d4be6bfe3b4754b885a081b6f5a555a8e1062854271d68"
    )


def test_partial_feed_failure_still_uses_other_genuine_sources() -> None:
    fetcher = FixtureFetcher(_fixtures(), failures={BLS_LATEST_NUMBERS_URL})
    delays: list[float] = []
    provider = OfficialFeedsResearchProvider(
        fetcher=fetcher,
        now=lambda: NOW,
        sleep=delays.append,
    )

    result = provider.market_news(_request())

    assert len(result.data.candidates) >= 3
    assert all("bls.gov" not in item.sources[0].url for item in result.data.candidates)
    assert result.metadata.attempts == 2
    assert result.metadata.warning_codes == ("official_feed_unavailable_bls_latest",)
    assert fetcher.call_count(BLS_LATEST_NUMBERS_URL) == 2
    assert fetcher.call_count(FED_PRESS_RELEASES_URL) == 1
    assert fetcher.call_count(SEC_PRESS_RELEASES_URL) == 1
    assert delays == [0.25]
    assert all("raw" not in code for code in result.metadata.warning_codes)


def test_transient_feed_failure_recovers_on_second_attempt_without_warning() -> None:
    fetcher = FixtureFetcher(
        _fixtures(), failures_before_success={BLS_LATEST_NUMBERS_URL: 1}
    )
    delays: list[float] = []
    provider = OfficialFeedsResearchProvider(
        fetcher=fetcher,
        now=lambda: NOW,
        sleep=delays.append,
    )

    result = provider.market_news(_request())

    assert result.metadata.attempts == 2
    assert result.metadata.warning_codes == ()
    assert fetcher.call_count(BLS_LATEST_NUMBERS_URL) == 2
    assert fetcher.call_count(FED_PRESS_RELEASES_URL) == 1
    assert fetcher.call_count(SEC_PRESS_RELEASES_URL) == 1
    assert delays == [0.25]


def test_unavailable_optional_feed_reports_actual_bounded_attempts() -> None:
    fetcher = FixtureFetcher(_fixtures(), failures={FED_RESEARCH_URL})
    provider = _provider(fetcher)

    with pytest.raises(TransientProviderError) as raised:
        provider.research_discovery(_request())

    assert raised.value.attempts == 2
    assert fetcher.call_count(FED_RESEARCH_URL) == 2


def test_fewer_than_three_distinct_releases_fails_closed() -> None:
    one_item = b"""<?xml version='1.0'?>
    <rss><channel><item>
      <title>One genuine official release</title>
      <link>https://www.federalreserve.gov/example-one.htm</link>
      <pubDate>Thu, 20 Aug 2026 15:00:00 GMT</pubDate>
    </item></channel></rss>"""
    fetcher = FixtureFetcher(
        {
            FED_PRESS_RELEASES_URL: one_item,
            BLS_LATEST_NUMBERS_URL: one_item,
            SEC_PRESS_RELEASES_URL: one_item,
            FED_RESEARCH_URL: one_item,
        }
    )

    with pytest.raises(PermanentProviderError, match="three distinct releases"):
        _provider(fetcher).market_news(_request())


@pytest.mark.parametrize(
    "payload",
    [
        b"<!DOCTYPE rss [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><rss>&xxe;</rss>",
        "<!DOCTYPE rss [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><rss>&xxe;</rss>".encode(
            "utf-16"
        ),
        b"x" * 16_385,
    ],
)
def test_unsafe_or_oversized_feed_payloads_fail_without_raw_details(
    payload: bytes,
) -> None:
    settings = OfficialFeedsResearchSettings(max_response_bytes=16_384)
    fetcher = FixtureFetcher(
        {
            FED_PRESS_RELEASES_URL: payload,
            BLS_LATEST_NUMBERS_URL: payload,
            SEC_PRESS_RELEASES_URL: payload,
            FED_RESEARCH_URL: payload,
        }
    )
    provider = OfficialFeedsResearchProvider(
        settings,
        fetcher=fetcher,
        now=lambda: NOW,
        sleep=lambda _: None,
    )

    with pytest.raises(TransientProviderError) as raised:
        provider.market_news(_request())

    assert (
        str(raised.value) == "Official public source feeds are temporarily unavailable."
    )
    assert "passwd" not in str(raised.value)


def test_redirect_handler_blocks_nonofficial_and_non_https_destinations() -> None:
    handler = _OfficialRedirectHandler()
    original = Request(FED_PRESS_RELEASES_URL)

    with pytest.raises(ValueError, match="HTTPS allowlist"):
        handler.redirect_request(
            original,
            None,
            302,
            "Found",
            {},
            "https://example.com/redirected-feed.xml",
        )
    with pytest.raises(ValueError, match="HTTPS allowlist"):
        handler.redirect_request(
            original,
            None,
            302,
            "Found",
            {},
            "http://www.federalreserve.gov/feeds/press_all.xml",
        )


class _FakeHeaders:
    def __init__(self, content_type: str) -> None:
        self.content_type = content_type

    def get_content_type(self) -> str:
        return self.content_type


class _FakeResponse:
    def __init__(self, *, final_url: str, content_type: str, payload: bytes) -> None:
        self.final_url = final_url
        self.headers = _FakeHeaders(content_type)
        self.payload = payload
        self.read_limits: list[int] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def geturl(self) -> str:
        return self.final_url

    def read(self, limit: int) -> bytes:
        self.read_limits.append(limit)
        return self.payload[:limit]


class _FakeOpener:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[Request, float]] = []

    def open(self, request: Request, *, timeout: float) -> _FakeResponse:
        self.calls.append((request, timeout))
        return self.response


def _patch_default_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeResponse,
    *,
    settings: OfficialFeedsResearchSettings | None = None,
) -> OfficialFeedsResearchProvider:
    opener = _FakeOpener(response)
    monkeypatch.setattr(
        official_research,
        "build_opener",
        lambda *_: opener,
    )
    return OfficialFeedsResearchProvider(settings, now=lambda: NOW)


def test_default_fetcher_rejects_nonfeed_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(
        final_url=FED_PRESS_RELEASES_URL,
        content_type="text/html",
        payload=b"<html>not a feed</html>",
    )
    provider = _patch_default_fetcher(monkeypatch, response)

    with pytest.raises(ValueError, match="content type"):
        provider._default_fetcher(
            FED_PRESS_RELEASES_URL,
            2.0,
            {"User-Agent": "test official feed client"},
        )

    assert response.read_limits == []


def test_default_fetcher_rechecks_final_redirect_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(
        final_url="https://example.com/redirected.xml",
        content_type="application/rss+xml",
        payload=b"<rss />",
    )
    provider = _patch_default_fetcher(monkeypatch, response)

    with pytest.raises(ValueError, match="HTTPS allowlist"):
        provider._default_fetcher(
            FED_PRESS_RELEASES_URL,
            2.0,
            {"User-Agent": "test official feed client"},
        )

    assert response.read_limits == []


def test_default_fetcher_reads_only_cap_plus_one_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = OfficialFeedsResearchSettings(max_response_bytes=16_384)
    response = _FakeResponse(
        final_url=FED_PRESS_RELEASES_URL,
        content_type="application/rss+xml",
        payload=b"x" * 16_385,
    )
    provider = _patch_default_fetcher(monkeypatch, response, settings=settings)

    with pytest.raises(ValueError, match="oversized"):
        provider._default_fetcher(
            FED_PRESS_RELEASES_URL,
            2.0,
            {"User-Agent": "test official feed client"},
        )

    assert response.read_limits == [16_385]


@pytest.mark.parametrize(
    ("age_days", "expected"),
    [
        (0, "high"),
        (3, "high"),
        (4, "medium"),
        (14, "medium"),
        (15, "low"),
        (30, "low"),
    ],
)
def test_macro_impact_is_bounded_by_actual_event_age(
    age_days: int,
    expected: str,
) -> None:
    report_date = date(2026, 8, 21)
    assert (
        _macro_impact(
            publication_date=report_date - timedelta(days=age_days),
            report_date=report_date,
        )
        == expected
    )


def test_macro_impact_rejects_future_event_date() -> None:
    with pytest.raises(ValueError, match="after the report date"):
        _macro_impact(
            publication_date=date(2026, 8, 22),
            report_date=date(2026, 8, 21),
        )


def test_earnings_capability_is_explicit_and_safe_if_called() -> None:
    fetcher = FixtureFetcher(_fixtures())
    provider = _provider(fetcher)

    result = provider.earnings(_request())

    assert provider.earnings_calendar_available is True
    assert result.data.candidates == []
    assert result.data.confirmed_events == []
    assert result.metadata.source_count == 0
    assert fetcher.calls == []


def test_earnings_orders_confirmed_release_before_call_only_event() -> None:
    release = _sec_event(
        ticker="REL",
        serial=1,
        scheduled_release_at=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        scheduled_release_window="exact_time",
        confirmation_basis="scheduled_results_release",
    )
    call_only = _sec_event(
        ticker="CALL",
        serial=2,
        conference_call_at=datetime(2026, 8, 22, 11, 0, tzinfo=UTC),
        confirmation_basis="earnings_conference_call",
    )

    class FixtureSECEarningsResearcher:
        def search(
            self,
            *,
            target_date: date,
            as_of_date: date,
        ) -> SECEarningsResearchResult:
            return SECEarningsResearchResult(
                target_date=target_date,
                as_of_date=as_of_date,
                events=(call_only, release),
            )

    provider = OfficialFeedsResearchProvider(
        fetcher=FixtureFetcher(_fixtures()),
        sec_earnings_researcher=FixtureSECEarningsResearcher(),  # type: ignore[arg-type]
        now=lambda: NOW,
        sleep=lambda _: None,
    )

    result = provider.earnings(_request())

    assert [event.ticker for event in result.data.confirmed_events] == ["REL", "CALL"]


def test_long_company_name_keeps_earnings_evidence_title_within_schema() -> None:
    event = _sec_event(
        ticker="LONG",
        serial=3,
        company_name="L" * 200,
    )

    converted = official_research._confirmed_earnings_event(event)

    evidence_title = converted.sources[0].title
    assert len(evidence_title) <= 200
    assert evidence_title.endswith(
        f"8-K issuer announcement ({event.accession_number})"
    )
    assert "…" in evidence_title


def test_knowledge_refresh_is_deterministic_and_network_free() -> None:
    fetcher = FixtureFetcher(_fixtures())
    provider = _provider(fetcher)

    first = provider.knowledge_refresh(_request())
    second = provider.knowledge_refresh(_request())

    assert first.data == second.data
    assert first.metadata.source_count == 0
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_timeout_seconds": 0.9},
        {"request_timeout_seconds": 31.0},
        {"max_response_bytes": 10},
        {"market_news_lookback_days": 31},
        {"macro_lookback_days": 91},
        {"research_lookback_days": 181},
        {"user_agent": "short"},
        {"user_agent": "valid agent\r\nInjected: value"},
    ],
)
def test_settings_reject_values_outside_safety_bounds(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        OfficialFeedsResearchSettings(**overrides)
