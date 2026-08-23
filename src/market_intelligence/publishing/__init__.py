"""Deterministic builders and verifiers for the GitHub Pages artifact."""

from __future__ import annotations

from typing import Any

__all__ = [
    "BuildResult",
    "PublicationError",
    "SiteBuilder",
    "VerifiedRecords",
    "VerifiedSite",
    "verify_records_tree",
    "verify_site_tree",
]


def __getattr__(name: str) -> Any:
    # The renderer imports the lightweight safety submodule.  Lazy exports avoid
    # importing the site builder back through the renderer during package setup.
    if name in {"BuildResult", "PublicationError", "SiteBuilder"}:
        from .site_builder import BuildResult, PublicationError, SiteBuilder

        return {
            "BuildResult": BuildResult,
            "PublicationError": PublicationError,
            "SiteBuilder": SiteBuilder,
        }[name]
    if name in {"VerifiedSite", "verify_site_tree"}:
        from .verification import VerifiedSite, verify_site_tree

        return {"VerifiedSite": VerifiedSite, "verify_site_tree": verify_site_tree}[name]
    if name in {"VerifiedRecords", "verify_records_tree"}:
        from .records_verification import VerifiedRecords, verify_records_tree

        return {
            "VerifiedRecords": VerifiedRecords,
            "verify_records_tree": verify_records_tree,
        }[name]
    raise AttributeError(name)
