"""Tests for keyword filtering."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.filter import (
    filter_by_description,
    filter_by_education,
    filter_by_posted_date,
    filter_by_us_location,
    load_education_filter,
    load_keywords,
    load_us_location_filter,
    matches_keywords,
)
from src.models import JobPosting

LOCATIONS_YAML = Path(__file__).resolve().parent.parent / "config" / "locations.yaml"
EDUCATION_YAML = Path(__file__).resolve().parent.parent / "config" / "education.yaml"


def _posting(
    title: str,
    description: str,
    location: str = "",
    *,
    date_posted: date | None = None,
) -> JobPosting:
    return JobPosting(
        company="Test",
        title=title,
        link="https://example.com/job",
        source_page="https://example.com/careers",
        location=location,
        description=description,
        date_posted=date_posted,
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
    assert not rules.is_us("Hyderabad, India")
    assert not rules.is_us("Bucharest, Romania")
    assert not rules.is_us("Remote - India")
    assert not rules.is_us("Seoul, South Korea")
    assert not rules.is_us("Cambridge, UK")
    assert not rules.is_us("Berlin, DEU")
    # Greenhouse office names attach a site number to the country code.
    assert not rules.is_us("London - UK2")
    assert not rules.is_us("UK2")
    assert rules.is_us("Cambridge, MA")


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


def test_us_location_drops_city_only_foreign_hubs() -> None:
    rules = load_us_location_filter(LOCATIONS_YAML)
    assert not rules.is_us("Shanghai")
    assert not rules.is_us("Linz")
    assert not rules.is_us("Linz; Austria")
    assert not rules.is_us("Shanghai; 上海 (China); China")
    assert not rules.is_us("Hyderabad")
    assert not rules.is_us("Munich")
    assert rules.is_us("Austin")
    assert rules.is_us("Lynnwood; WA (United States); United States")
    assert rules.is_us("Cambridge, MA")


def test_filter_by_posted_date_keeps_recent_and_undated() -> None:
    today = date(2026, 8, 19)
    posts = [
        _posting("Intern", "fpga", date_posted=date(2026, 8, 18)),
        _posting("Intern", "fpga", date_posted=date(2026, 8, 12)),
        _posting("Intern", "fpga", date_posted=date(2026, 8, 11)),
        _posting("Intern", "fpga", date_posted=None),
        _posting("Intern", "fpga", date_posted=date(2026, 6, 18)),
    ]
    kept, skipped = filter_by_posted_date(posts, today=today, lookback_days=7)
    assert [p.date_posted for p in kept] == [
        date(2026, 8, 18),
        date(2026, 8, 12),
        None,
    ]
    assert [p.date_posted for p in skipped] == [
        date(2026, 8, 11),
        date(2026, 6, 18),
    ]


def test_education_drops_grad_only_keeps_bachelor_or_above() -> None:
    rules = load_education_filter(EDUCATION_YAML)
    assert rules.is_post_undergrad_only("PhD Intern - RTL", "Work on FPGA.")
    assert rules.is_post_undergrad_only(
        "Digital Design Intern",
        "Must be currently enrolled in a Master's or PhD program in EE.",
    )
    assert rules.is_post_undergrad_only(
        "Firmware Intern",
        "PhD required. Experience with microcontrollers.",
    )
    # Infineon-style: bachelor in progress, master's preferred.
    assert not rules.is_post_undergrad_only(
        "Technical Marketing Intern",
        "Bachelor\u2019s degree or above (in progress); Master\u2019s preferred. ARM MCU firmware.",
    )
    assert not rules.is_post_undergrad_only(
        "Software Engineering Intern",
        "Pursuing a bachelor's or master's in CS. FPGA experience a plus.",
    )
    assert not rules.is_post_undergrad_only(
        "Graduate Intern",
        "Work on embedded firmware for sensors.",
    )
    posts = [
        _posting("PhD Intern", "FPGA and RTL"),
        _posting("Eng Intern", "Bachelor's in EE. Firmware."),
    ]
    kept, skipped = filter_by_education(posts, rules)
    assert [p.title for p in kept] == ["Eng Intern"]
    assert [p.title for p in skipped] == ["PhD Intern"]
