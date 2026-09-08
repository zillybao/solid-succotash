"""Tests for spreadsheet helpers that do not need the Sheets API."""

from src.dedupe import identity_hashes
from src.models import SHEET_HEADERS
from src.sheet import (
    needs_date_posted_column,
    records_from_values,
    resolve_seen_worksheet_name,
    resolve_worksheet_name,
    seen_links_needing_backfill,
    spreadsheet_id_from_value,
)


def test_spreadsheet_id_from_raw_id() -> None:
    assert spreadsheet_id_from_value(" 1H0buUOQGmhKLn93DnAH9DmHySsqXANciCAA74KmZWQs ") == (
        "1H0buUOQGmhKLn93DnAH9DmHySsqXANciCAA74KmZWQs"
    )


def test_spreadsheet_id_from_docs_url() -> None:
    url = (
        "https://docs.google.com/spreadsheets/d/"
        "1H0buUOQGmhKLn93DnAH9DmHySsqXANciCAA74KmZWQs/edit?gid=0#gid=0"
    )
    assert spreadsheet_id_from_value(url) == "1H0buUOQGmhKLn93DnAH9DmHySsqXANciCAA74KmZWQs"


def test_records_from_values_ignores_z1_schema_sentinel() -> None:
    header = list(SHEET_HEADERS) + [""] * 17 + ["schema_version=1"]
    values = [
        header,
        ["Acme", "Firmware Intern", "https://example.com/1", "Austin", "open", "2026-08-18", "", "https://board"],
        ["", "", "", "", "", "", "", ""],
    ]
    rows = records_from_values(values)
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"
    assert rows[0]["link"] == "https://example.com/1"


def test_records_from_values_uses_positional_headers_when_renamed() -> None:
    values = [
        ["Company", "Title", "Job URL", "Where", "Status", "Found", "Posted", "Source"],
        ["Acme", "Intern", "https://example.com/1", "Austin", "open", "2026-08-18", "", "https://board"],
    ]
    rows = records_from_values(values)
    assert rows[0]["link"] == "https://example.com/1"
    assert "Job URL" not in rows[0]


def test_records_from_values_headers_only() -> None:
    assert records_from_values([list(SHEET_HEADERS)]) == []


def test_needs_date_posted_column_detects_legacy_seven_col_header() -> None:
    legacy = [
        "company",
        "title",
        "link",
        "location",
        "status",
        "date_found",
        "source_page",
    ]
    assert needs_date_posted_column(legacy) is True
    assert needs_date_posted_column(list(SHEET_HEADERS)) is False
    assert needs_date_posted_column(["Company", "Title"]) is False


def test_resolve_worksheet_name_defaults_when_env_blank(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_SHEET_WORKSHEET", "")
    assert resolve_worksheet_name() == "Sheet1"
    monkeypatch.delenv("GOOGLE_SHEET_WORKSHEET", raising=False)
    assert resolve_worksheet_name() == "Sheet1"
    assert resolve_worksheet_name("  Internships  ") == "Internships"


def test_resolve_seen_worksheet_name_defaults_when_env_blank(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_SHEET_SEEN_WORKSHEET", "")
    assert resolve_seen_worksheet_name() == "_seen"
    monkeypatch.delenv("GOOGLE_SHEET_SEEN_WORKSHEET", raising=False)
    assert resolve_seen_worksheet_name() == "_seen"
    assert resolve_seen_worksheet_name("  history  ") == "history"


def test_seen_links_needing_backfill_skips_known_and_variants() -> None:
    known = identity_hashes("https://boards.greenhouse.io/spacex/jobs/1")
    inbox = [
        "https://job-boards.greenhouse.io/spacex/jobs/1",
        "https://boards.greenhouse.io/spacex/jobs/2",
        "",
    ]
    assert seen_links_needing_backfill(inbox, known) == [
        "https://boards.greenhouse.io/spacex/jobs/2",
    ]
