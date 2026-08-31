"""Tests for per-company sheet flush helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.dedupe import SeenJobsCache, link_hash
from src.models import JobPosting
from src.run import ROOT, _flush_company, load_sites
from src.sheet import SheetError


class FakeSheet:
    def __init__(self, *, fail_on_append: bool = False, fail_on_close: bool = False) -> None:
        self.fail_on_append = fail_on_append
        self.fail_on_close = fail_on_close
        self.appended: list[list[JobPosting]] = []
        self.closed: list[list[int]] = []

    def append_postings(self, postings: list[JobPosting]) -> int:
        if self.fail_on_append:
            raise SheetError("append failed")
        self.appended.append(list(postings))
        return len(postings)

    def mark_closed(self, row_numbers: list[int]) -> int:
        if self.fail_on_close:
            raise SheetError("close failed")
        self.closed.append(list(row_numbers))
        return len(row_numbers)


def _posting(company: str, link: str) -> JobPosting:
    return JobPosting(
        company=company,
        title="Firmware Intern",
        link=link,
        source_page="https://example.com/board",
        location="Austin, TX",
        description="",
        date_found=date(2026, 8, 31),
    )


def test_flush_company_appends_closes_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_path = tmp_path / "seen_jobs.json"
    state_path = tmp_path / "company_runs.json"
    monkeypatch.setattr("src.run.COMPANY_STATE_PATH", state_path)

    cache = SeenJobsCache(cache_path)
    sheet = FakeSheet()
    known: set[str] = set()
    company_state: dict[str, Any] = {"companies": {}}
    postings = [_posting("AMD", "https://careers.amd.com/jobs/1")]

    added, closed = _flush_company(
        sheet=sheet,  # type: ignore[arg-type]
        cache=cache,
        known_hashes=known,
        company_state=company_state,
        company="AMD",
        new_postings=postings,
        rows_to_close=[12, 15],
    )

    assert added == 1
    assert closed == 2
    assert sheet.appended == [postings]
    assert sheet.closed == [[12, 15]]
    assert link_hash(postings[0].link) in known
    assert postings[0].link in cache
    assert cache_path.exists()
    assert company_state["companies"]["AMD"]["runs"] == 1
    assert state_path.exists()


def test_flush_company_failed_append_does_not_advance_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "seen_jobs.json"
    cache = SeenJobsCache(cache_path)
    sheet = FakeSheet(fail_on_append=True)
    known: set[str] = set()
    company_state: dict[str, Any] = {"companies": {}}
    postings = [_posting("NVIDIA", "https://nvidia.example/jobs/2")]

    with pytest.raises(SheetError, match="append failed"):
        _flush_company(
            sheet=sheet,  # type: ignore[arg-type]
            cache=cache,
            known_hashes=known,
            company_state=company_state,
            company="NVIDIA",
            new_postings=postings,
            rows_to_close=[3],
        )

    assert known == set()
    assert cache.known_hashes() == set()
    assert not cache_path.exists()
    assert "NVIDIA" not in company_state["companies"]
    assert sheet.closed == []


def test_flush_company_close_failure_keeps_appended_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "seen_jobs.json"
    cache = SeenJobsCache(cache_path)
    sheet = FakeSheet(fail_on_close=True)
    known: set[str] = set()
    company_state: dict[str, Any] = {"companies": {}}
    postings = [_posting("Intel", "https://intel.example/jobs/3")]

    with pytest.raises(SheetError, match="close failed"):
        _flush_company(
            sheet=sheet,  # type: ignore[arg-type]
            cache=cache,
            known_hashes=known,
            company_state=company_state,
            company="Intel",
            new_postings=postings,
            rows_to_close=[9],
        )

    assert link_hash(postings[0].link) in known
    assert postings[0].link in cache
    assert cache_path.exists()
    assert sheet.appended == [postings]
    assert "Intel" not in company_state["companies"]


def test_sites_yaml_priority_scan_order() -> None:
    sites = load_sites(ROOT / "config" / "sites.yaml")
    names = [s.company for s in sites]
    assert names[0] == "AMD"
    assert names[1] == "NVIDIA"
    assert names[-1] == "Arm"
    assert names.index("AMD") < names.index("Broadcom")
    assert names.index("SpaceX") < names.index("Apple")
    assert names.index("Waymo") < names.index("Tesla")
