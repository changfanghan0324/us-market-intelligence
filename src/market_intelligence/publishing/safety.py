"""Content and filesystem-boundary checks for public artifacts."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path


class PublicArtifactSafetyError(RuntimeError):
    """Raised when output is not safe to commit or publish."""


# These are deliberately structural identifiers, not broad financial words such
# as "portfolio", which can legitimately occur in public market commentary.
PRIVATE_DATA_MARKERS = (
    "__private__",
    "private_payload",
    "portfolio_holdings",
    "holdings_payload",
    "account_number",
    "brokerage_account",
    "tax_lot",
    "cost_basis",
    "journal_entries",
    "investment_journal",
    "user_positions",
)

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "secret assignment",
        re.compile(
            r"(?im)^\s*(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN)\s*[=:]\s*\S+"
        ),
    ),
)

_SCRIPT_RE = re.compile(r"<\s*script\b", re.IGNORECASE)
_EVENT_HANDLER_RE = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
_EXTERNAL_ASSET_RE = re.compile(
    r"""(?:
        <\s*(?:img|iframe|audio|video|source|embed)\b[^>]*\bsrc\s*=\s*["']?\s*(?:https?:)?//
        |<\s*link\b[^>]*\bhref\s*=\s*["']?\s*(?:https?:)?//
        |@import\s+(?:url\()?\s*["']?\s*(?:https?:)?//
        |url\(\s*["']?\s*(?:https?:)?//
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def assert_public_text(
    text: str,
    *,
    label: str = "public artifact",
    blocked_values: Iterable[str] = (),
) -> None:
    """Reject private-field identifiers and common credential formats."""

    folded = text.casefold()
    for marker in PRIVATE_DATA_MARKERS:
        if marker.casefold() in folded:
            raise PublicArtifactSafetyError(
                f"{label} contains a blocked private-data marker"
            )
    for description, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise PublicArtifactSafetyError(f"{label} contains a blocked {description}")
    for value in blocked_values:
        if isinstance(value, str) and len(value) >= 8 and value in text:
            raise PublicArtifactSafetyError(
                f"{label} contains a blocked runtime secret value"
            )


def assert_standalone_html(
    html: str,
    *,
    label: str = "rendered HTML",
    blocked_values: Iterable[str] = (),
) -> None:
    """Enforce the CSS-only, no-external-assets browser boundary."""

    assert_public_text(html, label=label, blocked_values=blocked_values)
    if _SCRIPT_RE.search(html):
        raise PublicArtifactSafetyError(f"{label} contains a script element")
    if _EVENT_HANDLER_RE.search(html):
        raise PublicArtifactSafetyError(f"{label} contains an inline event handler")
    if _EXTERNAL_ASSET_RE.search(html):
        raise PublicArtifactSafetyError(f"{label} contains an external browser asset")


def assert_safe_tree(root: Path, *, blocked_values: Iterable[str] = ()) -> None:
    """Scan every regular text artifact in a staged publish tree.

    Symlinks and non-regular files are rejected.  Generated artifacts are UTF-8
    text only, which keeps the pre-commit scan auditable and complete.
    """

    root = Path(root)
    exact_secrets = tuple(
        value for value in blocked_values if isinstance(value, str) and len(value) >= 8
    )
    if root.is_symlink() or not root.is_dir():
        raise PublicArtifactSafetyError("staging scan root must be a real directory")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PublicArtifactSafetyError("symlinks are not allowed in staging")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PublicArtifactSafetyError("non-regular files are not allowed in staging")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PublicArtifactSafetyError("public staging contains a binary file") from exc
        assert_public_text(text, label=path.name, blocked_values=exact_secrets)
        if path.suffix.casefold() == ".html":
            assert_standalone_html(
                text,
                label=path.name,
                blocked_values=exact_secrets,
            )


__all__ = [
    "PRIVATE_DATA_MARKERS",
    "PublicArtifactSafetyError",
    "assert_public_text",
    "assert_safe_tree",
    "assert_standalone_html",
]
