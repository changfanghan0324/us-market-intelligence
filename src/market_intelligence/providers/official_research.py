"""No-key research adapter backed by documented, public official RSS feeds.

This adapter intentionally reads feed metadata only: title, canonical link, and
publication timestamp.  It does not scrape article bodies, call commercial
endpoints, or manufacture prices, estimates, quotations, or market reactions.
The explanatory text is produced by small deterministic templates and labels
that limitation directly in every analysis.
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
from .openai_research import (
    AIEvidence,
    AIGlobalMacroEvent,
    AIImpactAnalysis,
    AIKnowledgeRefresh,
    AIMarketNewsItem,
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

OFFICIAL_ALLOWED_DOMAINS: Final[tuple[str, ...]] = (
    "bls.gov",
    "federalreserve.gov",
    "sec.gov",
)

FED_PRESS_RELEASES_URL: Final = (
    "https://www.federalreserve.gov/feeds/press_all.xml"
)
BLS_LATEST_NUMBERS_URL: Final = "https://www.bls.gov/feed/bls_latest.rss"
SEC_PRESS_RELEASES_URL: Final = "https://www.sec.gov/news/pressreleases.rss"
FED_RESEARCH_URL: Final = "https://www.federalreserve.gov/feeds/feds.xml"

DEFAULT_OFFICIAL_USER_AGENT: Final = (
    "USMarketIntelligence/1.0 cpeter0814@gmail.com"
)
_MODEL_NAME: Final = "deterministic_rules_v1"
_MAX_CANDIDATES: Final = 8
_MAX_FEED_ATTEMPTS: Final = 2
_MAX_SOURCE_URL_LENGTH: Final = 2_048
_RETRY_BASE_DELAY_SECONDS: Final = 0.25
_XML_DECLARATION_PATTERN = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
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
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside the safety bound")
        if not isinstance(self.user_agent, str) or not 12 <= len(self.user_agent) <= 300:
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


_MARKET_FEEDS: Final = (
    _FeedDefinition("fed_press", FED_PRESS_RELEASES_URL, "Federal Reserve"),
    _FeedDefinition("bls_latest", BLS_LATEST_NUMBERS_URL, "U.S. Bureau of Labor Statistics"),
    _FeedDefinition("sec_press", SEC_PRESS_RELEASES_URL, "U.S. Securities and Exchange Commission"),
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

    earnings_calendar_available: ClassVar[bool] = False

    def __init__(
        self,
        settings: OfficialFeedsResearchSettings | None = None,
        *,
        fetcher: OfficialFeedFetcher | None = None,
        now: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self.settings = settings or OfficialFeedsResearchSettings()
        self._fetcher = fetcher or self._default_fetcher
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self._snapshots: dict[str, _FeedSnapshot] = {}
        self._snapshot_lock = threading.Lock()

    def market_news(self, request: ResearchRequest) -> ProviderResult[MarketNewsResponse]:
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
        for item in sorted(collection.items, key=_news_rank_key):
            try:
                candidate = _news_item(item, request=request)
            except ValidationError:
                # Keep one malformed upstream entry inside the item boundary;
                # it must not abort an otherwise valid required section.
                continue
            selected_items.append(item)
            candidates.append(candidate)
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
            source_urls=[item.url for item in selected_items],
            attempts=collection.attempts,
            warning_codes=collection.warning_codes,
        )

    def earnings(self, request: ResearchRequest) -> ProviderResult[EarningsResponse]:
        """Return no candidates; public feeds are not an earnings calendar."""

        started = time.monotonic()
        return self._result(
            EarningsResponse(candidates=[]),
            request=request,
            section=ResearchSection.EARNINGS,
            started=started,
            source_urls=[],
        )

    def global_macro(self, request: ResearchRequest) -> ProviderResult[GlobalMacroResponse]:
        started = time.monotonic()
        collection = self._market_items(
            request,
            ResearchSection.GLOBAL_MACRO,
            lookback_days=self.settings.macro_lookback_days,
        )
        macro_items = [
            item
            for item in collection.items
            if _theme(item.title) in {"monetary", "labor", "energy"}
        ]
        if not macro_items:
            raise PermanentProviderError(
                "Official feeds did not contain a qualifying macro release within the configured lookback.",
                section=ResearchSection.GLOBAL_MACRO,
            )
        selected: _FeedItem | None = None
        event: AIGlobalMacroEvent | None = None
        for item in macro_items:
            try:
                event = _macro_event(item, request=request)
            except (ValidationError, ValueError):
                continue
            selected = item
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
            source_urls=[selected.url],
            attempts=collection.attempts,
            warning_codes=collection.warning_codes,
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
        except ValueError:
            continue
        parsed.append(
            _FeedItem(
                title=cleaned_title,
                url=canonical_url,
                published_at=published_at,
                publisher=feed.publisher,
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
        and oldest <= item.published_at.astimezone(timezone).date() <= request.report_date
    ]
    return _deduplicate_items(eligible)


def _deduplicate_items(items: Sequence[_FeedItem]) -> list[_FeedItem]:
    by_url: dict[str, _FeedItem] = {}
    seen_titles: set[str] = set()
    for item in sorted(items, key=lambda value: (-value.published_at.timestamp(), value.url)):
        title_key = "".join(character.casefold() for character in item.title if character.isalnum())
        if item.url in by_url or title_key in seen_titles:
            continue
        by_url[item.url] = item
        seen_titles.add(title_key)
    return list(by_url.values())


def _news_item(item: _FeedItem, *, request: ResearchRequest) -> AIMarketNewsItem:
    theme = _theme(item.title)
    impact, sectors, scores = _news_classification(theme)
    publication_date = _local_publication_date(item, request)
    return AIMarketNewsItem(
        title=item.title,
        event_date=publication_date,
        market_impact=impact,
        affected_sectors=sectors,
        summary=(
            f"官方 RSS 顯示此更新於 {publication_date.isoformat()} 發布；"
            "免費模式僅使用標題、日期與連結中繼資料。"
        ),
        bullish_case="若官方原文降低不確定性或改善政策可見度，風險偏好可能獲得支撐。",
        bearish_case="若官方原文揭示更嚴格條件或更高風險，市場部位可能轉向防禦。",
        attention_components=AINewsAttentionComponents(**scores),
        impact_analysis=_impact_analysis(
            item, macro=False, publication_date=publication_date
        ),
        sources=[_evidence(item)],
    )


def _news_rank_key(item: _FeedItem) -> tuple[float, float, str]:
    """Match the deterministic attention ordering before capping candidates."""

    _, _, components = _news_classification(_theme(item.title))
    attention_score = sum(components.values()) / len(components)
    return (-attention_score, -item.published_at.timestamp(), item.url)


def _macro_event(
    item: _FeedItem, *, request: ResearchRequest
) -> AIGlobalMacroEvent:
    publication_date = _local_publication_date(item, request)
    return AIGlobalMacroEvent(
        global_event=item.title,
        event_date=publication_date,
        market_impact=_macro_impact(
            publication_date=publication_date,
            report_date=request.report_date,
        ),
        summary=(
            "此項官方發布涉及宏觀政策或經濟條件；免費模式只確認 RSS 標題、日期與官方連結，"
            "不推測未讀取的數值或政策細節。"
        ),
        affected_assets=["stocks", "bonds", "usd", "commodities"],
        impact_analysis=_impact_analysis(
            item, macro=True, publication_date=publication_date
        ),
        sources=[_evidence(item)],
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
    item: _FeedItem, *, macro: bool, publication_date: date
) -> AIImpactAnalysis:
    scope = "跨資產定價" if macro else "美國市場預期"
    return AIImpactAnalysis(
        what_changed=(
            f"{item.publisher} 的官方 RSS 在 {publication_date.isoformat()} 發布"
            f"「{item.title}」；本卡只確認可驗證的發布中繼資料。"
        ),
        why_it_matters=(
            f"官方發布可能影響{scope}，但標題本身不足以判定方向；應先閱讀連結中的官方原文。"
        ),
        beneficiaries=[
            AINoneIdentifiedActor(
                kind="none_identified",
                rationale="僅憑 RSS 標題與日期，無法可靠辨識確定受益的公司、產業或資產。",
            )
        ],
        losers=[
            AINoneIdentifiedActor(
                kind="none_identified",
                rationale="未讀取官方原文與後續證據前，無法可靠辨識確定受損的市場參與者。",
            )
        ],
        professional_investor_reaction=(
            "專業投資人通常先核對官方原文、發布時間與既有預期，再決定是否調整曝險。"
        ),
        indicators_to_monitor_next=[
            "官方原文中的政策或數據細節",
            "同一機構後續發布與修訂",
            "可信來源對事件方向的獨立確認",
        ],
    )


def _research_discovery(
    item: _FeedItem, *, request: ResearchRequest
) -> AIResearchDiscovery:
    return AIResearchDiscovery(
        research_title=item.title,
        research_date=_local_publication_date(item, request),
        simple_explanation=(
            f"聯準會 FEDS 官方研究目錄新增「{item.title}」。免費模式只使用目錄中繼資料；"
            "方法、樣本與結論必須以官方論文原文為準。"
        ),
        key_insight=(
            "研究標題適合用來發現待閱讀議題，但在檢查方法與證據前，不應把它當成已驗證的投資結論。"
        ),
        applications=[
            AIResearchApplication(
                category="finance",
                application="用官方論文原文檢查研究設計，再評估其對金融條件分析的用途。",
            ),
            AIResearchApplication(
                category="investing",
                application="把研究假設轉成可觀測指標，並在採用前進行樣本外驗證。",
            ),
            AIResearchApplication(
                category="business_strategy",
                application="將研究議題作為情境規劃輸入，而不是直接當成營運預測。",
            ),
            AIResearchApplication(
                category="technology",
                application="保留論文版本、日期與來源連結，以支援可重現的研究流程。",
            ),
        ],
        has_current_market_implication=False,
        impact_analysis=None,
        sources=[_evidence(item)],
    )


def _knowledge_refresh(report_date: date) -> AIKnowledgeRefresh:
    lessons = (
        AIKnowledgeRefresh(
            concept="基準情境與反證條件",
            historical_background="專業研究流程長期使用基準情境，將最可能路徑與極端可能性分開管理。",
            simple_explanation="先寫下目前最可能的結果，再列出哪些可觀測證據會證明這個判斷錯誤。",
            why_it_still_matters_today="市場敘事快速變動時，預先設定反證條件可降低事後合理化與確認偏誤。",
            real_world_example="若判斷融資環境將改善，可把官方利率訊號與信用條件惡化列為反證監控項目。",
        ),
        AIKnowledgeRefresh(
            concept="來源層級與證據鏈",
            historical_background="市場研究一向區分第一手官方資料、可靠轉述與未經證實的市場傳聞。",
            simple_explanation="重要主張應連回可驗證的原始來源，並保留發布日期、存取時間與連結。",
            why_it_still_matters_today="自動化報告容易快速放大錯誤；完整證據鏈讓讀者能獨立核對每項主張。",
            real_world_example="政策消息先引用官方公告，再用獨立來源補充市場解讀，而不是反向取代原文。",
        ),
        AIKnowledgeRefresh(
            concept="事件視窗與因果限制",
            historical_background="事件研究使用明確時間視窗，避免把事件前後所有價格變動都歸因於單一消息。",
            simple_explanation="先固定事件發生與觀察期間，再比較同時存在的其他重大資訊與共同市場因素。",
            why_it_still_matters_today="多個宏觀與公司事件常在同日發生，明確視窗可抑制過度歸因。",
            real_world_example="評估政策發布影響時，需同時記錄發布時間、後續修訂與同時公布的經濟資料。",
        ),
    )
    return lessons[report_date.toordinal() % len(lessons)]


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


def _theme(title: str) -> Literal["monetary", "labor", "energy", "regulation", "general"]:
    tokens = tuple(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", title).casefold()))
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
        ("labor", ("employment", "unemployment", "payroll", "job opening", "labor market", "productivity")),
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
        return "high", ["Financials", "Real Estate", "Technology"], _scores(8, 6, 9, 8, 8)
    if theme == "labor":
        return "high", ["Consumer Discretionary", "Industrials", "Financials"], _scores(8, 8, 9, 8, 8)
    if theme == "energy":
        return "medium", ["Energy", "Industrials", "Transportation"], _scores(6, 6, 7, 7, 6)
    if theme == "regulation":
        return "medium", ["Financials", "Capital Markets"], _scores(5, 5, 5, 7, 6)
    return "low", ["Broad US market"], _scores(4, 4, 4, 4, 4)


def _scores(market: int, earnings: int, macro: int, sector: int, trading: int) -> dict[str, float]:
    return {
        "market_size_impact": float(market),
        "earnings_impact": float(earnings),
        "macro_importance": float(macro),
        "sector_influence": float(sector),
        "short_term_trading_relevance": float(trading),
    }


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
