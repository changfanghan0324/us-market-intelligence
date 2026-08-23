from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from market_intelligence.domain.models import DailyReport
from market_intelligence.providers import ProviderUsage, ResearchSection
from market_intelligence.publishing.site_builder import PublicationError, SiteBuilder
from market_intelligence.usage import UsageAttemptJournal, load_monthly_usage

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"


def sample_report() -> DailyReport:
    return DailyReport.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def sample_mapping() -> dict[str, object]:
    return sample_report().model_dump(mode="json")


def dated_mapping(day: date) -> dict[str, object]:
    report = deepcopy(sample_mapping())
    previous = day - timedelta(days=1)
    tomorrow = day + timedelta(days=1)
    generated = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(
        hour=12, minute=5
    )
    source_cutoff = generated - timedelta(minutes=5)
    report["report_id"] = f"daily-market-report-{day.isoformat()}"
    report["report_date"] = day.isoformat()
    report["generated_at"] = generated.isoformat()
    report["source_cutoff_at"] = source_cutoff.isoformat()
    context = report["market_context"]
    assert isinstance(context, dict)
    context.update(
        {
            "report_date": day.isoformat(),
            "previous_session": previous.isoformat(),
            "tomorrow_date": tomorrow.isoformat(),
            "next_open_session": tomorrow.isoformat(),
            "tomorrow_opens_at": (
                generated + timedelta(days=1, hours=1, minutes=25)
            ).isoformat(),
            "tomorrow_closes_at": (
                generated + timedelta(days=1, hours=7, minutes=55)
            ).isoformat(),
        }
    )
    earnings = report["earnings"]
    assert isinstance(earnings, dict)
    earnings["target_date"] = tomorrow.isoformat()
    candidate = earnings["candidates"][0]
    candidate["earnings_at"] = (generated + timedelta(days=1, hours=8)).isoformat()
    prediction = candidate["prediction"]
    prediction["prediction_id"] = f"pred_{day.strftime('%Y%m%d')}"
    prediction["event_window"]["starts_at"] = candidate["earnings_at"]
    prediction["event_window"]["ends_at"] = (
        generated + timedelta(days=2, hours=8)
    ).isoformat()
    for item in report["provider_runs"]:
        item["request_id"] = f"req_{day.strftime('%Y%m%d')}"
    return report


def tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_build_creates_full_copy_latest_and_sha_verified_manifest(
    tmp_path: Path,
) -> None:
    result = SiteBuilder(tmp_path).build(sample_report())

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    dated_bytes = result.dated_html.read_bytes()
    latest_bytes = result.latest_html.read_bytes()
    digest = hashlib.sha256(dated_bytes).hexdigest()
    assert dated_bytes == latest_bytes
    assert result.sha256 == digest
    assert manifest["latest"]["sha256"] == digest
    assert manifest["latest"]["dated_sha256"] == digest
    assert manifest["latest"]["latest_sha256"] == digest
    assert manifest["retention"] == {"dated_reports": 8}
    assert (tmp_path / "site" / "robots.txt").read_text(encoding="utf-8") == (
        "User-agent: *\nDisallow: /\n"
    )
    assert (tmp_path / "site" / ".nojekyll").exists()
    assert result.record.exists()
    assert (tmp_path / "records" / "predictions.jsonl").exists()
    assert (tmp_path / "records" / "usage.json").exists()


def test_second_identical_build_is_a_noop(tmp_path: Path) -> None:
    builder = SiteBuilder(tmp_path)

    first = builder.build(sample_report())
    second = builder.build(sample_report())

    assert first.changed is True
    assert second.changed is False
    usage = json.loads(
        (tmp_path / "records" / "usage.json").read_text(encoding="utf-8")
    )
    assert len(usage["reports"]) == 1


def test_tampered_prior_report_blocks_next_day_before_any_mutation(
    tmp_path: Path,
) -> None:
    builder = SiteBuilder(tmp_path)
    first = builder.build(dated_mapping(date(2026, 8, 19)))
    first.dated_html.write_bytes(
        first.dated_html.read_bytes() + b"\n<!-- tampered prior report -->\n"
    )
    site_before = tree_snapshot(tmp_path / "site")
    records_before = tree_snapshot(tmp_path / "records")

    with pytest.raises(PublicationError, match="existing site failed integrity"):
        builder.build(dated_mapping(date(2026, 8, 20)))

    assert tree_snapshot(tmp_path / "site") == site_before
    assert tree_snapshot(tmp_path / "records") == records_before
    assert not list(tmp_path.glob(".publication-stage-*"))
    assert not (
        tmp_path / "site" / "reports" / "daily_market_report_2026-08-20.html"
    ).exists()
    assert not (tmp_path / "records" / "daily_market_report_2026-08-20.json").exists()


def test_tampered_prior_record_blocks_next_day_before_any_mutation(
    tmp_path: Path,
) -> None:
    builder = SiteBuilder(tmp_path)
    builder.build(dated_mapping(date(2026, 8, 19)))
    prior_record = tmp_path / "records" / "daily_market_report_2026-08-19.json"
    prior_record.write_bytes(prior_record.read_bytes() + b"\n")
    site_before = tree_snapshot(tmp_path / "site")
    records_before = tree_snapshot(tmp_path / "records")

    with pytest.raises(PublicationError, match="records failed integrity"):
        builder.build(dated_mapping(date(2026, 8, 20)))

    assert tree_snapshot(tmp_path / "site") == site_before
    assert tree_snapshot(tmp_path / "records") == records_before
    assert not list(tmp_path.glob(".publication-stage-*"))


def test_valid_prior_site_allows_next_day_build(tmp_path: Path) -> None:
    builder = SiteBuilder(tmp_path)
    first = builder.build(dated_mapping(date(2026, 8, 19)))

    second = builder.build(dated_mapping(date(2026, 8, 20)))

    manifest = json.loads(second.manifest.read_text(encoding="utf-8"))
    assert second.changed is True
    assert first.dated_html.exists()
    assert second.dated_html.exists()
    assert [entry["report_date"] for entry in manifest["reports"]] == [
        "2026-08-20",
        "2026-08-19",
    ]


def test_canonical_history_newer_than_pages_is_rejected_before_mutation(
    tmp_path: Path,
) -> None:
    builder = SiteBuilder(tmp_path)
    builder.build(dated_mapping(date(2026, 8, 19)))
    saved_site = tmp_path / "saved-site"
    shutil.copytree(tmp_path / "site", saved_site)
    builder.build(dated_mapping(date(2026, 8, 20)))
    shutil.rmtree(tmp_path / "site")
    saved_site.rename(tmp_path / "site")
    site_before = tree_snapshot(tmp_path / "site")
    records_before = tree_snapshot(tmp_path / "records")

    with pytest.raises(PublicationError, match="records failed integrity"):
        builder.build(dated_mapping(date(2026, 8, 21)))

    assert tree_snapshot(tmp_path / "site") == site_before
    assert tree_snapshot(tmp_path / "records") == records_before
    assert not list(tmp_path.glob(".publication-stage-*"))


def test_response_journal_is_reconciled_by_published_request_id(tmp_path: Path) -> None:
    journal = UsageAttemptJournal(
        tmp_path / "records" / "usage_events.jsonl",
        report_date=date(2026, 8, 19),
    )
    journal.record(
        ResearchSection.MARKET_NEWS,
        1,
        ProviderUsage(input_tokens=1200, output_tokens=600, web_search_calls=1),
        datetime(2026, 8, 19, 12, 4, tzinfo=UTC),
        "req_sample",
    )

    SiteBuilder(tmp_path).build(sample_report())
    usage = load_monthly_usage(
        tmp_path / "records" / "usage.json",
        report_date=date(2026, 8, 19),
    )

    assert usage.input_tokens == 1200
    assert usage.output_tokens == 600
    assert usage.web_search_calls == 1


def test_public_site_keeps_eight_but_records_and_predictions_are_append_only(
    tmp_path: Path,
) -> None:
    builder = SiteBuilder(tmp_path)
    days = [date(2026, 8, 1) + timedelta(days=offset) for offset in range(10)]
    for day in days:
        builder.build(dated_mapping(day))

    public_reports = sorted((tmp_path / "site" / "reports").glob("*.html"))
    canonical_records = sorted(
        (tmp_path / "records").glob("daily_market_report_????-??-??.json")
    )
    prediction_lines = (
        (tmp_path / "records" / "predictions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    usage = json.loads(
        (tmp_path / "records" / "usage.json").read_text(encoding="utf-8")
    )

    assert len(public_reports) == 8
    assert public_reports[0].name.endswith("2026-08-03.html")
    assert public_reports[-1].name.endswith("2026-08-10.html")
    assert len(canonical_records) == 10
    assert len(prediction_lines) == 10
    assert len(usage["reports"]) == 10
    assert usage["monthly"]["2026-08"]["reports"] == 10
    assert (tmp_path / "site" / "latest.html").read_bytes() == public_reports[
        -1
    ].read_bytes()


def test_private_payload_never_reaches_site_records_or_ledgers(tmp_path: Path) -> None:
    report = sample_mapping()
    report["private_payload"] = {
        "portfolio_holdings": "LEAK_SENTINEL_7749",
        "account_number": "PRIVATE-123",
    }
    report["market_news"][0]["journal_entries"] = ["LEAK_SENTINEL_7749"]

    SiteBuilder(tmp_path).build(report)

    all_output = "".join(
        path.read_text(encoding="utf-8")
        for root in (tmp_path / "site", tmp_path / "records")
        for path in root.rglob("*")
        if path.is_file()
    )
    assert "LEAK_SENTINEL_7749" not in all_output
    assert "portfolio_holdings" not in all_output
    assert "journal_entries" not in all_output


def test_render_or_scan_failure_preserves_last_known_good_site(tmp_path: Path) -> None:
    builder = SiteBuilder(tmp_path)
    first = builder.build(sample_report())
    old_latest = first.latest_html.read_bytes()
    malicious = dated_mapping(date(2026, 8, 20))
    malicious["market_news"][0]["title"] = (
        "Blocked portfolio_holdings marker must fail before publication"
    )

    with pytest.raises(PublicationError):
        builder.build(malicious)

    assert first.latest_html.read_bytes() == old_latest
    assert not (
        tmp_path / "site" / "reports" / "daily_market_report_2026-08-20.html"
    ).exists()


def test_exact_runtime_secret_is_rejected_without_a_known_prefix(
    tmp_path: Path,
) -> None:
    runtime_secret = "opaque-one-time-value-7f9ac423b1e65d88"
    report = sample_mapping()
    report["market_news"][0]["title"] = runtime_secret

    with pytest.raises(PublicationError):
        SiteBuilder(tmp_path, blocked_values=(runtime_secret,)).build(report)

    assert not (tmp_path / "site").exists()
    assert not (tmp_path / "records").exists()


def test_symlink_in_managed_output_is_rejected_without_following_it(
    tmp_path: Path,
) -> None:
    site_reports = tmp_path / "site" / "reports"
    site_reports.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("must remain", encoding="utf-8")
    (site_reports / "unsafe-link").symlink_to(outside)

    with pytest.raises(PublicationError, match="existing site failed integrity"):
        SiteBuilder(tmp_path).build(sample_report())

    assert outside.read_text(encoding="utf-8") == "must remain"


def test_force_rerun_adds_immutable_record_revision_usage_and_prediction(
    tmp_path: Path,
) -> None:
    builder = SiteBuilder(tmp_path)
    original = sample_mapping()
    builder.build(original)
    revised = deepcopy(original)
    revised["generated_at"] = "2026-08-19T12:15:00+00:00"
    revised["earnings"]["candidates"][0]["prediction"]["prediction_id"] = (
        "pred_acme_20260820_force"
    )

    result = builder.build(revised, force=True)

    records = sorted(
        (tmp_path / "records").glob("daily_market_report_2026-08-19*.json")
    )
    usage = json.loads(
        (tmp_path / "records" / "usage.json").read_text(encoding="utf-8")
    )
    predictions = (
        (tmp_path / "records" / "predictions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert result.record.name.startswith("daily_market_report_2026-08-19_revision_")
    assert len(records) == 2
    assert len(usage["reports"]) == 2
    assert len({item["usage_run_id"] for item in usage["reports"]}) == 2
    assert len(predictions) == 2
