"""Core domain model.

These types are deliberately free of any vendor detail: nothing here knows about
httpx, Google, or YAML. Adapters convert into and out of them at the edges.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class ContentKind(StrEnum):
    """Wire format of a retrieved document, before normalization."""

    MARKDOWN = "markdown"
    HTML = "html"


class FetchStrategy(StrEnum):
    """Which fetcher(s) a source is allowed to use.

    ``AUTO`` walks the configured priority chain (markdown -> mcp -> html).
    The explicit values pin a source to one backend, which is mainly useful for
    pages where the ``.md`` endpoint exists but is a content-free stub.
    """

    AUTO = "auto"
    MARKDOWN = "markdown"
    MCP = "mcp"
    HTML = "html"


class SyncAction(StrEnum):
    """Outcome decided by the planner for one bundle document."""

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    NO_CHANGE = "NO_CHANGE"
    SKIPPED_INCOMPLETE = "SKIPPED_INCOMPLETE"
    ERROR = "ERROR"


class SourceStatus(StrEnum):
    """Per-source outcome within a run."""

    FETCHED = "FETCHED"
    UNCHANGED = "UNCHANGED"
    CHANGED = "CHANGED"
    NEW = "NEW"
    FAILED = "FAILED"
    ORPHANED = "ORPHANED"
    NOT_CHECKED = "NOT_CHECKED"


# --------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase ``value`` into a stable, filesystem- and key-safe slug."""
    return _SLUG_RE.sub("_", value.strip().lower()).strip("_")


def source_id_for(url: str) -> str:
    """Derive a stable identifier for a source URL.

    The id must not change when unrelated registry entries move around, so it is
    derived purely from the URL. A short hash is appended because two different
    guides can legitimately host the same page basename.
    """
    from .urls import canonical_page_url

    canonical = canonical_page_url(url)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    tail = canonical.rstrip("/").rsplit("/", 1)[-1]
    tail = tail.removesuffix(".html").removesuffix(".md")
    return f"{slugify(tail) or 'page'}-{digest}"


# --------------------------------------------------------------------------------------
# Registry-facing models
# --------------------------------------------------------------------------------------

NonEmptyStr = Annotated[str, Field(min_length=1)]


class DocumentSource(BaseModel):
    """One AWS documentation page that participates in a bundle."""

    model_config = ConfigDict(frozen=True)

    url: NonEmptyStr
    collection_id: str = ""
    bundle_id: str = ""
    title_override: str | None = None
    strategy: FetchStrategy = FetchStrategy.AUTO
    #: Optional explicit markdown URL, for the (verified) cases where the
    #: ``.html`` -> ``.md`` rewrite does not hold.
    markdown_url: str | None = None

    @field_validator("url", "markdown_url")
    @classmethod
    def _must_be_http(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"source url must be absolute http(s): {v!r}")
        return v

    @property
    def id(self) -> str:
        return source_id_for(self.url)


class Bundle(BaseModel):
    """A semantic grouping of sources that maps to exactly one Google Doc.

    One Google Doc per bundle (never one per page) is what keeps a Gemini
    Notebook's source budget usable.
    """

    model_config = ConfigDict(frozen=True)

    id: NonEmptyStr
    collection_id: NonEmptyStr
    output: NonEmptyStr
    title: str = ""
    description: str = ""
    sources: tuple[DocumentSource, ...] = ()
    #: Document-history RSS feeds that can act as a change-detection fast path.
    #: Empty is normal and not an error -- several AWS guides publish no feed.
    rss_feeds: tuple[str, ...] = ()

    @property
    def display_title(self) -> str:
        return self.title or self.output.replace("_", " ")


class Collection(BaseModel):
    """A named group of bundles, used as the unit of CLI selection."""

    model_config = ConfigDict(frozen=True)

    id: NonEmptyStr
    description: str = ""
    bundles: tuple[Bundle, ...] = ()


class SourceRegistry(BaseModel):
    """The whole synchronization target set, loaded from YAML."""

    model_config = ConfigDict(frozen=True)

    collections: tuple[Collection, ...] = ()

    def iter_bundles(self):
        for collection in self.collections:
            yield from collection.bundles

    def iter_sources(self):
        for bundle in self.iter_bundles():
            yield from bundle.sources

    def collection(self, collection_id: str) -> Collection | None:
        return next((c for c in self.collections if c.id == collection_id), None)

    def bundle(self, bundle_id: str) -> Bundle | None:
        return next((b for b in self.iter_bundles() if b.id == bundle_id), None)


# --------------------------------------------------------------------------------------
# Pipeline models
# --------------------------------------------------------------------------------------


class HttpValidators(BaseModel):
    """Cache validators for one specific URL.

    The URL is part of the value, not context around it. An ETag identifies a
    representation of *one* resource, and this pipeline can legitimately retrieve
    the same page from two different URLs (``page.md`` normally, ``page.html`` on
    fallback). Carrying the URL makes it impossible to send one resource's
    validator while requesting another.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    etag: str | None = None
    last_modified: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.etag or self.last_modified)

    def applies_to(self, url: str) -> bool:
        return self.usable and self.url == url

    def headers(self) -> dict[str, str]:
        """Conditional request headers, per RFC 9110."""
        headers: dict[str, str] = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers


class NotModified(BaseModel):
    """HTTP 304: the server confirmed the stored copy is still current.

    A successful outcome, not an error -- which is why it is a value rather than
    an exception. Returned only when the caller supplied validators, so code that
    fetches unconditionally never has to consider it.
    """

    model_config = ConfigDict(frozen=True)

    source: DocumentSource
    fetched_from: str
    fetcher: str
    checked_at: datetime
    #: Refreshed from the 304 response when it carried one, otherwise the
    #: validators that were sent.
    validators: HttpValidators


class RawDocument(BaseModel):
    """Bytes as retrieved, before normalization. Never persisted."""

    model_config = ConfigDict(frozen=True)

    source: DocumentSource
    content: str
    kind: ContentKind
    #: The URL actually retrieved, which differs from ``source.url`` whenever the
    #: markdown endpoint was used.
    fetched_from: str
    fetcher: str
    retrieved_at: datetime
    etag: str | None = None
    last_modified: str | None = None


class NormalizedDocument(BaseModel):
    """Deterministically cleaned document content plus its provenance.

    ``content_hash`` is the source of truth for change detection; RSS is only ever
    an optimization that decides *what to check*, never *what changed*.
    """

    model_config = ConfigDict(frozen=True)

    source_url: str
    title: str
    retrieved_at: datetime
    content_hash: str
    content: str
    fetched_from: str = ""
    fetcher: str = ""
    etag: str | None = None
    last_modified: str | None = None

    @property
    def char_count(self) -> int:
        return len(self.content)


class DocumentMetadata(BaseModel):
    """Provenance header rendered above each source's body inside a bundle doc."""

    model_config = ConfigDict(frozen=True)

    source_url: str
    title: str
    retrieved_at: datetime
    content_hash: str
    fetcher: str = ""


class BundleDocument(BaseModel):
    """One rendered output document (a bundle, or one part of a split bundle)."""

    model_config = ConfigDict(frozen=True)

    bundle_id: str
    collection_id: str
    #: Drive file name, e.g. ``AWS_SageMaker_Training`` or ``..._01`` when split.
    name: str
    part: int = 1
    part_count: int = 1
    title: str = ""
    markdown: str = ""
    #: Hash of the *inputs* that produced this document, excluding volatile
    #: fields such as generation time. Comparing it against the manifest is what
    #: makes repeated syncs a no-op.
    composition_hash: str = ""
    source_urls: tuple[str, ...] = ()

    @property
    def char_count(self) -> int:
        return len(self.markdown)


class StoredDocument(BaseModel):
    """A document as it exists in the destination store."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    web_view_link: str | None = None
    modified_time: datetime | None = None
    trashed: bool = False
