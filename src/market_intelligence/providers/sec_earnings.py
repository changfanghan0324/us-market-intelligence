"""Bounded, no-key discovery of issuer-confirmed earnings events in SEC filings.

The SEC full-text search service is useful for finding issuer announcements,
but it is not an earnings calendar.  This module therefore exposes a narrow
research result with an explicit bounded-coverage note.  A filing is included
only when its HTML contains future-tense language that ties the requested date
to results publication, a board meeting considering results, or an earnings
conference call.

No filing is persisted.  Search responses and filing HTML are parsed in
memory, and every network request is restricted to a small HTTPS SEC
allowlist with response-size and request-count caps.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Final, Literal
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

SEC_EFTS_SEARCH_URL: Final = "https://efts.sec.gov/LATEST/search-index"
SEC_BOUNDED_COVERAGE_NOTE: Final = (
    "Bounded search of recent SEC 8-K and 6-K filings; this is not a complete "
    "earnings calendar."
)
SEC_SEARCH_UNAVAILABLE_WARNING: Final = (
    "SEC bounded filing search was unavailable; no earnings events were published."
)
SEC_DOCUMENTS_INCOMPLETE_WARNING: Final = (
    "Some matching SEC filing documents were unavailable; bounded coverage may be "
    "incomplete."
)

DEFAULT_SEC_USER_AGENT: Final = "USMarketIntelligence/1.0 cpeter0814@gmail.com"

_SEC_ALLOWED_HOSTS: Final = frozenset({"efts.sec.gov", "www.sec.gov"})
_SEC_ARCHIVE_PATH = re.compile(
    r"/Archives/edgar/data/[1-9][0-9]{0,9}/[0-9]{18}/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\.(?:htm|html)",
    re.IGNORECASE,
)
_ACCESSION_PATTERN = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
_DOCUMENT_NAME_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\.(?:htm|html)", re.IGNORECASE
)
_CIK_PATTERN = re.compile(r"[0-9]{1,10}")
_FORM_PATTERN = re.compile(r"(?:8-K|6-K)(?:/A)?", re.IGNORECASE)
_TICKER_PATTERN = re.compile(r"[A-Z][A-Z0-9.-]{0,9}")
_DISPLAY_NAME_PATTERN = re.compile(
    r"^(?P<company>.+)\s+\((?P<tickers>[A-Z0-9., -]+)\)\s+"
    r"\(CIK\s+(?P<cik>[0-9]{1,10})\)\s*$",
    re.IGNORECASE,
)
_MAX_URL_LENGTH: Final = 2_048
_SEARCH_REQUEST_CAP: Final = 3
_MAX_TEXT_CHARACTERS: Final = 300_000
_NEW_YORK_ZONE: Final = ZoneInfo("America/New_York")

type SECFetcher = Callable[[str, float, Mapping[str, str]], bytes]
type ReleaseWindow = Literal[
    "exact_time", "before_market_open", "after_market_close", "unspecified"
]
type ConfirmationBasis = Literal[
    "scheduled_results_release", "board_meeting_results", "earnings_conference_call"
]


@dataclass(frozen=True, slots=True)
class SECEarningsResearchSettings:
    """Safety and coverage bounds for SEC filing research."""

    request_timeout_seconds: float = 15.0
    max_search_response_bytes: int = 1_048_576
    max_filing_response_bytes: int = 2_097_152
    max_hits_per_query: int = 30
    max_filing_requests: int = 24
    lookback_days: int = 90
    user_agent: str = DEFAULT_SEC_USER_AGENT

    def __post_init__(self) -> None:
        if isinstance(self.request_timeout_seconds, bool) or not (
            1.0 <= self.request_timeout_seconds <= 30.0
        ):
            raise ValueError("SEC timeout must be between 1 and 30 seconds")
        for name, value, maximum in (
            ("max_search_response_bytes", self.max_search_response_bytes, 2_097_152),
            ("max_filing_response_bytes", self.max_filing_response_bytes, 5_242_880),
        ):
            if isinstance(value, bool) or not 16_384 <= value <= maximum:
                raise ValueError(f"{name} is outside the safety bound")
        for name, value, maximum in (
            ("max_hits_per_query", self.max_hits_per_query, 50),
            ("max_filing_requests", self.max_filing_requests, 24),
            ("lookback_days", self.lookback_days, 90),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the safety bound")
        if (
            not isinstance(self.user_agent, str)
            or not 12 <= len(self.user_agent) <= 300
        ):
            raise ValueError("SEC user agent must identify the client")
        if any(character in self.user_agent for character in "\r\n"):
            raise ValueError("SEC user agent cannot contain control lines")


@dataclass(frozen=True, slots=True)
class SECConfirmedEarningsEvent:
    """A raw issuer-confirmed event recovered from a specific SEC filing."""

    company_name: str
    ticker: str
    cik: str
    form: str
    filing_date: date
    target_date: date
    accession_number: str
    filing_url: str
    confirmation_basis: ConfirmationBasis
    scheduled_release_at: datetime | None
    scheduled_release_window: ReleaseWindow
    conference_call_at: datetime | None
    evidence_excerpt: str

    def __post_init__(self) -> None:
        if not 2 <= len(self.company_name) <= 200:
            raise ValueError("SEC event company name is outside the safe length")
        if not _TICKER_PATTERN.fullmatch(self.ticker):
            raise ValueError("SEC event ticker is invalid")
        if not _CIK_PATTERN.fullmatch(self.cik):
            raise ValueError("SEC event CIK is invalid")
        if not _FORM_PATTERN.fullmatch(self.form):
            raise ValueError("SEC event form is not a supported current report")
        if not _ACCESSION_PATTERN.fullmatch(self.accession_number):
            raise ValueError("SEC event accession number is invalid")
        _require_sec_url(self.filing_url, expected_kind="filing")
        if self.filing_date > self.target_date:
            raise ValueError("SEC filing date cannot be after the scheduled event")
        for value in (self.scheduled_release_at, self.conference_call_at):
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError("SEC event timestamps must be timezone-aware")
                if value.astimezone(_NEW_YORK_ZONE).date() != self.target_date:
                    raise ValueError(
                        "SEC event timestamp does not match the New York target date"
                    )
        if (
            self.scheduled_release_at is not None
            and self.scheduled_release_window != "exact_time"
        ):
            raise ValueError(
                "an exact release timestamp requires the exact_time window"
            )
        if not 12 <= len(self.evidence_excerpt) <= 600:
            raise ValueError("SEC event evidence excerpt is outside the safe length")


@dataclass(frozen=True, slots=True)
class SECEarningsResearchResult:
    """Bounded SEC search output, including non-secret operational counts."""

    target_date: date
    as_of_date: date
    events: tuple[SECConfirmedEarningsEvent, ...]
    coverage_note: str = SEC_BOUNDED_COVERAGE_NOTE
    warnings: tuple[str, ...] = ()
    search_requests: int = 0
    filing_requests: int = 0

    def __post_init__(self) -> None:
        if self.target_date <= self.as_of_date:
            raise ValueError("SEC earnings target date must be after the source cutoff")
        if not 0 <= self.search_requests <= _SEARCH_REQUEST_CAP:
            raise ValueError("SEC search request count exceeds the fixed cap")
        if not 0 <= self.filing_requests <= 24:
            raise ValueError("SEC filing request count exceeds the fixed cap")
        if any(event.target_date != self.target_date for event in self.events):
            raise ValueError("SEC result contains an event for another date")


@dataclass(frozen=True, slots=True)
class _SearchHit:
    company_name: str
    ticker: str
    cik: str
    form: str
    filing_date: date
    accession_number: str
    filing_url: str


class ProductionSECFetcher:
    """HTTPS-only SEC fetcher with redirect, media-type, and byte guards."""

    def __init__(self, settings: SECEarningsResearchSettings) -> None:
        self.settings = settings

    def __call__(
        self,
        url: str,
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> bytes:
        request_kind = _require_sec_url(url)
        cap = (
            self.settings.max_search_response_bytes
            if request_kind == "search"
            else self.settings.max_filing_response_bytes
        )
        request = Request(url, headers=dict(headers), method="GET")
        opener = build_opener(_SECRedirectHandler())
        with opener.open(request, timeout=timeout_seconds) as response:
            final_kind = _require_sec_url(response.geturl())
            if final_kind != request_kind:
                raise ValueError("SEC redirect changed the permitted resource kind")
            content_encoding = response.headers.get("Content-Encoding", "identity")
            if content_encoding.casefold() not in {"", "identity"}:
                raise ValueError("unexpected SEC response content encoding")
            content_type = response.headers.get_content_type().casefold()
            if request_kind == "search":
                if content_type != "application/json" and not content_type.endswith(
                    "+json"
                ):
                    raise ValueError("unexpected SEC search content type")
            elif content_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError("unexpected SEC filing content type")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > cap:
                        raise ValueError("oversized SEC response")
                except ValueError as exc:
                    raise ValueError("invalid or oversized SEC content length") from exc
            payload = response.read(cap + 1)
        if len(payload) > cap:
            raise ValueError("oversized SEC response")
        return payload


class SECEarningsResearcher:
    """Find confirmed upcoming results events in a bounded set of SEC filings."""

    def __init__(
        self,
        settings: SECEarningsResearchSettings | None = None,
        *,
        fetcher: SECFetcher | None = None,
    ) -> None:
        self.settings = settings or SECEarningsResearchSettings()
        self._fetcher = fetcher or ProductionSECFetcher(self.settings)

    def search(
        self,
        *,
        target_date: date,
        as_of_date: date,
    ) -> SECEarningsResearchResult:
        if target_date <= as_of_date:
            raise ValueError("SEC earnings target date must be after the source cutoff")

        headers = {
            "Accept": "application/json, text/html;q=0.9",
            "Accept-Encoding": "identity",
            "User-Agent": self.settings.user_agent,
        }
        search_requests = 0
        query_hit_sets: list[list[_SearchHit]] = []
        for query in _search_queries(target_date):
            search_requests += 1
            search_url = _search_url(
                query=query,
                start_date=as_of_date - timedelta(days=self.settings.lookback_days),
                end_date=as_of_date,
                page_size=self.settings.max_hits_per_query,
            )
            try:
                payload = self._fetcher(
                    search_url,
                    self.settings.request_timeout_seconds,
                    headers,
                )
                if len(payload) > self.settings.max_search_response_bytes:
                    raise ValueError("oversized SEC search response")
                hits = _parse_search_hits(
                    payload,
                    as_of_date=as_of_date,
                    target_date=target_date,
                    oldest_filing_date=as_of_date
                    - timedelta(days=self.settings.lookback_days),
                    limit=self.settings.max_hits_per_query,
                )
            except Exception:  # noqa: BLE001 - fixed warning must replace raw errors
                return _empty_result(
                    target_date=target_date,
                    as_of_date=as_of_date,
                    search_requests=search_requests,
                    warning=SEC_SEARCH_UNAVAILABLE_WARNING,
                )
            query_hit_sets.append(hits)

        candidates = _round_robin_unique_hits(
            query_hit_sets,
            limit=self.settings.max_filing_requests,
        )
        events: list[SECConfirmedEarningsEvent] = []
        filing_requests = 0
        document_failure = False
        filing_headers = dict(headers)
        filing_headers["Accept"] = "text/html, application/xhtml+xml;q=0.9"
        for hit in candidates:
            filing_requests += 1
            try:
                payload = self._fetcher(
                    hit.filing_url,
                    self.settings.request_timeout_seconds,
                    filing_headers,
                )
                if len(payload) > self.settings.max_filing_response_bytes:
                    raise ValueError("oversized SEC filing response")
                text = _html_text(payload)
            except Exception:  # noqa: BLE001 - upstream detail must stay private
                document_failure = True
                continue
            event = _confirmed_event(hit, text=text, target_date=target_date)
            if event is not None:
                events.append(event)

        deduplicated = tuple(_deduplicate_events(events))
        warnings = (SEC_DOCUMENTS_INCOMPLETE_WARNING,) if document_failure else ()
        return SECEarningsResearchResult(
            target_date=target_date,
            as_of_date=as_of_date,
            events=deduplicated,
            warnings=warnings,
            search_requests=search_requests,
            filing_requests=filing_requests,
        )


class _SECRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _require_sec_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _FilingTextParser(HTMLParser):
    _SUPPRESSED_TAGS = frozenset({"script", "style", "noscript", "svg"})
    _VOID_TAGS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )
    _BREAK_TAGS = frozenset(
        {"br", "div", "p", "li", "tr", "td", "th", "h1", "h2", "h3", "h4"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0
        self._characters = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized_tag = tag.casefold()
        if self._suppressed_depth:
            # HTML void elements never emit a matching end tag.  Counting one
            # while inside a suppressed container would keep all following
            # filing text suppressed after ``</noscript>`` or ``</svg>``.
            if normalized_tag not in self._VOID_TAGS:
                self._suppressed_depth += 1
            return
        if normalized_tag in self._SUPPRESSED_TAGS:
            self._suppressed_depth = 1
        elif normalized_tag in self._BREAK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if not self._suppressed_depth and tag.casefold() in self._BREAK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in self._VOID_TAGS:
            return
        if self._suppressed_depth:
            self._suppressed_depth -= 1
            return
        if normalized_tag in self._BREAK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._suppressed_depth or self._characters >= _MAX_TEXT_CHARACTERS:
            return
        remaining = _MAX_TEXT_CHARACTERS - self._characters
        accepted = data[:remaining]
        self.parts.append(accepted)
        self._characters += len(accepted)


def _search_queries(target_date: date) -> tuple[str, ...]:
    label = f"{target_date.strftime('%B')} {target_date.day}, {target_date.year}"
    return (
        f'"{label}" "quarterly results"',
        f'"{label}" "earnings conference call"',
        f'"{label}" "board meeting" results',
    )


def _search_url(*, query: str, start_date: date, end_date: date, page_size: int) -> str:
    if isinstance(page_size, bool) or not 1 <= page_size <= 50:
        raise ValueError("SEC search page size is outside the safety bound")
    parameters = urlencode(
        {
            "q": query,
            "dateRange": "custom",
            "startdt": start_date.isoformat(),
            "enddt": end_date.isoformat(),
            "forms": "8-K,6-K",
            # Each of the three semantic queries reads exactly one bounded
            # result page.  No pagination can silently exceed the fixed
            # three-search-request cap.
            "from": 0,
            "size": page_size,
        }
    )
    url = f"{SEC_EFTS_SEARCH_URL}?{parameters}"
    _require_sec_url(url, expected_kind="search")
    return url


def _parse_search_hits(
    payload: bytes,
    *,
    as_of_date: date,
    target_date: date,
    oldest_filing_date: date,
    limit: int,
) -> list[_SearchHit]:
    if not payload:
        raise ValueError("empty SEC search response")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid SEC search response") from exc
    if not isinstance(document, dict):
        raise TypeError("invalid SEC search root")
    hits_container = document.get("hits")
    if not isinstance(hits_container, dict) or not isinstance(
        hits_container.get("hits"), list
    ):
        raise TypeError("invalid SEC search hits")

    parsed: list[_SearchHit] = []
    for raw_hit in hits_container["hits"][:limit]:
        hit = _parse_search_hit(raw_hit)
        if hit is None:
            continue
        if not oldest_filing_date <= hit.filing_date <= as_of_date:
            continue
        if hit.filing_date >= target_date:
            continue
        parsed.append(hit)
    return parsed


def _parse_search_hit(value: object) -> _SearchHit | None:
    if not isinstance(value, dict):
        return None
    source = value.get("_source")
    identifier = value.get("_id")
    if not isinstance(source, dict) or not isinstance(identifier, str):
        return None
    try:
        accession_number, document_name = identifier.split(":", 1)
    except ValueError:
        return None
    if (
        not _ACCESSION_PATTERN.fullmatch(accession_number)
        or not _DOCUMENT_NAME_PATTERN.fullmatch(document_name)
        or source.get("adsh") != accession_number
    ):
        return None

    form_value = source.get("form")
    file_date_value = source.get("file_date")
    ciks = source.get("ciks")
    display_names = source.get("display_names")
    if (
        not isinstance(form_value, str)
        or not _FORM_PATTERN.fullmatch(form_value)
        or not isinstance(file_date_value, str)
        or not isinstance(ciks, list)
        or not ciks
        or not isinstance(display_names, list)
        or not display_names
    ):
        return None
    cik = ciks[0]
    if not isinstance(cik, str) or not _CIK_PATTERN.fullmatch(cik):
        return None
    display_name = display_names[0]
    if not isinstance(display_name, str):
        return None
    parsed_display = _parse_display_name(display_name, expected_cik=cik)
    if parsed_display is None:
        return None
    company_name, ticker = parsed_display
    try:
        filing_date = date.fromisoformat(file_date_value)
        filing_url = _archive_url(
            cik=cik,
            accession_number=accession_number,
            document_name=document_name,
        )
    except ValueError:
        return None
    return _SearchHit(
        company_name=company_name,
        ticker=ticker,
        cik=cik.zfill(10),
        form=form_value.upper(),
        filing_date=filing_date,
        accession_number=accession_number,
        filing_url=filing_url,
    )


def _parse_display_name(value: str, *, expected_cik: str) -> tuple[str, str] | None:
    cleaned = _clean_text(value, maximum=300)
    match = _DISPLAY_NAME_PATTERN.fullmatch(cleaned)
    if match is None or int(match.group("cik")) != int(expected_cik):
        return None
    tickers = [part.strip().upper() for part in match.group("tickers").split(",")]
    ticker = next(
        (candidate for candidate in tickers if _TICKER_PATTERN.fullmatch(candidate)),
        None,
    )
    if ticker is None:
        return None
    company_name = _clean_text(match.group("company"), maximum=200)
    if len(company_name) < 2:
        return None
    return company_name, ticker


def _archive_url(*, cik: str, accession_number: str, document_name: str) -> str:
    if (
        not _CIK_PATTERN.fullmatch(cik)
        or not _ACCESSION_PATTERN.fullmatch(accession_number)
        or not _DOCUMENT_NAME_PATTERN.fullmatch(document_name)
    ):
        raise ValueError("invalid SEC archive coordinates")
    normalized_cik = str(int(cik))
    compact_accession = accession_number.replace("-", "")
    url = (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{normalized_cik}/{compact_accession}/{document_name}"
    )
    _require_sec_url(url, expected_kind="filing")
    return url


def _html_text(payload: bytes) -> str:
    if not payload or b"\x00" in payload:
        raise ValueError("empty or binary SEC filing")
    parser = _FilingTextParser()
    try:
        parser.feed(payload.decode("utf-8", errors="replace"))
        parser.close()
    except Exception as exc:
        raise ValueError("invalid SEC filing HTML") from exc
    text = _clean_text(" ".join(parser.parts), maximum=_MAX_TEXT_CHARACTERS)
    if len(text) < 20:
        raise ValueError("SEC filing contains no usable text")
    return text


def _confirmed_event(
    hit: _SearchHit,
    *,
    text: str,
    target_date: date,
) -> SECConfirmedEarningsEvent | None:
    relevant_sentences = _sentences_with_target_date(text, target_date)
    release_sentences = [
        sentence
        for sentence in relevant_sentences
        if _is_release_confirmation(sentence)
    ]
    board_sentences = [
        sentence
        for sentence in relevant_sentences
        if _is_board_results_confirmation(sentence)
    ]
    call_sentences = [
        sentence for sentence in relevant_sentences if _is_call_confirmation(sentence)
    ]
    if not release_sentences and not board_sentences and not call_sentences:
        return None

    if release_sentences:
        basis: ConfirmationBasis = "scheduled_results_release"
        primary_sentence = release_sentences[0]
    elif board_sentences:
        basis = "board_meeting_results"
        primary_sentence = board_sentences[0]
    else:
        basis = "earnings_conference_call"
        primary_sentence = call_sentences[0]

    scheduled_release_at: datetime | None = None
    release_window: ReleaseWindow = "unspecified"
    for sentence in release_sentences:
        scheduled_release_at = _explicit_time(sentence, target_date)
        if scheduled_release_at is not None:
            release_window = "exact_time"
            break
        candidate_window = _release_window(sentence)
        if candidate_window != "unspecified":
            release_window = candidate_window

    conference_call_at: datetime | None = None
    for sentence in call_sentences:
        conference_call_at = _explicit_time(sentence, target_date)
        if conference_call_at is not None:
            break

    excerpts = [primary_sentence]
    if call_sentences and call_sentences[0] != primary_sentence:
        excerpts.append(call_sentences[0])
    evidence_excerpt = _clean_text(" ".join(excerpts), maximum=600)
    if len(evidence_excerpt) < 12:
        return None
    try:
        return SECConfirmedEarningsEvent(
            company_name=hit.company_name,
            ticker=hit.ticker,
            cik=hit.cik,
            form=hit.form,
            filing_date=hit.filing_date,
            target_date=target_date,
            accession_number=hit.accession_number,
            filing_url=hit.filing_url,
            confirmation_basis=basis,
            scheduled_release_at=scheduled_release_at,
            scheduled_release_window=release_window,
            conference_call_at=conference_call_at,
            evidence_excerpt=evidence_excerpt,
        )
    except ValueError:
        return None


def _sentences_with_target_date(text: str, target_date: date) -> list[str]:
    protected = _protect_abbreviations(text)
    date_pattern = _date_pattern(target_date)
    sentences = re.split(r"(?<=[!?;])\s+|(?<=\.)\s+(?=[A-Z0-9])", protected)
    selected: list[str] = []
    for sentence in sentences:
        restored = _restore_abbreviations(sentence)
        if len(restored) > 1_500 or not date_pattern.search(restored):
            continue
        cleaned = _clean_text(restored, maximum=1_500)
        if cleaned and cleaned not in selected:
            selected.append(cleaned)
    return selected


def _date_pattern(target_date: date) -> re.Pattern[str]:
    month = target_date.strftime("%B")
    short_month = target_date.strftime("%b")
    day = target_date.day
    year = target_date.year
    suffix = "(?:st|nd|rd|th)?"
    return re.compile(
        rf"(?:{month}|{short_month}\.?)\s+0?{day}{suffix},?\s+{year}"
        rf"|0?{day}{suffix}\s+(?:{month}|{short_month}\.?),?\s+{year}"
        rf"|{year}[-/]0?{target_date.month}[-/]0?{day}"
        rf"|0?{target_date.month}/0?{day}/{year}",
        re.IGNORECASE,
    )


def _protect_abbreviations(value: str) -> str:
    substitutions = {
        "a.m.": "a\u2024m\u2024",
        "p.m.": "p\u2024m\u2024",
        "U.S.": "U\u2024S\u2024",
        "u.s.": "u\u2024s\u2024",
        "Inc.": "Inc\u2024",
        "Corp.": "Corp\u2024",
        "Aug.": "Aug\u2024",
        "Sept.": "Sept\u2024",
        "Oct.": "Oct\u2024",
        "Nov.": "Nov\u2024",
        "Dec.": "Dec\u2024",
        "Jan.": "Jan\u2024",
        "Feb.": "Feb\u2024",
    }
    protected = value
    for old, new in substitutions.items():
        protected = protected.replace(old, new)
    return protected


def _restore_abbreviations(value: str) -> str:
    return value.replace("\u2024", ".")


def _is_release_confirmation(sentence: str) -> bool:
    # A board notice confirms that results will be considered/published on the
    # date, but it does not establish an exact release time.  Keep that basis
    # distinct so words such as "publication" do not masquerade as a timed
    # results-release announcement.
    if _is_board_results_confirmation(sentence):
        return False
    normalized = sentence.casefold()
    has_results = bool(
        re.search(
            r"\b(?:earnings|financial results|quarterly results|quarter results|"
            r"interim results|annual results|results for|results of)\b",
            normalized,
        )
    )
    has_future = bool(
        re.search(
            r"\b(?:will|shall|plans? to|expects? to|scheduled (?:to|for)|"
            r"to (?:announce|report|release|publish|post|issue))\b",
            normalized,
        )
    )
    has_release_action = bool(
        re.search(r"\b(?:announce|report|release|publish|post|issue)\w*\b", normalized)
    )
    return has_results and has_future and has_release_action


def _is_board_results_confirmation(sentence: str) -> bool:
    normalized = sentence.casefold()
    return (
        "board" in normalized
        and "meeting" in normalized
        and bool(re.search(r"\bwill be held\b|\bscheduled\b", normalized))
        and bool(
            re.search(
                r"\b(?:financial results|quarterly results|quarter results|"
                r"interim results|annual results)\b",
                normalized,
            )
        )
        and bool(re.search(r"\b(?:consider|approv|publicat)\w*\b", normalized))
    )


def _is_call_confirmation(sentence: str) -> bool:
    normalized = sentence.casefold()
    if "replay" in normalized:
        return False
    has_call = bool(
        re.search(
            r"\b(?:earnings|financial results|quarterly results|quarter results|results)"
            r"\s+(?:conference\s+)?call\b",
            normalized,
        )
    )
    has_future = bool(
        re.search(r"\b(?:will|shall|scheduled|plans? to|expects? to)\b", normalized)
    )
    has_host_action = bool(
        re.search(r"\b(?:host|hold|conduct|schedule|take place)\w*\b", normalized)
    )
    return has_call and has_future and has_host_action


def _release_window(sentence: str) -> ReleaseWindow:
    normalized = sentence.casefold()
    if re.search(
        r"\bbefore (?:the )?(?:u\.?s\.? )?(?:stock )?markets? "
        r"(?:open|opens|opening)\b",
        normalized,
    ):
        return "before_market_open"
    if re.search(
        r"\bafter (?:the )?(?:u\.?s\.? )?(?:stock )?markets? "
        r"(?:close|closes|closing)\b|\bfollowing the (?:market )?close\b",
        normalized,
    ):
        return "after_market_close"
    return "unspecified"


_TIME_PATTERN = re.compile(
    r"(?<![0-9])(?P<hour>[0-2]?[0-9])(?:[.:](?P<minute>[0-5][0-9]))?\s*"
    r"(?P<meridiem>a\.?m\.?|p\.?m\.?)?\s*"
    r"(?:\(\s*)?(?P<zone>U\.?S\.?\s+Eastern(?: Standard| Daylight)?\s+[Tt]ime|"
    r"Eastern(?: Standard| Daylight)?\s+[Tt]ime|ET|EST|EDT|"
    r"U\.?S\.?\s+Pacific(?: Standard| Daylight)?\s+[Tt]ime|"
    r"Pacific(?: Standard| Daylight)?\s+[Tt]ime|PT|PST|PDT|UTC|GMT)(?:\s*\))?",
    re.IGNORECASE,
)


def _explicit_time(sentence: str, target_date: date) -> datetime | None:
    for match in _TIME_PATTERN.finditer(sentence):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or "0")
        meridiem = (match.group("meridiem") or "").casefold().replace(".", "")
        if meridiem:
            if not 1 <= hour <= 12:
                continue
            if meridiem == "pm" and hour != 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
        elif hour > 23:
            continue
        tzinfo = _time_zone(match.group("zone"))
        if tzinfo is None:
            continue
        return datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            hour,
            minute,
            tzinfo=tzinfo,
        )
    return None


def _time_zone(value: str) -> timezone | ZoneInfo | None:
    normalized = re.sub(r"[^a-z]", "", value.casefold())
    if normalized in {"et", "easterntime", "useasterntime"}:
        return ZoneInfo("America/New_York")
    if normalized in {"est", "easternstandardtime", "useasternstandardtime"}:
        return timezone(timedelta(hours=-5), name="EST")
    if normalized in {"edt", "easterndaylighttime", "useasterndaylighttime"}:
        return timezone(timedelta(hours=-4), name="EDT")
    if normalized in {"pt", "pacifictime", "uspacifictime"}:
        return ZoneInfo("America/Los_Angeles")
    if normalized in {"pst", "pacificstandardtime", "uspacificstandardtime"}:
        return timezone(timedelta(hours=-8), name="PST")
    if normalized in {"pdt", "pacificdaylighttime", "uspacificdaylighttime"}:
        return timezone(timedelta(hours=-7), name="PDT")
    if normalized in {"utc", "gmt"}:
        return UTC
    return None


def _round_robin_unique_hits(
    hit_sets: Sequence[Sequence[_SearchHit]], *, limit: int
) -> list[_SearchHit]:
    selected: list[_SearchHit] = []
    seen_urls: set[str] = set()
    width = max((len(hits) for hits in hit_sets), default=0)
    for index in range(width):
        for hits in hit_sets:
            if index >= len(hits):
                continue
            hit = hits[index]
            if hit.filing_url in seen_urls:
                continue
            seen_urls.add(hit.filing_url)
            selected.append(hit)
            if len(selected) == limit:
                return selected
    return selected


def _deduplicate_events(
    events: Sequence[SECConfirmedEarningsEvent],
) -> list[SECConfirmedEarningsEvent]:
    by_issuer: dict[tuple[str, date], SECConfirmedEarningsEvent] = {}
    for event in sorted(
        events, key=lambda item: (item.cik, item.filing_date, item.filing_url)
    ):
        key = (event.cik, event.target_date)
        existing = by_issuer.get(key)
        if existing is None:
            by_issuer[key] = event
            continue
        scheduled_release_at = (
            existing.scheduled_release_at or event.scheduled_release_at
        )
        conference_call_at = existing.conference_call_at or event.conference_call_at
        release_window = existing.scheduled_release_window
        if release_window == "unspecified":
            release_window = event.scheduled_release_window
        if scheduled_release_at is not None:
            release_window = "exact_time"
        preferred = max((existing, event), key=_event_completeness)
        by_issuer[key] = replace(
            preferred,
            scheduled_release_at=scheduled_release_at,
            scheduled_release_window=release_window,
            conference_call_at=conference_call_at,
        )
    return sorted(by_issuer.values(), key=lambda item: (item.ticker, item.filing_url))


def _event_completeness(event: SECConfirmedEarningsEvent) -> tuple[int, int, int]:
    return (
        int(event.confirmation_basis != "earnings_conference_call"),
        int(event.scheduled_release_at is not None)
        + int(event.scheduled_release_window != "unspecified")
        + int(event.conference_call_at is not None),
        -event.filing_date.toordinal(),
    )


def _empty_result(
    *,
    target_date: date,
    as_of_date: date,
    search_requests: int,
    warning: str,
) -> SECEarningsResearchResult:
    return SECEarningsResearchResult(
        target_date=target_date,
        as_of_date=as_of_date,
        events=(),
        warnings=(warning,),
        search_requests=search_requests,
        filing_requests=0,
    )


def _clean_text(value: str, *, maximum: int) -> str:
    value = html.unescape(value)
    value = unicodedata.normalize("NFKC", value)
    value = " ".join(value.split())
    value = "".join(
        character
        for character in value
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    if len(value) <= maximum:
        return value
    return value[: maximum - 1].rstrip() + "…"


def _require_sec_url(
    value: str,
    *,
    expected_kind: Literal["search", "filing"] | None = None,
) -> Literal["search", "filing"]:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAX_URL_LENGTH
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid SEC URL")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid SEC URL port") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or host not in _SEC_ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("SEC URL is outside the HTTPS allowlist")

    if (
        host == "efts.sec.gov"
        and parsed.path == "/LATEST/search-index"
        and parsed.query
    ):
        kind: Literal["search", "filing"] = "search"
    elif (
        host == "www.sec.gov"
        and not parsed.query
        and _SEC_ARCHIVE_PATH.fullmatch(parsed.path)
    ):
        kind = "filing"
    else:
        raise ValueError("SEC URL path is outside the permitted resources")
    if expected_kind is not None and kind != expected_kind:
        raise ValueError("unexpected SEC URL resource kind")
    return kind


__all__ = [
    "DEFAULT_SEC_USER_AGENT",
    "SEC_BOUNDED_COVERAGE_NOTE",
    "SEC_DOCUMENTS_INCOMPLETE_WARNING",
    "SEC_EFTS_SEARCH_URL",
    "SEC_SEARCH_UNAVAILABLE_WARNING",
    "ProductionSECFetcher",
    "SECConfirmedEarningsEvent",
    "SECEarningsResearchResult",
    "SECEarningsResearchSettings",
    "SECEarningsResearcher",
]
