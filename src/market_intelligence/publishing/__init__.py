"""Deterministic builders and verifiers for the GitHub Pages artifact."""

from __future__ import annotations

from typing import Any

__all__ = [
    "BuildResult",
    "PublicationError",
    "SiteBuilder",
    "VerifiedSite",
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
    raise AttributeError(name)
