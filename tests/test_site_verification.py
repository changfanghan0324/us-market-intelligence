from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_intelligence.domain.models import DailyReport
from market_intelligence.publishing import SiteBuilder, verify_site_tree
from market_intelligence.publishing.safety import PublicArtifactSafetyError

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"


def _report() -> DailyReport:
    return DailyReport.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_exact_site_verification_accepts_generated_tree(tmp_path: Path) -> None:
    result = SiteBuilder(tmp_path).build(_report())
    verified = verify_site_tree(tmp_path / "site")
    assert verified.report_id == result.report_id
    assert verified.sha256 == result.sha256
    assert verified.report_count == 1


def test_exact_site_verification_rejects_unmanaged_old_html(tmp_path: Path) -> None:
    SiteBuilder(tmp_path).build(_report())
    (tmp_path / "site" / "reports" / "renamed-old-report.html").write_text(
        "<!doctype html><title>old</title>", encoding="utf-8"
    )
    with pytest.raises(PublicArtifactSafetyError, match="exactly match"):
        verify_site_tree(tmp_path / "site")


def test_exact_site_verification_checks_every_historical_digest(tmp_path: Path) -> None:
    SiteBuilder(tmp_path).build(_report())
    manifest_path = tmp_path / "site" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dated = tmp_path / "site" / manifest["reports"][0]["href"]
    dated.write_text(dated.read_text(encoding="utf-8") + "tamper", encoding="utf-8")
    with pytest.raises(PublicArtifactSafetyError, match="manifest digest"):
        verify_site_tree(tmp_path / "site")


def test_exact_site_verification_rejects_root_extras(tmp_path: Path) -> None:
    SiteBuilder(tmp_path).build(_report())
    (tmp_path / "site" / "forgotten.html").write_text("old report", encoding="utf-8")
    with pytest.raises(PublicArtifactSafetyError, match="unmanaged"):
        verify_site_tree(tmp_path / "site")
