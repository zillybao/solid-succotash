"""Tests for keyword filtering."""

from __future__ import annotations

from pathlib import Path

from src.filter import filter_by_description, load_keywords, matches_keywords
from src.models import JobPosting


def _posting(title: str, description: str) -> JobPosting:
    return JobPosting(
        company="Test",
        title=title,
        link="https://example.com/job",
        source_page="https://example.com/careers",
        description=description,
    )


def test_matches_keywords_case_insensitive() -> None:
    assert matches_keywords("Work on FPGA boards", ["fpga", "rtl"])
    assert not matches_keywords("Frontend React intern", ["fpga", "rtl"])


def test_filter_by_description_keeps_matches() -> None:
    posts = [
        _posting("Eng Intern", "Build firmware for sensors"),
        _posting("Eng Intern", "Write React components"),
    ]
    kept, skipped = filter_by_description(posts, ["firmware", "fpga"])
    assert len(kept) == 1
    assert kept[0].description.startswith("Build firmware")
    assert len(skipped) == 1


def test_filter_log_only_keeps_all() -> None:
    posts = [
        _posting("Eng Intern", "Build firmware for sensors"),
        _posting("Eng Intern", "Write React components"),
    ]
    kept, skipped = filter_by_description(posts, ["firmware"], log_only=True)
    assert len(kept) == 2
    assert len(skipped) == 1


def test_load_keywords(tmp_path: Path) -> None:
    path = tmp_path / "keywords.yaml"
    path.write_text("keywords:\n  - Embedded\n  - FPGA\n", encoding="utf-8")
    assert load_keywords(path) == ["embedded", "fpga"]
