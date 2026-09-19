"""Run results.

Separate from the service so that the report can be rendered, serialized, and
asserted on without importing anything that performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.models import BundleDocument, SourceStatus, SyncAction


@dataclass
class SourceResult:
    """Outcome for one AWS documentation page."""

    source_url: str
    bundle_id: str
    status: SourceStatus
    title: str = ""
    content_hash: str = ""
    fetcher: str = ""
    chars: int = 0
    error: str | None = None
    #: Served from the content cache after an HTTP 304, with no body transferred.
    not_modified: bool = False

    @property
    def ok(self) -> bool:
        return self.status not in (SourceStatus.FAILED,)


@dataclass
class DocumentPlan:
    """What will happen (or happened) to one output document."""

    name: str
    bundle_id: str
    action: SyncAction
    document_id: str | None = None
    web_view_link: str | None = None
    chars: int = 0
    composition_hash: str = ""
    reason: str = ""
    error: str | None = None
    document: BundleDocument | None = None


@dataclass
class BundleResult:
    """Outcome for one bundle."""

    bundle_id: str
    collection_id: str
    output: str
    documents: list[DocumentPlan] = field(default_factory=list)
    sources: list[SourceResult] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def failed_sources(self) -> list[SourceResult]:
        return [s for s in self.sources if s.status is SourceStatus.FAILED]

    @property
    def orphaned_sources(self) -> list[SourceResult]:
        return [s for s in self.sources if s.status is SourceStatus.ORPHANED]

    @property
    def complete(self) -> bool:
        """Whether every non-orphaned source was retrieved successfully."""
        return not self.failed_sources


@dataclass
class SyncReport:
    """Aggregate outcome of a run."""

    dry_run: bool = False
    bundles: list[BundleResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # -- aggregates --------------------------------------------------------------

    @property
    def all_sources(self) -> list[SourceResult]:
        return [s for b in self.bundles for s in b.sources]

    @property
    def all_documents(self) -> list[DocumentPlan]:
        return [d for b in self.bundles for d in b.documents]

    def source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for source in self.all_sources:
            counts[source.status.value] = counts.get(source.status.value, 0) + 1
        return counts

    def action_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for document in self.all_documents:
            counts[document.action.value] = counts.get(document.action.value, 0) + 1
        return counts

    @property
    def succeeded_sources(self) -> int:
        return sum(1 for s in self.all_sources if s.ok)

    @property
    def failed_sources(self) -> int:
        return sum(1 for s in self.all_sources if not s.ok)

    @property
    def not_modified_sources(self) -> int:
        """How many sources answered 304 and were served from the cache."""
        return sum(1 for s in self.all_sources if s.not_modified)

    @property
    def has_failures(self) -> bool:
        return bool(self.errors) or self.failed_sources > 0 or any(
            d.action is SyncAction.ERROR for d in self.all_documents
        )

    def exit_code(self) -> int:
        """0 clean, 1 partial failure, 2 nothing usable was produced.

        A partial failure must not read as success to a scheduler, but it must
        also not read as a total failure -- the sources that did sync are live
        and correct.
        """
        if self.errors and not self.bundles:
            return 2
        if self.has_failures:
            return 1
        return 0

    def summary(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "bundles": len(self.bundles),
            "sources_total": len(self.all_sources),
            "sources_succeeded": self.succeeded_sources,
            "sources_failed": self.failed_sources,
            "sources_not_modified": self.not_modified_sources,
            "source_status": self.source_counts(),
            "document_actions": self.action_counts(),
            "errors": len(self.errors),
        }
