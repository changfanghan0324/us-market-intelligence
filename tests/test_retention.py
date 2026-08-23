from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from market_intelligence.reporting.retention import (
    RetentionError,
    apply_retention,
    discover_reports,
    parse_report_filename,
    report_filename,
)


def _make_reports(directory: Path, count: int = 10) -> list[Path]:
    directory.mkdir()
    first = date(2026, 8, 1)
    created: list[Path] = []
    for offset in range(count):
        path = directory / report_filename(first + timedelta(days=offset))
        path.write_text(f"report {offset}", encoding="utf-8")
        created.append(path)
    return created


def test_retention_keeps_newest_eight_and_ignores_unrelated_files(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    created = _make_reports(reports)
    unrelated = reports / "daily_market_report_2020-01-01.html.backup"
    unrelated.write_text("do not delete", encoding="utf-8")
    note = reports / "operator-note.txt"
    note.write_text("do not delete", encoding="utf-8")

    plan = apply_retention(reports)

    assert [item.path.name for item in plan.expired] == [
        created[1].name,
        created[0].name,
    ]
    assert len(discover_reports(reports)) == 8
    assert unrelated.read_text(encoding="utf-8") == "do not delete"
    assert note.read_text(encoding="utf-8") == "do not delete"


@pytest.mark.parametrize(
    "filename",
    [
        "daily_market_report_2026-08-19.html.tmp",
        "../daily_market_report_2026-08-19.html",
        "daily_market_report_2026-02-30.html",
        "daily_market_report_2026-8-19.html",
        "Daily_market_report_2026-08-19.html",
        "daily_market_report_2026-08-19.json",
    ],
)
def test_only_exact_canonical_html_filenames_are_managed(filename: str) -> None:
    assert parse_report_filename(filename) is None


def test_retention_rejects_symlinks_without_deleting_anything(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    created = _make_reports(reports, count=9)
    outside = tmp_path / "outside.html"
    outside.write_text("outside", encoding="utf-8")
    (reports / "unexpected-link").symlink_to(outside)

    with pytest.raises(RetentionError, match="symlinks"):
        apply_retention(reports)

    assert all(path.exists() for path in created)
    assert outside.read_text(encoding="utf-8") == "outside"


def test_retention_cannot_be_pointed_at_canonical_records(tmp_path: Path) -> None:
    records = tmp_path / "records"
    records.mkdir()
    record = records / "daily_market_report_2026-08-01.json"
    record.write_text("{}", encoding="utf-8")

    with pytest.raises(RetentionError, match="named 'reports'"):
        apply_retention(records)

    assert record.exists()


def test_managed_name_that_is_not_a_regular_file_fails_closed(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / report_filename("2026-08-19")).mkdir()

    with pytest.raises(RetentionError, match="not a regular file"):
        discover_reports(reports)
