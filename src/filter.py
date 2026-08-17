"""Keyword filtering against posting descriptions."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import yaml

from src.models import JobPosting

logger = logging.getLogger(__name__)


def load_keywords(path: Path) -> list[str]:
    """Load description keywords from config/keywords.yaml."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(data, list):
        return [str(k).lower() for k in data]
    keywords = data.get("keywords", [])
    return [str(k).lower() for k in keywords]


def matches_keywords(text: str, keywords: Iterable[str]) -> bool:
    """Case-insensitive substring match against any keyword."""
    haystack = text.lower()
    return any(keyword.lower() in haystack for keyword in keywords)


def title_matches(title: str, title_keywords: list[str] | None) -> bool:
    """Optional title noise filter. Empty/None list means keep all titles."""
    if not title_keywords:
        return True
    return matches_keywords(title, title_keywords)


def filter_by_description(
    postings: list[JobPosting],
    keywords: list[str],
    *,
    log_only: bool = False,
) -> tuple[list[JobPosting], list[JobPosting]]:
    """Split postings into (kept, skipped) by description keyword match.

    When ``log_only`` is True (new-company buffer), all postings are kept but
    non-matches are still returned in ``skipped`` for logging.
    """
    kept: list[JobPosting] = []
    skipped: list[JobPosting] = []
    for posting in postings:
        if matches_keywords(posting.description, keywords):
            kept.append(posting)
        else:
            skipped.append(posting)
            if log_only:
                kept.append(posting)
    return kept, skipped


def strip_descriptions(postings: list[JobPosting]) -> list[JobPosting]:
    """Clear description text before any persistence (not written to the sheet)."""
    for posting in postings:
        posting.description = ""
    return postings
