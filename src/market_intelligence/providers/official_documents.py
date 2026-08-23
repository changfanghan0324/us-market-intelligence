"""Bounded, in-memory extraction for allowlisted U.S. agency HTML pages.

The research feed adapter treats an RSS link as the discovery boundary.  This
module provides the narrower second hop used to read that link without turning
the application into a general-purpose web scraper.  Requests are HTTPS-only,
redirects cannot leave the originating agency, response bodies are capped, and
only a small agency-specific content container is parsed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from typing import Final, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

type OfficialAgency = Literal["federal_reserve", "sec", "bls"]

_AGENCY_ROOTS: Final[dict[OfficialAgency, str]] = {
    "federal_reserve": "federalreserve.gov",
    "sec": "sec.gov",
    "bls": "bls.gov",
}
_MAX_URL_LENGTH: Final = 2_048
_HTML_MIME_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
_SAFE_CHARSETS: Final = frozenset(
    {"utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "latin-1", "windows-1252"}
)
_VOID_TAGS: Final = frozenset(
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
_SKIPPED_TAGS: Final = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "nav",
        "footer",
        "header",
        "aside",
        "form",
        "button",
        "select",
        "textarea",
        "option",
    }
)
_BLOCK_TAGS: Final = frozenset(
    {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
)
_IMPLIED_SIBLING_GROUPS: Final[dict[str, frozenset[str]]] = {
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "p": frozenset({"p"}),
    "rt": frozenset({"rt", "rp"}),
    "rp": frozenset({"rt", "rp"}),
    "option": frozenset({"option"}),
    "optgroup": frozenset({"option", "optgroup"}),
    "tr": frozenset({"tr"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
    "thead": frozenset({"thead", "tbody", "tfoot"}),
    "tbody": frozenset({"thead", "tbody", "tfoot"}),
    "tfoot": frozenset({"thead", "tbody", "tfoot"}),
    "colgroup": frozenset({"colgroup"}),
}
_IMPLIED_SIBLING_SCOPES: Final[dict[str, frozenset[str]]] = {
    "li": frozenset({"ol", "ul", "menu"}),
    "dt": frozenset({"dl"}),
    "dd": frozenset({"dl"}),
    "p": frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "details",
            "div",
            "dl",
            "fieldset",
            "figcaption",
            "figure",
            "footer",
            "header",
            "hgroup",
            "main",
            "menu",
            "nav",
            "ol",
            "pre",
            "search",
            "section",
            "table",
            "ul",
        }
    ),
    "rt": frozenset({"ruby"}),
    "rp": frozenset({"ruby"}),
    "option": frozenset({"select", "datalist"}),
    "optgroup": frozenset({"select"}),
    "tr": frozenset({"table", "thead", "tbody", "tfoot"}),
    "td": frozenset({"tr", "table", "thead", "tbody", "tfoot"}),
    "th": frozenset({"tr", "table", "thead", "tbody", "tfoot"}),
    "thead": frozenset({"table"}),
    "tbody": frozenset({"table"}),
    "tfoot": frozenset({"table"}),
    "colgroup": frozenset({"table"}),
}
_FOMC_MINUTES_PATH = re.compile(
    r"^/monetarypolicy/fomcminutes[0-9]{8}\.htm$",
    re.IGNORECASE,
)


class OfficialDocumentError(RuntimeError):
    """Base class for bounded official-document failures."""


class UnsafeOfficialDocumentUrl(OfficialDocumentError):
    """The requested or redirected URL is outside the allowlisted boundary."""


class OfficialDocumentFetchError(OfficialDocumentError):
    """The allowlisted document could not be retrieved."""


class OfficialDocumentTooLarge(OfficialDocumentError):
    """The response exceeded the configured in-memory byte cap."""


class UnsupportedOfficialDocumentType(OfficialDocumentError):
    """The response was not a supported HTML document."""


class OfficialDocumentParseError(OfficialDocumentError):
    """The expected agency content container was not present."""


@dataclass(frozen=True, slots=True)
class OfficialDocumentSettings:
    """Safety and output bounds for official document retrieval."""

    request_timeout_seconds: float = 15.0
    max_response_bytes: int = 1_048_576
    max_paragraphs: int = 240
    max_paragraph_chars: int = 4_000
    max_total_text_chars: int = 120_000
    max_links: int = 80
    max_nesting_depth: int = 128
    user_agent: str = "USMarketIntelligence/1.0 cpeter0814@gmail.com"

    def __post_init__(self) -> None:
        if isinstance(self.request_timeout_seconds, bool) or not (
            1.0 <= self.request_timeout_seconds <= 30.0
        ):
            raise ValueError(
                "official document timeout must be between 1 and 30 seconds"
            )
        if isinstance(self.max_response_bytes, bool) or not (
            16_384 <= self.max_response_bytes <= 5_242_880
        ):
            raise ValueError(
                "official document response cap is outside the safety bound"
            )
        for name, value, minimum, maximum in (
            ("max_paragraphs", self.max_paragraphs, 1, 500),
            ("max_paragraph_chars", self.max_paragraph_chars, 100, 10_000),
            ("max_total_text_chars", self.max_total_text_chars, 1_000, 500_000),
            ("max_links", self.max_links, 1, 200),
            ("max_nesting_depth", self.max_nesting_depth, 8, 512),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the safety bound")
        if (
            not isinstance(self.user_agent, str)
            or not 12 <= len(self.user_agent) <= 300
        ):
            raise ValueError("official document user agent must identify the client")
        if any(character in self.user_agent for character in "\r\n"):
            raise ValueError(
                "official document user agent cannot contain control lines"
            )


@dataclass(frozen=True, slots=True)
class RawOfficialDocumentResponse:
    """Transport result; callers still validate every field after retrieval."""

    final_url: str
    content_type: str
    body: bytes


@dataclass(frozen=True, slots=True)
class OfficialDocumentLink:
    """A cleaned link that stays on the final document's exact host."""

    text: str
    url: str


@dataclass(frozen=True, slots=True)
class OfficialDocument:
    """Bounded, cleaned material extracted from one official HTML document."""

    requested_url: str
    final_url: str
    agency: OfficialAgency
    mime_type: str
    paragraphs: tuple[str, ...]
    links: tuple[OfficialDocumentLink, ...]


type OfficialDocumentTransport = Callable[
    [str, float, Mapping[str, str]], RawOfficialDocumentResponse
]


class OfficialDocumentClient:
    """Fetch and parse allowlisted agency documents without persisting them."""

    def __init__(
        self,
        settings: OfficialDocumentSettings | None = None,
        *,
        transport: OfficialDocumentTransport | None = None,
    ) -> None:
        self.settings = settings or OfficialDocumentSettings()
        self._transport = transport or self._default_transport

    def fetch(self, url: str) -> OfficialDocument:
        requested_url = _canonical_official_document_url(url)
        requested_agency = _agency_for_url(requested_url)
        headers = {
            "Accept": "text/html, application/xhtml+xml;q=0.9",
            "Accept-Encoding": "identity",
            "User-Agent": self.settings.user_agent,
        }
        try:
            response = self._transport(
                requested_url,
                self.settings.request_timeout_seconds,
                headers,
            )
        except OfficialDocumentError:
            raise
        except Exception as error:
            raise OfficialDocumentFetchError(
                "The official HTML document could not be retrieved."
            ) from error

        if not isinstance(response.body, bytes):
            raise OfficialDocumentFetchError(
                "The document transport returned a non-byte body."
            )
        if len(response.body) > self.settings.max_response_bytes:
            raise OfficialDocumentTooLarge(
                "The official HTML document exceeded the configured response cap."
            )

        final_url = _canonical_official_document_url(response.final_url)
        if _agency_for_url(final_url) != requested_agency:
            raise UnsafeOfficialDocumentUrl(
                "The official document redirect crossed an agency boundary."
            )
        mime_type, charset = _parse_content_type(response.content_type)
        if mime_type not in _HTML_MIME_TYPES:
            raise UnsupportedOfficialDocumentType(
                "The official source did not return a supported HTML MIME type."
            )

        try:
            markup = response.body.decode(charset, errors="replace")
        except LookupError as error:
            raise UnsupportedOfficialDocumentType(
                "The official source declared an unsupported character encoding."
            ) from error

        parser = _OfficialBodyParser(
            agency=requested_agency,
            final_url=final_url,
            settings=self.settings,
        )
        try:
            parser.feed(markup)
            parser.close()
        except (RecursionError, UnicodeError) as error:
            raise OfficialDocumentParseError(
                "The official HTML document could not be parsed safely."
            ) from error
        if not parser.found_target:
            raise OfficialDocumentParseError(
                "The expected official content container was not present."
            )

        return OfficialDocument(
            requested_url=requested_url,
            final_url=final_url,
            agency=requested_agency,
            mime_type=mime_type,
            paragraphs=parser.paragraphs,
            links=parser.links,
        )

    def _default_transport(
        self,
        url: str,
        timeout: float,
        headers: Mapping[str, str],
    ) -> RawOfficialDocumentResponse:
        opener = build_opener(_SameAgencyRedirectHandler(url))
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with opener.open(request, timeout=timeout) as response:
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except (TypeError, ValueError):
                        declared_length = -1
                    if declared_length > self.settings.max_response_bytes:
                        raise OfficialDocumentTooLarge(
                            "The official HTML document exceeded the configured response cap."
                        )
                body = response.read(self.settings.max_response_bytes + 1)
        except OfficialDocumentError:
            raise
        except Exception as error:
            raise OfficialDocumentFetchError(
                "The official HTML document could not be retrieved."
            ) from error
        return RawOfficialDocumentResponse(
            final_url=final_url,
            content_type=content_type,
            body=body,
        )


class _SameAgencyRedirectHandler(HTTPRedirectHandler):
    """Reject redirects away from the agency selected by the initial URL."""

    def __init__(self, initial_url: str) -> None:
        super().__init__()
        canonical = _canonical_official_document_url(initial_url)
        self._agency = _agency_for_url(canonical)

    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> Request | None:
        redirected_url = _canonical_official_document_url(urljoin(req.full_url, newurl))
        if _agency_for_url(redirected_url) != self._agency:
            raise UnsafeOfficialDocumentUrl(
                "The official document redirect crossed an agency boundary."
            )
        return super().redirect_request(req, fp, code, msg, headers, redirected_url)


class _OfficialBodyParser(HTMLParser):
    """Small streaming parser for the agency-specific article container."""

    def __init__(
        self,
        *,
        agency: OfficialAgency,
        final_url: str,
        settings: OfficialDocumentSettings,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._agency = agency
        self._final_url = final_url
        self._settings = settings
        self._final_host = urlsplit(final_url).hostname or ""
        self._stack: list[str] = []
        self._target_depth: int | None = None
        self._skip_depth: int | None = None
        self._block_depth: int | None = None
        self._block_parts: list[str] = []
        self._anchor_depth: int | None = None
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self._paragraphs: list[str] = []
        self._links: list[OfficialDocumentLink] = []
        self._seen_links: set[str] = set()
        self._total_text_chars = 0
        self.found_target = False

    @property
    def paragraphs(self) -> tuple[str, ...]:
        return tuple(self._paragraphs)

    @property
    def links(self) -> tuple[OfficialDocumentLink, ...]:
        return tuple(self._links)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in _VOID_TAGS:
            if normalized_tag == "br" and self._inside_readable_target():
                self._append_text(" ")
            return
        self._close_implied_sibling(normalized_tag)
        if len(self._stack) >= self._settings.max_nesting_depth:
            raise OfficialDocumentParseError(
                "The official HTML document exceeded the nesting-depth limit."
            )

        depth = len(self._stack) + 1
        attributes = {
            name.casefold(): value
            for name, value in attrs
            if isinstance(name, str) and value is not None
        }
        if self._target_depth is None and self._is_target(normalized_tag, attributes):
            self._target_depth = depth
            self.found_target = True
        self._stack.append(normalized_tag)

        if not self._inside_target():
            return
        if self._skip_depth is None and normalized_tag in _SKIPPED_TAGS:
            self._skip_depth = depth
            return
        if self._skip_depth is not None:
            return
        if normalized_tag in _BLOCK_TAGS and self._block_depth is None:
            self._block_depth = depth
            self._block_parts = []
        if normalized_tag == "a" and self._anchor_depth is None:
            self._anchor_depth = depth
            self._anchor_href = attributes.get("href")
            self._anchor_parts = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "br" and self._inside_readable_target():
            self._append_text(" ")

    def handle_data(self, data: str) -> None:
        if self._inside_readable_target():
            self._append_text(data)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if self._stack and self._stack[-1] == normalized_tag:
            matching_index = len(self._stack) - 1
        else:
            matching_index = next(
                (
                    index
                    for index in range(len(self._stack) - 1, -1, -1)
                    if self._stack[index] == normalized_tag
                ),
                -1,
            )
            if matching_index < 0:
                return
        self._close_stack_from(matching_index)

    def _close_implied_sibling(self, tag: str) -> None:
        candidates = _IMPLIED_SIBLING_GROUPS.get(tag)
        if candidates is None:
            return
        scope_boundaries = _IMPLIED_SIBLING_SCOPES[tag]
        for index in range(len(self._stack) - 1, -1, -1):
            open_tag = self._stack[index]
            if open_tag in candidates:
                self._close_stack_from(index)
                return
            if open_tag in scope_boundaries:
                return

    def _close_stack_from(self, matching_index: int) -> None:
        matching_depth = matching_index + 1

        if self._anchor_depth is not None and self._anchor_depth >= matching_depth:
            self._finish_anchor()
        if self._block_depth is not None and self._block_depth >= matching_depth:
            self._finish_block()
        if self._skip_depth is not None and self._skip_depth >= matching_depth:
            self._skip_depth = None
        if self._target_depth is not None and self._target_depth >= matching_depth:
            self._target_depth = None
        del self._stack[matching_index:]

    def close(self) -> None:
        super().close()
        self._finish_anchor()
        self._finish_block()

    def _is_target(self, tag: str, attrs: Mapping[str, str]) -> bool:
        if self._agency == "federal_reserve":
            return attrs.get("id", "").casefold() == "article"
        if self._agency == "bls":
            return attrs.get("id", "").casefold() == "bodytext"
        return tag == "body"

    def _inside_target(self) -> bool:
        return self._target_depth is not None and len(self._stack) >= self._target_depth

    def _inside_readable_target(self) -> bool:
        return self._inside_target() and self._skip_depth is None

    def _append_text(self, value: str) -> None:
        if self._block_depth is not None:
            self._block_parts.append(value)
        if self._anchor_depth is not None:
            self._anchor_parts.append(value)

    def _finish_block(self) -> None:
        if self._block_depth is None:
            return
        cleaned = _clean_text("".join(self._block_parts))
        self._block_depth = None
        self._block_parts = []
        if not cleaned or len(self._paragraphs) >= self._settings.max_paragraphs:
            return
        remaining = self._settings.max_total_text_chars - self._total_text_chars
        if remaining <= 0:
            return
        bounded = cleaned[: min(self._settings.max_paragraph_chars, remaining)].rstrip()
        if not bounded:
            return
        self._paragraphs.append(bounded)
        self._total_text_chars += len(bounded)

    def _finish_anchor(self) -> None:
        if self._anchor_depth is None:
            return
        raw_href = self._anchor_href
        text = _clean_text("".join(self._anchor_parts))
        self._anchor_depth = None
        self._anchor_href = None
        self._anchor_parts = []
        if (
            not raw_href
            or len(self._links) >= self._settings.max_links
            or len(raw_href) > _MAX_URL_LENGTH
        ):
            return
        try:
            resolved = _canonical_official_document_url(
                urljoin(self._final_url, raw_href.strip())
            )
        except OfficialDocumentError:
            return
        if (
            urlsplit(resolved).hostname != self._final_host
            or resolved in self._seen_links
        ):
            return
        self._seen_links.add(resolved)
        self._links.append(OfficialDocumentLink(text=text[:300], url=resolved))


def _canonical_official_document_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_URL_LENGTH:
        raise UnsafeOfficialDocumentUrl("The official document URL is invalid.")
    if value != value.strip() or "\\" in value:
        raise UnsafeOfficialDocumentUrl("The official document URL is invalid.")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise UnsafeOfficialDocumentUrl("The official document URL contains controls.")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise UnsafeOfficialDocumentUrl("Official document URLs must use HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeOfficialDocumentUrl(
            "Official document URLs cannot contain credentials."
        )
    try:
        host = parsed.hostname.encode("ascii").decode("ascii").casefold()
        port = parsed.port
    except (UnicodeError, ValueError) as error:
        raise UnsafeOfficialDocumentUrl(
            "The official document host is invalid."
        ) from error
    if port not in (None, 443):
        raise UnsafeOfficialDocumentUrl(
            "Official document URLs cannot use a custom port."
        )
    _agency_for_host(host)
    path = parsed.path or "/"
    netloc = host
    return urlunsplit(("https", netloc, path, parsed.query, ""))


def _agency_for_url(url: str) -> OfficialAgency:
    host = urlsplit(url).hostname
    if host is None:
        raise UnsafeOfficialDocumentUrl("The official document host is missing.")
    return _agency_for_host(host.casefold())


def _agency_for_host(host: str) -> OfficialAgency:
    for agency, root in _AGENCY_ROOTS.items():
        if host == root or host.endswith(f".{root}"):
            return agency
    raise UnsafeOfficialDocumentUrl("The document host is not an allowlisted agency.")


def _parse_content_type(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise UnsupportedOfficialDocumentType(
            "The official source did not declare an HTML MIME type."
        )
    message = Message()
    message["content-type"] = value
    mime_type = message.get_content_type().casefold()
    charset = (message.get_content_charset() or "utf-8").casefold()
    if charset not in _SAFE_CHARSETS:
        raise UnsupportedOfficialDocumentType(
            "The official source declared an unsupported character encoding."
        )
    return mime_type, charset


def _clean_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    without_controls = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf", "Cs"} else character
        for character in normalized
    )
    return re.sub(r"\s+", " ", without_controls).strip()


def is_federal_reserve_fomc_minutes_url(url: str) -> bool:
    """Return whether ``url`` is an explicit dated Federal Reserve minutes page."""

    try:
        canonical = _canonical_official_document_url(url)
    except OfficialDocumentError:
        return False
    parsed = urlsplit(canonical)
    if _agency_for_url(canonical) != "federal_reserve" or parsed.query:
        return False
    return bool(_FOMC_MINUTES_PATH.fullmatch(parsed.path))


def find_fomc_minutes_link(document: OfficialDocument) -> OfficialDocumentLink | None:
    """Find the first explicit dated FOMC-minutes link in a Fed document."""

    if document.agency != "federal_reserve":
        return None
    return next(
        (
            link
            for link in document.links
            if is_federal_reserve_fomc_minutes_url(link.url)
        ),
        None,
    )


def fetch_linked_fomc_minutes(
    document: OfficialDocument,
    *,
    client: OfficialDocumentClient | None = None,
) -> OfficialDocument | None:
    """Fetch the linked dated FOMC minutes, if the source document exposes one."""

    link = find_fomc_minutes_link(document)
    if link is None:
        return None
    return (client or OfficialDocumentClient()).fetch(link.url)
