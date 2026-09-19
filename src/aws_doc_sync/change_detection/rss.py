"""Document-history RSS -- a fast path, never an authority.

AWS publishes a release-notes feed for many (not all) guides; IAM, for one,
publishes none. So this layer answers only *which sources are worth fetching
first*, and the hash still decides what changed. ``scan`` bypasses it entirely.

Two properties of the real feeds shaped this module:

* An item's ``<link>`` points at the guide's ``doc-history.html``, not at the page
  that changed. The changed pages appear as links inside the HTML-escaped
  ``<description>``, so URLs are harvested from both fields.
* Feeds are per guide, so a candidate URL is only meaningful for sources under
  the same guide root.
"""

from __future__ import annotations

import html as html_module
import re
from datetime import UTC, datetime, timedelta

import feedparser

from ..domain.errors import FetchError
from ..domain.models import Bundle
from ..domain.urls import canonical_page_url, guide_root
from ..fetchers.http_client import HttpClient
from ..logging_setup import get_logger

log = get_logger("change.rss")

DOCS_URL_RE = re.compile(r"https?://docs\.aws\.amazon\.com/[^\s\"'<>)\]]+", re.IGNORECASE)


class RssChangeCandidates:
    """Collects change candidates from AWS document-history feeds."""

    def __init__(self, client: HttpClient, *, lookback_days: int = 30) -> None:
        self._client = client
        self._lookback = timedelta(days=lookback_days)
        self._cache: dict[str, set[str]] = {}

    def covers(self, bundle: Bundle) -> bool:
        """Whether the bundle's feeds can observe *every* source in it.

        AWS publishes one feed per guide. A bundle mixing two guides where only
        one has a feed would otherwise look quiet whenever the unwatched guide
        was the thing that changed -- the feed physically cannot mention it. In
        that case the bundle is not eligible for the fast path at all.
        """
        if not bundle.rss_feeds:
            return False
        feed_roots = {guide_root(feed) for feed in bundle.rss_feeds}
        return all(guide_root(source.url) in feed_roots for source in bundle.sources)

    def candidates(self, bundle: Bundle) -> set[str]:
        """Canonical URLs that the bundle's feeds suggest may have changed.

        Returns an empty set when the bundle declares no feed. Callers must treat
        that as "no hint available", not as "nothing changed" -- the difference is
        the whole reason hashing stays authoritative.
        """
        if not bundle.rss_feeds:
            return set()

        found: set[str] = set()
        for feed_url in bundle.rss_feeds:
            try:
                found |= self._feed_urls(feed_url)
            except FetchError as exc:
                # A dead feed must not fail the bundle; the hash comparison that
                # follows still covers every source.
                log.warning(
                    "rss_fetch_failed",
                    extra={"feed_url": feed_url, "bundle_id": bundle.id, "reason": str(exc)},
                )

        # Only registered sources matter. A feed mentions plenty of pages this
        # bundle does not track.
        matched = {canonical_page_url(s.url) for s in bundle.sources} & found
        log.info(
            "rss_candidates_resolved",
            extra={
                "bundle_id": bundle.id,
                "feeds": len(bundle.rss_feeds),
                "urls_in_feed": len(found),
                "matched_sources": len(matched),
            },
        )
        return matched

    def _feed_urls(self, feed_url: str) -> set[str]:
        if feed_url in self._cache:
            return self._cache[feed_url]

        response = self._client.get(feed_url)
        parsed = feedparser.parse(response.text)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise FetchError(f"feed could not be parsed: {feed_url}")

        cutoff = datetime.now(UTC) - self._lookback
        urls: set[str] = set()

        for entry in parsed.entries:
            if not _within(entry, cutoff):
                continue
            for blob in _entry_text(entry):
                for match in DOCS_URL_RE.findall(html_module.unescape(blob)):
                    urls.add(canonical_page_url(match))

        self._cache[feed_url] = urls
        log.info("rss_feed_parsed", extra={"feed_url": feed_url, "urls": len(urls)})
        return urls


def _entry_text(entry: object) -> list[str]:
    """All fields of a feed entry that can contain a documentation URL."""
    blobs: list[str] = []
    for field in ("link", "id", "title", "summary", "description"):
        value = getattr(entry, field, None) or (
            entry.get(field) if isinstance(entry, dict) else None
        )
        if isinstance(value, str):
            blobs.append(value)
    for content in getattr(entry, "content", None) or []:
        value = content.get("value") if isinstance(content, dict) else None
        if isinstance(value, str):
            blobs.append(value)
    return blobs


def _within(entry: object, cutoff: datetime) -> bool:
    """Whether the entry is recent enough to be worth acting on.

    Undated entries are kept: dropping them could hide a real change, and the
    cost of a needless fetch is one conditional HTTP request.
    """
    for field in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            try:
                year, month, day, hour, minute, second = parsed[:6]
                when = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
            except (TypeError, ValueError):
                continue
            return when >= cutoff
    return True
