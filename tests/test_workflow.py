import re
from pathlib import Path

from market_intelligence.config import load_config

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "daily-report.yml"
CONFIG = ROOT / "config" / "config.yaml"


def test_scheduled_production_run_uses_free_mode_without_secret_injection() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    config = load_config(CONFIG)

    assert config.research.provider == "official_free"
    assert config.openai is None
    assert "OPENAI_API_KEY:" not in workflow
    assert "secrets.OPENAI_API_KEY" not in workflow
    assert config.schedule.hour == 8
    assert config.schedule.minute == 7
    schedule_pairs = re.findall(
        r'- cron: "([^"]+)"\n\s+timezone: "([^"]+)"', workflow
    )
    assert schedule_pairs == [
        ("7 8 * * *", "America/New_York"),
        ("37 8 * * *", "America/New_York"),
        ("7 9 * * *", "America/New_York"),
    ]
    assert "cancel-in-progress: false" in workflow
    assert (
        "FORCE_REQUESTED: ${{ github.event_name == 'workflow_dispatch' "
        "&& inputs.force || false }}"
    ) in workflow


def test_optional_paid_usage_journal_remains_recoverable_after_failure() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "Persist observed usage after a failed run" in workflow
    assert "records/usage_events.jsonl" in workflow


def test_pages_verification_does_not_pipe_curl_into_an_early_consumer() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    verification = workflow.split("- name: Verify public latest report ID", maxsplit=1)[1]

    assert 'latest_html="$(curl' in verification
    assert '| grep --fixed-strings --quiet "${REPORT_ID}"' not in verification
    assert 'grep --fixed-strings --quiet "${REPORT_ID}" <<< "${latest_html}"' in verification
