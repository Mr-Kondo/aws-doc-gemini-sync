"""Hash-based change detection -- the source of truth.

RSS may say a page changed; HTTP may hand back an ETag. Neither decides anything
here. A source is changed if and only if the SHA-256 of its *normalized* content
differs from the stored one.

Hashing after normalization rather than over the raw bytes is deliberate: AWS
rotates markup, ETags, and whitespace far more often than it rewrites prose, and
hashing raw bytes would rebuild Google Docs for changes no reader would notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..domain.models import NormalizedDocument, SourceStatus
from ..logging_setup import get_logger
from ..manifest.models import SourceState

log = get_logger("change.hash")


@dataclass(frozen=True)
class ChangeDecision:
    status: SourceStatus
    previous_hash: str | None
    current_hash: str

    @property
    def changed(self) -> bool:
        return self.status in (SourceStatus.CHANGED, SourceStatus.NEW)


def evaluate_change(previous: SourceState | None, document: NormalizedDocument) -> ChangeDecision:
    """Compare a freshly normalized document against stored state."""
    if previous is None or not previous.sha256:
        return ChangeDecision(SourceStatus.NEW, None, document.content_hash)
    if previous.sha256 == document.content_hash:
        return ChangeDecision(SourceStatus.UNCHANGED, previous.sha256, document.content_hash)
    return ChangeDecision(SourceStatus.CHANGED, previous.sha256, document.content_hash)


class HashChangeDetector:
    """Applies a change decision to manifest state.

    The timestamp policy lives here so it cannot drift between call sites:
    ``last_checked`` always advances; ``last_changed`` and
    ``content_retrieved_at`` advance only on a real content change. Those two
    feed the rendered provenance block, which is what keeps a repeat sync
    byte-identical and therefore ``NO_CHANGE``.
    """

    def apply(
        self,
        *,
        previous: SourceState | None,
        document: NormalizedDocument,
        bundle_id: str,
        collection_id: str = "",
        now: datetime | None = None,
    ) -> tuple[SourceState, ChangeDecision]:
        now = now or datetime.now(UTC)
        decision = evaluate_change(previous, document)

        content_retrieved_at: datetime
        last_changed: datetime | None
        if decision.changed:
            content_retrieved_at = document.retrieved_at
            last_changed = now
        else:
            assert previous is not None
            content_retrieved_at = previous.content_retrieved_at or document.retrieved_at
            last_changed = previous.last_changed

        state = SourceState(
            source_url=document.source_url,
            bundle_id=bundle_id,
            collection_id=collection_id,
            title=document.title,
            sha256=document.content_hash,
            etag=document.etag,
            last_modified=document.last_modified,
            fetcher=document.fetcher,
            fetched_from=document.fetched_from,
            char_count=document.char_count,
            last_checked=now,
            last_changed=last_changed,
            content_retrieved_at=content_retrieved_at,
            orphaned=False,
            orphaned_at=None,
            last_error=None,
        )

        log.info(
            "source_changed" if decision.changed else "source_unchanged",
            extra={
                "source_url": document.source_url,
                "status": decision.status.value,
                "content_hash": decision.current_hash,
                "previous_hash": decision.previous_hash,
                "bundle_id": bundle_id,
            },
        )
        return state, decision
