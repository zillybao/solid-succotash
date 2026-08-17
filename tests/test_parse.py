"""Parser tests against saved HTML fixtures."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from src.parse import (
    SiteConfig,
    _parse_date,
    _smartrecruiters_description,
    _smartrecruiters_location,
    _talentbrew_cards,
    parse_html_fixture,
    parse_site,
)

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeFetcher:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get_json(self, url: str) -> Any:
        self.calls.append(url)
        if url in self.responses:
            return self.responses[url]
        for key, value in self.responses.items():
            if key in url:
                return value
        raise KeyError(url)


def test_parse_html_fixture_with_selector() -> None:
    html = (FIXTURES / "generic_listings.html").read_text(encoding="utf-8")
    postings = parse_html_fixture(
        html,
        company="Fixture Co",
        source_page="https://example.com/careers",
        selector="a.job-listing",
    )
    assert len(postings) == 2
    titles = {p.title for p in postings}
    assert "Firmware Engineering Intern" in titles
    assert "Software Engineering Intern" in titles
    assert all(p.link.startswith("https://example.com/") for p in postings)
    assert all(p.company == "Fixture Co" for p in postings)


def test_smartrecruiters_location_and_description() -> None:
    loc = _smartrecruiters_location(
        {"city": "Austin", "region": "TX", "country": "us", "fullLocation": "Austin, TX, United States"}
    )
    assert loc == "Austin, TX, United States"
    assert _smartrecruiters_location({"city": "Tokyo", "country": "jp"}) == "Tokyo, jp"
    detail = {
        "jobAd": {
            "sections": {
                "jobDescription": {"text": "<p>Work on FPGA and firmware.</p>"},
                "qualifications": {"text": "<ul><li>Python</li></ul>"},
            }
        }
    }
    text = _smartrecruiters_description(detail)
    assert "FPGA" in text
    assert "Python" in text
    assert "<p>" not in text


def test_talentbrew_cards_synopsys_and_arm() -> None:
    synopsys_html = """
    <a class="sr-job-link" href="/job/yerevan/asic-digital-design-intern/44408/1" data-job-id="1">
        <h2>ASIC Digital Design Intern<img src="x"></h2>
    </a>
    """
    cards = _talentbrew_cards(synopsys_html, "https://careers.synopsys.com")
    assert len(cards) == 1
    assert cards[0]["title"] == "ASIC Digital Design Intern"
    assert cards[0]["link"].endswith("/asic-digital-design-intern/44408/1")

    arm_html = """
    <li class="job-card fs-start">
        <a class="job-card__title fs-11" href="/job/cambridge/internship-cpu/33099/2">Internship - CPU Group</a>
        <span class="location">Cambridge, UK</span>
    </li>
    """
    cards = _talentbrew_cards(arm_html, "https://careers.arm.com")
    assert len(cards) == 1
    assert cards[0]["title"] == "Internship - CPU Group"
    assert cards[0]["location"] == "Cambridge, UK"


def test_parse_date_amazon_and_iso() -> None:
    assert _parse_date("July 29, 2026") == date(2026, 7, 29)
    assert _parse_date("Aug 12, 2026") == date(2026, 8, 12)
    assert _parse_date("2026-08-15T12:00:00.000Z") == date(2026, 8, 15)
    assert _parse_date("") is None


def test_greenhouse_detail_fetch_only_intern_titles() -> None:
    list_url = "https://boards-api.greenhouse.io/v1/boards/spacex/jobs"
    intern_detail = "https://boards-api.greenhouse.io/v1/boards/spacex/jobs/1"
    staff_detail = "https://boards-api.greenhouse.io/v1/boards/spacex/jobs/2"
    fetcher = _FakeFetcher(
        {
            list_url: {
                "jobs": [
                    {
                        "id": 1,
                        "title": "FPGA Intern",
                        "absolute_url": "https://boards.greenhouse.io/spacex/jobs/1",
                        "location": {"name": "CA"},
                        "updated_at": "2026-08-16T00:00:00Z",
                    },
                    {
                        "id": 2,
                        "title": "Staff Accountant",
                        "absolute_url": "https://boards.greenhouse.io/spacex/jobs/2",
                    },
                    {
                        "id": 3,
                        "title": "Avionics Intern",
                        "absolute_url": "https://boards.greenhouse.io/spacex/jobs/3",
                        "content": "<p>embedded firmware</p>",
                    },
                ]
            },
            intern_detail: {"content": "<p>FPGA and RTL</p>"},
            staff_detail: {"content": "<p>should not be fetched</p>"},
        }
    )
    site = SiteConfig(
        {
            "company": "SpaceX",
            "url": "https://boards.greenhouse.io/spacex",
            "ats": "greenhouse",
            "board": "spacex",
        }
    )
    postings = parse_site(site, fetcher, today=date(2026, 8, 17))  # type: ignore[arg-type]
    titles = {p.title for p in postings}
    assert titles == {"FPGA Intern", "Avionics Intern"}
    assert intern_detail in fetcher.calls
    assert staff_detail not in fetcher.calls
    by_title = {p.title: p for p in postings}
    assert "RTL" in by_title["FPGA Intern"].description
    assert "firmware" in by_title["Avionics Intern"].description
