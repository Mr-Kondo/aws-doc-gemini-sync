"""Sync decisions.

Deliberately free of I/O. Everything it needs -- the rendered document, the
previous composition hash, whether a matching document already exists -- is passed
in, so every idempotency and safety rule can be tested directly instead of through
a mocked Google client.

The rules:

``CREATE``
    No document exists for this name, by id or by name lookup.
``UPDATE``
    A document exists and the composition hash moved.
``NO_CHANGE``
    A document exists and the composition hash is identical. This is the expected
    result of running the same sync twice, and it performs no API write.
``SKIPPED_INCOMPLETE``
    At least one source failed to retrieve. An existing, complete document is left
    alone rather than being overwritten with a document that silently lost a page.
    A *orphaned* source (404 upstream) does not trigger this: the page is gone, so
    a bundle without it is correct, not incomplete.
"""

from __future__ import annotations

from ..bundling.builder import BundleBuilder, SourceSection
from ..bundling.splitter import document_name, plan_parts
from ..domain.models import Bundle, BundleDocument, NormalizedDocument, StoredDocument, SyncAction


def decide_action(
    document: BundleDocument,
    *,
    existing: StoredDocument | None,
    previous_hash: str | None,
    bundle_complete: bool,
    allow_partial: bool = False,
) -> tuple[SyncAction, str]:
    """Return the action for one document plus a human-readable reason."""
    if not bundle_complete and not allow_partial:
        if existing is not None:
            return (
                SyncAction.SKIPPED_INCOMPLETE,
                "one or more sources failed to fetch; existing document left untouched",
            )
        return (
            SyncAction.SKIPPED_INCOMPLETE,
            "one or more sources failed to fetch; refusing to create a partial document",
        )

    if existing is None:
        return SyncAction.CREATE, "no existing document found"

    if previous_hash and previous_hash == document.composition_hash:
        return SyncAction.NO_CHANGE, "composition hash matches the last sync"

    if not previous_hash:
        # The document exists but this run has no record of it -- adopt rather
        # than create a duplicate, and rewrite once to guarantee agreement.
        return SyncAction.UPDATE, "adopting an existing document with no recorded hash"

    return SyncAction.UPDATE, "composition hash changed"


def plan_bundle_documents(
    bundle: Bundle,
    documents: list[NormalizedDocument],
    retrieved_at: dict[str, object],
    *,
    builder: BundleBuilder,
    target_max_chars: int,
    hard_max_chars: int,
    part_suffix_format: str = "_{part:02d}",
) -> list[BundleDocument]:
    """Render a bundle into one or more output documents.

    Section order follows the registry, never the fetch order: a bundle whose
    content shuffled because two pages happened to return in a different order
    would produce a new hash and a pointless rewrite on every run.
    """
    by_url = {d.source_url: d for d in documents}

    sections: list[SourceSection] = []
    for source in bundle.sources:
        from ..domain.urls import canonical_page_url

        document = by_url.get(canonical_page_url(source.url))
        if document is None:
            continue  # failed or orphaned; the caller decides what that means
        when = retrieved_at.get(document.source_url) or document.retrieved_at
        sections.append(builder.section(document, retrieved_at=when))  # type: ignore[arg-type]

    if not sections:
        return []

    parts = plan_parts(
        sections, target_max_chars=target_max_chars, hard_max_chars=hard_max_chars
    )
    part_count = len(parts)

    return [
        builder.document(
            bundle,
            list(plan.sections),
            name=document_name(
                bundle.output,
                part=plan.part,
                part_count=part_count,
                suffix_format=part_suffix_format,
            ),
            part=plan.part,
            part_count=part_count,
        )
        for plan in parts
    ]
