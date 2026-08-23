"""Provider contracts and safe, bounded error classification.

The provider layer deliberately exposes normalized, validated data rather than
SDK response objects.  Exceptions contain fixed messages only: raw provider
payloads and credentials must never be placed in logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from market_intelligence import errors as application_errors


class ResearchSection(StrEnum):
    MARKET_NEWS = "market_news"
    EARNINGS = "earnings"
    GLOBAL_MACRO = "global_macro"
    RESEARCH_DISCOVERY = "research_discovery"
    KNOWLEDGE_REFRESH = "knowledge_refresh"


OPENAI_KEY_CONFIGURATION_MESSAGE = (
    "OPENAI_API_KEY is not configured. Add it in GitHub: repository Settings > "
    "Secrets and variables > Actions > New repository secret, name it "
    "OPENAI_API_KEY, then rerun the workflow."
)
OPENAI_BILLING_CONFIGURATION_MESSAGE = (
    "OpenAI API credit balance is exhausted. Add API credits at "
    "https://platform.openai.com/account/billing, wait a few minutes for the balance "
    "to update, then rerun the workflow."
)


class ProviderError(application_errors.MarketIntelligenceError):
    """Base class whose string form is always safe to log."""

    error_code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        section: ResearchSection | None = None,
        retry_after_seconds: float | None = None,
        attempts: int = 0,
    ) -> None:
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 0 <= attempts <= 10
        ):
            raise ValueError("provider error attempts must be an integer from 0 to 10")
        super().__init__(message)
        self.section = section
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts


class ConfigurationError(ProviderError, application_errors.ConfigurationError):
    error_code = "configuration_error"


class AuthenticationProviderError(
    ConfigurationError, application_errors.ProviderAuthenticationError
):
    error_code = "provider_authentication_error"


class BillingProviderError(ConfigurationError):
    error_code = "provider_billing_error"


class TransientProviderError(ProviderError, application_errors.TransientProviderError):
    error_code = "transient_provider_error"


class PermanentProviderError(ProviderError):
    error_code = "permanent_provider_error"


class EvidenceValidationError(
    ProviderError, application_errors.EvidenceValidationError
):
    error_code = "evidence_validation_error"


class ProviderDeadlineExceeded(TransientProviderError):
    error_code = "provider_deadline_exceeded"


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    web_search_calls: int = 0


@dataclass(frozen=True, slots=True)
class ProviderRunMetadata:
    provider: str
    model: str
    section: ResearchSection
    attempts: int
    duration_ms: int
    request_id: str | None
    source_count: int
    request_ids: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()
    usage: ProviderUsage = field(default_factory=ProviderUsage)


@dataclass(frozen=True, slots=True)
class ProviderResult[T]:
    data: T
    accessed_at: datetime
    metadata: ProviderRunMetadata


@runtime_checkable
class ResearchProvider(Protocol):
    """Section-scoped research interface consumed by the daily pipeline."""

    def market_news(self, request: Any) -> ProviderResult[Any]: ...

    def earnings(self, request: Any) -> ProviderResult[Any]: ...

    def global_macro(self, request: Any) -> ProviderResult[Any]: ...

    def research_discovery(self, request: Any) -> ProviderResult[Any]: ...

    def knowledge_refresh(self, request: Any) -> ProviderResult[Any]: ...


def classify_provider_exception(
    exc: BaseException,
    *,
    section: ResearchSection,
) -> ProviderError:
    """Map an SDK/network exception to a fixed, non-secret error.

    This intentionally uses only status/class/header metadata.  The exception's
    message and response body are never copied into the public exception.
    """

    if isinstance(exc, ProviderError):
        return exc

    status = _status_code(exc)
    retry_after = _retry_after_seconds(exc)
    class_name = type(exc).__name__.casefold()

    if _is_exhausted_credit_balance(exc):
        return BillingProviderError(
            OPENAI_BILLING_CONFIGURATION_MESSAGE,
            section=section,
        )

    if (
        status in {401, 403}
        or "authentication" in class_name
        or "permission" in class_name
    ):
        return AuthenticationProviderError(
            "OpenAI authentication failed; verify the OPENAI_API_KEY GitHub secret.",
            section=section,
        )

    transient_name = any(
        marker in class_name
        for marker in (
            "timeout",
            "ratelimit",
            "rate_limit",
            "connection",
            "internalserver",
        )
    )
    truncated_output = "length" in class_name and "finish" in class_name
    if (
        status in {408, 409, 425, 429}
        or (status is not None and status >= 500)
        or transient_name
        or truncated_output
    ):
        return TransientProviderError(
            "OpenAI temporarily failed while generating this section.",
            section=section,
            retry_after_seconds=retry_after,
        )

    return PermanentProviderError(
        "OpenAI rejected the section request.",
        section=section,
    )


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _retry_after_seconds(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers: Mapping[str, Any] | None = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return None

    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(seconds, 0.0), 60.0)


def _is_exhausted_credit_balance(exc: BaseException) -> bool:
    """Recognize only known billing codes without copying provider payloads."""
    values: list[object] = [getattr(exc, "code", None)]
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        values.extend((body.get("code"), body.get("type")))
    return any(
        value
        in {
            "billing_hard_limit_reached",
            "credit_balance_exhausted",
            "insufficient_quota",
        }
        for value in values
    )
