"""Link normalization and seen-job cache."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

# Common tracking / analytics query params to strip for stable dedupe keys.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
        "gh_src",
        "lever-source",
    }
)

_HYPERLINK_RE = re.compile(
    r"""^=HYPERLINK\(\s*["']([^"']+)["']""",
    re.I,
)
_LOCALE_PREFIX = re.compile(r"^/(?:en(?:-[a-z]{2})?)(?=/)", re.I)
_GREENHOUSE_HOSTS = frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"})
_WORKDAY_REQ = re.compile(r"(?:^|_)((?:JR|R)[-_]?\d+)$", re.I)


def sheet_cell_url(value: str) -> str:
    """URL from a sheet cell, including ``=HYPERLINK("url", ...)`` formulas."""
    text = (value or "").strip()
    match = _HYPERLINK_RE.match(text)
    return match.group(1).strip() if match else text


def normalize_link(url: str) -> str:
    """Normalize a job URL for use as the primary dedupe key.

    - Unwrap HYPERLINK formulas
    - Lowercase scheme + host
    - Strip fragment
    - Strip trailing slash from path (except root)
    - Drop known tracking query params
    """
    raw = sheet_cell_url(url)
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(kept)

    return urlunparse((scheme, netloc, path, "", query, ""))


def _canonical_netloc(netloc: str) -> str:
    host = netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in _GREENHOUSE_HOSTS:
        return "boards.greenhouse.io"
    return host


def identity_strings(url: str) -> set[str]:
    """Stable strings for one posting so URL drift still matches prior rows.

    Always includes the normalized URL. Also adds host aliases, locale-stripped
    paths, and ATS requisition ids when they are unambiguous.
    """
    normalized = normalize_link(url)
    if not normalized:
        return set()

    parsed = urlparse(normalized)
    host = _canonical_netloc(parsed.netloc)
    path = _LOCALE_PREFIX.sub("", parsed.path or "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    canonical = urlunparse((parsed.scheme or "https", host, path or "/", "", parsed.query, ""))

    identities = {normalized, canonical}

    gh = re.search(r"^/([^/]+)/jobs/(\d+)$", path)
    if gh and host == "boards.greenhouse.io":
        identities.add(f"id:greenhouse:{gh.group(1)}:{gh.group(2)}")

    last = path.rsplit("/", 1)[-1]
    wd = _WORKDAY_REQ.search(last)
    if wd and "myworkdayjobs.com" in host:
        req = wd.group(1).upper().replace("_", "-")
        identities.add(f"id:workday:{host}:{req}")

    apple = re.search(r"/details/([^/]+)", path)
    if apple and host.endswith("jobs.apple.com"):
        identities.add(f"id:apple:{apple.group(1)}")

    amazon = re.search(r"/jobs/(\d+)", path)
    if amazon and host.endswith("amazon.jobs"):
        identities.add(f"id:amazon:{amazon.group(1)}")

    path_job = re.search(r"/jobs?/([^/]+)$", path)
    if path_job and "myworkdayjobs.com" not in host:
        identities.add(f"id:job:{host}:{path_job.group(1)}")

    return identities


def identity_hashes(url: str) -> set[str]:
    """SHA-256 digests of all identity strings for ``url``."""
    return {hashlib.sha256(s.encode("utf-8")).hexdigest() for s in identity_strings(url)}


def link_hash(url: str) -> str:
    """SHA-256 hex digest of the normalized link."""
    normalized = normalize_link(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_known_link(url: str, known: set[str]) -> bool:
    """True if any identity hash of ``url`` is already in ``known``."""
    return not identity_hashes(url).isdisjoint(known)


class SeenJobsCache:
    """Local JSON cache of previously seen link hashes (belt-and-suspenders vs sheet)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._hashes: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load seen-jobs cache %s: %s", self.path, exc)
            return

        if isinstance(data, dict) and "hashes" in data:
            self._hashes = set(data["hashes"])
        elif isinstance(data, list):
            self._hashes = set(data)
        elif isinstance(data, dict):
            # legacy: {hash: meta} or empty {}
            self._hashes = set(data.keys()) if data else set()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"hashes": sorted(self._hashes)}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def __contains__(self, url: str) -> bool:
        return is_known_link(url, self._hashes)

    def add(self, url: str) -> None:
        self._hashes.update(identity_hashes(url))

    def update(self, urls: list[str]) -> None:
        for url in urls:
            self.add(url)

    def known_hashes(self) -> set[str]:
        return set(self._hashes)


def filter_new(links: list[str], known: set[str]) -> list[str]:
    """Return links whose identity hashes are not in ``known``."""
    return [link for link in links if not is_known_link(link, known)]
