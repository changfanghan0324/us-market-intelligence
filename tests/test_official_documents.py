from __future__ import annotations

from collections.abc import Mapping
from email.message import Message
from urllib.request import Request

import pytest

from market_intelligence.providers.official_documents import (
    OfficialDocumentClient,
    OfficialDocumentParseError,
    OfficialDocumentSettings,
    OfficialDocumentTooLarge,
    RawOfficialDocumentResponse,
    UnsafeOfficialDocumentUrl,
    UnsupportedOfficialDocumentType,
    _SameAgencyRedirectHandler,
    fetch_linked_fomc_minutes,
    find_fomc_minutes_link,
    is_federal_reserve_fomc_minutes_url,
)

FED_RELEASE_URL = (
    "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260819a.htm"
)
FED_MINUTES_URL = (
    "https://www.federalreserve.gov/monetarypolicy/fomcminutes20260729.htm"
)


class FixtureTransport:
    def __init__(
        self,
        responses: Mapping[str, RawOfficialDocumentResponse],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, float, dict[str, str]]] = []

    def __call__(
        self,
        url: str,
        timeout: float,
        headers: Mapping[str, str],
    ) -> RawOfficialDocumentResponse:
        self.calls.append((url, timeout, dict(headers)))
        return self.responses[url]


def _response(
    url: str, html: str, content_type: str = "text/html; charset=UTF-8"
) -> RawOfficialDocumentResponse:
    return RawOfficialDocumentResponse(
        final_url=url,
        content_type=content_type,
        body=html.encode(),
    )


def test_federal_reserve_article_is_bounded_cleaned_and_same_host_only() -> None:
    markup = """<!doctype html><html><body>
      <nav><p>Outside navigation</p></nav>
      <div id="article">
        <h1>Policy release\u202e spoof</h1>
        <p>The Committee <strong>released</strong> its minutes.
          <script>alert('not article text')</script> Markets can inspect the record.</p>
        <p>Line one<br>line two\u0007.</p>
        <a href="/monetarypolicy/fomcminutes20260729.htm">Read the minutes</a>
        <a href="https://federalreserve.gov/other.htm">Different host</a>
        <a href="https://www.sec.gov/news/example">Other agency</a>
        <a href="javascript:alert(1)">Unsafe scheme</a>
        <footer><p>Footer controls</p></footer>
      </div>
    </body></html>"""
    transport = FixtureTransport({FED_RELEASE_URL: _response(FED_RELEASE_URL, markup)})

    document = OfficialDocumentClient(transport=transport).fetch(FED_RELEASE_URL)

    assert document.final_url == FED_RELEASE_URL
    assert document.agency == "federal_reserve"
    assert document.mime_type == "text/html"
    assert document.paragraphs == (
        "Policy release spoof",
        "The Committee released its minutes. Markets can inspect the record.",
        "Line one line two .",
    )
    assert [(link.text, link.url) for link in document.links] == [
        ("Read the minutes", FED_MINUTES_URL)
    ]
    assert transport.calls[0][1] == 15.0
    assert transport.calls[0][2]["Accept-Encoding"] == "identity"
    assert "@" in transport.calls[0][2]["User-Agent"]


@pytest.mark.parametrize(
    ("url", "markup", "agency", "expected"),
    [
        (
            "https://www.sec.gov/newsroom/press-releases/2026-100",
            """<html><body><header>SEC menu</header><h1>SEC action</h1>
            <p>The Commission issued an order.</p><form><p>Search form</p></form>
            </body></html>""",
            "sec",
            ("SEC action", "The Commission issued an order."),
        ),
        (
            "https://www.bls.gov/news.release/cpi.nr0.htm",
            """<html><body><p>Site chrome</p><main id="bodytext">
            <h1>Consumer Price Index</h1><p>Prices rose during the month.</p>
            <aside><p>Related links</p></aside></main></body></html>""",
            "bls",
            ("Consumer Price Index", "Prices rose during the month."),
        ),
    ],
)
def test_agency_specific_content_boundaries(
    url: str,
    markup: str,
    agency: str,
    expected: tuple[str, ...],
) -> None:
    client = OfficialDocumentClient(
        transport=FixtureTransport({url: _response(url, markup)})
    )

    document = client.fetch(url)

    assert document.agency == agency
    assert document.paragraphs == expected


def test_missing_agency_container_fails_closed() -> None:
    markup = (
        "<html><body><main><p>No Federal Reserve article id.</p></main></body></html>"
    )
    client = OfficialDocumentClient(
        transport=FixtureTransport(
            {FED_RELEASE_URL: _response(FED_RELEASE_URL, markup)}
        )
    )

    with pytest.raises(OfficialDocumentParseError, match="expected official content"):
        client.fetch(FED_RELEASE_URL)


def test_transport_final_url_cannot_cross_agency_boundary() -> None:
    redirected = "https://www.sec.gov/news/example"
    transport = FixtureTransport(
        {
            FED_RELEASE_URL: _response(
                redirected,
                "<html><body><p>Crossed agency</p></body></html>",
            )
        }
    )

    with pytest.raises(UnsafeOfficialDocumentUrl, match="agency boundary"):
        OfficialDocumentClient(transport=transport).fetch(FED_RELEASE_URL)


def test_redirect_handler_rejects_cross_agency_redirect_before_request() -> None:
    handler = _SameAgencyRedirectHandler(FED_RELEASE_URL)
    request = Request(FED_RELEASE_URL)

    with pytest.raises(UnsafeOfficialDocumentUrl, match="agency boundary"):
        handler.redirect_request(
            request,
            object(),
            302,
            "Found",
            Message(),
            "https://www.sec.gov/news/example",
        )


def test_redirect_handler_allows_same_agency_https_redirect() -> None:
    handler = _SameAgencyRedirectHandler(FED_RELEASE_URL)
    request = Request(FED_RELEASE_URL)

    redirected = handler.redirect_request(
        request,
        object(),
        302,
        "Found",
        Message(),
        "/monetarypolicy/fomcminutes20260729.htm",
    )

    assert redirected is not None
    assert redirected.full_url == FED_MINUTES_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://www.federalreserve.gov/example.htm",
        "https://federalreserve.gov.evil.example/example.htm",
        "https://user@www.sec.gov/example.htm",
        "https://www.bls.gov:444/example.htm",
        "https://www.sec.gov/example.htm\u202e",
    ],
)
def test_unsafe_document_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeOfficialDocumentUrl):
        OfficialDocumentClient(transport=FixtureTransport({})).fetch(url)


def test_mime_type_and_response_cap_are_enforced_after_custom_transport() -> None:
    non_html = FixtureTransport(
        {
            FED_RELEASE_URL: RawOfficialDocumentResponse(
                final_url=FED_RELEASE_URL,
                content_type="application/pdf",
                body=b"%PDF",
            )
        }
    )
    with pytest.raises(UnsupportedOfficialDocumentType, match="HTML MIME"):
        OfficialDocumentClient(transport=non_html).fetch(FED_RELEASE_URL)

    settings = OfficialDocumentSettings(max_response_bytes=16_384)
    oversized = FixtureTransport(
        {
            FED_RELEASE_URL: RawOfficialDocumentResponse(
                final_url=FED_RELEASE_URL,
                content_type="text/html",
                body=b"x" * 16_385,
            )
        }
    )
    with pytest.raises(OfficialDocumentTooLarge, match="response cap"):
        OfficialDocumentClient(settings, transport=oversized).fetch(FED_RELEASE_URL)


def test_fomc_minutes_link_is_explicit_and_can_be_fetched_with_same_client() -> None:
    release_markup = f"""<html><body><div id="article">
      <p>The Federal Reserve released meeting minutes.</p>
      <a href="{FED_MINUTES_URL}">Minutes</a>
    </div></body></html>"""
    minutes_markup = """<html><body><div id="article">
      <h1>Minutes of the Federal Open Market Committee</h1>
      <p>Participants discussed inflation and labor-market conditions.</p>
    </div></body></html>"""
    transport = FixtureTransport(
        {
            FED_RELEASE_URL: _response(FED_RELEASE_URL, release_markup),
            FED_MINUTES_URL: _response(FED_MINUTES_URL, minutes_markup),
        }
    )
    client = OfficialDocumentClient(transport=transport)

    release = client.fetch(FED_RELEASE_URL)
    link = find_fomc_minutes_link(release)
    minutes = fetch_linked_fomc_minutes(release, client=client)

    assert link is not None
    assert link.url == FED_MINUTES_URL
    assert minutes is not None
    assert minutes.final_url == FED_MINUTES_URL
    assert minutes.paragraphs[-1].startswith("Participants discussed")
    assert [call[0] for call in transport.calls] == [FED_RELEASE_URL, FED_MINUTES_URL]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (FED_MINUTES_URL, True),
        ("https://federalreserve.gov/monetarypolicy/fomcminutes20260729.htm", True),
        ("https://www.federalreserve.gov/monetarypolicy/fomcminutes.htm", False),
        (
            "https://www.federalreserve.gov/monetarypolicy/fomcminutes20260729.htm?download=1",
            False,
        ),
        ("https://www.sec.gov/monetarypolicy/fomcminutes20260729.htm", False),
        ("javascript:fomcminutes20260729.htm", False),
    ],
)
def test_fomc_minutes_url_recognition(url: str, expected: bool) -> None:
    assert is_federal_reserve_fomc_minutes_url(url) is expected


def test_text_and_link_output_bounds_are_applied() -> None:
    paragraphs = "".join(
        f"<p>Paragraph {index} has useful text.</p>" for index in range(6)
    )
    links = "".join(
        f'<a href="/link-{index}.htm">Link {index}</a>' for index in range(5)
    )
    markup = f'<html><body><div id="article">{paragraphs}{links}</div></body></html>'
    settings = OfficialDocumentSettings(
        max_paragraphs=2,
        max_paragraph_chars=100,
        max_total_text_chars=1_000,
        max_links=2,
    )
    client = OfficialDocumentClient(
        settings,
        transport=FixtureTransport(
            {FED_RELEASE_URL: _response(FED_RELEASE_URL, markup)}
        ),
    )

    document = client.fetch(FED_RELEASE_URL)

    assert document.paragraphs == (
        "Paragraph 0 has useful text.",
        "Paragraph 1 has useful text.",
    )
    assert [link.text for link in document.links] == ["Link 0", "Link 1"]


def test_excessive_document_nesting_fails_closed() -> None:
    settings = OfficialDocumentSettings(max_nesting_depth=8)
    nested = "<div>" * 8 + "<p>Useful text.</p>" + "</div>" * 8
    markup = f"<html><body><div id='article'>{nested}</div></body></html>"
    client = OfficialDocumentClient(
        settings,
        transport=FixtureTransport(
            {FED_RELEASE_URL: _response(FED_RELEASE_URL, markup)}
        ),
    )

    with pytest.raises(OfficialDocumentParseError, match="nesting-depth"):
        client.fetch(FED_RELEASE_URL)


def test_flat_optional_close_list_items_do_not_count_as_nested_elements() -> None:
    items = "".join(f"<li>List item {index} has useful text." for index in range(150))
    markup = f"<html><body><div id='article'><ul>{items}</ul></div></body></html>"
    settings = OfficialDocumentSettings(
        max_nesting_depth=8,
        max_paragraphs=200,
    )
    client = OfficialDocumentClient(
        settings,
        transport=FixtureTransport(
            {FED_RELEASE_URL: _response(FED_RELEASE_URL, markup)}
        ),
    )

    document = client.fetch(FED_RELEASE_URL)

    assert len(document.paragraphs) == 150
    assert document.paragraphs[0] == "List item 0 has useful text."
    assert document.paragraphs[-1] == "List item 149 has useful text."


@pytest.mark.parametrize("max_nesting_depth", [True, 7, 513])
def test_document_nesting_setting_is_safely_bounded(
    max_nesting_depth: int,
) -> None:
    with pytest.raises(ValueError, match="max_nesting_depth"):
        OfficialDocumentSettings(max_nesting_depth=max_nesting_depth)
