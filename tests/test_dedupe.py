"""Tests for link normalization and dedupe helpers."""

from __future__ import annotations

from pathlib import Path

from src.dedupe import (
    SeenJobsCache,
    filter_new,
    identity_hashes,
    is_known_link,
    link_hash,
    normalize_link,
)


def test_normalize_strips_tracking_params() -> None:
    url = "https://Jobs.Example.com/careers/123/?utm_source=x&utm_medium=y&id=1"
    assert normalize_link(url) == "https://jobs.example.com/careers/123?id=1"


def test_normalize_trailing_slash_and_fragment() -> None:
    assert normalize_link("https://Example.com/jobs/abc/#section") == "https://example.com/jobs/abc"


def test_normalize_preserves_root_slash() -> None:
    assert normalize_link("https://example.com/") == "https://example.com/"


def test_link_hash_stable() -> None:
    a = "https://EXAMPLE.com/job/1?utm_source=twitter"
    b = "https://example.com/job/1/"
    assert link_hash(a) == link_hash(b)


def test_filter_new() -> None:
    known = {link_hash("https://example.com/a")}
    links = ["https://example.com/a", "https://example.com/b"]
    assert filter_new(links, known) == ["https://example.com/b"]


def test_filter_new_matches_greenhouse_host_alias() -> None:
    known = identity_hashes("https://boards.greenhouse.io/spacex/jobs/123")
    links = [
        "https://job-boards.greenhouse.io/spacex/jobs/123",
        "https://boards.greenhouse.io/spacex/jobs/999",
    ]
    assert filter_new(links, known) == ["https://boards.greenhouse.io/spacex/jobs/999"]


def test_filter_new_matches_workday_locale_and_req_id() -> None:
    known = identity_hashes(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US/Title_JR1987654"
    )
    same = "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US/Title_JR1987654"
    other = "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US/Title_JR1"
    assert filter_new([same, other], known) == [other]


def test_identity_unwraps_hyperlink_formula() -> None:
    formula = '=HYPERLINK("https://example.com/job/1","Firmware Intern")'
    known = identity_hashes(formula)
    assert is_known_link("https://example.com/job/1/", known)


def test_seen_jobs_cache_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    cache = SeenJobsCache(path)
    cache.add("https://example.com/job/1?utm_campaign=x")
    cache.save()

    reloaded = SeenJobsCache(path)
    assert "https://example.com/job/1" in reloaded
    assert "https://example.com/other" not in reloaded
