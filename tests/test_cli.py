from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from market_intelligence import cli
from market_intelligence.config import OpenAIConfig, ResearchConfig, load_config
from market_intelligence.domain.models import DailyReport
from market_intelligence.errors import ConfigurationError
from market_intelligence.providers import ProviderUsage, ResearchSection
from market_intelligence.publishing import SiteBuilder
from market_intelligence.usage import UsageAttemptJournal

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"
CONFIG = Path(__file__).parents[1] / "config" / "config.yaml"


def _sample_report() -> DailyReport:
    return DailyReport.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def _report_for_date(report_date: date) -> dict[str, object]:
    report = _sample_report().model_dump(mode="json")
    date_string = report_date.isoformat()
    report["report_id"] = f"daily-market-report-{date_string}"
    report["report_date"] = date_string
    report["earnings"]["candidates"][0]["prediction"]["prediction_id"] = (
        f"pred_{report_date.strftime('%Y%m%d')}"
    )
    return report


def _openai_config_file(tmp_path: Path) -> Path:
    config = load_config(CONFIG).model_copy(
        update={
            "research": ResearchConfig(provider="openai"),
            "openai": OpenAIConfig(allowed_domains=["sec.gov"]),
        }
    )
    path = tmp_path / "openai-config.yaml"
    path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_official_free_mode_generates_without_openai_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime(2026, 8, 19, 12, 30, tzinfo=UTC),
    )
    constructed: dict[str, object] = {}

    class FakeOfficialProvider:
        earnings_calendar_available = False

        def __init__(self, settings) -> None:
            self.settings = settings
            constructed["settings"] = settings

    class FakePipeline:
        def __init__(self, provider, *, market_data_provider, policy) -> None:
            del market_data_provider
            constructed["provider"] = provider
            constructed["policy"] = policy

        def generate(self, context):
            del context
            return _sample_report()

    monkeypatch.setattr(cli, "OfficialFeedsResearchProvider", FakeOfficialProvider)
    monkeypatch.setattr(cli, "DailyReportPipeline", FakePipeline)
    monkeypatch.setattr(cli, "validate_source_cutoff", lambda report: None)
    monkeypatch.setattr(
        cli,
        "validate_source_domains",
        lambda report, *, allowed_domains: constructed.update(
            {"allowed_domains": allowed_domains}
        ),
    )
    result = tmp_path / "result.json"
    log = tmp_path / "run.jsonl"

    exit_code = cli.main(
        [
            "generate",
            "--config",
            str(CONFIG),
            "--project-root",
            str(tmp_path),
            "--result-file",
            str(result),
            "--log-file",
            str(log),
        ]
    )

    assert exit_code == 0
    assert result.exists()
    assert not (tmp_path / "records" / "usage_events.jsonl").exists()
    assert constructed["provider"].__class__ is FakeOfficialProvider
    policy = constructed["policy"]
    assert policy.earnings_calendar_available is False
    assert policy.market_news_lookback_days == 14
    assert policy.global_macro_lookback_days == 30
    assert tuple(constructed["allowed_domains"]) == cli.OFFICIAL_ALLOWED_DOMAINS


def test_openai_mode_still_requires_key_before_publication(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = tmp_path / "result.json"
    log = tmp_path / "run.jsonl"

    exit_code = cli.main(
        [
            "generate",
            "--config",
            str(_openai_config_file(tmp_path)),
            "--project-root",
            str(tmp_path),
            "--result-file",
            str(result),
            "--log-file",
            str(log),
        ]
    )

    assert exit_code == 2
    assert not result.exists()
    assert not (tmp_path / "site").exists()
    assert not (tmp_path / "records").exists()
    logged = log.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY is not configured" in logged
    assert "Settings > Secrets and variables > Actions" in logged


def test_existing_valid_date_is_noop_before_provider_call(
    tmp_path: Path, monkeypatch
) -> None:
    report = _sample_report()
    SiteBuilder(tmp_path).build(report)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime(2026, 8, 19, 12, 30, tzinfo=UTC),
    )

    def provider_must_not_be_constructed(*args, **kwargs):
        del args, kwargs
        raise AssertionError("idempotent report must not construct a provider")

    monkeypatch.setattr(
        cli, "OfficialFeedsResearchProvider", provider_must_not_be_constructed
    )
    result = tmp_path / "result.json"
    log = tmp_path / "run.jsonl"
    exit_code = cli.main(
        [
            "generate",
            "--config",
            str(CONFIG),
            "--project-root",
            str(tmp_path),
            "--result-file",
            str(result),
            "--log-file",
            str(log),
        ]
    )

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["changed"] is False
    assert payload["publishable"] is True
    assert payload["report_id"] == "daily-market-report-2026-08-19"


def test_scan_accepts_valid_tree_and_rejects_tampering(tmp_path: Path) -> None:
    SiteBuilder(tmp_path).build(_sample_report())
    assert cli.main(["scan", "--project-root", str(tmp_path)]) == 0

    latest = tmp_path / "site" / "latest.html"
    latest.write_text(latest.read_text(encoding="utf-8") + "tampered", encoding="utf-8")
    assert cli.main(["scan", "--project-root", str(tmp_path)]) == 2


def test_existing_publication_uses_manifest_integrity(tmp_path: Path) -> None:
    SiteBuilder(tmp_path).build(_sample_report())
    result = cli._existing_publication(tmp_path, date(2026, 8, 19))
    assert result is not None
    assert result.changed is False
    assert len(result.sha256) == 64


def test_prior_day_publication_allows_next_day_generation(tmp_path: Path) -> None:
    SiteBuilder(tmp_path).build(_sample_report())

    result = cli._existing_publication(tmp_path, date(2026, 8, 20))

    assert result is None


def test_prior_day_publication_still_rejects_partial_site_state(
    tmp_path: Path,
) -> None:
    SiteBuilder(tmp_path).build(_sample_report())
    (tmp_path / "site" / "manifest.json").unlink()

    with pytest.raises(ConfigurationError, match="incomplete"):
        cli._existing_publication(tmp_path, date(2026, 8, 20))


def test_prior_day_publication_rejects_canonical_history_newer_than_pages(
    tmp_path: Path,
) -> None:
    builder = SiteBuilder(tmp_path)
    builder.build(_report_for_date(date(2026, 8, 19)))
    saved_site = tmp_path / "saved-site"
    shutil.copytree(tmp_path / "site", saved_site)
    builder.build(_report_for_date(date(2026, 8, 20)))
    shutil.rmtree(tmp_path / "site")
    saved_site.rename(tmp_path / "site")

    with pytest.raises(ConfigurationError, match="incomplete"):
        cli._existing_publication(tmp_path, date(2026, 8, 21))


@pytest.mark.parametrize("force", [False, True])
def test_missing_public_site_stops_before_provider_even_when_forced(
    tmp_path: Path,
    monkeypatch,
    force: bool,
) -> None:
    SiteBuilder(tmp_path).build(_sample_report())
    shutil.rmtree(tmp_path / "site")
    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime(2026, 8, 20, 12, 30, tzinfo=UTC),
    )
    provider_calls = 0

    def provider_must_not_be_constructed(*args, **kwargs):
        nonlocal provider_calls
        del args, kwargs
        provider_calls += 1
        raise AssertionError("invalid historical state must fail during preflight")

    monkeypatch.setattr(
        cli, "OfficialFeedsResearchProvider", provider_must_not_be_constructed
    )
    arguments = [
        "generate",
        "--config",
        str(CONFIG),
        "--project-root",
        str(tmp_path),
        "--result-file",
        str(tmp_path / "result.json"),
        "--log-file",
        str(tmp_path / "run.jsonl"),
    ]
    if force:
        arguments.append("--force")

    assert cli.main(arguments) == 2
    assert provider_calls == 0


def test_same_day_noop_rejects_exact_opaque_runtime_secret(tmp_path: Path) -> None:
    SiteBuilder(tmp_path).build(_sample_report())
    opaque_secret = "opaque-runtime-secret-7f9ac423b1e65d88"
    records = tmp_path / "records"
    for path in (
        records / "daily_market_report_2026-08-19.json",
        records / "latest.json",
    ):
        document = json.loads(path.read_text(encoding="utf-8"))
        document["market_news"][0]["title"] = opaque_secret
        path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(ConfigurationError, match="integrity scan"):
        cli._existing_publication(
            tmp_path,
            date(2026, 8, 19),
            blocked_values=(opaque_secret,),
        )


def test_usage_events_only_is_a_valid_unpublished_state(tmp_path: Path) -> None:
    journal = UsageAttemptJournal(
        tmp_path / "records" / "usage_events.jsonl",
        report_date=date(2026, 8, 19),
    )
    journal.record(
        ResearchSection.MARKET_NEWS,
        1,
        ProviderUsage(input_tokens=10, output_tokens=5, web_search_calls=1),
        datetime(2026, 8, 19, 12, tzinfo=UTC),
        "req_unpublished",
    )

    assert cli._existing_publication(tmp_path, date(2026, 8, 19)) is None


@pytest.mark.parametrize("orphan", ["dated_report", "canonical_record"])
def test_orphan_current_day_artifact_is_rejected(
    tmp_path: Path,
    orphan: str,
) -> None:
    SiteBuilder(tmp_path).build(_sample_report())
    if orphan == "dated_report":
        current = tmp_path / "site" / "reports" / "daily_market_report_2026-08-20.html"
        current.write_text("orphan", encoding="utf-8")
    else:
        current = tmp_path / "records" / "daily_market_report_2026-08-20.json"
        current.write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="incomplete"):
        cli._existing_publication(tmp_path, date(2026, 8, 20))


def test_missing_current_day_artifacts_are_not_silently_repaired(
    tmp_path: Path,
) -> None:
    SiteBuilder(tmp_path).build(_sample_report())
    (tmp_path / "site" / "reports" / "daily_market_report_2026-08-19.html").unlink()
    (tmp_path / "records" / "daily_market_report_2026-08-19.json").unlink()

    with pytest.raises(ConfigurationError, match="incomplete"):
        cli._existing_publication(tmp_path, date(2026, 8, 19))


def test_company_ir_domains_are_enabled_only_for_news_and_earnings() -> None:
    openai = OpenAIConfig(
        allowed_domains=["sec.gov", "investor.acme.example"],
        company_ir_domains=["investor.acme.example"],
    )

    settings = cli._settings(openai)

    assert (
        "investor.acme.example"
        in settings.domain_overrides[ResearchSection.MARKET_NEWS]
    )
    assert (
        "investor.acme.example" in settings.domain_overrides[ResearchSection.EARNINGS]
    )
    assert ResearchSection.GLOBAL_MACRO not in settings.domain_overrides


def test_production_config_uses_bounded_free_official_sources() -> None:
    config = load_config(CONFIG)

    assert config.research.provider == "official_free"
    assert config.research.official.max_parallel_sections == 1
    assert config.research.official.request_timeout_seconds == 20.0
    assert config.openai is None
