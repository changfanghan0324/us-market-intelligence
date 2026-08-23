"""Non-secret YAML configuration and environment-only secret preflight."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from market_intelligence.errors import ConfigurationError

_MAX_CONFIG_BYTES = 128 * 1024
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)

EnvironmentName = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        pattern=r"^[A-Z][A-Z0-9_]{1,63}$",
    ),
]
ConfigName = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=2, max_length=100),
]
DomainName = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=4, max_length=253),
]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ReportConfig(ConfigModel):
    timezone: Literal["America/New_York"] = "America/New_York"
    language: Literal["zh-TW"] = "zh-TW"
    public_history_count: Literal[8] = 8


class OfficialResearchConfig(ConfigModel):
    """Bounded settings for the zero-cost official-public-source provider."""

    request_timeout_seconds: Annotated[
        float, Field(strict=True, ge=5.0, le=30.0, allow_inf_nan=False)
    ] = 20.0
    # Keep automated requests polite and deterministic across public endpoints.
    max_parallel_sections: Literal[1] = 1


class ResearchConfig(ConfigModel):
    """Select exactly one reviewed research implementation."""

    provider: Literal["official_free", "openai"] = "official_free"
    official: OfficialResearchConfig = Field(default_factory=OfficialResearchConfig)


class OpenAIConfig(ConfigModel):
    api_key_env: Literal["OPENAI_API_KEY"] = "OPENAI_API_KEY"
    model: ConfigName = "gpt-5.6-terra"
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = (
        "medium"
    )
    search_context_size: Literal["low", "medium", "high"] = "medium"
    max_output_tokens_per_section: Annotated[
        int, Field(strict=True, ge=500, le=8_000)
    ] = 5_000
    max_tool_calls_per_section: Annotated[int, Field(strict=True, ge=1, le=8)] = 6
    max_retries: Annotated[int, Field(strict=True, ge=0, le=3)] = 2
    request_timeout_seconds: Annotated[
        float, Field(strict=True, ge=5.0, le=180.0, allow_inf_nan=False)
    ] = 90.0
    total_deadline_seconds: Annotated[
        float, Field(strict=True, ge=30.0, le=600.0, allow_inf_nan=False)
    ] = 600.0
    max_parallel_sections: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    monthly_input_token_limit: Annotated[
        int, Field(strict=True, ge=100_000, le=100_000_000)
    ] = 2_000_000
    monthly_output_token_limit: Annotated[
        int, Field(strict=True, ge=50_000, le=20_000_000)
    ] = 500_000
    monthly_web_search_call_limit: Annotated[
        int, Field(strict=True, ge=30, le=10_000)
    ] = 500
    usage_warning_fraction: Annotated[
        float, Field(strict=True, ge=0.5, le=0.95, allow_inf_nan=False)
    ] = 0.8
    store: Literal[False] = False
    allowed_domains: Annotated[list[DomainName], Field(min_length=1, max_length=100)]
    company_ir_domains: Annotated[list[DomainName], Field(max_length=50)] = Field(
        default_factory=list
    )

    @field_validator("allowed_domains")
    @classmethod
    def domains_are_hosts_not_urls(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            domain = value.casefold().rstrip(".")
            if not _DOMAIN_RE.fullmatch(domain):
                raise ValueError("allowed_domains entries must be DNS hostnames")
            normalized.append(domain)
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_domains cannot contain duplicates")
        return normalized

    @field_validator("company_ir_domains")
    @classmethod
    def ir_domains_are_hosts_not_urls(cls, values: list[str]) -> list[str]:
        normalized = [value.casefold().rstrip(".") for value in values]
        if any(not _DOMAIN_RE.fullmatch(domain) for domain in normalized):
            raise ValueError("company_ir_domains entries must be DNS hostnames")
        if len(normalized) != len(set(normalized)):
            raise ValueError("company_ir_domains cannot contain duplicates")
        return normalized

    @model_validator(mode="after")
    def ir_domains_are_in_the_global_allowlist(self) -> OpenAIConfig:
        if not set(self.company_ir_domains).issubset(self.allowed_domains):
            raise ValueError("company_ir_domains must also appear in allowed_domains")
        return self

    @field_validator("usage_warning_fraction")
    @classmethod
    def warning_fraction_is_stable(cls, value: float) -> float:
        if value != 0.8:
            raise ValueError("usage_warning_fraction is fixed at 0.8")
        return value


class MarketDataConfig(ConfigModel):
    enabled: bool = False
    provider: ConfigName = "disabled"
    api_key_env: EnvironmentName | None = None
    public_display_license_confirmed: bool = False

    @model_validator(mode="after")
    def enabled_provider_requires_license_and_env_name(self) -> MarketDataConfig:
        if self.enabled:
            if self.provider.casefold() == "disabled":
                raise ValueError("enabled market data requires a named provider")
            if self.api_key_env is None:
                raise ValueError(
                    "enabled market data requires an API-key environment name"
                )
            if not self.public_display_license_confirmed:
                raise ValueError("public display license must be explicitly confirmed")
        elif (
            self.provider.casefold() != "disabled"
            or self.api_key_env is not None
            or self.public_display_license_confirmed
        ):
            raise ValueError(
                "disabled market data must not claim provider credentials or license"
            )
        return self


class PublicationConfig(ConfigModel):
    required_news_items: Literal[3] = 3
    earnings_min_score: Annotated[
        float, Field(strict=True, ge=0.0, le=10.0, allow_inf_nan=False)
    ] = 7.0
    require_global_macro: Literal[True] = True
    research_may_degrade: Literal[True] = True
    knowledge_may_degrade: Literal[True] = True

    @field_validator("earnings_min_score")
    @classmethod
    def fixed_earnings_threshold(cls, value: float) -> float:
        if value != 7.0:
            raise ValueError("earnings_min_score is fixed at 7.0")
        return value


class PagesConfig(ConfigModel):
    base_url_env: EnvironmentName = "PAGES_BASE_URL"
    reports_branch: Literal["reports"] = "reports"
    site_directory: Literal["site"] = "site"
    verification_timeout_seconds: Annotated[
        int, Field(strict=True, ge=60, le=1_200)
    ] = 600
    verification_poll_seconds: Annotated[int, Field(strict=True, ge=2, le=60)] = 15


class ScheduleConfig(ConfigModel):
    timezone: Literal["America/New_York"] = "America/New_York"
    hour: Literal[8] = 8
    minute: Literal[7] = 7
    inactivity_reminder_days: Annotated[int, Field(strict=True, ge=7, le=60)] = 30


class AppConfig(ConfigModel):
    report: ReportConfig = Field(default_factory=ReportConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    openai: OpenAIConfig | None = None
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    publication: PublicationConfig = Field(default_factory=PublicationConfig)
    pages: PagesConfig = Field(default_factory=PagesConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)

    @model_validator(mode="after")
    def timezones_agree(self) -> AppConfig:
        if self.report.timezone != self.schedule.timezone:
            raise ValueError("report and schedule timezones must match")
        if self.research.provider == "openai" and self.openai is None:
            raise ValueError("openai research mode requires an openai configuration")
        return self


def load_config(path: str | Path) -> AppConfig:
    """Load a strict non-secret YAML file without echoing unsafe values."""

    config_path = Path(path)
    try:
        if config_path.is_symlink() or not config_path.is_file():
            raise ConfigurationError(
                "Configuration file is missing or is not a regular file."
            )
        if config_path.stat().st_size > _MAX_CONFIG_BYTES:
            raise ConfigurationError(
                "Configuration file exceeds the 128 KiB safety limit."
            )
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigurationError(
            "Configuration file could not be read safely."
        ) from error
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a YAML mapping.")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as error:
        # Pydantic messages can include the rejected input.  Never propagate them
        # because a mistakenly pasted key must not appear in CI logs.
        raise ConfigurationError(
            "Configuration file is invalid; check documented field names and types."
        ) from error


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def require_openai_api_key(
    config: AppConfig | OpenAIConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the environment-only API key or fail with fixed instructions."""

    openai_config = config.openai if isinstance(config, AppConfig) else config
    if openai_config is None:
        raise ConfigurationError(
            "OpenAI research mode is selected but its non-secret configuration is missing."
        )
    env_name = openai_config.api_key_env
    value = _environment(environ).get(env_name, "")
    plausible = (
        value.startswith("sk-")
        and len(value.strip()) >= 20
        and not any(character.isspace() for character in value)
    )
    if not plausible:
        raise ConfigurationError(
            "OPENAI_API_KEY is not configured. Add a GitHub Actions repository "
            "secret named OPENAI_API_KEY under Settings > Secrets and variables > "
            "Actions, or export it in the local process environment, then rerun."
        )
    return value


def require_market_data_api_key(
    config: AppConfig | MarketDataConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve an enabled provider's key; disabled market data returns None."""

    market_data = config.market_data if isinstance(config, AppConfig) else config
    if not market_data.enabled:
        return None
    assert market_data.api_key_env is not None  # enforced by model validation
    value = _environment(environ).get(market_data.api_key_env, "")
    if len(value.strip()) < 12 or any(character.isspace() for character in value):
        raise ConfigurationError(
            "The configured market-data secret is missing. Add the named environment "
            "variable in GitHub Actions Secrets, then rerun."
        )
    return value


def require_pages_base_url(
    config: AppConfig | PagesConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve and validate the public Pages base URL without storing it in YAML."""

    pages = config.pages if isinstance(config, AppConfig) else config
    raw = _environment(environ).get(pages.base_url_env, "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "PAGES_BASE_URL is missing or invalid. Configure it as an HTTPS GitHub "
            "Actions variable without query parameters or credentials."
        )
    return raw.rstrip("/")
