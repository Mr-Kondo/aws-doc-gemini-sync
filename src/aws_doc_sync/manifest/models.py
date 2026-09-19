"""Sync state.

The manifest is the pipeline's memory: what each source looked like last time,
which Google Doc a bundle lives in, and when each of those last actually changed.
It is *local operator state*, never committed -- two people syncing the same
registry to two different Drive folders must not fight over one checked-in file.

The distinction that makes idempotency work:

``last_checked``
    Every run touches this. Says nothing about content.
``etag`` / ``last_modified`` / ``fetched_from``
    HTTP cache validators and the URL they describe, used to make the next
    request conditional. An optimization only: a 304 is believed just far enough
    to reuse content that already hashed to ``sha256``.
``content_retrieved_at`` / ``last_changed``
    Advance **only when the hash moves**. These are what the rendered document
    quotes as provenance, which is why re-running sync produces a byte-identical
    document and therefore ``NO_CHANGE``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MANIFEST_VERSION = 1


class SourceState(BaseModel):
    """Last known state of one AWS documentation page."""

    model_config = ConfigDict(extra="ignore")

    source_url: str
    bundle_id: str
    collection_id: str = ""
    title: str = ""
    sha256: str = ""
    etag: str | None = None
    last_modified: str | None = None
    fetcher: str = ""
    #: The URL the ETag / Last-Modified above actually describe. A page can be
    #: retrieved from ``page.md`` normally and ``page.html`` on fallback, and a
    #: validator is only valid for the resource that issued it.
    fetched_from: str = ""
    char_count: int = 0
    last_checked: datetime | None = None
    last_changed: datetime | None = None
    #: When the bytes behind the current hash were retrieved. Quoted as
    #: provenance in the rendered bundle, so it must not move on a no-op run.
    content_retrieved_at: datetime | None = None
    #: Upstream returned 404/410 on the last check. Recorded, never auto-deleted.
    orphaned: bool = False
    orphaned_at: datetime | None = None
    last_error: str | None = None


class BundleState(BaseModel):
    """Last known state of one bundle and the documents it produced."""

    model_config = ConfigDict(extra="ignore")

    bundle_id: str
    collection_id: str = ""
    output: str = ""
    #: Drive file id per rendered part, keyed by document name.
    document_ids: dict[str, str] = Field(default_factory=dict)
    #: Composition hash per document name; the UPDATE/NO_CHANGE discriminator.
    composition_hashes: dict[str, str] = Field(default_factory=dict)
    #: Documents this bundle used to produce and no longer does, typically
    #: because a split boundary moved. Kept out of ``document_ids`` so the
    #: retirement is reported once rather than on every subsequent run, and
    #: retained so the operator can find what to delete by hand.
    orphaned_documents: dict[str, str] = Field(default_factory=dict)
    part_count: int = 1
    last_synced: datetime | None = None
    last_changed: datetime | None = None
    last_error: str | None = None


class SyncManifest(BaseModel):
    """Whole-repository sync state."""

    model_config = ConfigDict(extra="ignore")

    version: int = MANIFEST_VERSION
    updated_at: datetime | None = None
    sources: dict[str, SourceState] = Field(default_factory=dict)
    bundles: dict[str, BundleState] = Field(default_factory=dict)
    #: Feed URL -> last entry timestamp already accounted for.
    rss_cursors: dict[str, datetime] = Field(default_factory=dict)

    # -- sources -----------------------------------------------------------------

    def source(self, source_url: str) -> SourceState | None:
        return self.sources.get(source_url)

    def put_source(self, state: SourceState) -> None:
        self.sources[state.source_url] = state

    # -- bundles -----------------------------------------------------------------

    def bundle(self, bundle_id: str) -> BundleState | None:
        return self.bundles.get(bundle_id)

    def put_bundle(self, state: BundleState) -> None:
        self.bundles[state.bundle_id] = state

    # -- reporting ---------------------------------------------------------------

    def orphans(self) -> list[SourceState]:
        return [s for s in self.sources.values() if s.orphaned]

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "sources": len(self.sources),
            "bundles": len(self.bundles),
            "orphaned": len(self.orphans()),
            "documents": sum(len(b.document_ids) for b in self.bundles.values()),
        }
