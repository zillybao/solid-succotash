"""Keyword, location, date, and education filtering."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import yaml

from src.models import JobPosting

logger = logging.getLogger(__name__)

# Dated postings older than this are dropped every run. Undated are kept.
POSTED_LOOKBACK_DAYS = 7


def _normalize_edu_text(text: str) -> str:
    """Fold curly quotes so YAML phrases with ASCII apostrophes still match."""
    return (
        (text or "")
        .lower()
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Match a phrase as a token (not as a substring of a longer word)."""
    escaped = re.escape(phrase.lower())
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")


def _foreign_code_pattern(code: str) -> re.Pattern[str]:
    """Match a country code as a token, including office suffixes like UK2."""
    escaped = re.escape(code.lower())
    return re.compile(rf"(?<![a-z0-9]){escaped}\d*(?![a-z0-9])")


@dataclass(frozen=True)
class UsLocationFilter:
    """Keep US / ambiguous locations; drop explicit non-US countries and cities."""

    us_patterns: tuple[re.Pattern[str], ...]
    foreign_patterns: tuple[re.Pattern[str], ...]
    foreign_city_patterns: tuple[re.Pattern[str], ...]
    state_abbr_re: re.Pattern[str]

    @classmethod
    def from_yaml(cls, path: Path) -> UsLocationFilter:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        us_phrases = [
            str(p).lower()
            for p in list(data.get("us_country") or []) + list(data.get("us_states") or [])
        ]
        us_phrases.sort(key=len, reverse=True)
        foreign_phrases = [str(p).lower() for p in (data.get("foreign_countries") or [])]
        foreign_phrases.sort(key=len, reverse=True)
        foreign_codes = [str(p).lower() for p in (data.get("foreign_codes") or [])]
        foreign_cities = [str(p).lower() for p in (data.get("foreign_cities") or [])]
        foreign_cities.sort(key=len, reverse=True)
        abbrs = [str(a).lower() for a in (data.get("us_state_abbreviations") or [])]
        abbr_alt = "|".join(re.escape(a) for a in abbrs) if abbrs else "a^"
        # "Austin, TX" / "CA" / "Santa Clara, CA, United States"
        state_abbr_re = re.compile(rf"(?:^|,\s*)(?:{abbr_alt})(?:\s*,|\s*$)", re.I)
        return cls(
            us_patterns=tuple(_phrase_pattern(p) for p in us_phrases),
            foreign_patterns=tuple(_phrase_pattern(p) for p in foreign_phrases)
            + tuple(_foreign_code_pattern(c) for c in foreign_codes),
            foreign_city_patterns=tuple(_phrase_pattern(p) for p in foreign_cities),
            state_abbr_re=state_abbr_re,
        )

    def is_us(self, location: str) -> bool:
        text = (location or "").strip().lower()
        if not text:
            return True
        if any(p.search(text) for p in self.us_patterns) or self.state_abbr_re.search(text):
            return True
        if any(p.search(text) for p in self.foreign_patterns):
            return False
        if any(p.search(text) for p in self.foreign_city_patterns):
            return False
        return True


def load_us_location_filter(path: Path) -> UsLocationFilter:
    return UsLocationFilter.from_yaml(path)


def load_keywords(path: Path) -> list[str]:
    """Load description keywords from config/keywords.yaml."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(data, list):
        return [str(k).lower() for k in data]
    keywords = data.get("keywords", [])
    return [str(k).lower() for k in keywords]


def matches_keywords(text: str, keywords: Iterable[str]) -> bool:
    """Case-insensitive token match against any keyword.

    Requires a word boundary so ``asic`` does not match ``basic``.
    A trailing ``s`` is allowed (``ASICs``, ``FPGAs``).
    """
    haystack = text.lower()
    for keyword in keywords:
        needle = keyword.lower().strip()
        if not needle:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}s?(?![a-z0-9])", haystack):
            return True
    return False


def title_matches(title: str, title_keywords: list[str] | None) -> bool:
    """Optional title noise filter. Empty/None list means keep all titles."""
    if not title_keywords:
        return True
    return matches_keywords(title, title_keywords)


def filter_by_us_location(
    postings: list[JobPosting],
    rules: UsLocationFilter,
) -> tuple[list[JobPosting], list[JobPosting]]:
    """Split postings into (kept, skipped) by US location. Empty location is kept."""
    kept: list[JobPosting] = []
    skipped: list[JobPosting] = []
    for posting in postings:
        if rules.is_us(posting.location):
            kept.append(posting)
        else:
            skipped.append(posting)
    return kept, skipped


def filter_by_posted_date(
    postings: list[JobPosting],
    *,
    today: date,
    lookback_days: int = POSTED_LOOKBACK_DAYS,
) -> tuple[list[JobPosting], list[JobPosting]]:
    """Keep undated postings and those posted within ``lookback_days``."""
    cutoff = today - timedelta(days=lookback_days)
    kept: list[JobPosting] = []
    skipped: list[JobPosting] = []
    for posting in postings:
        if posting.date_posted is None or posting.date_posted >= cutoff:
            kept.append(posting)
        else:
            skipped.append(posting)
    return kept, skipped


@dataclass(frozen=True)
class EducationFilter:
    """Drop internships that are clearly post-undergrad only."""

    title_drop_patterns: tuple[re.Pattern[str], ...]
    graduate_required_patterns: tuple[re.Pattern[str], ...]
    undergrad_ok_patterns: tuple[re.Pattern[str], ...]

    @classmethod
    def from_yaml(cls, path: Path) -> EducationFilter:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            title_drop_patterns=tuple(
                _phrase_pattern(str(p)) for p in (data.get("title_drop") or [])
            ),
            graduate_required_patterns=tuple(
                _phrase_pattern(str(p)) for p in (data.get("graduate_required") or [])
            ),
            undergrad_ok_patterns=tuple(
                _phrase_pattern(str(p)) for p in (data.get("undergrad_ok") or [])
            ),
        )

    def is_post_undergrad_only(self, title: str, description: str) -> bool:
        title_text = _normalize_edu_text(title)
        if any(p.search(title_text) for p in self.title_drop_patterns):
            return True
        blob = f"{title_text}\n{_normalize_edu_text(description)}"
        if not any(p.search(blob) for p in self.graduate_required_patterns):
            return False
        if any(p.search(blob) for p in self.undergrad_ok_patterns):
            return False
        return True


def load_education_filter(path: Path) -> EducationFilter:
    return EducationFilter.from_yaml(path)


def filter_by_education(
    postings: list[JobPosting],
    rules: EducationFilter,
) -> tuple[list[JobPosting], list[JobPosting]]:
    """Split postings into (kept, skipped) by graduate-only requirements."""
    kept: list[JobPosting] = []
    skipped: list[JobPosting] = []
    for posting in postings:
        if rules.is_post_undergrad_only(posting.title, posting.description):
            skipped.append(posting)
        else:
            kept.append(posting)
    return kept, skipped


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
