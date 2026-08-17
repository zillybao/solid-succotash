"""Link normalization and seen-job cache."""

from __future__ import annotations

import hashlib
import json
import logging
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


def normalize_link(url: str) -> str:
    """Normalize a job URL for use as the primary dedupe key.

    - Lowercase scheme + host
    - Strip fragment
    - Strip trailing slash from path (except root)
    - Drop known tracking query params
    """
    raw = url.strip()
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


def link_hash(url: str) -> str:
    """SHA-256 hex digest of the normalized link."""
    normalized = normalize_link(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
        return link_hash(url) in self._hashes

    def add(self, url: str) -> None:
        self._hashes.add(link_hash(url))

    def update(self, urls: list[str]) -> None:
        for url in urls:
            self.add(url)

    def known_hashes(self) -> set[str]:
        return set(self._hashes)


def filter_new(links: list[str], known: set[str]) -> list[str]:
    """Return links whose normalized hash is not in ``known``."""
    return [link for link in links if link_hash(link) not in known]
