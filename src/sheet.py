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

from src.dedupe import identity_hashes, is_known_link, normalize_link
from src.models import SHEET_HEADERS, JobPosting

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column indices (0-based) matching SHEET_HEADERS
COL_LINK = 2
COL_STATUS = 4

# Sheet1 originally had source_page in column G, before date_posted was added.
LEGACY_HEADERS_WITHOUT_DATE_POSTED: list[str] = [
    "company",
    "title",
    "link",
    "location",
    "status",
    "date_found",
    "source_page",
]


def needs_date_posted_column(existing: list[str]) -> bool:
    """True when row 1 is the 7-column layout that omitted date_posted."""
    lowered = [str(h).strip().lower() for h in existing]
    if lowered[: len(SHEET_HEADERS)] == SHEET_HEADERS:
        return False
    return lowered[:7] == LEGACY_HEADERS_WITHOUT_DATE_POSTED

DEFAULT_SEEN_WORKSHEET = "_seen"
SEEN_HEADERS: list[str] = ["link", "company", "title", "date_found"]

_SHEET_ID_FROM_URL = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


class SheetError(Exception):
    """Spreadsheet configuration or API failure."""


def spreadsheet_id_from_value(value: str) -> str:
    """Accept a raw spreadsheet ID or a docs.google.com/spreadsheets URL."""
    text = value.strip()
    match = _SHEET_ID_FROM_URL.search(text)
    return match.group(1) if match else text


def resolve_worksheet_name(explicit: str | None = None) -> str:
    """Worksheet tab name. Blank env/secret values fall back to Sheet1.

    GitHub Actions always injects ``GOOGLE_SHEET_WORKSHEET`` when the workflow
    maps the secret, even if the secret is unset (empty string). ``os.getenv``
    would otherwise skip the default and look up a tab named ``""``.
    """
    raw = explicit if explicit is not None else os.getenv("GOOGLE_SHEET_WORKSHEET")
    name = (raw or "").strip()
    return name or "Sheet1"


def resolve_seen_worksheet_name(explicit: str | None = None) -> str:
    """Durable seen-history tab. Blank env/secret values fall back to ``_seen``.

    This tab is the skip list that survives wiping the inbox worksheet.
    """
    raw = explicit if explicit is not None else os.getenv("GOOGLE_SHEET_SEEN_WORKSHEET")
    name = (raw or "").strip()
    return name or DEFAULT_SEEN_WORKSHEET


def seen_links_needing_backfill(inbox_links: list[str], seen_hashes: set[str]) -> list[str]:
    """Inbox links not yet recorded in the seen-history tab."""
    known = set(seen_hashes)
    missing: list[str] = []
    for link in inbox_links:
        text = (link or "").strip()
        if not text or is_known_link(text, known):
            continue
        missing.append(text)
        known.update(identity_hashes(text))
    return missing


def records_from_values(values: list[list[Any]]) -> list[dict[str, str]]:
    """Turn sheet grid values into row dicts using columns A–H only.

    Keys are always ``SHEET_HEADERS`` by column index, so a renamed or
    Title-Cased header row still maps ``link`` to column C.
    """
    if len(values) <= 1:
        return []

    records: list[dict[str, str]] = []
    for row in values[1:]:
        record: dict[str, str] = {}
        nonempty = False
        for i, key in enumerate(SHEET_HEADERS):
            cell = row[i] if i < len(row) and row[i] is not None else ""
            text = str(cell)
            record[key] = text
            if text.strip():
                nonempty = True
        if nonempty:
            records.append(record)
    return records


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

    def __init__(
        self,
        spreadsheet_id: str | None = None,
        worksheet_name: str | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        raw_id = spreadsheet_id or os.getenv("GOOGLE_SHEET_ID", "")
        self.spreadsheet_id = spreadsheet_id_from_value(raw_id)
        if not self.spreadsheet_id:
            raise SheetError("GOOGLE_SHEET_ID is not set.")
        self.worksheet_name = resolve_worksheet_name(worksheet_name)
        self.seen_worksheet_name = resolve_seen_worksheet_name()
        if self.seen_worksheet_name == self.worksheet_name:
            raise SheetError(
                "GOOGLE_SHEET_SEEN_WORKSHEET must be a different tab than "
                f"{self.worksheet_name!r} (the inbox you can clear)."
            )
        self._client = gspread.authorize(_credentials_from_env())
        try:
            self._book = self._client.open_by_key(self.spreadsheet_id)
            self._sheet = self._book.worksheet(self.worksheet_name)
        except gspread.exceptions.WorksheetNotFound as exc:
            raise SheetError(
                f"Worksheet {self.worksheet_name!r} not found. Set "
                "GOOGLE_SHEET_WORKSHEET to the tab name at the bottom of the "
                "spreadsheet, or omit it to use Sheet1."
            ) from exc
        except gspread.exceptions.SpreadsheetNotFound as exc:
            raise SheetError(
                f"Spreadsheet {self.spreadsheet_id!r} not found or the service "
                "account does not have access."
            ) from exc
        if not read_only:
            self._ensure_headers()
        self._seen = self._open_seen_worksheet(read_only=read_only)
        if not read_only:
            backfilled = self.backfill_seen_from_inbox()
            if backfilled:
                logger.info(
                    "Backfilled %s inbox link(s) into %s",
                    backfilled,
                    self.seen_worksheet_name,
                )

    def _ensure_headers(self) -> None:
        existing = self._sheet.row_values(1)
        if not existing:
            self._sheet.update(range_name="A1", values=[SHEET_HEADERS], value_input_option="RAW")
            return
        if [h.lower() for h in existing[: len(SHEET_HEADERS)]] == SHEET_HEADERS:
            return
        if needs_date_posted_column(existing):
            # Insert G so existing source_page values shift to H; do not overwrite G.
            self._sheet.insert_cols([["date_posted"]], col=7)
            logger.info("Inserted missing date_posted column (G) on %s.", self.worksheet_name)
            return
        logger.warning(
            "Sheet header mismatch (expected %s, got %s). Not reshaping existing data.",
            SHEET_HEADERS,
            existing,
        )

    def _open_seen_worksheet(self, *, read_only: bool) -> Any:
        try:
            ws = self._book.worksheet(self.seen_worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            if read_only:
                logger.warning(
                    "Seen-history tab %s not found; skip list is inbox-only this run.",
                    self.seen_worksheet_name,
                )
                return None
            ws = self._book.add_worksheet(
                title=self.seen_worksheet_name,
                rows=2000,
                cols=len(SEEN_HEADERS),
            )
            ws.update(range_name="A1", values=[SEEN_HEADERS], value_input_option="RAW")
            logger.info("Created seen-history tab %s.", self.seen_worksheet_name)
            return ws
        if not read_only:
            existing = ws.row_values(1)
            if not existing:
                ws.update(range_name="A1", values=[SEEN_HEADERS], value_input_option="RAW")
        return ws

    def _seen_link_hashes(self) -> set[str]:
        if self._seen is None:
            return set()
        values = self._seen.get_all_values()
        if not values:
            return set()
        start = 0
        if str(values[0][0]).strip().lower() == "link":
            start = 1
        hashes: set[str] = set()
        for row in values[start:]:
            if not row:
                continue
            link = str(row[0] or "")
            if link.strip():
                hashes.update(identity_hashes(link))
        return hashes

    def backfill_seen_from_inbox(self) -> int:
        """Copy inbox links into the seen tab so wiping the inbox keeps skip history."""
        if self._seen is None:
            return 0
        inbox_links = [row.get("link") or "" for row in self.all_rows()]
        missing = seen_links_needing_backfill(inbox_links, self._seen_link_hashes())
        if not missing:
            return 0
        by_link = {row.get("link") or "": row for row in self.all_rows()}
        rows = []
        for link in missing:
            record = by_link.get(link) or {}
            rows.append(
                [
                    link,
                    record.get("company") or "",
                    record.get("title") or "",
                    record.get("date_found") or "",
                ]
            )
        self._seen.append_rows(rows, value_input_option="RAW", table_range="A1")
        return len(rows)

    def _append_seen(self, postings: list[JobPosting]) -> None:
        if not postings or self._seen is None:
            return
        rows = [
            [
                p.link,
                p.company,
                p.title,
                p.date_found.isoformat() if p.date_found else "",
            ]
            for p in postings
        ]
        self._seen.append_rows(rows, value_input_option="RAW", table_range="A1")

    def all_rows(self) -> list[dict[str, str]]:
        # get_all_records() treats the entire first row as headers. The schema
        # sentinel in Z1 leaves blank cells in I1:Y1, which gspread rejects as
        # duplicate empty headers. Map A–H only.
        return records_from_values(self._sheet.get_all_values())

    def known_link_hashes(self) -> set[str]:
        hashes: set[str] = set()
        for row in self.all_rows():
            link = row.get("link") or ""
            if link:
                hashes.update(identity_hashes(link))
        hashes |= self._seen_link_hashes()
        return hashes

    def open_rows_by_source(self) -> dict[str, list[dict[str, Any]]]:
        """Map source_page -> open/applied rows (with sheet row number)."""
        values = self._sheet.get_all_values()
        if len(values) <= 1:
            return {}

        headers = [str(h).strip().lower() for h in values[0]]
        link_i = headers.index("link") if "link" in headers else COL_LINK
        status_i = headers.index("status") if "status" in headers else COL_STATUS
        source_i = headers.index("source_page") if "source_page" in headers else 7

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
        # table_range="A1" pins the logical table to the header row. Without it,
        # Sheets treats the schema sentinel in Z1 as the table and appends at Z.
        self._sheet.append_rows(
            rows,
            value_input_option="USER_ENTERED",
            table_range="A1",
        )
        try:
            self._append_seen(postings)
        except Exception as exc:  # noqa: BLE001 — inbox write already succeeded
            logger.warning("Failed to append seen-history rows: %s", exc)
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
