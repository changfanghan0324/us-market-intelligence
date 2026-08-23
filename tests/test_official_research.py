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

    def __call__(
        self, url: str, timeout: float, headers: Any
    ) -> bytes:
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
    assert all(item.sources[0].quoted_fragment is None for item in result.data.candidates)
    assert all(
        any(
            (item.sources[0].url.split("/", 3)[2]).endswith(domain)
            for domain in OFFICIAL_ALLOWED_DOMAINS
        )
        for item in result.data.candidates
    )
    assert result.metadata.provider == "official_public_sources"
    assert result.metadata.model == "deterministic_rules_v1"
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
                "<?xml version='1.0'?><rss><channel>"
                f"{healthy_items}</channel></rss>"
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
    assert crossed.sources[0].published_at == datetime(
        2026, 8, 21, 0, 30, tzinfo=UTC
    )
    assert crossed.event_date == date(2026, 8, 20)


def test_global_macro_and_research_use_real_official_entries() -> None:
    fetcher = FixtureFetcher(_fixtures())
    provider = _provider(fetcher)

    macro = provider.global_macro(_request())
    research = provider.research_discovery(_request())

    assert macro.data.event.global_event == (
        "State Employment and Unemployment summary released"
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
    assert research.data.discovery.has_current_market_implication is False
    assert research.data.discovery.impact_analysis is None


def test_boiler_room_is_regulation_not_an_oil_or_macro_event() -> None:
    assert _theme("SEC Charges Boiler Room Operator in Investment Scam") == "regulation"
    assert _theme("OIL-PRICE policy update") == "energy"
    assert _theme("INTEREST-RATE policy update") == "monetary"
    assert _theme("Minutes of the Federal Open Market Committee") == "monetary"

    result = _provider(FixtureFetcher(_fixtures())).global_macro(_request())

    assert result.data.event.global_event == (
        "State Employment and Unemployment summary released"
    )
    assert "Boiler Room" not in result.data.event.global_event


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
        call[2]["User-Agent"] == DEFAULT_OFFICIAL_USER_AGENT
        for call in fetcher.calls
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
    assert result.metadata.warning_codes == (
        "official_feed_unavailable_bls_latest",
    )
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

    assert str(raised.value) == "Official public source feeds are temporarily unavailable."
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
    assert _macro_impact(
        publication_date=report_date - timedelta(days=age_days),
        report_date=report_date,
    ) == expected


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

    assert provider.earnings_calendar_available is False
    assert result.data.candidates == []
    assert result.metadata.source_count == 0
    assert fetcher.calls == []


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
def test_settings_reject_values_outside_safety_bounds(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        OfficialFeedsResearchSettings(**overrides)
