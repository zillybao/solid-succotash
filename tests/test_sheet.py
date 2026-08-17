"""Tests for spreadsheet helpers that do not need the Sheets API."""

from src.sheet import spreadsheet_id_from_value


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
