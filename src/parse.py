"""Parsers for ATS JSON APIs and generic HTML career pages."""

from __future__ import annotations

import logging
import json
import re
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from src.dedupe import normalize_link
from src.fetch import Fetcher
from src.filter import title_matches
from src.models import JobPosting

logger = logging.getLogger(__name__)

FIRST_RUN_LOOKBACK_DAYS = 3

GREENHOUSE_JOBS = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
GREENHOUSE_JOB = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
LEVER_POSTINGS = "https://api.lever.co/v0/postings/{board}?mode=json"
ASHBY_JOB_BOARD = "https://api.ashbyhq.com/posting-api/job-board/{board}"
WORKDAY_JOBS = "https://{host}/wday/cxs/{tenant}/{board}/jobs"
WORKDAY_PAGE_SIZE = 20
EIGHTFOLD_PAGE_SIZE = 10
ORACLE_PAGE_SIZE = 25
AMAZON_PAGE_SIZE = 20
PHENOM_PAGE_SIZE = 50
SMARTRECRUITERS_PAGE_SIZE = 100
TALENTBREW_PAGE_SIZE = 15
SMARTRECRUITERS_POSTINGS = (
    "https://api.smartrecruiters.com/v1/companies/{board}/postings"
)

DEFAULT_TITLE_KEYWORDS = ["intern", "internship", "co-op", "coop"]


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Greenhouse uses ms timestamps; Lever uses ms too.
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    if isinstance(value, str):
        text = value.strip()
        for fmt in (
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%B %d, %Y",  # Amazon.jobs: "July 29, 2026"
            "%b %d, %Y",
        ):
            try:
                return datetime.strptime(text.replace("+00:00", "Z"), fmt).date()
            except ValueError:
                continue
        # ISO-ish fallback
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _parse_workday_posted_on(value: str | None, *, today: date) -> date | None:
    """Parse Workday list strings like 'Posted Today' / 'Posted 3 Days Ago'."""
    if not value:
        return None
    text = value.strip()
    if re.search(r"Posted\s+Today", text, re.I):
        return today
    if re.search(r"Posted\s+Yesterday", text, re.I):
        return today - timedelta(days=1)
    if re.search(r"Posted\s+30\+\s+Days\s+Ago", text, re.I):
        return today - timedelta(days=30)
    m = re.search(r"Posted\s+(\d+)\s+Days?\s+Ago", text, re.I)
    if m:
        return today - timedelta(days=int(m.group(1)))
    return _parse_date(text)


def _within_lookback(posted: date | None, *, today: date, lookback_days: int) -> bool:
    """First-run lookback: keep if dated within window, or undated (treat as today)."""
    if posted is None:
        return True
    return posted >= today - timedelta(days=lookback_days)


def _soup(html: str) -> BeautifulSoup:
    """Prefer lxml when installed; fall back to stdlib parser."""
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html, "html.parser")


def _clean_html(html: str) -> str:
    if not html:
        return ""
    return _soup(html).get_text(" ", strip=True)


class SiteConfig:
    """Typed view of one entry from config/sites.yaml."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.company: str = str(raw["company"])
        self.url: str = str(raw["url"])
        self.ats: str = str(raw.get("ats", "html")).lower()
        self.board: str | None = raw.get("board")
        self.selector: str | None = raw.get("selector")
        self.title_keywords: list[str] = list(
            raw.get("title_keywords") or raw.get("keywords") or DEFAULT_TITLE_KEYWORDS
        )
        self.expected_min: int = int(raw.get("expected_min", 0))
        self.first_seen_runs: int = int(raw.get("first_seen_runs", 0))
        self.is_new_company: bool = bool(raw.get("is_new_company", False))
        # Workday
        self.workday_host: str | None = raw.get("workday_host") or raw.get("host")
        self.workday_tenant: str | None = raw.get("workday_tenant") or raw.get("tenant")
        facets = raw.get("applied_facets") or raw.get("facets") or {}
        self.applied_facets: dict[str, list[str]] = {
            str(k): [str(x) for x in (v if isinstance(v, list) else [v])]
            for k, v in dict(facets).items()
        }
        # Eightfold
        self.domain: str | None = raw.get("domain")
        self.eightfold_api: str = str(raw.get("eightfold_api", "v2")).lower()
        self.query: str = str(raw.get("query") or "")
        self.extra_params: dict[str, str] = {
            str(k): str(v) for k, v in dict(raw.get("extra_params") or {}).items()
        }
        # Oracle CE
        self.oracle_host: str | None = raw.get("oracle_host")
        # Phenom /api/jobs (AMD etc.)
        self.api_base: str | None = raw.get("api_base")
        # Apple search filters (locations, teams, etc.)
        apple_filters = raw.get("apple_filters") or raw.get("filters") or {}
        self.apple_filters: dict[str, list[str]] = {
            str(k): [str(x) for x in (v if isinstance(v, list) else [v])]
            for k, v in dict(apple_filters).items()
        }
        # Tesla
        self.tesla_site: str = str(raw.get("tesla_site") or "US")
        self.tesla_type: str = str(raw.get("tesla_type") or "intern")


def parse_site(
    site: SiteConfig,
    fetcher: Fetcher,
    *,
    today: date | None = None,
    apply_lookback: bool = False,
) -> list[JobPosting]:
    """Fetch and parse all postings for a configured site."""
    today = today or date.today()
    if site.ats == "greenhouse":
        postings = _parse_greenhouse(site, fetcher)
    elif site.ats == "lever":
        postings = _parse_lever(site, fetcher)
    elif site.ats == "ashby":
        postings = _parse_ashby(site, fetcher)
    elif site.ats == "workday":
        postings = _parse_workday(site, fetcher, today=today)
    elif site.ats == "eightfold":
        postings = _parse_eightfold(site, fetcher)
    elif site.ats == "smartrecruiters":
        postings = _parse_smartrecruiters(site, fetcher)
    elif site.ats in {"talentbrew", "smashfly"}:
        postings = _parse_talentbrew(site, fetcher)
    elif site.ats == "oracle":
        postings = _parse_oracle(site, fetcher)
    elif site.ats == "amazon":
        postings = _parse_amazon(site, fetcher)
    elif site.ats in {"phenom", "amd"}:
        postings = _parse_phenom_jobs(site, fetcher)
    elif site.ats == "apple":
        postings = _parse_apple(site, fetcher)
    elif site.ats == "google":
        postings = _parse_google(site, fetcher)
    elif site.ats == "tesla":
        postings = _parse_tesla(site, fetcher)
    elif site.ats == "arm":
        postings = _parse_arm(site, fetcher)
    elif site.ats == "html":
        postings = _parse_html(site, fetcher)
    else:
        raise ValueError(f"Unknown ats type '{site.ats}' for {site.company}")

    # Title noise filter (intern/internship etc.)
    postings = [p for p in postings if title_matches(p.title, site.title_keywords)]

    if apply_lookback or site.is_new_company:
        postings = [
            p
            for p in postings
            if _within_lookback(p.date_posted, today=today, lookback_days=FIRST_RUN_LOOKBACK_DAYS)
        ]

    for posting in postings:
        posting.link = normalize_link(posting.link)
        if posting.date_found is None:
            posting.date_found = today

    return postings


def _parse_greenhouse(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    if not site.board:
        raise ValueError(f"{site.company}: greenhouse requires 'board'")
    # Slim list (no content=true): one small JSON payload, then detail-fetch only
    # intern-titled jobs. content=true can be tens of MB and time out on large boards.
    list_url = GREENHOUSE_JOBS.format(board=site.board)
    payload = fetcher.get_json(list_url)
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    postings: list[JobPosting] = []

    for job in jobs:
        job_id = job.get("id")
        title = str(job.get("title") or "").strip()
        absolute_url = str(job.get("absolute_url") or "").strip()
        if not title or not absolute_url:
            continue
        if not title_matches(title, site.title_keywords):
            continue

        location = ""
        loc = job.get("location")
        if isinstance(loc, dict):
            location = str(loc.get("name") or "")
        elif isinstance(loc, str):
            location = loc

        description = _clean_html(str(job.get("content") or ""))
        if not description and job_id is not None:
            detail_url = GREENHOUSE_JOB.format(board=site.board, job_id=job_id)
            try:
                detail = fetcher.get_json(detail_url)
                description = _clean_html(str(detail.get("content") or ""))
            except Exception as exc:  # noqa: BLE001 — continue without description
                logger.warning(
                    "%s: failed to fetch Greenhouse job %s: %s",
                    site.company,
                    job_id,
                    exc,
                )

        posted = _parse_date(job.get("updated_at") or job.get("created_at"))
        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=absolute_url,
                location=location,
                description=description,
                date_posted=posted,
                source_page=site.url,
            )
        )
    return postings


def _parse_lever(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    if not site.board:
        raise ValueError(f"{site.company}: lever requires 'board'")
    list_url = LEVER_POSTINGS.format(board=site.board)
    payload = fetcher.get_json(list_url)
    jobs = payload if isinstance(payload, list) else []
    postings: list[JobPosting] = []

    for job in jobs:
        title = str(job.get("text") or "").strip()
        hosted = str(job.get("hostedUrl") or job.get("applyUrl") or "").strip()
        if not title or not hosted:
            continue

        location = ""
        categories = job.get("categories") or {}
        if isinstance(categories, dict):
            location = str(categories.get("location") or "")

        description_parts: list[str] = []
        for key in ("descriptionPlain", "description", "additionalPlain", "additional"):
            val = job.get(key)
            if val:
                description_parts.append(_clean_html(str(val)) if "<" in str(val) else str(val))
        lists = job.get("lists") or []
        if isinstance(lists, list):
            for block in lists:
                if isinstance(block, dict):
                    description_parts.append(str(block.get("text") or ""))
                    description_parts.append(_clean_html(str(block.get("content") or "")))

        posted = _parse_date(job.get("createdAt"))
        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=hosted,
                location=location,
                description=" ".join(p for p in description_parts if p),
                date_posted=posted,
                source_page=site.url,
            )
        )
    return postings


def _parse_ashby(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    if not site.board:
        raise ValueError(f"{site.company}: ashby requires 'board'")
    list_url = ASHBY_JOB_BOARD.format(board=site.board)
    payload = fetcher.get_json(list_url)
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    postings: list[JobPosting] = []

    for job in jobs:
        title = str(job.get("title") or "").strip()
        job_url = str(job.get("jobUrl") or "").strip()
        if not title or not job_url:
            continue
        location = str(job.get("location") or "")
        description = _clean_html(str(job.get("descriptionHtml") or job.get("descriptionPlain") or ""))
        posted = _parse_date(job.get("publishedAt") or job.get("updatedAt"))
        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=job_url,
                location=location,
                description=description,
                date_posted=posted,
                source_page=site.url,
            )
        )
    return postings


def _parse_workday(site: SiteConfig, fetcher: Fetcher, *, today: date) -> list[JobPosting]:
    """Parse Workday CXS job board (POST /wday/cxs/{tenant}/{board}/jobs)."""
    if not site.board or not site.workday_host or not site.workday_tenant:
        raise ValueError(
            f"{site.company}: workday requires 'board', 'workday_host', and 'workday_tenant'"
        )

    list_url = WORKDAY_JOBS.format(
        host=site.workday_host,
        tenant=site.workday_tenant,
        board=site.board,
    )

    offset = 0
    listings: list[dict[str, Any]] = []
    total: int | None = None
    while True:
        payload: dict[str, Any] = {
            "appliedFacets": site.applied_facets,
            "limit": WORKDAY_PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        data = fetcher.post_json(list_url, payload)
        if not isinstance(data, dict):
            break
        if total is None:
            total = int(data.get("total") or 0)
        batch = data.get("jobPostings") or []
        if not isinstance(batch, list) or not batch:
            break
        listings.extend(batch)
        offset += len(batch)
        if total is not None and offset >= total:
            break
        if len(batch) < WORKDAY_PAGE_SIZE:
            break

    postings: list[JobPosting] = []
    for job in listings:
        title = str(job.get("title") or "").strip()
        path = str(job.get("externalPath") or "").strip()
        if not title or not path:
            continue
        # Title filter before fetching descriptions (Workday boards are large).
        if not title_matches(title, site.title_keywords):
            continue

        location = str(job.get("locationsText") or "")
        posted = _parse_workday_posted_on(str(job.get("postedOn") or ""), today=today)
        # Public job URLs follow the board's careers path (may include /recruiting/{tenant}/...).
        board_base = site.url.split("?", 1)[0].rstrip("/")
        link = normalize_link(f"{board_base}{path}")

        description = ""
        detail_url = (
            f"https://{site.workday_host}/wday/cxs/{site.workday_tenant}/{site.board}{path}"
        )
        try:
            detail = fetcher.get_json(detail_url)
            info = detail.get("jobPostingInfo", {}) if isinstance(detail, dict) else {}
            description = _clean_html(str(info.get("jobDescription") or ""))
            if info.get("externalUrl"):
                link = normalize_link(str(info["externalUrl"]))
            posted = _parse_date(info.get("postedDate") or info.get("startDate")) or posted
        except Exception as exc:  # noqa: BLE001 — keep listing without description
            logger.warning("%s: Workday detail failed for %s: %s", site.company, path, exc)

        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=link,
                location=location,
                description=description,
                date_posted=posted,
                source_page=site.url,
            )
        )
    return postings


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _smartrecruiters_location(loc: Any) -> str:
    if isinstance(loc, dict):
        full = str(loc.get("fullLocation") or "").strip()
        if full:
            return full
        parts = [str(loc.get(k) or "").strip() for k in ("city", "region", "country")]
        return ", ".join(p for p in parts if p)
    if isinstance(loc, str):
        return loc
    return ""


def _smartrecruiters_description(detail: dict[str, Any]) -> str:
    ad = detail.get("jobAd") if isinstance(detail.get("jobAd"), dict) else {}
    sections = ad.get("sections") if isinstance(ad, dict) else {}
    if not isinstance(sections, dict):
        return ""
    parts: list[str] = []
    for key in (
        "jobDescription",
        "qualifications",
        "additionalInformation",
        "companyDescription",
    ):
        block = sections.get(key)
        if isinstance(block, dict) and block.get("text"):
            parts.append(str(block["text"]))
        elif isinstance(block, str) and block.strip():
            parts.append(block)
    return _clean_html(" ".join(parts))


def _parse_smartrecruiters(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """SmartRecruiters public postings API."""
    board = site.board
    if not board:
        raise ValueError(f"{site.company}: smartrecruiters requires 'board' (company identifier)")

    offset = 0
    listings: list[dict[str, Any]] = []
    total: int | None = None
    query = site.query if site.query else "intern"
    while True:
        params: dict[str, str] = {
            "limit": str(SMARTRECRUITERS_PAGE_SIZE),
            "offset": str(offset),
            "q": query,
        }
        params.update(site.extra_params)
        list_url = f"{SMARTRECRUITERS_POSTINGS.format(board=board)}?{urlencode(params)}"
        data = fetcher.get_json(list_url)
        if not isinstance(data, dict):
            break
        if total is None:
            total = int(data.get("totalFound") or 0)
        batch = list(data.get("content") or [])
        if not batch:
            break
        listings.extend(batch)
        offset += len(batch)
        if total and offset >= total:
            break
        if len(batch) < SMARTRECRUITERS_PAGE_SIZE:
            break

    postings: list[JobPosting] = []
    for job in listings:
        title = str(job.get("name") or "").strip()
        job_id = job.get("id")
        if not title or job_id is None:
            continue
        if not title_matches(title, site.title_keywords):
            continue

        link = f"https://jobs.smartrecruiters.com/{board}/{job_id}"
        location = _smartrecruiters_location(job.get("location"))
        posted = _parse_date(job.get("releasedDate"))
        description = ""
        try:
            detail = fetcher.get_json(
                SMARTRECRUITERS_POSTINGS.format(board=board) + f"/{job_id}"
            )
            if isinstance(detail, dict):
                description = _smartrecruiters_description(detail)
                if detail.get("postingUrl"):
                    link = str(detail["postingUrl"])
                posted = _parse_date(detail.get("releasedDate")) or posted
                if not location:
                    location = _smartrecruiters_location(detail.get("location"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: SmartRecruiters detail failed for %s: %s", site.company, job_id, exc)

        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=link,
                location=location,
                description=description,
                date_posted=posted,
                source_page=site.url,
            )
        )
    return postings


def _talentbrew_cards(html: str, origin: str) -> list[dict[str, str]]:
    """Extract job cards from TalentBrew/Smashfly search-jobs/results HTML."""
    cards: list[dict[str, str]] = []
    seen: set[str] = set()
    # Synopsys-style: <a class="sr-job-link" href="..."><h2>Title
    for href, raw_title in re.findall(
        r'<a class="sr-job-link" href="([^"]+)"[^>]*>\s*<h2>([^<]+)',
        html,
        re.I,
    ):
        title = unescape(raw_title).strip()
        link = urljoin(origin + "/", href)
        if title and link not in seen:
            seen.add(link)
            cards.append({"title": title, "link": link, "location": ""})
    # Arm-style: <a class="job-card__title" href="...">Title</a> ... <span class="location">
    for block in re.findall(r'<li class="job-card[\s\S]*?</li>', html, re.I):
        m = re.search(
            r'<a class="job-card__title[^"]*" href="([^"]+)"[^>]*>([^<]+)</a>',
            block,
            re.I,
        )
        if not m:
            continue
        href, raw_title = m.group(1), m.group(2)
        title = unescape(raw_title).strip()
        link = urljoin(origin + "/", href)
        loc_m = re.search(r'<span class="location">([^<]+)</span>', block, re.I)
        location = unescape(loc_m.group(1)).strip() if loc_m else ""
        if title and link not in seen:
            seen.add(link)
            cards.append({"title": title, "link": link, "location": location})
    return cards


def _talentbrew_description(html: str) -> tuple[str, str]:
    """Return (description, location) from a TalentBrew job page."""
    desc_match = re.search(
        r'class="[^"]*ats-description[^"]*"[^>]*>([\s\S]*?)</div>',
        html,
        re.I,
    )
    if not desc_match:
        desc_match = re.search(
            r'class="[^"]*job-description[^"]*"[^>]*>([\s\S]*?)</div>',
            html,
            re.I,
        )
    description = _clean_html(desc_match.group(1) if desc_match else "")
    loc_match = re.search(
        r'class="[^"]*job-location[^"]*"[^>]*>([\s\S]*?)</(?:div|span|p)>',
        html,
        re.I,
    )
    if not loc_match:
        loc_match = re.search(r'<span class="location">([^<]+)</span>', html, re.I)
    location = _clean_html(loc_match.group(1)) if loc_match else ""
    return description, location


def _parse_talentbrew(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """TalentBrew/Smashfly search-jobs/results JSON (Synopsys, Arm, etc.)."""
    origin = _origin(site.url)
    page = 1
    total_pages = 1
    cards: list[dict[str, str]] = []
    while page <= total_pages:
        params: dict[str, str] = {
            "ActiveFacetID": "0",
            "CurrentPage": str(page),
            "RecordsPerPage": str(TALENTBREW_PAGE_SIZE),
            "Distance": "50",
            "RadiusUnitType": "0",
            "Keywords": site.query,
            "Location": "",
            "ShowRadius": "False",
            "CustomFacetName": "",
            "FacetTerm": "",
            "FacetType": "0",
            "SearchResultsModuleName": "Search Results",
            "SearchFiltersModuleName": "Search Filters",
            "SortCriteria": "0",
            "SortDirection": "0",
            "SearchType": "5",
        }
        params.update(site.extra_params)
        list_url = f"{origin}/search-jobs/results?{urlencode(params)}"
        data = fetcher.get_json(list_url)
        if not isinstance(data, dict):
            break
        html = str(data.get("results") or "")
        if page == 1:
            m = re.search(r'data-total-pages="(\d+)"', html)
            total_pages = max(int(m.group(1)), 1) if m else 1
            total_pages = min(total_pages, 40)
        batch = _talentbrew_cards(html, origin)
        if not batch:
            break
        cards.extend(batch)
        page += 1

    postings: list[JobPosting] = []
    for card in cards:
        title = card["title"]
        if not title_matches(title, site.title_keywords):
            continue
        link = card["link"]
        location = card.get("location") or ""
        description = ""
        try:
            html = fetcher.get_text(link)
            description, loc = _talentbrew_description(html)
            if loc:
                location = loc
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: TalentBrew detail failed for %s: %s", site.company, link, exc)
        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=link,
                location=location,
                description=description,
                source_page=site.url,
            )
        )
    return postings


def _parse_eightfold(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """Eightfold PCS / PCSX public job search APIs."""
    domain = site.domain or site.board
    if not domain:
        raise ValueError(f"{site.company}: eightfold requires 'domain'")
    base = _origin(site.url)
    api = site.eightfold_api
    if api not in {"v2", "pcsx"}:
        raise ValueError(f"{site.company}: eightfold_api must be 'v2' or 'pcsx'")

    start = 0
    listings: list[dict[str, Any]] = []
    total: int | None = None
    while True:
        params: dict[str, str] = {
            "domain": domain,
            "query": site.query,
            "start": str(start),
            "num": str(EIGHTFOLD_PAGE_SIZE),
        }
        params.update(site.extra_params)
        if api == "pcsx":
            list_url = f"{base}/api/pcsx/search?{urlencode(params)}"
        else:
            list_url = f"{base}/api/apply/v2/jobs?{urlencode(params)}"
        data = fetcher.get_json(list_url)
        if not isinstance(data, dict):
            break
        if api == "pcsx":
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            batch = list((payload or {}).get("positions") or [])
            if total is None:
                total = int((payload or {}).get("count") or (payload or {}).get("totalCount") or 0)
        else:
            batch = list(data.get("positions") or [])
            if total is None:
                total = int(data.get("count") or 0)
        if not batch:
            break
        listings.extend(batch)
        start += len(batch)
        if total and start >= total:
            break
        if len(batch) < EIGHTFOLD_PAGE_SIZE:
            break

    postings: list[JobPosting] = []
    for job in listings:
        title = str(job.get("name") or job.get("posting_name") or "").strip()
        job_id = job.get("id")
        if not title or job_id is None:
            continue
        if not title_matches(title, site.title_keywords):
            continue

        locations = job.get("locations") or []
        location = ""
        if isinstance(locations, list) and locations:
            location = str(locations[0])
        elif job.get("location"):
            location = str(job.get("location"))

        link = str(job.get("canonicalPositionUrl") or job.get("positionUrl") or "").strip()
        if link.startswith("/"):
            link = urljoin(base + "/", link.lstrip("/"))
        if not link:
            link = f"{base}/careers/job/{job_id}"

        posted = _parse_date(job.get("postedTs") or job.get("t_create") or job.get("creationTs"))
        description = _clean_html(str(job.get("job_description") or ""))
        if not description:
            detail_url = f"{base}/api/apply/v2/jobs/{job_id}?{urlencode({'domain': domain})}"
            try:
                detail = fetcher.get_json(detail_url)
                if isinstance(detail, dict):
                    description = _clean_html(str(detail.get("job_description") or ""))
                    if detail.get("canonicalPositionUrl"):
                        link = str(detail["canonicalPositionUrl"])
                    posted = _parse_date(detail.get("t_create") or detail.get("t_update")) or posted
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s: Eightfold detail failed for %s: %s", site.company, job_id, exc)

        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=link,
                location=location,
                description=description,
                date_posted=posted,
                source_page=site.url,
            )
        )
    return postings


def _parse_oracle(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """Oracle Cloud HCM Candidate Experience recruitingCEJobRequisitions API."""
    host = site.oracle_host
    site_number = site.board
    if not host or not site_number:
        raise ValueError(f"{site.company}: oracle requires 'oracle_host' and 'board' (siteNumber)")

    offset = 0
    listings: list[dict[str, Any]] = []
    total: int | None = None
    keyword = site.query
    while True:
        finder = (
            f"findReqs;siteNumber={site_number},limit={ORACLE_PAGE_SIZE},"
            f"offset={offset},keyword={keyword}"
        )
        list_url = (
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList"
            f"&finder={finder}"
        )
        data = fetcher.get_json(list_url)
        items = data.get("items") if isinstance(data, dict) else None
        if not items:
            break
        head = items[0] if isinstance(items[0], dict) else {}
        if total is None:
            total = int(head.get("TotalJobsCount") or 0)
        batch = list(head.get("requisitionList") or [])
        if not batch:
            break
        listings.extend(batch)
        offset += len(batch)
        if total and offset >= total:
            break
        if len(batch) < ORACLE_PAGE_SIZE:
            break

    board_base = site.url.split("?", 1)[0].rstrip("/")
    postings: list[JobPosting] = []
    for job in listings:
        title = str(job.get("Title") or "").strip()
        job_id = job.get("Id")
        if not title or job_id is None:
            continue
        if not title_matches(title, site.title_keywords):
            continue

        location = str(job.get("PrimaryLocation") or job.get("PrimaryLocationCountry") or "")
        posted = _parse_date(job.get("PostedDate"))
        link = f"{board_base}/job/{job_id}"

        description = ""
        detail_url = (
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f"?finder=ById;Id={job_id},siteNumber={site_number}"
        )
        try:
            detail_payload = fetcher.get_json(detail_url)
            detail_items = detail_payload.get("items") if isinstance(detail_payload, dict) else None
            detail = detail_items[0] if detail_items else detail_payload
            if isinstance(detail, dict):
                parts = [
                    str(detail.get("ExternalDescriptionStr") or ""),
                    str(detail.get("ExternalResponsibilitiesStr") or ""),
                    str(detail.get("ExternalQualificationsStr") or ""),
                    str(detail.get("ShortDescriptionStr") or ""),
                ]
                description = _clean_html(" ".join(p for p in parts if p))
                if detail.get("PrimaryLocation"):
                    location = str(detail["PrimaryLocation"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: Oracle detail failed for %s: %s", site.company, job_id, exc)

        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=link,
                location=location,
                description=description,
                date_posted=posted,
                source_page=site.url,
            )
        )
    return postings


def _parse_amazon(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """Amazon.jobs public search.json endpoint."""
    query = site.query or "internship"
    offset = 0
    listings: list[dict[str, Any]] = []
    total: int | None = None
    while True:
        params = {
            "base_query": query,
            "result_limit": str(AMAZON_PAGE_SIZE),
            "offset": str(offset),
        }
        params.update(site.extra_params)
        list_url = f"https://www.amazon.jobs/en/search.json?{urlencode(params)}"
        data = fetcher.get_json(list_url)
        if not isinstance(data, dict):
            break
        if total is None:
            total = int(data.get("hits") or 0)
        batch = list(data.get("jobs") or [])
        if not batch:
            break
        listings.extend(batch)
        offset += len(batch)
        if total and offset >= total:
            break
        if len(batch) < AMAZON_PAGE_SIZE:
            break
        # Hard cap to avoid huge Amazon result sets on first run.
        if offset >= 200:
            logger.warning("%s: Amazon result cap reached (%s)", site.company, offset)
            break

    postings: list[JobPosting] = []
    for job in listings:
        title = str(job.get("title") or "").strip()
        job_path = str(job.get("job_path") or job.get("id_to_use") or "").strip()
        if not title:
            continue
        if not title_matches(title, site.title_keywords):
            continue
        if job_path.startswith("/"):
            link = f"https://www.amazon.jobs{job_path}"
        elif job_path.startswith("http"):
            link = job_path
        else:
            job_id = job.get("id_to_use") or job.get("id")
            link = f"https://www.amazon.jobs/jobs/{job_id}" if job_id else ""
        if not link:
            continue

        location_parts = [
            str(job.get("city") or ""),
            str(job.get("state") or ""),
            str(job.get("country_code") or ""),
        ]
        location = ", ".join(p for p in location_parts if p)
        description = _clean_html(
            " ".join(
                str(job.get(k) or "")
                for k in ("description", "basic_qualifications", "preferred_qualifications")
            )
        )
        posted = _parse_date(job.get("posted_date"))
        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=link,
                location=location,
                description=description,
                date_posted=posted,
                source_page=site.url,
            )
        )
    return postings


def _parse_phenom_jobs(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """Phenom-style /api/jobs JSON boards (e.g. AMD careers.amd.com/api/jobs)."""
    api_base = (site.api_base or _origin(site.url)).rstrip("/")
    page = 1
    listings: list[dict[str, Any]] = []
    total: int | None = None
    while True:
        params: dict[str, str] = {
            "limit": str(PHENOM_PAGE_SIZE),
            "page": str(page),
            "sortBy": "posted_date",
            "descending": "true",
        }
        if site.query:
            params["query"] = site.query
        params.update(site.extra_params)
        list_url = f"{api_base}/api/jobs?{urlencode(params)}"
        data = fetcher.get_json(list_url)
        if not isinstance(data, dict):
            break
        if total is None:
            total = int(data.get("totalCount") or 0)
        batch = list(data.get("jobs") or [])
        if not batch:
            break
        listings.extend(batch)
        fetched = page * PHENOM_PAGE_SIZE
        page += 1
        if total and fetched >= total:
            break
        if len(batch) < PHENOM_PAGE_SIZE:
            break

    postings: list[JobPosting] = []
    for row in listings:
        job = row.get("data") if isinstance(row, dict) else None
        if not isinstance(job, dict):
            continue
        title = str(job.get("title") or "").strip()
        slug = str(job.get("slug") or job.get("req_id") or "").strip()
        if not title or not slug:
            continue
        if not title_matches(title, site.title_keywords):
            continue
        link = f"{api_base}/jobs/{slug}"
        location = ""
        locs = job.get("locations") or job.get("location")
        if isinstance(locs, list) and locs:
            first = locs[0]
            if isinstance(first, dict):
                location = str(first.get("name") or first.get("city") or "")
            else:
                location = str(first)
        elif isinstance(locs, str):
            location = locs
        description = _clean_html(str(job.get("description") or ""))
        posted = _parse_date(job.get("posted_date") or job.get("create_date"))
        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=link,
                location=location,
                description=description,
                date_posted=posted,
                source_page=site.url,
            )
        )
    return postings


def _extract_apple_hydration(html: str) -> dict[str, Any]:
    match = re.search(
        r"__staticRouterHydrationData\s*=\s*JSON\.parse\(\"((?:\\.|[^\"\\])*)\"\)",
        html,
    )
    if not match:
        raise ValueError("Apple page missing __staticRouterHydrationData")
    return json.loads(json.loads('"' + match.group(1) + '"'))


def _parse_apple(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """Apple jobs.apple.com via SSR hydration JSON (no public list API)."""
    page = 1
    listings: list[dict[str, Any]] = []
    total: int | None = None
    base = site.url.split("?", 1)[0]
    if "/search" not in base:
        base = "https://jobs.apple.com/en-us/search"
    existing_q = ""
    if "?" in site.url:
        existing_q = re.sub(r"(?:^|&)page=\d+", "", site.url.split("?", 1)[1]).strip("&")

    while True:
        parts = [p for p in (existing_q, f"page={page}") if p]
        list_url = f"{base}?{'&'.join(parts)}"
        html = fetcher.get_text(list_url)
        data = _extract_apple_hydration(html)
        search = (data.get("loaderData") or {}).get("search") or {}
        if total is None:
            total = int(search.get("totalRecords") or 0)
        batch = list(search.get("searchResults") or [])
        if not batch:
            break
        listings.extend(batch)
        page += 1
        if total and len(listings) >= total:
            break
        if len(batch) < 20:
            break
        if page > 50:
            logger.warning("%s: Apple page cap reached", site.company)
            break

    postings: list[JobPosting] = []
    for job in listings:
        title = str(job.get("postingTitle") or "").strip()
        position_id = str(job.get("positionId") or "").strip()
        if not title or not position_id:
            continue
        if not title_matches(title, site.title_keywords):
            continue

        locs = job.get("locations") or []
        location = ""
        if isinstance(locs, list) and locs:
            first = locs[0]
            if isinstance(first, dict):
                location = str(first.get("name") or first.get("city") or first.get("countryName") or "")

        slug = str(job.get("transformedPostingTitle") or title).lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
        job_id = str(job.get("id") or position_id)
        link = (
            f"https://jobs.apple.com/en-us/details/{job_id}/{slug}"
            if slug
            else f"https://jobs.apple.com/en-us/details/{position_id}"
        )

        description = str(job.get("jobSummary") or "")
        if len(description) < 200:
            try:
                detail_html = fetcher.get_text(f"https://jobs.apple.com/en-us/details/{position_id}")
                detail = _extract_apple_hydration(detail_html)
                jobs_data = ((detail.get("loaderData") or {}).get("jobDetails") or {}).get("jobsData") or {}
                description = str(
                    jobs_data.get("description")
                    or jobs_data.get("jobSummary")
                    or description
                )
                if jobs_data.get("postingTitle"):
                    title = str(jobs_data["postingTitle"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s: Apple detail failed for %s: %s", site.company, position_id, exc)

        posted = _parse_date(job.get("postDateInGMT") or job.get("postingDate"))
        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=link,
                location=location,
                description=_clean_html(description),
                date_posted=posted,
                source_page=site.url,
            )
        )
    return postings


def _extract_google_ds1(html: str) -> Any:
    match = re.search(r"AF_initDataCallback\(\{key:\s*'ds:1'.*?data:", html)
    if not match:
        raise ValueError("Google page missing AF_initDataCallback ds:1")
    start = match.end()
    i = start
    while i < len(html) and html[i] in " \n\r\t":
        i += 1
    if i >= len(html) or html[i] != "[":
        raise ValueError("Google ds:1 data is not a JSON array")
    depth = 0
    j = i
    while j < len(html):
        ch = html[j]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                j += 1
                break
        elif ch == '"':
            j += 1
            while j < len(html):
                if html[j] == "\\":
                    j += 2
                    continue
                if html[j] == '"':
                    j += 1
                    break
                j += 1
            continue
        j += 1
    return json.loads(html[i:j])


def _parse_google(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """Google Careers via AF_initDataCallback embedded JSON on results pages."""
    page = 1
    listings: list[list[Any]] = []
    total: int | None = None
    while True:
        if page == 1 and "jobs/results" in site.url:
            list_url = site.url if "page=" in site.url else (
                site.url + ("&" if "?" in site.url else "?") + "page=1"
            )
        else:
            params = {
                "employment_type": "INTERN",
                "hl": "en_US",
                "location": "United States",
                "q": site.query,
                "page": str(page),
            }
            params.update(site.extra_params)
            list_url = (
                "https://www.google.com/about/careers/applications/jobs/results/?"
                + urlencode(params)
            )
        html = fetcher.get_text(list_url)
        try:
            data = _extract_google_ds1(html)
        except ValueError as exc:
            logger.warning("%s: %s", site.company, exc)
            break
        jobs_blob = data[0] if isinstance(data, list) and data else []
        if not isinstance(jobs_blob, list):
            break
        # jobs_blob is either list of jobs, or nested
        batch: list[Any] = []
        if jobs_blob and isinstance(jobs_blob[0], list) and jobs_blob[0] and isinstance(jobs_blob[0][0], str):
            batch = jobs_blob
        elif isinstance(data, list) and len(data) >= 1 and isinstance(data[0], list):
            batch = [x for x in data[0] if isinstance(x, list)]
        if total is None and isinstance(data, list) and len(data) >= 3 and isinstance(data[2], int):
            total = int(data[2])
        if not batch:
            break
        listings.extend(batch)
        page += 1
        if total is not None and len(listings) >= total:
            break
        if len(batch) < 10:
            break
        if page > 30:
            break

    postings: list[JobPosting] = []
    for job in listings:
        if not isinstance(job, list) or len(job) < 2:
            continue
        job_id = str(job[0])
        title = str(job[1] or "").strip()
        if not title:
            continue
        if not title_matches(title, site.title_keywords):
            # Google employment_type=INTERN pages already filtered; keep student roles.
            if not re.search(r"student|research|intern|phd|bs/ms|apprentice", title, re.I):
                continue

        link = f"https://www.google.com/about/careers/applications/jobs/results/{job_id}"
        description_parts: list[str] = []
        for idx in (3, 4):
            if len(job) > idx and isinstance(job[idx], list) and len(job[idx]) > 1:
                description_parts.append(str(job[idx][1] or ""))
        description = _clean_html(" ".join(description_parts))
        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=link,
                location="",
                description=description,
                source_page=site.url,
            )
        )
    return postings


def _parse_tesla(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """Tesla careers JSON API (cua-api). May 403 from some datacenter IPs."""
    offset = 0
    limit = 100
    listing_ids: list[Any] = []
    while True:
        params = {
            "query": site.query,
            "site": site.tesla_site,
            "type": site.tesla_type,
            "offset": str(offset),
            "limit": str(limit),
            "sort": "desc",
            "sort_by": "relevant",
        }
        params.update(site.extra_params)
        url = f"https://www.tesla.com/cua-api/apps/careers/search?{urlencode(params)}"
        payload = fetcher.get_json(url)
        batch: list[Any]
        if isinstance(payload, list):
            batch = payload
        elif isinstance(payload, dict):
            batch = list(payload.get("listings") or payload.get("results") or payload.get("ids") or [])
        else:
            batch = []
        if not batch:
            break
        listing_ids.extend(batch)
        offset += len(batch)
        if len(batch) < limit:
            break
        if offset >= 500:
            break

    # Prefer bulk state map when available.
    state_listings: dict[str, Any] = {}
    try:
        state = fetcher.get_json("https://www.tesla.com/cua-api/apps/careers/state")
        if isinstance(state, dict):
            raw = state.get("listings") or state.get("lookup") or {}
            if isinstance(raw, dict):
                state_listings = raw
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: Tesla state endpoint unavailable: %s", site.company, exc)

    postings: list[JobPosting] = []
    for raw_id in listing_ids:
        job_id = str(raw_id.get("id") if isinstance(raw_id, dict) else raw_id)
        job: dict[str, Any]
        if job_id in state_listings and isinstance(state_listings[job_id], dict):
            job = state_listings[job_id]
        elif isinstance(raw_id, dict):
            job = raw_id
        else:
            try:
                detail = fetcher.get_json(f"https://www.tesla.com/cua-api/apps/careers/listings/{job_id}")
                job = detail if isinstance(detail, dict) else {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s: Tesla listing %s failed: %s", site.company, job_id, exc)
                continue

        title = str(job.get("t") or job.get("title") or job.get("jobTitle") or "").strip()
        if not title:
            continue
        if not title_matches(title, site.title_keywords):
            continue
        location = str(job.get("l") or job.get("location") or job.get("jobLocation") or "")
        description = _clean_html(
            str(job.get("description") or job.get("d") or job.get("jobDescription") or "")
        )
        link = f"https://www.tesla.com/careers/search/job/{job_id}"
        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=link,
                location=location,
                description=description,
                source_page=site.url,
            )
        )
    return postings


def _parse_arm(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """Arm careers via sitemap + job pages (AJAX search is bot-blocked)."""
    sitemap = fetcher.get_text("https://careers.arm.com/sitemap.xml").lstrip("\ufeff")
    urls = re.findall(r"<loc>(https://careers\.arm\.com/job/[^<]+)</loc>", sitemap)
    postings: list[JobPosting] = []
    for job_url in urls:
        try:
            html = fetcher.get_text(job_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: failed %s: %s", site.company, job_url, exc)
            continue
        title_match = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        if not title_match:
            continue
        title = unescape(title_match.group(1)).strip()
        if not title_matches(title, site.title_keywords):
            continue
        desc_match = re.search(
            r'class="[^"]*ats-description[^"]*"[^>]*>([\s\S]*?)</div>',
            html,
            re.I,
        )
        if not desc_match:
            desc_match = re.search(
                r'class="[^"]*job-description[^"]*"[^>]*>([\s\S]*?)</div>',
                html,
                re.I,
            )
        description = _clean_html(desc_match.group(1) if desc_match else "")
        loc_match = re.search(r'class="[^"]*job-location[^"]*"[^>]*>([\s\S]*?)</div>', html, re.I)
        location = _clean_html(loc_match.group(1) if loc_match else "")
        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=job_url,
                location=location,
                description=description,
                source_page=site.url,
            )
        )
    return postings


def _parse_html(site: SiteConfig, fetcher: Fetcher) -> list[JobPosting]:
    """Generic HTML parser: CSS selector or common job-link heuristics."""
    html = fetcher.get_text(site.url)
    soup = _soup(html)
    postings: list[JobPosting] = []
    seen_links: set[str] = set()

    if site.selector:
        elements = soup.select(site.selector)
    else:
        # Heuristic: anchors whose href or text looks job-like.
        elements = soup.find_all("a", href=True)

    for el in elements:
        href = el.get("href") if hasattr(el, "get") else None
        if not href:
            continue
        title = el.get_text(" ", strip=True)
        if not title:
            continue
        if not site.selector:
            href_l = href.lower()
            title_l = title.lower()
            looks_job = any(
                token in href_l for token in ("/job", "/jobs", "/career", "/position", "gh_jid", "lever")
            ) or any(token in title_l for token in ("intern", "internship", "co-op", "coop"))
            if not looks_job:
                continue
            # Skip tiny nav crumbs
            if len(title) < 4:
                continue

        absolute = normalize_link(urljoin(site.url, href))
        if absolute in seen_links:
            continue
        seen_links.add(absolute)

        # HTML pages rarely expose full descriptions on listing pages.
        description = title
        parent = el.parent
        if parent is not None:
            description = parent.get_text(" ", strip=True)

        postings.append(
            JobPosting(
                company=site.company,
                title=title,
                link=absolute,
                location="",
                description=description,
                date_posted=None,
                source_page=site.url,
            )
        )
    return postings


def parse_html_fixture(
    html: str,
    *,
    company: str,
    source_page: str,
    selector: str | None = None,
) -> list[JobPosting]:
    """Parse saved HTML without network access (for unit tests)."""
    site = SiteConfig(
        {
            "company": company,
            "url": source_page,
            "ats": "html",
            "selector": selector,
            "title_keywords": [],
        }
    )

    class _StaticFetcher(Fetcher):
        def __init__(self, body: str) -> None:
            self._body = body

        def get_text(self, url: str) -> str:  # type: ignore[override]
            return self._body

        def get_json(self, url: str) -> Any:  # type: ignore[override]
            raise NotImplementedError

        def close(self) -> None:
            return None

    return _parse_html(site, _StaticFetcher(html))  # type: ignore[arg-type]


_BOARD_FROM_GREENHOUSE = re.compile(
    r"boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([^/]+)",
    re.I,
)
_BOARD_FROM_LEVER = re.compile(r"(?:jobs|api)\.lever\.co/(?:v0/postings/)?([^/?]+)", re.I)
_BOARD_FROM_ASHBY = re.compile(r"ashbyhq\.com/(?:[^/]+/)?jobs/([^/?]+)|job-board/([^/?]+)", re.I)


def infer_board(url: str, ats: str) -> str | None:
    """Best-effort board slug extraction from a career URL."""
    if ats == "greenhouse":
        m = _BOARD_FROM_GREENHOUSE.search(url)
        return m.group(1) if m else None
    if ats == "lever":
        m = _BOARD_FROM_LEVER.search(url)
        return m.group(1) if m else None
    if ats == "ashby":
        m = _BOARD_FROM_ASHBY.search(url)
        if not m:
            return None
        return m.group(1) or m.group(2)
    return None
