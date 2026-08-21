from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from market_intelligence.config import OpenAIConfig
from market_intelligence.errors import ConfigurationError
from market_intelligence.providers import ProviderUsage, ResearchSection
from market_intelligence.usage import (
    UsageAttemptJournal,
    UsageBudgetExceeded,
    enforce_monthly_usage_budget,
    load_monthly_usage,
)


def _config() -> OpenAIConfig:
    return OpenAIConfig(allowed_domains=["sec.gov"])


def _write_usage(path: Path, *, input_tokens: int, output_tokens: int, calls: int) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "reports": [],
                "monthly": {
                    "2026-08": {
                        "reports": 10,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "web_search_calls": calls,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_missing_usage_ledger_is_zero(tmp_path: Path) -> None:
    usage = load_monthly_usage(tmp_path / "usage.json", report_date=date(2026, 8, 21))
    assert usage.month == "2026-08"
    assert usage.input_tokens == 0


def test_warns_at_eighty_percent(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    config = _config()
    _write_usage(
        path,
        input_tokens=int(config.monthly_input_token_limit * 0.8),
        output_tokens=0,
        calls=0,
    )
    status = enforce_monthly_usage_budget(
        config, usage_path=path, report_date=date(2026, 8, 21)
    )
    assert status.warning_codes == ("monthly_input_tokens_at_80_percent",)


def test_stops_at_monthly_limit(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    config = _config()
    _write_usage(
        path,
        input_tokens=0,
        output_tokens=config.monthly_output_token_limit,
        calls=0,
    )
    with pytest.raises(UsageBudgetExceeded, match="monthly OpenAI usage limit"):
        enforce_monthly_usage_budget(
            config, usage_path=path, report_date=date(2026, 8, 21)
        )


def test_rejects_negative_or_linked_usage(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    _write_usage(target, input_tokens=-1, output_tokens=0, calls=0)
    with pytest.raises(ConfigurationError, match="input-token"):
        load_monthly_usage(target, report_date=date(2026, 8, 21))

    link = tmp_path / "usage.json"
    link.symlink_to(target)
    with pytest.raises(ConfigurationError, match="regular file"):
        load_monthly_usage(link, report_date=date(2026, 8, 21))


def test_failed_run_response_event_counts_without_a_published_report(
    tmp_path: Path,
) -> None:
    journal = UsageAttemptJournal(
        tmp_path / "records" / "usage_events.jsonl",
        report_date=date(2026, 8, 21),
    )
    journal.record(
        ResearchSection.MARKET_NEWS,
        1,
        ProviderUsage(input_tokens=120, output_tokens=45, web_search_calls=1),
        datetime(2026, 8, 21, 12, 1, tzinfo=UTC),
        "req_failed_observed",
    )

    usage = load_monthly_usage(
        tmp_path / "records" / "usage.json",
        report_date=date(2026, 8, 21),
    )

    assert usage.input_tokens == 120
    assert usage.output_tokens == 45
    assert usage.web_search_calls == 1


def test_published_request_id_reconciles_attempt_event_without_double_counting(
    tmp_path: Path,
) -> None:
    records = tmp_path / "records"
    journal = UsageAttemptJournal(
        records / "usage_events.jsonl",
        report_date=date(2026, 8, 21),
    )
    journal.record(
        ResearchSection.GLOBAL_MACRO,
        1,
        ProviderUsage(input_tokens=100, output_tokens=50, web_search_calls=1),
        datetime(2026, 8, 21, 12, 2, tzinfo=UTC),
        "req_reconciled",
    )
    (records / "usage.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "reports": [
                    {
                        "runs": [
                            {
                                "request_id": "req_reconciled",
                                "request_ids": ["req_reconciled"],
                            }
                        ]
                    }
                ],
                "monthly": {
                    "2026-08": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "web_search_calls": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    usage = load_monthly_usage(
        records / "usage.json",
        report_date=date(2026, 8, 21),
    )

    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.web_search_calls == 1
