"""Orchestrator: fetch -> parse -> filter -> dedupe -> sheet write."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from src.dedupe import SeenJobsCache, link_hash
from src.fetch import FetchError, Fetcher
from src.filter import (
    filter_by_description,
    filter_by_education,
    filter_by_posted_date,
    filter_by_us_location,
    load_education_filter,
    load_keywords,
    load_us_location_filter,
    strip_descriptions,
)
from src.models import JobPosting
from src.notify import notify_failures, notify_new_postings
from src.parse import SiteConfig, parse_site
from src.sheet import JobSheet, SheetError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "state"
LOGS_DIR = ROOT / "logs"
SITES_PATH = CONFIG_DIR / "sites.yaml"
KEYWORDS_PATH = CONFIG_DIR / "keywords.yaml"
LOCATIONS_PATH = CONFIG_DIR / "locations.yaml"
EDUCATION_PATH = CONFIG_DIR / "education.yaml"
SEEN_PATH = STATE_DIR / "seen_jobs.json"
COMPANY_STATE_PATH = STATE_DIR / "company_runs.json"


def _setup_logging(today: date) -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"run-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("run")


def load_sites(path: Path = SITES_PATH) -> list[SiteConfig]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise ValueError(f"{path} must be a YAML list of site configs")
    return [SiteConfig(entry) for entry in raw]


def _load_company_state(path: Path = COMPANY_STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"companies": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"companies": {}}


def _save_company_state(state: dict[str, Any], path: Path = COMPANY_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _company_run_count(state: dict[str, Any], company: str) -> int:
    return int(state.get("companies", {}).get(company, {}).get("runs", 0))


def _bump_company_run(state: dict[str, Any], company: str) -> None:
    companies = state.setdefault("companies", {})
    entry = companies.setdefault(company, {"runs": 0})
    entry["runs"] = int(entry.get("runs", 0)) + 1


def _log_skipped(skipped: list[JobPosting], today: date) -> None:
    if not skipped:
        return
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"skipped-{today.isoformat()}.log"
    with path.open("a", encoding="utf-8") as fh:
        for p in skipped:
            fh.write(f"{p.company}\t{p.title}\t{p.link}\n")


def run(
    *,
    dry_run: bool = False,
    sites_path: Path = SITES_PATH,
    keywords_path: Path = KEYWORDS_PATH,
) -> int:
    """Execute one full scan. Returns process exit code (0 ok, 1 partial failures)."""
    load_dotenv(ROOT / ".env")
    today = date.today()
    log = _setup_logging(today)

    sites = load_sites(sites_path)
    keywords = load_keywords(keywords_path)
    us_locations = load_us_location_filter(LOCATIONS_PATH)
    education = load_education_filter(EDUCATION_PATH)
    company_state = _load_company_state()
    cache = SeenJobsCache(SEEN_PATH)

    sheet: JobSheet | None = None
    known_hashes: set[str] = set(cache.known_hashes())
    if not dry_run:
        try:
            sheet = JobSheet()
            known_hashes |= sheet.known_link_hashes()
        except SheetError as exc:
            log.error("Sheet unavailable: %s", exc)
            return 2

    open_by_source: dict[str, list[dict[str, Any]]] = {}
    if sheet is not None:
        open_by_source = sheet.open_rows_by_source()

    all_new: list[JobPosting] = []
    failures: list[str] = []
    rows_to_close: list[int] = []

    with Fetcher() as fetcher:
        for site in sites:
            run_count = _company_run_count(company_state, site.company)
            log_only_filter = run_count < site.first_seen_runs

            log.info(
                "Scanning %s (%s) [runs=%s, filter_log_only=%s]",
                site.company,
                site.ats,
                run_count,
                log_only_filter,
            )

            try:
                postings = parse_site(
                    site,
                    fetcher,
                    today=today,
                )
            except (FetchError, ValueError, Exception) as exc:  # noqa: BLE001
                msg = f"{site.company}: {exc}"
                log.error(msg)
                failures.append(msg)
                _bump_company_run(company_state, site.company)
                continue

            if site.expected_min and len(postings) < site.expected_min:
                    log.warning(
                        "%s returned %s postings (expected_min=%s) - possible selector/API break",
                        site.company,
                        len(postings),
                        site.expected_min,
                    )

            # Status tracking: mark missing open/applied links as closed.
            live_links = {p.link for p in postings}
            for entry in open_by_source.get(site.url, []):
                if entry["normalized_link"] not in live_links:
                    # Only move toward closed; applied stays applied until gone.
                    rows_to_close.append(int(entry["row_number"]))
                    log.info(
                        "Marking closed: %s (was %s)",
                        entry["link"],
                        entry["status"],
                    )

            us_kept, non_us = filter_by_us_location(postings, us_locations)
            if non_us:
                log.info(
                    "%s: dropped %s non-US location(s)",
                    site.company,
                    len(non_us),
                )
                _log_skipped(non_us, today)

            dated, too_old = filter_by_posted_date(us_kept, today=today)
            if too_old:
                log.info(
                    "%s: dropped %s posting(s) older than 7 days",
                    site.company,
                    len(too_old),
                )
                _log_skipped(too_old, today)

            undergrad, grad_only = filter_by_education(dated, education)
            if grad_only:
                log.info(
                    "%s: dropped %s post-undergrad posting(s)",
                    site.company,
                    len(grad_only),
                )
                _log_skipped(grad_only, today)

            kept, skipped = filter_by_description(
                undergrad,
                keywords,
                log_only=log_only_filter,
            )
            if log_only_filter and skipped:
                log.info(
                    "%s: keyword log-only buffer - %s would-be skips (still kept)",
                    site.company,
                    len(skipped),
                )
            _log_skipped(skipped if not log_only_filter else skipped, today)

            new_postings = [
                p for p in kept if link_hash(p.link) not in known_hashes
            ]
            # Avoid duplicates within this run across sites
            for p in new_postings:
                known_hashes.add(link_hash(p.link))

            strip_descriptions(new_postings)
            all_new.extend(new_postings)
            log.info(
                "%s: %s parsed, %s non-US, %s too old, %s grad-only, %s kept after keywords, %s new",
                site.company,
                len(postings),
                len(non_us),
                len(too_old),
                len(grad_only),
                len(kept),
                len(new_postings),
            )
            _bump_company_run(company_state, site.company)

    if dry_run:
        log.info("Dry run - %s new posting(s) would be added:", len(all_new))
        for p in all_new:
            log.info("  %s | %s | %s", p.company, p.title, p.link)
        if failures:
            notify_failures(failures)
            return 1
        return 0

    assert sheet is not None
    added = sheet.append_postings(all_new)
    closed = sheet.mark_closed(rows_to_close)
    cache.update([p.link for p in all_new])
    cache.save()
    _save_company_state(company_state)

    log.info("Done: added=%s closed=%s failures=%s", added, closed, len(failures))
    notify_new_postings(all_new)
    if failures:
        notify_failures(failures)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan career pages for new internships.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and filter without writing to Google Sheets.",
    )
    parser.add_argument(
        "--sites",
        type=Path,
        default=SITES_PATH,
        help="Path to sites.yaml",
    )
    parser.add_argument(
        "--keywords",
        type=Path,
        default=KEYWORDS_PATH,
        help="Path to keywords.yaml",
    )
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run, sites_path=args.sites, keywords_path=args.keywords)


if __name__ == "__main__":
    raise SystemExit(main())
