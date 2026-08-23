from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from pydantic import ValidationError

from market_intelligence.config import (
    AppConfig,
    MarketDataConfig,
    OpenAIConfig,
    load_config,
    require_openai_api_key,
    require_pages_base_url,
)
from market_intelligence.errors import ConfigurationError
from market_intelligence.log import configure_logging


def valid_config_data() -> dict[str, object]:
    return {
        "report": {
            "timezone": "America/New_York",
            "language": "zh-TW",
            "public_history_count": 8,
        },
        "research": {
            "provider": "official_free",
            "official": {
                "request_timeout_seconds": 20.0,
                "max_parallel_sections": 1,
            },
        },
        "openai": {
            "api_key_env": "OPENAI_API_KEY",
            "model": "gpt-5.4",
            "reasoning_effort": "medium",
            "search_context_size": "medium",
            "max_output_tokens_per_section": 5000,
            "max_tool_calls_per_section": 6,
            "max_retries": 2,
            "request_timeout_seconds": 90.0,
            "total_deadline_seconds": 600.0,
            "max_parallel_sections": 3,
            "store": False,
            "allowed_domains": ["sec.gov", "federalreserve.gov", "nyse.com"],
        },
        "market_data": {
            "enabled": False,
            "provider": "disabled",
            "api_key_env": None,
            "public_display_license_confirmed": False,
        },
        "publication": {
            "required_news_items": 3,
            "earnings_min_score": 7.0,
            "require_global_macro": True,
            "research_may_degrade": True,
            "knowledge_may_degrade": True,
        },
        "pages": {
            "base_url_env": "PAGES_BASE_URL",
            "reports_branch": "reports",
            "site_directory": "site",
            "verification_timeout_seconds": 600,
            "verification_poll_seconds": 15,
        },
        "schedule": {
            "timezone": "America/New_York",
            "hour": 8,
            "minute": 7,
            "inactivity_reminder_days": 30,
        },
    }


def write_yaml(path: Path, data: str) -> None:
    path.write_text(data, encoding="utf-8")


def test_loads_strict_non_secret_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    write_yaml(
        config_file,
        """
report:
  timezone: America/New_York
  language: zh-TW
  public_history_count: 8
research:
  provider: official_free
  official:
    request_timeout_seconds: 20.0
    max_parallel_sections: 1
market_data:
  enabled: false
  provider: disabled
  api_key_env: null
  public_display_license_confirmed: false
publication:
  required_news_items: 3
  earnings_min_score: 7.0
  require_global_macro: true
  research_may_degrade: true
  knowledge_may_degrade: true
pages:
  base_url_env: PAGES_BASE_URL
  reports_branch: reports
  site_directory: site
  verification_timeout_seconds: 600
  verification_poll_seconds: 15
schedule:
  timezone: America/New_York
  hour: 8
  minute: 7
  inactivity_reminder_days: 30
""",
    )
    loaded = load_config(config_file)
    assert loaded.report.public_history_count == 8
    assert loaded.research.provider == "official_free"
    assert loaded.research.official.request_timeout_seconds == 20.0
    assert loaded.openai is None
    assert '"api_key":' not in loaded.model_dump_json()


def test_strict_config_rejects_stringified_numbers() -> None:
    raw = valid_config_data()
    raw["openai"] = {**raw["openai"], "max_tool_calls_per_section": "6"}  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_load_error_never_echoes_mistaken_secret(tmp_path: Path) -> None:
    secret = "sk-" + "this-value-must-never-appear-in-an-error"
    config_file = tmp_path / "config.yaml"
    write_yaml(
        config_file,
        f"openai:\n  api_key: {secret}\n  allowed_domains: [sec.gov]\n",
    )
    with pytest.raises(ConfigurationError) as captured:
        load_config(config_file)
    assert secret not in str(captured.value)
    assert "invalid" in str(captured.value).casefold()


def test_missing_openai_key_stops_with_fixed_configuration_instructions() -> None:
    config = OpenAIConfig(allowed_domains=["sec.gov"])
    with pytest.raises(ConfigurationError) as captured:
        require_openai_api_key(config, environ={})
    message = str(captured.value)
    assert "OPENAI_API_KEY" in message
    assert "Settings > Secrets and variables > Actions" in message


def test_openai_key_is_read_only_from_supplied_environment() -> None:
    config = OpenAIConfig(allowed_domains=["sec.gov"])
    value = "sk-" + "environment-only-example-value"
    assert require_openai_api_key(config, environ={"OPENAI_API_KEY": value}) == value
    assert value not in config.model_dump_json()


def test_openai_key_shape_check_does_not_echo_rejected_value() -> None:
    config = OpenAIConfig(allowed_domains=["sec.gov"])
    rejected = "not-an-openai-secret-but-long-enough"
    with pytest.raises(ConfigurationError) as captured:
        require_openai_api_key(config, environ={"OPENAI_API_KEY": rejected})
    assert rejected not in str(captured.value)


def test_openai_mode_requires_explicit_non_secret_configuration() -> None:
    raw = valid_config_data()
    raw["research"] = {
        "provider": "openai",
        "official": {
            "request_timeout_seconds": 20.0,
            "max_parallel_sections": 1,
        },
    }
    raw.pop("openai")

    with pytest.raises(ValidationError, match="requires an openai configuration"):
        AppConfig.model_validate(raw)


def test_official_free_mode_rejects_unknown_provider_and_needs_no_openai_config() -> None:
    raw = valid_config_data()
    raw.pop("openai")
    assert AppConfig.model_validate(raw).openai is None

    research = raw["research"]
    assert isinstance(research, dict)
    raw["research"] = {**research, "provider": "unreviewed-free-service"}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_market_data_requires_explicit_public_display_license() -> None:
    with pytest.raises(ValidationError):
        MarketDataConfig(
            enabled=True,
            provider="Example Data",
            api_key_env="MARKET_DATA_API_KEY",
            public_display_license_confirmed=False,
        )
    enabled = MarketDataConfig(
        enabled=True,
        provider="Example Data",
        api_key_env="MARKET_DATA_API_KEY",
        public_display_license_confirmed=True,
    )
    assert enabled.enabled


def test_pages_base_url_is_an_environment_variable() -> None:
    config = AppConfig.model_validate(valid_config_data())
    assert (
        require_pages_base_url(
            config,
            environ={"PAGES_BASE_URL": "https://owner.github.io/repository/"},
        )
        == "https://owner.github.io/repository"
    )
    with pytest.raises(ConfigurationError):
        require_pages_base_url(
            config,
            environ={"PAGES_BASE_URL": "https://user:password@example.com"},
        )


def test_domain_allowlist_rejects_urls_and_duplicates() -> None:
    raw = valid_config_data()
    raw["openai"] = {**raw["openai"], "allowed_domains": ["https://sec.gov"]}  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_company_ir_domains_must_also_be_globally_allowlisted() -> None:
    raw = valid_config_data()
    raw["openai"] = {
        **raw["openai"],
        "company_ir_domains": ["investor.example.com"],
    }  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="must also appear"):
        AppConfig.model_validate(raw)

    raw["openai"] = {
        **raw["openai"],
        "allowed_domains": [
            *raw["openai"]["allowed_domains"],  # type: ignore[index]
            "investor.example.com",
        ],
    }  # type: ignore[arg-type]
    config = AppConfig.model_validate(raw)
    assert config.openai.company_ir_domains == ["investor.example.com"]
    raw["openai"] = {**valid_config_data()["openai"], "allowed_domains": ["SEC.gov", "sec.gov"]}  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_structured_log_redacts_secrets_and_drops_raw_payload_fields() -> None:
    secret = "sk-" + "synthetic-redaction-value-123456"
    output = StringIO()
    logger = configure_logging(secrets=[secret], stream=output)
    logger.info(
        "provider used %s",
        secret,
        extra={
            "phase": "research",
            "status": "success",
            "raw_response": f"unsafe {secret}",
        },
    )
    event = json.loads(output.getvalue())
    assert event["message"] == "provider used [REDACTED]"
    assert event["phase"] == "research"
    assert "raw_response" not in event
    assert secret not in output.getvalue()
