from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from market_intelligence.providers.sec_earnings import (
    SEC_DOCUMENTS_INCOMPLETE_WARNING,
    SEC_EFTS_SEARCH_URL,
    SEC_SEARCH_UNAVAILABLE_WARNING,
    SECEarningsResearcher,
    SECEarningsResearchSettings,
    _require_sec_url,
    _SECRedirectHandler,
)

TARGET_DATE = date(2026, 8, 24)
AS_OF_DATE = date(2026, 8, 23)
XPENG_DOCUMENT_URL = (
    "https://www.sec.gov/Archives/edgar/data/1810997/"
    "000119312526331640/d251912dex991.htm"
)

XPENG_HTML = b"""<!doctype html><html><body>
<h1>NOTICE OF BOARD MEETING</h1>
<p>The board of directors (the "Board") of XPeng Inc. (the "Company") hereby
announces that a meeting of the Board will be held on Monday, August 24, 2026,
for the purposes of, among other matters, considering and approving (i) the
second quarterly results of the Company for the three months ended June 30,
2026 and its publication, and (ii) the interim results of the Company for the
six months ended June 30, 2026 and its publication.</p>
<p>The Company's management will host an earnings conference call at 8:00 a.m.
U.S. Eastern time on August 24, 2026 (8:00 p.m. Beijing/Hong Kong time on
August 24, 2026).</p>
</body></html>"""


def _hit(
    *,
    identifier: str = "0001193125-26-331640:d251912dex991.htm",
    display_name: str = "XPENG INC.  (XPEV, XPNGF)  (CIK 0001810997)",
    cik: str = "0001810997",
    form: str = "6-K",
    filing_date: str = "2026-08-04",
) -> dict[str, Any]:
    return {
        "_id": identifier,
        "_source": {
            "adsh": identifier.split(":", 1)[0],
            "ciks": [cik],
            "display_names": [display_name],
            "file_date": filing_date,
            "form": form,
        },
    }


def _search_payload(*hits: dict[str, Any]) -> bytes:
    return json.dumps({"hits": {"hits": list(hits)}}).encode()


class FixtureFetcher:
    def __init__(
        self,
        *,
        search_payload: bytes,
        documents: dict[str, bytes] | None = None,
        fail_search_call: int | None = None,
        failed_documents: set[str] | None = None,
    ) -> None:
        self.search_payload = search_payload
        self.documents = documents or {}
        self.fail_search_call = fail_search_call
        self.failed_documents = failed_documents or set()
        self.calls: list[tuple[str, float, dict[str, str]]] = []
        self.search_calls = 0

    def __call__(self, url: str, timeout: float, headers: Any) -> bytes:
        self.calls.append((url, timeout, dict(headers)))
        if url.startswith(SEC_EFTS_SEARCH_URL):
            self.search_calls += 1
            if self.search_calls == self.fail_search_call:
                raise OSError("private upstream detail")
            return self.search_payload
        if url in self.failed_documents:
            raise OSError("private filing detail")
        return self.documents[url]


def test_xpeng_board_notice_confirms_event_and_keeps_call_time_separate() -> None:
    fetcher = FixtureFetcher(
        search_payload=_search_payload(_hit()),
        documents={XPENG_DOCUMENT_URL: XPENG_HTML},
    )

    result = SECEarningsResearcher(fetcher=fetcher).search(
        target_date=TARGET_DATE,
        as_of_date=AS_OF_DATE,
    )

    assert result.search_requests == 3
    assert result.filing_requests == 1
    assert result.warnings == ()
    assert len(result.events) == 1
    event = result.events[0]
    assert event.company_name == "XPENG INC."
    assert event.ticker == "XPEV"
    assert event.cik == "0001810997"
    assert event.form == "6-K"
    assert event.filing_date == date(2026, 8, 4)
    assert event.target_date == TARGET_DATE
    assert event.confirmation_basis == "board_meeting_results"
    assert event.scheduled_release_at is None
    assert event.scheduled_release_window == "unspecified"
    assert event.conference_call_at == datetime.fromisoformat(
        "2026-08-24T08:00:00-04:00"
    )
    assert event.filing_url == XPENG_DOCUMENT_URL
    assert "considering and approving" in event.evidence_excerpt

    search_urls = [url for url, _, _ in fetcher.calls if "search-index" in url]
    queries = [parse_qs(urlsplit(url).query)["q"][0] for url in search_urls]
    assert queries == [
        '"August 24, 2026" "quarterly results"',
        '"August 24, 2026" "earnings conference call"',
        '"August 24, 2026" "board meeting" results',
    ]
    assert all(
        parse_qs(urlsplit(url).query)["startdt"] == ["2026-05-25"]
        for url in search_urls
    )
    assert all(
        parse_qs(urlsplit(url).query)["forms"] == ["8-K,6-K"] for url in search_urls
    )
    assert all(parse_qs(urlsplit(url).query)["from"] == ["0"] for url in search_urls)
    assert all(parse_qs(urlsplit(url).query)["size"] == ["30"] for url in search_urls)


def test_search_page_size_matches_the_configured_hit_bound_without_pagination() -> None:
    settings = SECEarningsResearchSettings(max_hits_per_query=7)
    fetcher = FixtureFetcher(search_payload=_search_payload())

    result = SECEarningsResearcher(settings, fetcher=fetcher).search(
        target_date=TARGET_DATE,
        as_of_date=AS_OF_DATE,
    )

    search_urls = [url for url, _, _ in fetcher.calls if "search-index" in url]
    assert result.search_requests == 3
    assert len(search_urls) == 3
    assert all(parse_qs(urlsplit(url).query)["size"] == ["7"] for url in search_urls)
    assert all(parse_qs(urlsplit(url).query)["from"] == ["0"] for url in search_urls)


def test_release_time_and_conference_call_time_are_not_conflated() -> None:
    html = b"""<html><body><p>Example Corp. will release its quarterly results at
    7:00 a.m. Eastern Time on August 24, 2026.</p><p>Management will host an
    earnings conference call at 8:30 a.m. Eastern Time on August 24, 2026.</p>
    </body></html>"""
    fetcher = FixtureFetcher(
        search_payload=_search_payload(_hit()),
        documents={XPENG_DOCUMENT_URL: html},
    )

    event = (
        SECEarningsResearcher(fetcher=fetcher)
        .search(
            target_date=TARGET_DATE,
            as_of_date=AS_OF_DATE,
        )
        .events[0]
    )

    assert event.confirmation_basis == "scheduled_results_release"
    assert event.scheduled_release_at == datetime.fromisoformat(
        "2026-08-24T07:00:00-04:00"
    )
    assert event.scheduled_release_window == "exact_time"
    assert event.conference_call_at == datetime.fromisoformat(
        "2026-08-24T08:30:00-04:00"
    )


@pytest.mark.parametrize(
    ("clock", "zone", "expected_new_york_time"),
    [
        ("12:00 p.m.", "UTC", "2026-08-24T08:00:00-04:00"),
        ("12:00 p.m.", "GMT", "2026-08-24T08:00:00-04:00"),
        ("8:00 a.m.", "Pacific Time", "2026-08-24T11:00:00-04:00"),
    ],
)
def test_foreign_zone_release_time_uses_the_new_york_target_date(
    clock: str,
    zone: str,
    expected_new_york_time: str,
) -> None:
    html = (
        "<html><body><p>Example Corp. will release its quarterly results at "
        f"{clock} {zone} on August 24, 2026.</p></body></html>"
    ).encode()
    fetcher = FixtureFetcher(
        search_payload=_search_payload(_hit()),
        documents={XPENG_DOCUMENT_URL: html},
    )

    event = (
        SECEarningsResearcher(fetcher=fetcher)
        .search(
            target_date=TARGET_DATE,
            as_of_date=AS_OF_DATE,
        )
        .events[0]
    )

    assert event.scheduled_release_at is not None
    assert event.scheduled_release_at.astimezone(
        ZoneInfo("America/New_York")
    ) == datetime.fromisoformat(expected_new_york_time)


@pytest.mark.parametrize(
    ("clock", "zone"),
    [
        ("2:00 a.m.", "UTC"),
        ("2:00 a.m.", "GMT"),
        ("11:30 p.m.", "Pacific Time"),
    ],
)
def test_foreign_zone_release_time_on_another_new_york_date_is_rejected(
    clock: str,
    zone: str,
) -> None:
    html = (
        "<html><body><p>Example Corp. will release its quarterly results at "
        f"{clock} {zone} on August 24, 2026.</p></body></html>"
    ).encode()
    fetcher = FixtureFetcher(
        search_payload=_search_payload(_hit()),
        documents={XPENG_DOCUMENT_URL: html},
    )

    result = SECEarningsResearcher(fetcher=fetcher).search(
        target_date=TARGET_DATE,
        as_of_date=AS_OF_DATE,
    )

    assert result.events == ()
    assert result.warnings == ()


def test_market_window_is_recorded_without_inventing_an_exact_release_time() -> None:
    html = b"""<html><body><p>XPeng will announce its quarterly results before
    the U.S. markets open on August 24, 2026.</p></body></html>"""
    fetcher = FixtureFetcher(
        search_payload=_search_payload(_hit()),
        documents={XPENG_DOCUMENT_URL: html},
    )

    event = (
        SECEarningsResearcher(fetcher=fetcher)
        .search(
            target_date=TARGET_DATE,
            as_of_date=AS_OF_DATE,
        )
        .events[0]
    )

    assert event.scheduled_release_at is None
    assert event.scheduled_release_window == "before_market_open"


def test_replay_expiry_on_target_date_is_not_an_upcoming_earnings_event() -> None:
    replay_only = b"""<html><body><p>Cannae has released its second quarter 2026
    financial results.</p><p>A replay of the earnings conference call will be
    available until August 24, 2026.</p></body></html>"""
    fetcher = FixtureFetcher(
        search_payload=_search_payload(_hit()),
        documents={XPENG_DOCUMENT_URL: replay_only},
    )

    result = SECEarningsResearcher(fetcher=fetcher).search(
        target_date=TARGET_DATE,
        as_of_date=AS_OF_DATE,
    )

    assert result.events == ()
    assert result.warnings == ()


@pytest.mark.parametrize("suppressed_tag", ["noscript", "svg"])
def test_void_tags_inside_suppressed_markup_do_not_hide_following_filing_text(
    suppressed_tag: str,
) -> None:
    html = (
        f"<html><body><{suppressed_tag}><img><br><input>hidden controls"
        f"</{suppressed_tag}><p>Example Corp. will release its quarterly results "
        "before the U.S. markets open on August 24, 2026.</p></body></html>"
    ).encode()
    fetcher = FixtureFetcher(
        search_payload=_search_payload(_hit()),
        documents={XPENG_DOCUMENT_URL: html},
    )

    result = SECEarningsResearcher(fetcher=fetcher).search(
        target_date=TARGET_DATE,
        as_of_date=AS_OF_DATE,
    )

    assert len(result.events) == 1
    assert result.events[0].scheduled_release_window == "before_market_open"
    assert "hidden controls" not in result.events[0].evidence_excerpt


def test_any_search_failure_fails_closed_with_only_the_fixed_warning() -> None:
    fetcher = FixtureFetcher(
        search_payload=_search_payload(_hit()),
        fail_search_call=2,
    )

    result = SECEarningsResearcher(fetcher=fetcher).search(
        target_date=TARGET_DATE,
        as_of_date=AS_OF_DATE,
    )

    assert result.events == ()
    assert result.search_requests == 2
    assert result.filing_requests == 0
    assert result.warnings == (SEC_SEARCH_UNAVAILABLE_WARNING,)
    assert "private" not in " ".join(result.warnings)


def test_oversized_injected_search_response_also_fails_closed() -> None:
    settings = SECEarningsResearchSettings(max_search_response_bytes=16_384)
    fetcher = FixtureFetcher(search_payload=b"{" + b"x" * 16_384)

    result = SECEarningsResearcher(settings, fetcher=fetcher).search(
        target_date=TARGET_DATE,
        as_of_date=AS_OF_DATE,
    )

    assert result.events == ()
    assert result.warnings == (SEC_SEARCH_UNAVAILABLE_WARNING,)
    assert result.search_requests == 1


def test_unavailable_filing_is_item_local_and_uses_a_fixed_warning() -> None:
    fetcher = FixtureFetcher(
        search_payload=_search_payload(_hit()),
        failed_documents={XPENG_DOCUMENT_URL},
    )

    result = SECEarningsResearcher(fetcher=fetcher).search(
        target_date=TARGET_DATE,
        as_of_date=AS_OF_DATE,
    )

    assert result.events == ()
    assert result.filing_requests == 1
    assert result.warnings == (SEC_DOCUMENTS_INCOMPLETE_WARNING,)


def test_duplicate_documents_for_one_issuer_merge_complementary_times() -> None:
    second_identifier = "0001193125-26-331640:d251912dex992.htm"
    second_url = (
        "https://www.sec.gov/Archives/edgar/data/1810997/"
        "000119312526331640/d251912dex992.htm"
    )
    release_html = b"""<html><body><p>XPeng will announce its quarterly results
    before the U.S. markets open on August 24, 2026.</p></body></html>"""
    call_html = b"""<html><body><p>XPeng management will host an earnings
    conference call at 8:00 a.m. Eastern Time on August 24, 2026.</p>
    </body></html>"""
    fetcher = FixtureFetcher(
        search_payload=_search_payload(
            _hit(),
            _hit(identifier=second_identifier),
        ),
        documents={
            XPENG_DOCUMENT_URL: release_html,
            second_url: call_html,
        },
    )

    result = SECEarningsResearcher(fetcher=fetcher).search(
        target_date=TARGET_DATE,
        as_of_date=AS_OF_DATE,
    )

    assert result.filing_requests == 2
    assert len(result.events) == 1
    event = result.events[0]
    assert event.scheduled_release_window == "before_market_open"
    assert event.conference_call_at == datetime.fromisoformat(
        "2026-08-24T08:00:00-04:00"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://efts.sec.gov/LATEST/search-index?q=test",
        "https://efts.sec.gov.evil.example/LATEST/search-index?q=test",
        "https://user@efts.sec.gov/LATEST/search-index?q=test",
        "https://www.sec.gov/Archives/edgar/data/1810997/../../secret.htm",
        (
            "https://www.sec.gov/Archives/edgar/data/1810997/"
            "000119312526331640/document.htm?redirect=1"
        ),
    ],
)
def test_sec_url_guard_rejects_non_allowlisted_resources(url: str) -> None:
    with pytest.raises(ValueError):
        _require_sec_url(url)


def test_redirect_guard_rejects_a_non_sec_destination_before_following_it() -> None:
    handler = _SECRedirectHandler()

    with pytest.raises(ValueError):
        handler.redirect_request(
            None,  # type: ignore[arg-type]
            None,
            302,
            "Found",
            {},
            "https://evil.example/filing.htm",
        )


def test_target_must_be_after_source_cutoff_without_network_access() -> None:
    fetcher = FixtureFetcher(search_payload=_search_payload())

    with pytest.raises(ValueError, match="after the source cutoff"):
        SECEarningsResearcher(fetcher=fetcher).search(
            target_date=AS_OF_DATE,
            as_of_date=AS_OF_DATE,
        )

    assert fetcher.calls == []
