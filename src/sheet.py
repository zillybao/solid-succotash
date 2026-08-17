"""Google Sheets read/write via gspread."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from src.dedupe import link_hash, normalize_link
from src.models import SHEET_HEADERS, SCHEMA_VERSION, JobPosting

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column indices (0-based) matching SHEET_HEADERS
COL_LINK = 2
COL_STATUS = 4

_SHEET_ID_FROM_URL = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


class SheetError(Exception):
    """Spreadsheet configuration or API failure."""


def spreadsheet_id_from_value(value: str) -> str:
    """Accept a raw spreadsheet ID or a docs.google.com/spreadsheets URL."""
    text = value.strip()
    match = _SHEET_ID_FROM_URL.search(text)
    return match.group(1) if match else text


def _credentials_from_env() -> Credentials:
    json_blob = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")

    if json_blob:
        info = json.loads(json_blob)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    path = Path(file_path)
    if not path.exists():
        raise SheetError(
            "Google credentials not found. Set GOOGLE_SERVICE_ACCOUNT_FILE "
            "or GOOGLE_SERVICE_ACCOUNT_JSON."
        )
    return Credentials.from_service_account_file(str(path), scopes=SCOPES)


class JobSheet:
    """Append-only internship tracker backed by Google Sheets."""

    def __init__(self, spreadsheet_id: str | None = None, worksheet_name: str | None = None) -> None:
        raw_id = spreadsheet_id or os.getenv("GOOGLE_SHEET_ID", "")
        self.spreadsheet_id = spreadsheet_id_from_value(raw_id)
        if not self.spreadsheet_id:
            raise SheetError("GOOGLE_SHEET_ID is not set.")
        self.worksheet_name = worksheet_name or os.getenv("GOOGLE_SHEET_WORKSHEET", "Sheet1")
        self._client = gspread.authorize(_credentials_from_env())
        self._sheet = self._client.open_by_key(self.spreadsheet_id).worksheet(self.worksheet_name)
        self._ensure_headers()

    def _ensure_headers(self) -> None:
        existing = self._sheet.row_values(1)
        if not existing:
            self._sheet.update(range_name="A1", values=[SHEET_HEADERS], value_input_option="RAW")
            # Store schema version in a sentinel cell; bump SCHEMA_VERSION on layout changes.
            self._sheet.update(range_name="Z1", values=[[f"schema_version={SCHEMA_VERSION}"]])
            return
        if [h.lower() for h in existing[: len(SHEET_HEADERS)]] != SHEET_HEADERS:
            logger.warning(
                "Sheet header mismatch (expected %s, got %s). Not reshaping existing data.",
                SHEET_HEADERS,
                existing,
            )

    def all_rows(self) -> list[dict[str, str]]:
        records = self._sheet.get_all_records()
        return [{str(k): str(v) if v is not None else "" for k, v in row.items()} for row in records]

    def known_link_hashes(self) -> set[str]:
        hashes: set[str] = set()
        for row in self.all_rows():
            link = row.get("link") or row.get("Link") or ""
            if link:
                hashes.add(link_hash(link))
        return hashes

    def open_rows_by_source(self) -> dict[str, list[dict[str, Any]]]:
        """Map source_page -> open/applied rows (with sheet row number)."""
        values = self._sheet.get_all_values()
        if len(values) <= 1:
            return {}

        headers = [h.lower() for h in values[0]]
        try:
            link_i = headers.index("link")
            status_i = headers.index("status")
            source_i = headers.index("source_page")
        except ValueError:
            logger.error("Sheet missing required columns among: %s", headers)
            return {}

        by_source: dict[str, list[dict[str, Any]]] = {}
        for idx, row in enumerate(values[1:], start=2):
            if len(row) <= max(link_i, status_i, source_i):
                continue
            status = (row[status_i] or "").strip().lower()
            if status not in {"open", "applied"}:
                continue
            source = row[source_i].strip()
            entry = {
                "row_number": idx,
                "link": row[link_i],
                "status": status,
                "normalized_link": normalize_link(row[link_i]),
            }
            by_source.setdefault(source, []).append(entry)
        return by_source

    def append_postings(self, postings: list[JobPosting]) -> int:
        if not postings:
            return 0
        rows = [p.sheet_row() for p in postings]
        self._sheet.append_rows(rows, value_input_option="USER_ENTERED")
        return len(rows)

    def mark_closed(self, row_numbers: list[int]) -> int:
        """Set status=closed for the given 1-based sheet row numbers. Never touches applied→open."""
        if not row_numbers:
            return 0
        # Status is column E (5)
        cells = []
        for row_num in row_numbers:
            cells.append(gspread.Cell(row_num, COL_STATUS + 1, "closed"))
        self._sheet.update_cells(cells)
        return len(cells)
