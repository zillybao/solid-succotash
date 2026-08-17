"""Shared data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Status = Literal["open", "applied", "closed"]


@dataclass(slots=True)
class JobPosting:
    """Normalized job posting extracted from an ATS or HTML page."""

    company: str
    title: str
    link: str
    source_page: str
    location: str = ""
    description: str = field(default="", repr=False)
    status: Status = "open"
    date_found: date | None = None
    date_posted: date | None = None

    def sheet_row(self) -> list[str]:
        """Columns written to the spreadsheet (description intentionally omitted)."""
        return [
            self.company,
            self.title,
            self.link,
            self.location,
            self.status,
            self.date_found.isoformat() if self.date_found else "",
            self.date_posted.isoformat() if self.date_posted else "",
            self.source_page,
        ]


SHEET_HEADERS: list[str] = [
    "company",
    "title",
    "link",
    "location",
    "status",
    "date_found",
    "date_posted",
    "source_page",
]

# Bump when column layout changes; stored in sheet metadata / A1 comment contract.
SCHEMA_VERSION = 1
