"""No-key research adapter backed by public, official US-government sources.

RSS is used for bounded release discovery.  Sanitized feed descriptions and
transient, size-limited official HTML pages may be read to recover the facts
needed for a source-grounded digest.  Raw documents are never persisted.  This
adapter does not call commercial endpoints or manufacture prices, estimates,
quotations, consensus figures, or observed market reactions.
"""

from __future__ import annotations

import html
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, ClassVar, Final, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import HttpUrl, TypeAdapter, ValidationError

from .base import (
    PermanentProviderError,
    ProviderResult,
    ProviderRunMetadata,
    ProviderUsage,
    ResearchSection,
    TransientProviderError,
)
from .knowledge_catalog import knowledge_refresh_for_date
from .official_documents import (
    OfficialDocument,
    OfficialDocumentClient,
    OfficialDocumentError,
    OfficialDocumentSettings,
    find_fomc_minutes_link,
)
from .openai_research import (
    AIConfirmedEarningsEvent,
    AIEvidence,
    AIGlobalMacroEvent,
    AIImpactAnalysis,
    AIKnowledgeRefresh,
    AIMarketNewsItem,
    AINamedImpactActor,
    AINewsAttentionComponents,
    AINoneIdentifiedActor,
    AIResearchApplication,
    AIResearchDiscovery,
    EarningsResponse,
    GlobalMacroResponse,
    KnowledgeRefreshResponse,
    MarketNewsResponse,
    ResearchDiscoveryResponse,
    ResearchRequest,
)
from .sec_earnings import (
    SEC_DOCUMENTS_INCOMPLETE_WARNING,
    SEC_SEARCH_UNAVAILABLE_WARNING,
    SECConfirmedEarningsEvent,
    SECEarningsResearcher,
    SECEarningsResearchSettings,
)

OFFICIAL_ALLOWED_DOMAINS: Final[tuple[str, ...]] = (
    "bls.gov",
    "federalreserve.gov",
    "sec.gov",
)

FED_PRESS_RELEASES_URL: Final = "https://www.federalreserve.gov/feeds/press_all.xml"
BLS_LATEST_NUMBERS_URL: Final = "https://www.bls.gov/feed/bls_latest.rss"
SEC_PRESS_RELEASES_URL: Final = "https://www.sec.gov/news/pressreleases.rss"
FED_RESEARCH_URL: Final = "https://www.federalreserve.gov/feeds/feds.xml"

DEFAULT_OFFICIAL_USER_AGENT: Final = "USMarketIntelligence/1.0 cpeter0814@gmail.com"
_MODEL_NAME: Final = "deterministic_official_sources_v2"
_MAX_CANDIDATES: Final = 8
_MAX_MACRO_DOCUMENT_ATTEMPTS: Final = 3
_MAX_FEED_ATTEMPTS: Final = 2
_MAX_SOURCE_URL_LENGTH: Final = 2_048
_RETRY_BASE_DELAY_SECONDS: Final = 0.25
_XML_DECLARATION_PATTERN = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_OFFICIAL_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


@dataclass(frozen=True, slots=True)
class OfficialFeedsResearchSettings:
    """Bounded settings for the public-feed adapter."""

    request_timeout_seconds: float = 15.0
    max_response_bytes: int = 1_048_576
    market_news_lookback_days: int = 14
    macro_lookback_days: int = 30
    research_lookback_days: int = 90
    user_agent: str = DEFAULT_OFFICIAL_USER_AGENT

    def __post_init__(self) -> None:
        if isinstance(self.request_timeout_seconds, bool) or not (
            1.0 <= self.request_timeout_seconds <= 30.0
        ):
            raise ValueError("official feed timeout must be between 1 and 30 seconds")
        if isinstance(self.max_response_bytes, bool) or not (
            16_384 <= self.max_response_bytes <= 5_242_880
        ):
            raise ValueError("official feed response cap is outside the safety bound")
        for name, value, maximum in (
            ("market_news_lookback_days", self.market_news_lookback_days, 30),
            ("macro_lookback_days", self.macro_lookback_days, 90),
            ("research_lookback_days", self.research_lookback_days, 180),
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
            raise ValueError("official feed user agent must identify the client")
        if any(character in self.user_agent for character in "\r\n"):
            raise ValueError("official feed user agent cannot contain control lines")


@dataclass(frozen=True, slots=True)
class _FeedDefinition:
    key: str
    url: str
    publisher: str


@dataclass(frozen=True, slots=True)
class _FeedItem:
    title: str
    url: str
    published_at: datetime
    publisher: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class _FeedSnapshot:
    available: bool
    items: tuple[_FeedItem, ...] = ()
    attempts: int = 1


@dataclass(frozen=True, slots=True)
class _FeedCollection:
    items: tuple[_FeedItem, ...]
    attempts: int
    warning_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DocumentMaterial:
    text: str | None
    related_sources: tuple[tuple[str, str], ...] = ()
    warning_code: str | None = None


_MARKET_FEEDS: Final = (
    _FeedDefinition("fed_press", FED_PRESS_RELEASES_URL, "Federal Reserve"),
    _FeedDefinition(
        "bls_latest", BLS_LATEST_NUMBERS_URL, "U.S. Bureau of Labor Statistics"
    ),
    _FeedDefinition(
        "sec_press", SEC_PRESS_RELEASES_URL, "U.S. Securities and Exchange Commission"
    ),
)
_RESEARCH_FEED: Final = _FeedDefinition(
    "fed_research", FED_RESEARCH_URL, "Federal Reserve"
)

type OfficialFeedFetcher = Callable[[str, float, Mapping[str, str]], bytes]
type Clock = Callable[[], datetime]
type Sleeper = Callable[[float], None]


class OfficialFeedsResearchProvider:
    """Implement the research contract without a key or paid API.

    Market-news cards are always based on genuine, distinct RSS entries inside
    the configured lookback.  A quiet or unavailable source window fails
    closed instead of being padded with synthetic events.
    """

    earnings_calendar_available: ClassVar[bool] = True

    def __init__(
        self,
        settings: OfficialFeedsResearchSettings | None = None,
        *,
        fetcher: OfficialFeedFetcher | None = None,
        document_client: OfficialDocumentClient | None = None,
        sec_earnings_researcher: SECEarningsResearcher | None = None,
        now: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self.settings = settings or OfficialFeedsResearchSettings()
        self._fetcher = fetcher or self._default_fetcher
        self._document_client = (
            document_client
            if document_client is not None
            else (
                OfficialDocumentClient(
                    OfficialDocumentSettings(
                        request_timeout_seconds=self.settings.request_timeout_seconds,
                        max_response_bytes=self.settings.max_response_bytes,
                        user_agent=self.settings.user_agent,
                    )
                )
                if fetcher is None
                else None
            )
        )
        self._sec_earnings_researcher = (
            sec_earnings_researcher
            if sec_earnings_researcher is not None
            else (
                SECEarningsResearcher(
                    SECEarningsResearchSettings(
                        request_timeout_seconds=self.settings.request_timeout_seconds,
                        user_agent=self.settings.user_agent,
                    )
                )
                if fetcher is None
                else None
            )
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self._snapshots: dict[str, _FeedSnapshot] = {}
        self._snapshot_lock = threading.Lock()
        self._documents: dict[str, _DocumentMaterial] = {}
        self._document_lock = threading.Lock()

    def market_news(
        self, request: ResearchRequest
    ) -> ProviderResult[MarketNewsResponse]:
        started = time.monotonic()
        collection = self._market_items(request, ResearchSection.MARKET_NEWS)
        if len(collection.items) < 3:
            raise PermanentProviderError(
                "Official feeds did not contain three distinct releases within the configured lookback.",
                section=ResearchSection.MARKET_NEWS,
            )
        # Rank the full eligible set before applying the schema-size cap.  A
        # recency-first slice could otherwise discard an older high-impact
        # release while retaining newer low-impact entries.
        selected_items: list[_FeedItem] = []
        candidates: list[AIMarketNewsItem] = []
        document_warnings: list[str] = []
        source_urls: list[str] = []
        for item in sorted(collection.items, key=_news_rank_key):
            # Only the three report cards need full-document enrichment. The
            # remaining bounded candidates retain their official feed abstract,
            # avoiding unnecessary traffic to public government websites.
            material = (
                self._load_document_material(item)
                if len(candidates) < 3
                else _DocumentMaterial(text=item.description)
            )
            try:
                candidate = _news_item(
                    item,
                    request=request,
                    source_text=material.text,
                    related_sources=material.related_sources,
                )
            except ValidationError:
                # Keep one malformed upstream entry inside the item boundary;
                # it must not abort an otherwise valid required section.
                continue
            selected_items.append(item)
            candidates.append(candidate)
            source_urls.append(item.url)
            source_urls.extend(url for url, _ in material.related_sources)
            if material.warning_code:
                document_warnings.append(material.warning_code)
            if len(candidates) == _MAX_CANDIDATES:
                break
        if len(candidates) < 3:
            raise PermanentProviderError(
                "Official feeds did not contain three distinct valid releases within the configured lookback.",
                section=ResearchSection.MARKET_NEWS,
            )
        data = MarketNewsResponse(candidates=candidates)
        return self._result(
            data,
            request=request,
            section=ResearchSection.MARKET_NEWS,
            started=started,
            source_urls=source_urls,
            attempts=collection.attempts,
            warning_codes=tuple(
                dict.fromkeys((*collection.warning_codes, *document_warnings))
            ),
        )

    def earnings(self, request: ResearchRequest) -> ProviderResult[EarningsResponse]:
        """Return issuer-confirmed SEC events, never a claimed full calendar."""

        if request.target_date is None:
            raise PermanentProviderError(
                "The earnings target date is missing.",
                section=ResearchSection.EARNINGS,
            )
        started = time.monotonic()
        if self._sec_earnings_researcher is None:
            data = EarningsResponse(candidates=[], confirmed_events=[])
            return self._result(
                data,
                request=request,
                section=ResearchSection.EARNINGS,
                started=started,
                source_urls=[],
            )
        result = self._sec_earnings_researcher.search(
            target_date=request.target_date,
            as_of_date=request.report_date,
        )
        ordered_events = sorted(
            result.events,
            key=lambda event: (
                event.scheduled_release_at is None,
                event.scheduled_release_window == "unspecified",
                event.conference_call_at is None,
                event.ticker,
            ),
        )
        events = [_confirmed_earnings_event(event) for event in ordered_events]
        warning_codes = tuple(
            code
            for warning, code in (
                (SEC_SEARCH_UNAVAILABLE_WARNING, "sec_earnings_search_unavailable"),
                (SEC_DOCUMENTS_INCOMPLETE_WARNING, "sec_earnings_documents_incomplete"),
            )
            if warning in result.warnings
        )
        return self._result(
            EarningsResponse(candidates=[], confirmed_events=events),
            request=request,
            section=ResearchSection.EARNINGS,
            started=started,
            source_urls=[event.filing_url for event in result.events],
            warning_codes=warning_codes,
        )

    def global_macro(
        self, request: ResearchRequest
    ) -> ProviderResult[GlobalMacroResponse]:
        started = time.monotonic()
        collection = self._market_items(
            request,
            ResearchSection.GLOBAL_MACRO,
            lookback_days=self.settings.macro_lookback_days,
        )
        macro_items = sorted(
            [
                item
                for item in collection.items
                if _item_theme(item) in {"monetary", "labor", "energy"}
            ],
            key=_macro_rank_key,
        )
        if not macro_items:
            raise PermanentProviderError(
                "Official feeds did not contain a qualifying macro release within the configured lookback.",
                section=ResearchSection.GLOBAL_MACRO,
            )
        selected: _FeedItem | None = None
        event: AIGlobalMacroEvent | None = None
        selected_material = _DocumentMaterial(text=None)
        for item in macro_items[:_MAX_MACRO_DOCUMENT_ATTEMPTS]:
            material = self._load_document_material(item)
            try:
                event = _macro_event(
                    item,
                    request=request,
                    source_text=material.text,
                    related_sources=material.related_sources,
                )
            except (ValidationError, ValueError):
                continue
            selected = item
            selected_material = material
            break
        if selected is None or event is None:
            raise PermanentProviderError(
                "Official feeds did not contain a valid qualifying macro release within the configured lookback.",
                section=ResearchSection.GLOBAL_MACRO,
            )
        data = GlobalMacroResponse(event=event)
        return self._result(
            data,
            request=request,
            section=ResearchSection.GLOBAL_MACRO,
            started=started,
            source_urls=[
                selected.url,
                *(url for url, _ in selected_material.related_sources),
            ],
            attempts=collection.attempts,
            warning_codes=tuple(
                dict.fromkeys(
                    (
                        *collection.warning_codes,
                        *(
                            (selected_material.warning_code,)
                            if selected_material.warning_code
                            else ()
                        ),
                    )
                )
            ),
        )

    def research_discovery(
        self, request: ResearchRequest
    ) -> ProviderResult[ResearchDiscoveryResponse]:
        started = time.monotonic()
        snapshot = self._load_feed(_RESEARCH_FEED)
        if not snapshot.available:
            raise TransientProviderError(
                "The official research feed is temporarily unavailable.",
                section=ResearchSection.RESEARCH_DISCOVERY,
                attempts=snapshot.attempts,
            )
        items = _eligible_items(
            snapshot.items,
            request=request,
            lookback_days=self.settings.research_lookback_days,
        )
        if not items:
            raise PermanentProviderError(
                "The official research feed had no recent eligible entry.",
                section=ResearchSection.RESEARCH_DISCOVERY,
                attempts=snapshot.attempts,
            )
        selected: _FeedItem | None = None
        discovery: AIResearchDiscovery | None = None
        for item in items:
            try:
                discovery = _research_discovery(item, request=request)
            except (ValidationError, ValueError):
                continue
            selected = item
            break
        if selected is None or discovery is None:
            raise PermanentProviderError(
                "The official research feed had no recent valid eligible entry.",
                section=ResearchSection.RESEARCH_DISCOVERY,
                attempts=snapshot.attempts,
            )
        data = ResearchDiscoveryResponse(discovery=discovery)
        return self._result(
            data,
            request=request,
            section=ResearchSection.RESEARCH_DISCOVERY,
            started=started,
            source_urls=[selected.url],
            attempts=snapshot.attempts,
        )

    def knowledge_refresh(
        self, request: ResearchRequest
    ) -> ProviderResult[KnowledgeRefreshResponse]:
        started = time.monotonic()
        knowledge = _knowledge_refresh(request.report_date)
        return self._result(
            KnowledgeRefreshResponse(knowledge=knowledge),
            request=request,
            section=ResearchSection.KNOWLEDGE_REFRESH,
            started=started,
            source_urls=[],
        )

    def _market_items(
        self,
        request: ResearchRequest,
        section: ResearchSection,
        *,
        lookback_days: int | None = None,
    ) -> _FeedCollection:
        pairs = [(feed, self._load_feed(feed)) for feed in _MARKET_FEEDS]
        snapshots = [snapshot for _, snapshot in pairs]
        if not any(snapshot.available for snapshot in snapshots):
            raise TransientProviderError(
                "Official public source feeds are temporarily unavailable.",
                section=section,
            )
        candidates = [item for snapshot in snapshots for item in snapshot.items]
        return _FeedCollection(
            items=tuple(
                _eligible_items(
                    candidates,
                    request=request,
                    lookback_days=(
                        lookback_days or self.settings.market_news_lookback_days
                    ),
                )
            ),
            attempts=max(snapshot.attempts for snapshot in snapshots),
            warning_codes=tuple(
                _feed_warning_code(feed)
                for feed, snapshot in pairs
                if not snapshot.available
            ),
        )

    def _load_document_material(self, item: _FeedItem) -> _DocumentMaterial:
        """Read one allowlisted page once; retain only bounded derived material."""

        if self._document_client is None:
            return _DocumentMaterial(text=item.description)
        # The BLS indicators feed already carries the official data summary.
        # Its item link can point to a generic landing page, so another fetch
        # adds latency without adding event-specific evidence.
        if (
            item.publisher == "U.S. Bureau of Labor Statistics"
            and item.description is not None
            and len(item.description) >= 80
        ):
            return _DocumentMaterial(text=item.description)
        with self._document_lock:
            cached = self._documents.get(item.url)
            if cached is not None:
                return cached
            try:
                document = self._document_client.fetch(item.url)
                documents: list[OfficialDocument] = [document]
                related_sources: list[tuple[str, str]] = []
                minutes_link = find_fomc_minutes_link(document)
                if minutes_link is not None:
                    try:
                        minutes = self._document_client.fetch(minutes_link.url)
                    except OfficialDocumentError:
                        minutes = None
                    if minutes is not None:
                        documents.append(minutes)
                        related_sources.append(
                            (
                                minutes.final_url,
                                "Federal Open Market Committee meeting minutes",
                            )
                        )
                paragraphs = [
                    paragraph
                    for source_document in documents
                    for paragraph in source_document.paragraphs
                ]
                text = "\n".join(paragraphs).strip() or item.description
                material = _DocumentMaterial(
                    text=text,
                    related_sources=tuple(related_sources),
                )
            except OfficialDocumentError:
                material = _DocumentMaterial(
                    text=item.description,
                    warning_code="official_document_unavailable",
                )
            self._documents[item.url] = material
            return material

    def _load_feed(self, feed: _FeedDefinition) -> _FeedSnapshot:
        # The pipeline calls market news and macro concurrently. Holding this
        # short-lived lock around a first fetch prevents duplicate traffic to
        # public government servers; subsequent reads are memory-only.
        with self._snapshot_lock:
            cached = self._snapshots.get(feed.url)
            if cached is not None:
                return cached
            headers = {
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9",
                "Accept-Encoding": "identity",
                "User-Agent": self.settings.user_agent,
            }
            snapshot = _FeedSnapshot(available=False, attempts=_MAX_FEED_ATTEMPTS)
            for attempt in range(1, _MAX_FEED_ATTEMPTS + 1):
                try:
                    payload = self._fetcher(
                        feed.url,
                        self.settings.request_timeout_seconds,
                        headers,
                    )
                    if len(payload) > self.settings.max_response_bytes:
                        raise ValueError("oversized feed")
                    items = _parse_feed(payload, feed)
                    snapshot = _FeedSnapshot(
                        available=True,
                        items=tuple(items),
                        attempts=attempt,
                    )
                    break
                except Exception:  # noqa: BLE001 - raw errors must not escape
                    if attempt < _MAX_FEED_ATTEMPTS:
                        with suppress(Exception):
                            self._sleep(
                                _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                            )
            self._snapshots[feed.url] = snapshot
            return snapshot

    def _default_fetcher(
        self,
        url: str,
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> bytes:
        _require_official_url(url)
        request = Request(url, headers=dict(headers), method="GET")
        opener = build_opener(_OfficialRedirectHandler())
        with opener.open(request, timeout=timeout_seconds) as response:
            final_url = response.geturl()
            _require_official_url(final_url)
            content_type = response.headers.get_content_type().casefold()
            if not any(marker in content_type for marker in ("xml", "rss", "atom")):
                raise ValueError("unexpected feed content type")
            payload = response.read(self.settings.max_response_bytes + 1)
        if len(payload) > self.settings.max_response_bytes:
            raise ValueError("oversized feed")
        return payload

    def _result(
        self,
        data: Any,
        *,
        request: ResearchRequest,
        section: ResearchSection,
        started: float,
        source_urls: Sequence[str],
        attempts: int = 1,
        warning_codes: tuple[str, ...] = (),
    ) -> ProviderResult[Any]:
        accessed_at = _aware_utc(self._now())
        # A historical replay may deliberately use an older source cutoff; the
        # actual access timestamp remains the clock value, while eligibility is
        # determined against request.generated_at before this point.
        unique_sources = len(set(source_urls))
        return ProviderResult(
            data=data,
            accessed_at=accessed_at,
            metadata=ProviderRunMetadata(
                provider="official_public_sources",
                model=_MODEL_NAME,
                section=section,
                attempts=attempts,
                duration_ms=max(0, int((time.monotonic() - started) * 1_000)),
                request_id=None,
                request_ids=(),
                source_count=unique_sources,
                warning_codes=warning_codes,
                usage=ProviderUsage(),
            ),
        )


# Backward-compatible concise name for callers that selected this name while
# the free-mode wiring was being introduced.
OfficialResearchProvider = OfficialFeedsResearchProvider


def _feed_warning_code(feed: _FeedDefinition) -> str:
    return f"official_feed_unavailable_{feed.key}"


class _OfficialRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _require_official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _parse_feed(payload: bytes, feed: _FeedDefinition) -> list[_FeedItem]:
    # The supported official feeds are UTF-8/ASCII-compatible XML. Reject NUL
    # bytes so UTF-16/32 encodings cannot interleave the letters in DOCTYPE or
    # ENTITY and evade the byte-level DTD guard below.
    if not payload or b"\x00" in payload or _XML_DECLARATION_PATTERN.search(payload):
        raise ValueError("unsafe or empty XML feed")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ValueError("invalid XML feed") from exc

    parsed: list[_FeedItem] = []
    for element in root.iter():
        if _local_name(element.tag) not in {"item", "entry"}:
            continue
        title = _child_text(element, "title")
        published = (
            _child_text(element, "pubDate")
            or _child_text(element, "published")
            or _child_text(element, "updated")
        )
        link = _entry_link(element)
        if not title or not published or not link:
            continue
        try:
            published_at = _parse_timestamp(published)
            canonical_url = _canonical_official_url(urljoin(feed.url, link))
            cleaned_title = _clean_title(title)
            description = _clean_feed_description(
                _child_text(element, "description")
                or _child_text(element, "summary")
                or _child_text(element, "content"),
                research_feed=feed.key == "fed_research",
            )
        except ValueError:
            continue
        parsed.append(
            _FeedItem(
                title=cleaned_title,
                url=canonical_url,
                published_at=published_at,
                publisher=feed.publisher,
                description=description,
            )
        )
    return _deduplicate_items(parsed)


def _eligible_items(
    items: Sequence[_FeedItem],
    *,
    request: ResearchRequest,
    lookback_days: int,
) -> list[_FeedItem]:
    try:
        timezone = ZoneInfo(request.timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise PermanentProviderError(
            "The report timezone is unavailable to the official feed provider."
        ) from exc
    cutoff = request.generated_at.astimezone(UTC)
    oldest = request.report_date - timedelta(days=lookback_days)
    eligible = [
        item
        for item in items
        if item.published_at <= cutoff
        and oldest
        <= item.published_at.astimezone(timezone).date()
        <= request.report_date
    ]
    return _deduplicate_items(eligible)


def _deduplicate_items(items: Sequence[_FeedItem]) -> list[_FeedItem]:
    by_url: dict[str, _FeedItem] = {}
    seen_titles: set[str] = set()
    for item in sorted(
        items, key=lambda value: (-value.published_at.timestamp(), value.url)
    ):
        title_key = "".join(
            character.casefold() for character in item.title if character.isalnum()
        )
        if item.url in by_url or title_key in seen_titles:
            continue
        by_url[item.url] = item
        seen_titles.add(title_key)
    return list(by_url.values())


def _confirmed_earnings_event(
    event: SECConfirmedEarningsEvent,
) -> AIConfirmedEarningsEvent:
    timing = _sec_release_timing(event)
    if event.confirmation_basis == "scheduled_results_release":
        basis_copy = "SEC EDGAR 中的公司公告明確安排在目標日公布財務結果"
    elif event.confirmation_basis == "board_meeting_results":
        basis_copy = "SEC EDGAR 中的公司公告明確安排董事會在目標日審議或批准財務結果"
    else:
        basis_copy = "SEC EDGAR 中的公司公告明確安排目標日的財報電話會議"
    if event.scheduled_release_at is not None:
        release_copy = (
            "公告同時確認業績發布時間為 "
            f"{event.scheduled_release_at.astimezone(ZoneInfo('America/New_York')).strftime('%H:%M')} ET。"
        )
    elif event.scheduled_release_window == "before_market_open":
        release_copy = "公告確認發布時段為美股開盤前，但沒有可供取信的精確時間。"
    elif event.scheduled_release_window == "after_market_close":
        release_copy = "公告確認發布時段為美股收盤後，但沒有可供取信的精確時間。"
    else:
        release_copy = "公告沒有確認業績發布的精確時間或盤前/盤後時段。"
    if event.conference_call_at is not None:
        call_copy = (
            "另外，電話會議安排在 "
            f"{event.conference_call_at.astimezone(ZoneInfo('America/New_York')).strftime('%H:%M')} ET；"
            "這是會議時間，不會被冒充為業績發布時間。"
        )
    else:
        call_copy = "本次可驗證文件沒有提供可取信的電話會議時間。"
    summary = (
        f"{basis_copy}：{event.company_name}（{event.ticker}）的目標日為 {event.target_date.isoformat()}。"
        f"{release_copy}{call_copy}"
        "此記錄只證明公司已向 SEC 提交這項安排；它不提供分析師共識、價格反應或全市場完整涵蓋。"
    )
    published_at = datetime.combine(event.filing_date, datetime.min.time(), UTC)
    return AIConfirmedEarningsEvent(
        ticker=event.ticker,
        company_name=event.company_name,
        cik=event.cik,
        form=event.form,
        filing_date=event.filing_date,
        target_date=event.target_date,
        confirmation_basis=event.confirmation_basis,
        release_timing=timing,
        scheduled_release_at=event.scheduled_release_at,
        conference_call_at=event.conference_call_at,
        confirmation_summary=summary,
        sources=[
            AIEvidence(
                title=_earnings_evidence_title(event),
                publisher="U.S. Securities and Exchange Commission",
                url=event.filing_url,
                published_at=published_at,
                evidence_role="primary",
                quoted_fragment=None,
            )
        ],
    )


def _earnings_evidence_title(event: SECConfirmedEarningsEvent) -> str:
    """Keep the filing identifier visible without exceeding evidence bounds."""

    suffix = f" {event.form} issuer announcement ({event.accession_number})"
    company_limit = 200 - len(suffix)
    company = event.company_name
    if len(company) > company_limit:
        company = company[: company_limit - 1].rstrip() + "…"
    return f"{company}{suffix}"


def _sec_release_timing(
    event: SECConfirmedEarningsEvent,
) -> Literal["before_market", "during_market", "after_market", "time_not_confirmed"]:
    if event.scheduled_release_window == "before_market_open":
        return "before_market"
    if event.scheduled_release_window == "after_market_close":
        return "after_market"
    if event.scheduled_release_at is None:
        return "time_not_confirmed"
    local = event.scheduled_release_at.astimezone(ZoneInfo("America/New_York"))
    minute_of_day = local.hour * 60 + local.minute
    if minute_of_day < 9 * 60 + 30:
        return "before_market"
    if minute_of_day < 16 * 60:
        return "during_market"
    return "after_market"


def _news_digest(
    item: _FeedItem,
    *,
    publication_date: date,
    theme: str,
    source_text: str | None,
) -> str:
    """Build a factual paragraph from bounded official text and reviewed rules."""

    title_key = item.title.casefold()
    if theme == "monetary" and (
        "fomc" in title_key or "federal open market committee" in title_key
    ):
        return _fomc_digest(item, publication_date, source_text)
    if item.publisher == "U.S. Bureau of Labor Statistics":
        return _labor_digest(item, publication_date, source_text)
    if _is_sec_enforcement(item):
        return _sec_enforcement_digest(item, publication_date, source_text)
    if _is_fed_enforcement(item):
        return _fed_enforcement_digest(item, publication_date, source_text)
    if "approval of application" in title_key or "announces approval" in title_key:
        return _bank_order_digest(item, publication_date, source_text)

    source_passage = _bounded_source_passage(source_text)
    verified = (
        "本報告把官方頁面中連續、可核對的核心段落整理如下（保留英文原句，避免免費規則式翻譯改變原意）："
        f"「{source_passage}」"
        if source_passage
        else "官方文件本次未能提供足夠內文；目前只能核對發布機構、日期與標題。"
    )
    return (
        f"{item.publisher} 於 {publication_date.isoformat()} 發布「{item.title}」。"
        f"{verified} 這張卡將「已確認的官方事實」與「對市場的條件式影響」分開；"
        "若原文沒有公布數值、相關決策或後續時點，本報告不會自行補齊。"
        "對投資人而言，下一步是用同一官方來源的後續公告或數據驗證方向，而不是只根據標題下單。"
    )


def _fomc_digest(
    item: _FeedItem, publication_date: date, source_text: str | None
) -> str:
    text = source_text or ""
    target = re.search(
        r"maintain(?:ed)? the target range for the federal funds rate (?:at|of) "
        r"([^.;]{3,70}?percent)",
        text,
        re.IGNORECASE,
    )
    dissent = re.search(
        r"([A-Za-z]+) members voted against[^.]{0,180}?preferr(?:ing|ed) "
        r"(?:an )?increase of ([^.;]{3,60})",
        text,
        re.IGNORECASE,
    )
    if target:
        target_copy = _format_rate_range(target.group(1).strip())
        decision = f"會議紀錄證實委員會將聯邦基金利率目標區間維持在 {target_copy}"
        if dissent:
            dissent_count = _english_number_to_digit(dissent.group(1).strip())
            dissent_move = _format_basis_points(dissent.group(2).strip())
            decision += f"，並記載 {dissent_count} 位成員反對，主張上調 {dissent_move}"
        decision += "。"
    else:
        decision = (
            "官方發布頁證實會議紀錄已公開，但本次可用內文並未提供"
            "足以重建投票、利率區間與委員分歧的細節。"
        )
    inflation = (
        "紀錄將通膨仍高於 2% 目標、勞動市場大致穩定、經濟活動仍在擴張並列，"
        "顯示政策兩難是「成長尚可」與「價格壓力未解」同時存在。"
        if re.search(r"inflation remained elevated", text, re.IGNORECASE)
        else "此類紀錄的重點不是公布日本身，而是委員對通膨、就業與增長的權衡是否改變。"
    )
    tightening = (
        "多位與會者表示，若通膨沒有回落，可能仍需要進一步緊縮；"
        "因此這份紀錄比單純「本次按兵不動」更偏鷹。"
        if re.search(
            r"tightening would likely be necessary if inflation did not decline",
            text,
            re.IGNORECASE,
        )
        else "在沒有完整與會者意見前，不應將「公布紀錄」本身解讀成偏鷹或偏鴿。"
    )
    return (
        f"聯準會於 {publication_date.isoformat()} 公布「{item.title}」的官方紀錄。"
        f"{decision}{inflation}{tightening}"
        "市場真正需要重新定價的，是下一次會議前的通膨路徑與政策分歧，而不是只看新聞標題。"
    )


def _labor_digest(
    item: _FeedItem, publication_date: date, source_text: str | None
) -> str:
    text = source_text or ""
    patterns = (
        (
            "消費者物價指數",
            r"Consumer Price Index \(CPI\):\s*([+\-−]?[0-9.,]+%)\s+in\s+([A-Za-z]{3}\s+\d{4})",
        ),
        (
            "失業率",
            r"Unemployment Rate:\s*([+\-−]?[0-9.,]+%)\s+in\s+([A-Za-z]{3}\s+\d{4})",
        ),
        (
            "非農就業",
            r"Payroll Employment:\s*([+\-−]?[0-9.,]+)\s+in\s+([A-Za-z]{3}\s+\d{4})",
        ),
        (
            "平均時薪",
            r"Average Hourly Earnings:\s*([+\-−]?[0-9.,]+%)\s+in\s+([A-Za-z]{3}\s+\d{4})",
        ),
    )
    facts: list[str] = []
    for label, pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            facts.append(f"{label}為 {match.group(1)}（{match.group(2)}）")
    if facts:
        fact_copy = "、".join(facts[:4])
        evidence_sentence = f"官方摘要中可直接核對的數值包括：{fact_copy}。"
    else:
        evidence_sentence = (
            "這份官方更新將勞動市場或物價資料納入同一次宏觀狀態檢查，"
            "但可用內文不足以安全重建完整數值表。"
        )
    return (
        f"{item.publisher} 於 {publication_date.isoformat()} 更新「{item.title}」。"
        f"{evidence_sentence} 閱讀時不能只抓一個數字：通膨決定實質購買力與利率門檻，"
        "失業率、就業增量與薪資則用來判斷需求是否仍有支撐。"
        "專業投資人會把同期趨勢、前值修訂與市場預期一起比較；未經這三步，不應將單期變動當成景氣轉折。"
    )


def _is_enforcement_title(title: str) -> bool:
    title_key = title.casefold()
    return "enforcement" in title_key or "charges" in title_key


def _is_sec_enforcement(item: _FeedItem) -> bool:
    return (
        item.publisher == "U.S. Securities and Exchange Commission"
        and _is_enforcement_title(item.title)
    )


def _is_fed_enforcement(item: _FeedItem) -> bool:
    return item.publisher == "Federal Reserve" and _is_enforcement_title(item.title)


def _sec_enforcement_digest(
    item: _FeedItem, publication_date: date, source_text: str | None
) -> str:
    passage = _bounded_source_passage(source_text, max_sentences=3, max_chars=680)
    evidence = (
        "官方起訴摘要的核心原文如下（保留英文以免規則式翻譯扭曲法律措辭）："
        f"「{passage}」"
        if passage
        else "本次只能核對 SEC 的發布動作與案件標題，無法安全重建起訴書細節。"
    )
    return (
        f"美國證券交易委員會（SEC）於 {publication_date.isoformat()} 發布「{item.title}」。"
        f"{evidence} 公告中的 charged、alleged 或 complaint 代表監管機關提出的指控，"
        "不是法院終局認定，也不能被改寫成其他監管機關的處分或特定金融機構已出現資本問題。"
        "投資上的直接重點是涉案證券、募資或受託管理活動的法律與合規風險；"
        "後續應追蹤起訴文件、被告回應、法院裁定、追繳與罰款，而不是只依案件金額推論整個產業。"
    )


def _fed_enforcement_digest(
    item: _FeedItem, publication_date: date, source_text: str | None
) -> str:
    text = source_text or ""
    records = _fed_enforcement_records(text)
    has_consent_prohibition = bool(
        re.search(r"consent(?:\s+order\s+of)?\s+prohibition", text, re.IGNORECASE)
    )
    if has_consent_prohibition and records:
        conduct_copy = (
            "本頁至少一項記錄提及挪用客戶資金"
            if re.search(r"misappropriation of customer funds", text, re.IGNORECASE)
            else "各當事人的具體所涉行為仍須逐筆以命令原文核對"
        )
        paired_records = "；".join(
            f"{respondent}（前任職機構：{institution}）"
            for respondent, institution in records[:2]
        )
        verified = (
            f"官方內文可逐筆核對的對象包括 {paired_records}；"
            f"文件明載措施為 consent prohibition；{conduct_copy}。"
            "其中 consent 表示當事人同意命令生效，不等於法院或完整對抗程序已對全部事實作出終局裁判；"
            "是否承認事實或責任仍以命令原文為準。"
        )
    else:
        passage = _bounded_source_passage(text, max_sentences=2, max_chars=560)
        verified = (
            "官方頁面的核心原文為："
            f"「{passage}」本報告未能從可用文字逐筆配對當事人、前任職機構與 consent prohibition，"
            "因此不會自行補上禁業令、前雇主或客戶資金等細節。"
            if passage
            else "可用內文不足以安全重建措施類型、對象與所涉行為，本報告不會自行補齊。"
        )
    return (
        f"聯準會於 {publication_date.isoformat()} 公布「{item.title}」。"
        f"{verified} 這不等於公告認定銀行整體出現資本或流動性問題；"
        "只有原文明載時，才能把事件連結到客戶資金保護、員工監督或內控執行。"
        "對銀行投資者而言，關鍵不是將個人案件外推為系統性事件，而是觀察是否還有後續罰款、管理層整改或更廣泛的監理行動。"
    )


def _fed_enforcement_records(text: str) -> tuple[tuple[str, str], ...]:
    """Extract respondent/institution pairs from the same enforcement record."""

    normalized = " ".join(text.split())
    marker = re.compile(
        r"\bConsent(?:\s+order\s+of)?\s+prohibition\s+against\s+",
        re.IGNORECASE,
    )
    starts = list(marker.finditer(normalized))
    record_pattern = re.compile(
        r"^"
        r"(?P<respondent>[A-Z][A-Za-z .'’\-]{1,79}?)\s+"
        r"Former employee of\s+"
        r"(?P<institution>[A-Z][A-Za-z0-9 &.'’(),\-]{1,99}?)"
        r"(?="
        r"\s*,?\s*(?:Misappropriation|Violation|Unsafe or unsound|Breach|"
        r"Embezzlement|Unauthorized|Theft|Fraud)\b"
        r"|$"
        r")",
        re.IGNORECASE,
    )
    records: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(normalized)
        segment = normalized[start.end() : end].strip()
        match = record_pattern.search(segment)
        if match is None:
            continue
        respondent = match.group("respondent").strip(" ,.;")
        if re.search(r"\b(?:consent|prohibition|order)\b", respondent, re.IGNORECASE):
            continue
        institution = match.group("institution").strip(" ,;")
        if institution.endswith(".") and not re.search(
            r"(?:\b[A-Z]\.){2,}$", institution
        ):
            institution = institution[:-1].rstrip()
        record = (
            respondent,
            institution,
        )
        key = (record[0].casefold(), record[1].casefold())
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    return tuple(records)


def _bank_order_digest(
    item: _FeedItem, publication_date: date, source_text: str | None
) -> str:
    text = source_text or ""
    match = re.search(
        r"approval of the application by\s+(.{2,100}?),\s+of\s+(.{2,100}?),\s+to\s+(.{2,180}?)\.",
        text,
        re.IGNORECASE,
    )
    if match:
        company, origin, action = (part.strip() for part in match.groups())
        action_copy = _translate_bank_order_action(action)
        fact = f"官方內文證實，{company}（{origin}）獲准{action_copy}"
    else:
        company_match = re.search(
            r"(?:application by|approval of application by)\s+(.+)$",
            item.title,
            re.IGNORECASE,
        )
        company = company_match.group(1).strip() if company_match else "申請機構"
        fact = f"官方內文證實 {company} 的申請已獲聯準會核准"
    return (
        f"聯準會於 {publication_date.isoformat()} 公布「{item.title}」。{fact}。"
        "這是一項特定機構的法定核准，不是對銀行業盈利、信用週期或貨幣政策的廣泛方向判斷。"
        "對該機構而言，核准消除了一個進入或擴張流程中的法規關卡；對大盤而言，除非後續有實質業務規模、資本配置或監理條件，否則影響應屬局部。"
        "下一步應查核完整 order 的限制條件與實際營運時程，不將「核准」自動等同於「財務貢獻已發生」。"
    )


def _bounded_source_fragment(value: str | None, *, max_words: int = 20) -> str | None:
    if not value:
        return None
    words = " ".join(value.split()).split(" ")
    if len(words) < 5:
        return None
    return " ".join(words[:max_words]).rstrip(" ,;:") + (
        "…" if len(words) > max_words else ""
    )


def _bounded_source_passage(
    value: str | None,
    *,
    max_sentences: int = 3,
    max_chars: int = 650,
) -> str | None:
    """Return complete substantive official sentences, excluding page chrome."""

    if not value:
        return None
    paragraphs = [
        " ".join(paragraph.split())
        for paragraph in re.split(r"(?:\r?\n)+", value)
        if paragraph.strip()
    ]
    if not paragraphs:
        return None
    markers = (
        "The Securities and Exchange Commission today",
        "The Federal Reserve Board on",
        "The Federal Reserve Board today",
        "The Federal Open Market Committee",
        "The U.S. Bureau of Labor Statistics",
    )
    marker_boundary: tuple[int, int] | None = None
    for paragraph_index, paragraph in enumerate(paragraphs):
        positions = [paragraph.find(marker) for marker in markers]
        positions = [position for position in positions if position >= 0]
        if positions:
            marker_boundary = (paragraph_index, min(positions))
            break
    if marker_boundary is not None:
        paragraph_index, character_index = marker_boundary
        paragraphs = [
            paragraphs[paragraph_index][character_index:],
            *paragraphs[paragraph_index + 1 :],
        ]

    selected: list[str] = []
    current_length = 0
    boilerplate = (
        "official websites use .gov",
        "a .gov website belongs",
        "secure .gov websites use https",
        "share sensitive information",
        "for immediate release",
    )
    for paragraph in paragraphs:
        sentences = re.split(
            r"(?<!\b[A-Z]\.)(?<!\b[A-Z][A-Z]\.)(?<!\b[A-Z][a-z]\.)"
            r"(?<=[.!?])\s+(?=[A-Z\"“])",
            paragraph,
        )
        for raw_sentence in sentences:
            sentence = raw_sentence.strip()
            if (
                len(sentence) < 35
                or sentence[-1:] not in {".", "!", "?", "。", "！", "？"}
                or sentence.endswith(("...", "…"))
                or any(phrase in sentence.casefold() for phrase in boilerplate)
            ):
                continue
            added = len(sentence) + (1 if selected else 0)
            if current_length + added > max_chars:
                # An oversized sentence should not hide a later, bounded one.
                continue
            selected.append(sentence)
            current_length += added
            if len(selected) == max_sentences:
                return " ".join(selected)
    return " ".join(selected) or None


def _format_rate_range(value: str) -> str:
    match = re.fullmatch(
        r"(?P<left>\d+)-(?P<left_num>\d+)/(?P<left_den>\d+)\s+to\s+"
        r"(?P<right>\d+)-(?P<right_num>\d+)/(?P<right_den>\d+)\s+percent",
        value,
        re.IGNORECASE,
    )
    if not match:
        return value
    left_denominator = int(match.group("left_den"))
    right_denominator = int(match.group("right_den"))
    if left_denominator == 0 or right_denominator == 0:
        return value
    left = int(match.group("left")) + int(match.group("left_num")) / left_denominator
    right = (
        int(match.group("right")) + int(match.group("right_num")) / right_denominator
    )
    return f"{left:.2f}%–{right:.2f}%"


def _english_number_to_digit(value: str) -> str:
    return {
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
        "ten": "10",
    }.get(value.casefold(), value)


def _format_basis_points(value: str) -> str:
    match = re.search(r"(\d+)\s+basis points?", value, re.IGNORECASE)
    return f"{match.group(1)} 個基點" if match else value


def _translate_bank_order_action(value: str) -> str:
    match = re.fullmatch(
        r"establish a representative office in\s+(.+)", value, re.IGNORECASE
    )
    if match:
        return f"在 {match.group(1).strip()} 設立代表處"
    return value


def _bullish_case(theme: str, title: str) -> str:
    if theme == "monetary":
        return (
            "若後續通膨數據放緩、會議分歧沒有擴大，市場可能將紀錄解讀為聯準會有空間維持而非追加緊縮。"
            "這會壓低尾部升息風險，對長久期資產、住宅敏感板塊與信用利差較有利。"
        )
    if theme == "labor":
        return (
            "若就業、薪資與通膨組合指向需求仍穩定但價格壓力緩和，市場會往軟著陸定價移動。"
            "企業營收預期可獲得支撐，同時不必大幅上調折現率。"
        )
    if "approval" in title.casefold():
        return (
            "若獲准機構能依原計畫啟動業務、且 order 沒有超出預期的限制，法規不確定性將下降。"
            "真正的正面財務影響仍需由後續客戶、收入與成本證明。"
        )
    return (
        "若這次官方行動將問題限制在個案並促成可驗證的整改，市場可能下調法規尾部風險。"
        "但正面判斷需要後續公告顯示沒有擴大至更多個人、產品或機構。"
    )


def _bearish_case(theme: str, title: str) -> str:
    del title
    if theme == "monetary":
        return (
            "若通膨停滯、分歧擴大或後續委員發言強化升息門檻，短端利率與實質利率可能再上移。"
            "估值依賴遠期現金流的成長股、高槓桿借款人與長久期債券會承受較大壓力。"
        )
    if theme == "labor":
        return (
            "若就業同時轉弱、物價或薪資壓力卻未消退，市場將面對滾脹式下修與利率難以快速下降的雙重風險。"
            "消費循環、低品質信用與小型企業的盈利預期可能同時惡化。"
        )
    return (
        "若後續調查顯示問題不是個案，或監理機關要求額外整改、罰款與營運限制，相關機構的成本與聲譽風險將上升。"
        "投資人會調高合規成本假設，並檢查同類業務是否也有控制缺口。"
    )


def _news_item(
    item: _FeedItem,
    *,
    request: ResearchRequest,
    source_text: str | None = None,
    related_sources: Sequence[tuple[str, str]] = (),
) -> AIMarketNewsItem:
    theme = _item_theme(item)
    impact, sectors, scores = _news_classification(theme)
    publication_date = _local_publication_date(item, request)
    grounded_text = source_text or item.description
    return AIMarketNewsItem(
        title=item.title,
        event_date=publication_date,
        market_impact=impact,
        affected_sectors=sectors,
        summary=_complete_text_cap(
            _news_digest(
                item,
                publication_date=publication_date,
                theme=theme,
                source_text=grounded_text,
            ),
            1_180,
        ),
        bullish_case=_bullish_case(theme, item.title),
        bearish_case=_bearish_case(theme, item.title),
        attention_components=AINewsAttentionComponents(**scores),
        impact_analysis=_impact_analysis(
            item,
            macro=False,
            publication_date=publication_date,
            theme=theme,
            source_text=grounded_text,
        ),
        sources=[
            _evidence(item),
            *(
                _related_evidence(item, url=url, title=title)
                for url, title in related_sources
            ),
        ],
    )


def _news_rank_key(item: _FeedItem) -> tuple[float, float, str]:
    """Match the deterministic attention ordering before capping candidates."""

    _, _, components = _news_classification(_item_theme(item))
    attention_score = sum(components.values()) / len(components)
    title = item.title.casefold()
    if "approval of application" in title or "announces approval" in title:
        # Routine institution-specific approvals should not displace an
        # enforcement action with wider compliance implications.
        attention_score -= 1.5
    return (-attention_score, -item.published_at.timestamp(), item.url)


def _macro_rank_key(item: _FeedItem) -> tuple[int, float, float, str]:
    """Prefer policy-transmission events within the eligible macro set."""

    priority = {"monetary": 0, "labor": 1, "energy": 2}.get(_item_theme(item), 3)
    rank = _news_rank_key(item)
    return (priority, *rank)


def _macro_digest(
    item: _FeedItem,
    *,
    publication_date: date,
    theme: str,
    source_text: str | None,
) -> str:
    base = _news_digest(
        item,
        publication_date=publication_date,
        theme=theme,
        source_text=source_text,
    )
    if theme == "monetary":
        transmission = (
            "跨資產傳導上，利率路徑會先改變美債殖利率與實質折現率，再影響股票估值和信用利差；"
            "美國利差若擴大通常支撐美元，而美元與實質利率同向上行往往壓抑無息商品。"
        )
    elif theme == "labor":
        transmission = (
            "跨資產傳導上，偏強就業可同時支撐股票盈利與推高債券殖利率；"
            "美元反應取決於數據如何改變相對利差，商品則在需求預期與實質利率之間權衡。"
        )
    else:
        transmission = (
            "對跨資產投資人而言，應分開直接受到法規或經濟條件影響的機構，"
            "與只會經由風險偏好、利率或美元間接受影響的股票、債券與商品。"
        )
    return _complete_text_cap(f"{base}{transmission}", 1_180)


def _complete_text_cap(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    prefix = value[:maximum]
    boundary = max(
        prefix.rfind("。"), prefix.rfind("."), prefix.rfind("！"), prefix.rfind("？")
    )
    if boundary >= max(80, maximum // 2):
        return prefix[: boundary + 1].rstrip()
    return prefix[: maximum - 1].rstrip() + "…"


def _macro_event(
    item: _FeedItem,
    *,
    request: ResearchRequest,
    source_text: str | None = None,
    related_sources: Sequence[tuple[str, str]] = (),
) -> AIGlobalMacroEvent:
    publication_date = _local_publication_date(item, request)
    theme = _item_theme(item)
    grounded_text = source_text or item.description
    return AIGlobalMacroEvent(
        global_event=item.title,
        event_date=publication_date,
        market_impact=_macro_impact(
            publication_date=publication_date,
            report_date=request.report_date,
        ),
        summary=_macro_digest(
            item,
            publication_date=publication_date,
            theme=theme,
            source_text=grounded_text,
        ),
        affected_assets=["stocks", "bonds", "usd", "commodities"],
        impact_analysis=_impact_analysis(
            item,
            macro=True,
            publication_date=publication_date,
            theme=theme,
            source_text=grounded_text,
        ),
        sources=[
            _evidence(item),
            *(
                _related_evidence(item, url=url, title=title)
                for url, title in related_sources
            ),
        ],
    )


def _macro_impact(
    *, publication_date: date, report_date: date
) -> Literal["high", "medium", "low"]:
    age_days = (report_date - publication_date).days
    if age_days < 0:
        raise ValueError("macro publication date cannot be after the report date")
    if age_days <= 3:
        return "high"
    if age_days <= 14:
        return "medium"
    return "low"


def _impact_analysis(
    item: _FeedItem,
    *,
    macro: bool,
    publication_date: date,
    theme: str | None = None,
    source_text: str | None = None,
) -> AIImpactAnalysis:
    selected_theme = theme or _item_theme(item)
    detail = _impact_detail(
        selected_theme,
        item=item,
        macro=macro,
        publication_date=publication_date,
        source_text=source_text,
    )
    return AIImpactAnalysis(
        what_changed=detail["what_changed"],
        why_it_matters=detail["why_it_matters"],
        beneficiaries=detail["beneficiaries"],
        losers=detail["losers"],
        professional_investor_reaction=detail["professional_reaction"],
        indicators_to_monitor_next=detail["indicators"],
    )


def _impact_detail(
    theme: str,
    *,
    item: _FeedItem,
    macro: bool,
    publication_date: date,
    source_text: str | None,
) -> dict[str, Any]:
    title_key = item.title.casefold()
    scope = "全球跨資產定價" if macro else "美國風險資產定價"
    if theme == "monetary":
        target = re.search(
            r"maintain(?:ed)? the target range for the federal funds rate (?:at|of) "
            r"([^.;]{3,70}?percent)",
            source_text or "",
            re.IGNORECASE,
        )
        decision = (
            f"紀錄確認利率目標區間維持在 {target.group(1).strip()}"
            if target
            else "聯準會公開了新一份政策會議紀錄"
        )
        return {
            "what_changed": (
                f"{item.publisher} 於 {publication_date.isoformat()} 發布「{item.title}」；{decision}。"
                "與當天決議相比，紀錄新增的資訊是委員對通膨持續性、勞動市場與後續緊縮條件的分歧。"
            ),
            "why_it_matters": (
                f"政策路徑是{scope}的折現率起點。即使本次利率沒變，"
                "只要市場上調未來升息或推遲降息機率，美債、美元、股票估值與商品就可以同時重新定價。"
            ),
            "beneficiaries": [
                AINamedImpactActor(
                    kind="named_entity",
                    name="美國短期國債與美元現金工具",
                    rationale="若政策利率維持高檔或升息機率增加，前端收益率和美國利差支撐可能延長。",
                ),
                AINamedImpactActor(
                    kind="named_entity",
                    name="存款成本重定價較慢的大型銀行",
                    rationale="在放款收益率先上升、存款 beta 滞後的條件下，部分淨利差業務可能受益。",
                ),
            ],
            "losers": [
                AINamedImpactActor(
                    kind="named_entity",
                    name="長久期科技與高估值成長股",
                    rationale="未來現金流比重高，實質利率上移會對現值與估值倍數形成較大壓力。",
                ),
                AINamedImpactActor(
                    kind="named_entity",
                    name="高槓桿房地產與浮動利率借款人",
                    rationale="再融資成本與利息負擔對高利率維持時間特別敏感。",
                ),
            ],
            "professional_reaction": (
                "利率團隊會先將紀錄與原始會後聲明逐項比較，拆出「委員分歧」與「經濟預測」的新資訊；"
                "然後通過 OIS/聯邦基金期貨、2 年期美債、實質利率和美元即時反應，確認訊號是否已被定價。"
            ),
            "indicators": [
                "2 年期美債殖利率、聯邦基金期貨與下次會議的隱含利率機率",
                "核心 PCE、CPI 服務項、薪資成長與中長期通膨預期",
                "美元指數、10 年期實質利率與長久期股相對大盤的表現",
                "後續聯準會委員發言是否重複紀錄中的緊縮門檻與風險判斷",
            ],
        }

    if theme == "labor":
        return {
            "what_changed": (
                f"{item.publisher} 於 {publication_date.isoformat()} 更新「{item.title}」，提供可核對的勞動市場或物價快照。"
                "新資訊不只是單一標題，而是就業、失業、薪資與物價對經濟需求的共同訊號。"
            ),
            "why_it_matters": (
                f"勞動需求決定家戶所得與企業成本，並經由通膨與聯準會反應函數影響{scope}。"
                "「就業強、通膨降」與「就業弱、通膨高」對資產的含義完全不同，因此必須聯立讀取。"
            ),
            "beneficiaries": [
                AINamedImpactActor(
                    kind="named_entity",
                    name="美國大眾消費與就業敏感服務業",
                    rationale="若就業與實質薪資保持正成長，家戶現金流會繼續支撐需求。",
                ),
                AINamedImpactActor(
                    kind="named_entity",
                    name="軟著陸情境下的投資級信用債",
                    rationale="成長未明顯轉負且通膨放緩時，違約風險與無風險利率壓力可能同時受控。",
                ),
            ],
            "losers": [
                AINamedImpactActor(
                    kind="named_entity",
                    name="低利潤且人力密集的零售與餐飲業",
                    rationale="若薪資比售價更快上升，營運利潤率會遭到擠壓。",
                ),
                AINamedImpactActor(
                    kind="named_entity",
                    name="低品質消費信用與小型企業借款",
                    rationale="若就業轉弱，現金流緩衝較少的借款人通常會先出現逾期壓力。",
                ),
            ],
            "professional_reaction": (
                "專業投資人會將當期數值與前值修訂、3 個月移動平均、勞參率與薪資分布一起比較，"
                "再用美債、美元與週期/防禦板塊的相對反應判斷是成長訊號還是利率訊號佔上風。"
            ),
            "indicators": [
                "非農就業前值修訂、3 個月平均、每週初領失業救濟金與招聘率",
                "失業率、勞動參與率、就業人口比與長期失業占比",
                "平均時薪、生產力、單位勞工成本與服務業通膨",
                "2 年期美債、美元與美股週期板塊對數據意外的相對反應",
            ],
        }

    if _is_sec_enforcement(item):
        return {
            "what_changed": (
                f"美國證券交易委員會於 {publication_date.isoformat()} 發布「{item.title}」，"
                "正式提出證券法相關指控。這改變的是案件已進入公開執法程序；指控本身仍不是法院的終局事實認定。"
            ),
            "why_it_matters": (
                "SEC 案件可能改變涉案募資、證券發行、投資顧問或客戶資產管理活動的法律成本與信任風險。"
                "個案未必影響大盤，但若揭露同類產品或控制流程的共同缺口，估值折價與資金流出就可能擴散。"
            ),
            "beneficiaries": [
                AINamedImpactActor(
                    kind="named_entity",
                    name="受證券法與資訊揭露制度保護的投資人",
                    rationale="公開起訴文件可增加案件事實、涉案產品與救濟要求的可核查性。",
                ),
                AINamedImpactActor(
                    kind="named_entity",
                    name="具透明費用與強健合規控制的同業",
                    rationale="若投資人提高對揭露、託管與銷售行為的要求，控制較完整的業者相對風險可能下降。",
                ),
            ],
            "losers": [
                AINamedImpactActor(
                    kind="named_entity",
                    name="公告中被 SEC 起訴的個人與實體",
                    rationale="即使案件尚未判決，公開訴訟仍會帶來抗辯成本、業務限制與聲譽壓力。",
                ),
                AINamedImpactActor(
                    kind="named_entity",
                    name="與涉案產品或控制流程高度相似的業者",
                    rationale="若後續文件顯示問題具有共通性，同業可能面臨更嚴格審查、資金流出或合規成本上升。",
                ),
            ],
            "professional_reaction": (
                "專業投資人會先讀 SEC complaint，分清楚 allegation、被告承認事項與法院已裁定事項；"
                "再核對涉案公司的公開揭露、產品曝險、客戶贖回及是否有司法部或州級平行行動。"
            ),
            "indicators": [
                "SEC complaint 的具體法律請求、涉案期間、產品、金額與被告答辯",
                "法院禁令、資產凍結、追繳、民事罰款及和解是否實際發生",
                "涉案或相似業者的資金流、客戶揭露、8-K/10-Q 法律程序與合規整改",
                "其他監管或執法機關是否就同一事實採取平行行動",
            ],
        }

    if _is_fed_enforcement(item):
        return {
            "what_changed": (
                f"{item.publisher} 於 {publication_date.isoformat()} 對「{item.title}」所述對象採取正式執法行動。"
                "公告將對象、前雇主、措施類型與所涉行為綁定到可核對的聯準會監理文件。"
            ),
            "why_it_matters": (
                "執法行動會測試機構的內控、客戶資產保護與人員監督是否有結構性缺口。"
                "個案本身未必改變大盤，但後續罰款、整改與同類案件可將影響從個人擴展到機構盈利。"
            ),
            "beneficiaries": [
                AINamedImpactActor(
                    kind="named_entity",
                    name="相關銀行的客戶與存款人",
                    rationale="禁業與整改機制若有效執行，可降低客戶資金遭不當處理的重複風險。",
                )
            ],
            "losers": [
                AINamedImpactActor(
                    kind="named_entity",
                    name="公告中遭禁業或被起訴的個人",
                    rationale="正式行動會直接限制從業、增加法律成本並造成職業聲譽後果。",
                ),
                AINamedImpactActor(
                    kind="named_entity",
                    name="若內控缺口被證實的相關銀行",
                    rationale="擴大後的整改、稽核、訴訟與客戶補償可能增加營運成本。",
                ),
            ],
            "professional_reaction": (
                "金融業分析師會先閱讀 consent order/起訴文件，區分個人行為與機構控制失效；"
                "然後核對公司揭露、法律準備金、管理層整改與其他監理機構是否有平行行動。"
            ),
            "indicators": [
                "完整執法文件中的事實認定、禁令範圍、整改期限與當事人是否承認責任",
                "涉及機構的 8-K/10-Q 法律程序揭露、準備金與管理層說明",
                "同一機構或同類業務是否出現後續罰款、合意令或客戶補償",
            ],
        }

    if "approval" in title_key or "application" in title_key:
        company_match = re.search(
            r"(?:application by|approval of application by)\s+(.+)$",
            item.title,
            re.IGNORECASE,
        )
        company = company_match.group(1).strip() if company_match else "獲准申請機構"
        return {
            "what_changed": (
                f"{item.publisher} 於 {publication_date.isoformat()} 正式核准 {company} 的申請。"
                "這個變化是法規關卡已通過，不是業務收入或市場份額已經實現。"
            ),
            "why_it_matters": (
                "核准為指定機構移除了進入、設立據點或調整架構的一個必要條件。"
                "對大盤影響通常局部；只有後續營運規模、資本需求或審批標準具有系統性訊號時，才會擴散到同業定價。"
            ),
            "beneficiaries": [
                AINamedImpactActor(
                    kind="named_entity",
                    name=company,
                    rationale="核准降低了申請本身的法規不確定性，並允許機構進入後續建置與營運步驟。",
                )
            ],
            "losers": [
                AINoneIdentifiedActor(
                    kind="none_identified",
                    rationale="官方核准公告未證明特定競爭者、產業或資產已因此承受可量化損失。",
                )
            ],
            "professional_reaction": (
                "專業投資人會閱讀完整 order，檢查限制條件、實施時程、資本要求與管理層先前預期；"
                "在沒有業務量與財務資料前，不會把一份核准公告直接轉成盈利上調。"
            ),
            "indicators": [
                "完整核准 order 中的限制條件、監理承諾、生效日與後續申請",
                "獲准機構對實際營運開始日、前期成本、人員與客戶目標的公開揭露",
                "後續報表中該業務的收入、費用、資產與資本佔用是否實際出現",
            ],
        }

    passage = _bounded_source_passage(source_text, max_sentences=2, max_chars=420)
    grounded_change = (
        f"官方內文的核心事實為：「{passage}」"
        if passage
        else "本次沒有足夠官方內文可安全重建更多細節。"
    )
    return {
        "what_changed": (
            f"{item.publisher} 於 {publication_date.isoformat()} 發布「{item.title}」。"
            f"{grounded_change} 本報告已將官方事實與條件式市場推論分開。"
        ),
        "why_it_matters": (
            f"這項更新可能透過政策、信用、成本或風險偏好影響{scope}，"
            "但官方內文沒有證明的方向與市場反應不會被當成事實。"
        ),
        "beneficiaries": [
            AINoneIdentifiedActor(
                kind="none_identified",
                rationale="現有官方事實不足以指認已經獲得可量化經濟利益的具名機構。",
            )
        ],
        "losers": [
            AINoneIdentifiedActor(
                kind="none_identified",
                rationale="現有官方事實不足以指認已經承受可量化經濟損失的具名機構。",
            )
        ],
        "professional_reaction": (
            "專業投資人會核對原文與既有預期，把新事實轉成可驗證的傳導路徑，"
            "再檢查價格、利率、信用或後續公告是否支持該路徑。"
        ),
        "indicators": [
            "同一官方機構的後續發布、修訂、實施文件與生效時間",
            "直接受影響資產的成交量、波動、利差與相對大盤表現",
            "至少一個獨立一手來源對關鍵事實與影響範圍的確認",
        ],
    }


def _research_discovery(
    item: _FeedItem, *, request: ResearchRequest
) -> AIResearchDiscovery:
    abstract = (item.description or "").strip()
    if "structural labor market indicator" in item.title.casefold():
        simple_explanation = (
            "這篇聯準會 FEDS 論文建立 Structural Labor Market Indicator（SLMI），目的不是再做一個單純的失業率指標，"
            "而是把含搜尋與配對摩擦、內生勞動參與和可變工時的中型新凱因斯 DSGE 模型，與廣泛的總體及勞動資料結合。"
            "作者先估計模型隱含的多種勞動市場缺口，再用主成分方法濃縮成共同的結構性訊號。"
            "依官方摘要所述，GDP 成長與通膨能提供超越傳統勞動變數的額外資訊；作者也報告該指標對衰退能較早發出警示，"
            "並在復甦初期比常見指標顯示更緩慢的修復。這些是作者的研究結果，尚未在本報告中做獨立樣本外驗證。"
        )
        key_insight = (
            "最值得帶走的觀念是：勞動市場的『鬆緊』不是一個可直接觀察的數字。"
            "把就業、參與率、工時、產出與通膨放進同一個結構模型，可能比只看失業率更早發現週期轉折；"
            "但模型結果會依賴估計窗口、先驗假設與資料修訂，因此不能直接當成交易訊號。"
        )
        applications = [
            AIResearchApplication(
                category="finance",
                application="把 SLMI 與薪資、核心服務通膨及短端利率並列，檢查勞動鬆動是否足以改變政策路徑。",
            ),
            AIResearchApplication(
                category="investing",
                application="以歷次衰退和復甦做樣本外回測，評估它是否能改善週期/防禦板塊切換，而非只驗證樣本內擬合。",
            ),
            AIResearchApplication(
                category="business_strategy",
                application="將結構性勞動缺口納入招聘、薪資預算與需求情境，並與公司自身職缺及離職率交叉驗證。",
            ),
            AIResearchApplication(
                category="technology",
                application="建立可重現資料管線，保存資料版本與模型參數，避免經濟資料修訂造成回測前視偏誤。",
            ),
        ]
    else:
        abstract_copy = _bounded_source_fragment(abstract, max_words=18)
        evidence_copy = (
            f"官方摘要確認其研究範圍以「{abstract_copy}」開頭；"
            if abstract_copy
            else "官方目錄未提供足夠摘要；"
        )
        simple_explanation = (
            f"聯準會 FEDS 官方研究目錄新增「{item.title}」。{evidence_copy}"
            "本報告只把它列為待驗證的研究發現，不會在未讀取模型設定、樣本期間與穩健性檢驗前，把標題推演成投資結論。"
        )
        key_insight = "可用價值在於把論文提出的假設轉成可觀測、可回測的指標；是否有效必須由原文方法、資料修訂與樣本外結果共同決定。"
        applications = [
            AIResearchApplication(
                category="finance",
                application="閱讀官方論文的方法與樣本，確認研究設計後再納入金融條件監測。",
            ),
            AIResearchApplication(
                category="investing",
                application="把核心假設轉成可觀測訊號，設定基準模型並做樣本外回測。",
            ),
            AIResearchApplication(
                category="business_strategy",
                application="把研究結論作為情境變數，並用公司自身需求、成本與招聘資料交叉驗證。",
            ),
            AIResearchApplication(
                category="technology",
                application="保存論文版本、資料版本與計算步驟，讓研究結果可重現並避免前視偏誤。",
            ),
        ]
    return AIResearchDiscovery(
        research_title=item.title,
        research_date=_local_publication_date(item, request),
        simple_explanation=simple_explanation,
        key_insight=key_insight,
        applications=applications,
        has_current_market_implication=False,
        impact_analysis=None,
        sources=[_evidence(item)],
    )


def _knowledge_refresh(report_date: date) -> AIKnowledgeRefresh:
    return knowledge_refresh_for_date(report_date)


def _local_publication_date(item: _FeedItem, request: ResearchRequest) -> date:
    try:
        timezone = ZoneInfo(request.timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise PermanentProviderError(
            "The report timezone is unavailable to the official feed provider."
        ) from exc
    return item.published_at.astimezone(timezone).date()


def _evidence(item: _FeedItem) -> AIEvidence:
    return AIEvidence(
        title=item.title,
        publisher=item.publisher,
        url=item.url,
        published_at=item.published_at,
        evidence_role="primary",
        quoted_fragment=None,
    )


def _related_evidence(item: _FeedItem, *, url: str, title: str) -> AIEvidence:
    return AIEvidence(
        title=title,
        publisher=item.publisher,
        url=_canonical_official_url(url),
        published_at=item.published_at,
        evidence_role="primary",
        quoted_fragment=None,
    )


def _theme(
    title: str,
) -> Literal["monetary", "labor", "energy", "regulation", "general"]:
    tokens = tuple(
        re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", title).casefold())
    )
    rules: tuple[
        tuple[
            Literal["monetary", "labor", "energy", "regulation"],
            tuple[str, ...],
        ],
        ...,
    ] = (
        (
            "monetary",
            (
                "fomc",
                "federal open market committee",
                "monetary",
                "interest rate",
                "inflation",
                "consumer price index",
                "producer price index",
                "federal funds",
            ),
        ),
        (
            "labor",
            (
                "employment",
                "unemployment",
                "payroll",
                "job opening",
                "labor market",
                "productivity",
            ),
        ),
        ("energy", ("energy", "oil", "natural gas", "petroleum")),
        (
            "regulation",
            (
                "enforcement",
                "regulation",
                "regulations",
                "rule",
                "rules",
                "charges",
                "bank",
                "banks",
                "banking",
                "application",
            ),
        ),
    )
    for theme, keywords in rules:
        if any(_contains_token_phrase(tokens, keyword) for keyword in keywords):
            return theme
    return "general"


def _item_theme(
    item: _FeedItem,
) -> Literal["monetary", "labor", "energy", "regulation", "general"]:
    """Classify on sanitized source text, not a frequently generic feed title."""

    if item.publisher == "U.S. Bureau of Labor Statistics":
        return "energy" if _theme(item.title) == "energy" else "labor"
    context = item.title
    if item.description:
        context = f"{context} {item.description[:2_000]}"
    return _theme(context)


def _contains_token_phrase(tokens: tuple[str, ...], phrase: str) -> bool:
    """Match a complete normalized token sequence, never a substring.

    Punctuation and case are normalized before this function, so phrases such
    as ``interest-rate`` still match ``interest rate`` while ``boiler`` cannot
    accidentally match the token ``oil``.
    """

    expected = tuple(re.findall(r"[a-z0-9]+", phrase.casefold()))
    width = len(expected)
    return bool(width) and any(
        tokens[index : index + width] == expected
        for index in range(len(tokens) - width + 1)
    )


def _news_classification(
    theme: str,
) -> tuple[Literal["high", "medium", "low"], list[str], dict[str, float]]:
    if theme == "monetary":
        return (
            "high",
            ["Financials", "Real Estate", "Technology"],
            _scores(8, 6, 9, 8, 8),
        )
    if theme == "labor":
        return (
            "high",
            ["Consumer Discretionary", "Industrials", "Financials"],
            _scores(8, 8, 9, 8, 8),
        )
    if theme == "energy":
        return (
            "medium",
            ["Energy", "Industrials", "Transportation"],
            _scores(6, 6, 7, 7, 6),
        )
    if theme == "regulation":
        return "medium", ["Financials", "Capital Markets"], _scores(5, 5, 5, 7, 6)
    return "low", ["Broad US market"], _scores(4, 4, 4, 4, 4)


def _scores(
    market: int, earnings: int, macro: int, sector: int, trading: int
) -> dict[str, float]:
    return {
        "market_size_impact": float(market),
        "earnings_impact": float(earnings),
        "macro_importance": float(macro),
        "sector_influence": float(sector),
        "short_term_trading_relevance": float(trading),
    }


class _FeedDescriptionTextParser(HTMLParser):
    """Turn the small HTML fragments used in RSS descriptions into plain text."""

    _BLOCK_TAGS = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"})
    _IGNORED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.casefold()
        if normalized in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif not self._ignored_depth and normalized in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and normalized in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _clean_feed_description(value: str | None, *, research_feed: bool) -> str | None:
    """Sanitize a bounded RSS description without retaining raw markup."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 32_000:
        return None
    parser = _FeedDescriptionTextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed optional fragments fail closed
        return None
    raw_lines = "".join(parser.parts).splitlines()
    lines: list[str] = []
    for raw_line in raw_lines:
        normalized = unicodedata.normalize("NFKC", html.unescape(raw_line))
        cleaned = " ".join(
            "".join(
                character
                for character in normalized
                if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
            ).split()
        )
        lines.append(cleaned)
    if research_feed:
        # FEDS uses two <br> elements between the author byline and the abstract.
        # Take the first substantive block after that boundary when present.
        joined = "\n".join(lines)
        blocks = [" ".join(block.split()) for block in re.split(r"\n\s*\n+", joined)]
        blocks = [block for block in blocks if block]
        if len(blocks) >= 2:
            cleaned_value = blocks[-1]
        else:
            cleaned_value = " ".join(line for line in lines if line)
    else:
        cleaned_value = " ".join(line for line in lines if line)
    if len(cleaned_value) < 12:
        return None
    return cleaned_value[:4_000].rstrip()


def _child_text(element: ElementTree.Element, local_name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == local_name and child.text:
            return child.text.strip()
    return None


def _entry_link(element: ElementTree.Element) -> str | None:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            relation = child.attrib.get("rel", "alternate")
            if relation == "alternate":
                return href.strip()
        if child.text and child.text.strip():
            return child.text.strip()
    return None


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid feed timestamp") from exc
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _clean_title(value: str) -> str:
    value = html.unescape(value)
    value = unicodedata.normalize("NFKC", value)
    value = " ".join(value.split())
    value = "".join(
        character
        for character in value
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    if len(value) < 4:
        raise ValueError("feed title is too short")
    return value if len(value) <= 200 else value[:199].rstrip() + "…"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _canonical_official_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_SOURCE_URL_LENGTH:
        raise ValueError("official source URL exceeds the safe length")
    try:
        normalized = str(_OFFICIAL_HTTP_URL_ADAPTER.validate_python(value))
    except ValidationError as exc:
        raise ValueError("official source URL is not a valid HTTPS URL") from exc
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid official source port") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or not any(_host_matches(host, domain) for domain in OFFICIAL_ALLOWED_DOMAINS)
    ):
        raise ValueError("official source URL is outside the HTTPS allowlist")
    netloc = host
    path = parsed.path or "/"
    canonical = urlunsplit(("https", netloc, path, parsed.query, ""))
    if len(canonical) > _MAX_SOURCE_URL_LENGTH:
        raise ValueError("official source URL exceeds the safe length")
    return canonical


def _require_official_url(value: str) -> None:
    _canonical_official_url(value)


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


__all__ = [
    "BLS_LATEST_NUMBERS_URL",
    "DEFAULT_OFFICIAL_USER_AGENT",
    "FED_PRESS_RELEASES_URL",
    "FED_RESEARCH_URL",
    "OFFICIAL_ALLOWED_DOMAINS",
    "SEC_PRESS_RELEASES_URL",
    "OfficialFeedsResearchProvider",
    "OfficialFeedsResearchSettings",
    "OfficialResearchProvider",
]
