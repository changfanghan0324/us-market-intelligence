from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from market_intelligence.domain.models import DailyReport
from market_intelligence.providers import ProviderUsage, ResearchSection
from market_intelligence.publishing import SiteBuilder, verify_records_tree
from market_intelligence.publishing.safety import PublicArtifactSafetyError
from market_intelligence.usage import UsageAttemptJournal

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"


def _report() -> DailyReport:
    return DailyReport.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def _write_canonical(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def test_records_verifier_accepts_current_builder_tree_and_usage_events(
    tmp_path: Path,
) -> None:
    SiteBuilder(tmp_path).build(_report())
    journal = UsageAttemptJournal(
        tmp_path / "records" / "usage_events.jsonl",
        report_date=date(2026, 8, 20),
    )
    journal.record(
        ResearchSection.MARKET_NEWS,
        1,
        ProviderUsage(input_tokens=10, output_tokens=5, web_search_calls=1),
        datetime(2026, 8, 20, 12, tzinfo=UTC),
        "req_unpublished",
    )

    verified = verify_records_tree(
        tmp_path / "records", public_report_dates=[date(2026, 8, 19)]
    )

    assert verified.latest_report_id == "daily-market-report-2026-08-19"
    assert verified.base_record_count == 1
    assert verified.prediction_count == 1
    assert verified.usage_event_count == 1


@pytest.mark.parametrize(
    "release_timing",
    ["before_market", "during_market", "after_market", "time_not_confirmed"],
)
def test_records_verifier_accepts_unconfirmed_earnings_time(
    tmp_path: Path,
    release_timing: str,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    candidate = document["earnings"]["candidates"][0]
    candidate["release_timing"] = release_timing
    candidate["earnings_at"] = None
    candidate["prediction"]["event_window"]["anchor_basis"] = "market_window_proxy"
    report = DailyReport.model_validate_json(json.dumps(document))

    SiteBuilder(tmp_path).build(report)
    verified = verify_records_tree(
        tmp_path / "records", public_report_dates=[date(2026, 8, 19)]
    )

    assert verified.prediction_count == 1


def test_records_verifier_rejects_missing_public_base_record(tmp_path: Path) -> None:
    SiteBuilder(tmp_path).build(_report())
    (tmp_path / "records" / "daily_market_report_2026-08-19.json").unlink()

    with pytest.raises(PublicArtifactSafetyError, match="missing its base"):
        verify_records_tree(tmp_path / "records", public_report_dates=["2026-08-19"])


def test_records_verifier_rejects_orphan_artifact(tmp_path: Path) -> None:
    SiteBuilder(tmp_path).build(_report())
    (tmp_path / "records" / "operator-note.txt").write_text(
        "not managed", encoding="utf-8"
    )

    with pytest.raises(PublicArtifactSafetyError, match="unmanaged artifact"):
        verify_records_tree(tmp_path / "records")


def test_records_verifier_rejects_record_metadata_tampering(tmp_path: Path) -> None:
    SiteBuilder(tmp_path).build(_report())
    base = tmp_path / "records" / "daily_market_report_2026-08-19.json"
    document = json.loads(base.read_text(encoding="utf-8"))
    document["report_id"] = "daily-market-report-2026-08-18"
    _write_canonical(base, document)

    with pytest.raises(PublicArtifactSafetyError, match="ID does not match"):
        verify_records_tree(tmp_path / "records")


def test_records_verifier_accepts_force_revision_then_rejects_digest_tamper(
    tmp_path: Path,
) -> None:
    SiteBuilder(tmp_path).build(_report())
    records = tmp_path / "records"
    base = records / "daily_market_report_2026-08-19.json"
    revised = json.loads(base.read_text(encoding="utf-8"))
    revised["market_news"][0]["title"] += " revised"
    revised_bytes = (
        json.dumps(revised, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(revised_bytes).hexdigest()[:12]
    revision = records / f"daily_market_report_2026-08-19_revision_{digest}.json"
    revision.write_bytes(revised_bytes)
    (records / "latest.json").write_bytes(revised_bytes)
    verified = verify_records_tree(
        tmp_path / "records", public_report_dates=["2026-08-19"]
    )
    assert verified.revision_count == 1

    document = json.loads(revision.read_text(encoding="utf-8"))
    document["validation_status"] = "tampered"
    _write_canonical(revision, document)

    with pytest.raises(PublicArtifactSafetyError, match="content digest"):
        verify_records_tree(tmp_path / "records")


def test_records_verifier_rejects_prediction_and_usage_tampering(
    tmp_path: Path,
) -> None:
    SiteBuilder(tmp_path).build(_report())
    predictions = tmp_path / "records" / "predictions.jsonl"
    original_predictions = predictions.read_bytes()
    predictions.write_bytes(original_predictions * 2)
    with pytest.raises(PublicArtifactSafetyError, match="prediction ledger entry"):
        verify_records_tree(tmp_path / "records")

    predictions.write_bytes(original_predictions)
    usage = tmp_path / "records" / "usage.json"
    document = json.loads(usage.read_text(encoding="utf-8"))
    document["totals"]["reports"] += 1
    _write_canonical(usage, document)
    with pytest.raises(PublicArtifactSafetyError, match="aggregate totals"):
        verify_records_tree(tmp_path / "records")
