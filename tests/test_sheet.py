"""Tests for spreadsheet helpers that do not need the Sheets API."""

from src.models import SHEET_HEADERS
from src.sheet import records_from_values, resolve_worksheet_name, spreadsheet_id_from_value


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


def test_records_from_values_headers_only() -> None:
    assert records_from_values([list(SHEET_HEADERS)]) == []


def test_resolve_worksheet_name_defaults_when_env_blank(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_SHEET_WORKSHEET", "")
    assert resolve_worksheet_name() == "Sheet1"
    monkeypatch.delenv("GOOGLE_SHEET_WORKSHEET", raising=False)
    assert resolve_worksheet_name() == "Sheet1"
    assert resolve_worksheet_name("  Internships  ") == "Internships"
