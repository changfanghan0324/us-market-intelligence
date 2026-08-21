"""Stable, non-secret-bearing application error classes."""


class MarketIntelligenceError(Exception):
    """Base class for an expected application failure."""


class ConfigurationError(MarketIntelligenceError):
    """Configuration is absent or unsafe; this error is never retried."""


class CalendarCoverageError(MarketIntelligenceError):
    """The pinned exchange calendar cannot authoritatively answer a date."""


class EvidenceValidationError(MarketIntelligenceError):
    """An item lacks valid evidence or contains unsafe provenance."""


class ReportValidationError(MarketIntelligenceError):
    """The canonical report does not meet publication requirements."""


class TransientProviderError(MarketIntelligenceError):
    """A retryable upstream-provider failure."""


class ProviderAuthenticationError(ConfigurationError):
    """Provider credentials are absent or rejected; this is not retryable."""


class GitPublishError(MarketIntelligenceError):
    """Publishing generated artifacts to Git failed."""


class PagesVerificationError(MarketIntelligenceError):
    """The deployed Pages site did not verify within its deadline."""
