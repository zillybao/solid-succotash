"""Tests for keyword filtering."""

from __future__ import annotations

from pathlib import Path

from src.filter import (
    filter_by_description,
    filter_by_us_location,
    load_keywords,
    load_us_location_filter,
    matches_keywords,
)
from src.models import JobPosting

LOCATIONS_YAML = Path(__file__).resolve().parent.parent / "config" / "locations.yaml"


def _posting(title: str, description: str, location: str = "") -> JobPosting:
    return JobPosting(
        company="Test",
        title=title,
        link="https://example.com/job",
        source_page="https://example.com/careers",
        location=location,
        description=description,
    )


def test_matches_keywords_case_insensitive() -> None:
    assert matches_keywords("Work on FPGA boards", ["fpga", "rtl"])
    assert matches_keywords("Experience with ASICs and RTL", ["asic", "rtl"])
    assert not matches_keywords("Frontend React intern", ["fpga", "rtl"])


def test_matches_keywords_does_not_hit_substrings() -> None:
    assert not matches_keywords("Basic qualifications: Excel, SQL", ["asic"])
    assert not matches_keywords("Internal tools and international travel", ["intern"])
    assert matches_keywords("ASIC design intern", ["asic"])
    assert matches_keywords("Firmware Intern", ["intern"])


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


def test_us_location_keeps_country_and_state_forms() -> None:
    rules = load_us_location_filter(LOCATIONS_YAML)
    assert rules.is_us("")
    assert rules.is_us("Remote")
    assert rules.is_us("Santa Clara, CA, United States")
    assert rules.is_us("Austin, TX")
    assert rules.is_us("California")
    assert rules.is_us("New York, NY")
    assert rules.is_us("Washington, DC")
    assert rules.is_us("USA")
    assert rules.is_us("US and Canada")
    assert rules.is_us("Cambridge, MA")
    assert rules.is_us("Austin, Texas, USA")


def test_us_location_drops_foreign_countries() -> None:
    rules = load_us_location_filter(LOCATIONS_YAML)
    assert not rules.is_us("Munich, Germany")
    assert not rules.is_us("Toronto, ON, Canada")
    assert not rules.is_us("London, UK")
    assert not rules.is_us("Bengaluru, India")
    assert not rules.is_us("Remote - India")
    assert not rules.is_us("Seoul, South Korea")
    assert not rules.is_us("Cambridge, UK")
    assert not rules.is_us("Berlin, DEU")


def test_filter_by_us_location_splits_postings() -> None:
    rules = load_us_location_filter(LOCATIONS_YAML)
    posts = [
        _posting("Intern", "firmware", "Austin, TX"),
        _posting("Intern", "firmware", "Munich, Germany"),
        _posting("Intern", "firmware", ""),
    ]
    kept, skipped = filter_by_us_location(posts, rules)
    assert [p.location for p in kept] == ["Austin, TX", ""]
    assert [p.location for p in skipped] == ["Munich, Germany"]
