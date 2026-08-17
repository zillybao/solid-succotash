"""Optional Slack digest for new postings / repeated failures."""

from __future__ import annotations

import logging
import os
from typing import Sequence

import httpx

from src.models import JobPosting

logger = logging.getLogger(__name__)


def notify_new_postings(postings: Sequence[JobPosting], webhook_url: str | None = None) -> None:
    """Post a short Slack digest if SLACK_WEBHOOK_URL is configured."""
    url = webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    if not url or not postings:
        return

    lines = [f"*{len(postings)} new internship posting(s)*"]
    for p in postings[:25]:
        lines.append(f"• {p.company}: <{p.link}|{p.title}> ({p.location or 'n/a'})")
    if len(postings) > 25:
        lines.append(f"_…and {len(postings) - 25} more_")

    _post(url, "\n".join(lines))


def notify_failures(failures: Sequence[str], webhook_url: str | None = None) -> None:
    url = webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    if not url or not failures:
        return
    body = "*Intern finder site failures*\n" + "\n".join(f"• {f}" for f in failures)
    _post(url, body)


def _post(webhook_url: str, text: str) -> None:
    try:
        response = httpx.post(webhook_url, json={"text": text}, timeout=15.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Slack notify failed: %s", exc)
