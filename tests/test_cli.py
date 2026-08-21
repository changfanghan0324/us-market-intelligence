from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from market_intelligence import cli
from market_intelligence.config import load_config
from market_intelligence.domain.models import DailyReport
from market_intelligence.providers import ResearchSection
from market_intelligence.publishing import SiteBuilder

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"
CONFIG = Path(__file__).parents[1] / "config" / "config.yaml"


def _sample_report() -> DailyReport:
    return DailyReport.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_missing_openai_key_stops_before_publication(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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
    monkeypatch.setenv(
        "OPENAI_API_KEY", "sk-" + "synthetic-test-only-value-123456789"
    )
    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: datetime(2026, 8, 19, 12, 30, tzinfo=UTC),
    )

    def provider_must_not_be_constructed(*args, **kwargs):
        del args, kwargs
        raise AssertionError("idempotent report must not call a paid provider")

    monkeypatch.setattr(cli, "OpenAIResearchProvider", provider_must_not_be_constructed)
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


def test_company_ir_domains_are_enabled_only_for_news_and_earnings() -> None:
    config = load_config(CONFIG)
    openai = config.openai.model_copy(
        update={"company_ir_domains": ["investor.acme.example"]}
    )

    settings = cli._settings(openai)

    assert "investor.acme.example" in settings.domain_overrides[
        ResearchSection.MARKET_NEWS
    ]
    assert "investor.acme.example" in settings.domain_overrides[
        ResearchSection.EARNINGS
    ]
    assert ResearchSection.GLOBAL_MACRO not in settings.domain_overrides


def test_production_config_avoids_bursting_web_search_requests() -> None:
    config = load_config(CONFIG)

    assert config.openai.max_output_tokens_per_section == 8_000
    assert config.openai.max_parallel_sections == 1
    assert config.openai.request_timeout_seconds == 120.0
    assert config.openai.total_deadline_seconds == 300.0
