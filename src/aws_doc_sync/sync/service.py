"""Sync orchestration.

Wires the ports together and owns the two behaviours that make repeated runs safe:

**Failure isolation.** Every source is fetched inside its own try/except. One
page returning 503 costs that page, not the run -- the other 119 sources still
reach Drive. The consequence is handled one level up: a bundle that lost a source
does not overwrite its existing, complete document.

**Orphan handling, never deletion.** A page that 404s upstream, or that was
removed from the registry, is recorded as orphaned and reported. Nothing is ever
deleted from Drive by this pipeline; that stays a human decision.

**Conditional requests are an optimization, like RSS.** When a source's ETag is
known *and* the content behind its hash is still in the cache, the request is
made conditional and a 304 reuses that content. ``scan`` and ``sync --full``
never send validators, so a wrong ETag upstream cannot hide a change beyond the
next full pass.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

from ..bundling.builder import BundleBuilder
from ..bundling.splitter import document_name
from ..change_detection.hash import HashChangeDetector
from ..change_detection.rss import RssChangeCandidates
from ..config.settings import Settings
from ..domain.errors import FetchError, NotFoundError, SourceError, StoreError
from ..domain.models import (
    Bundle,
    BundleDocument,
    DocumentSource,
    HttpValidators,
    NormalizedDocument,
    NotModified,
    SourceStatus,
    SyncAction,
)
from ..domain.protocols import DocumentFetcher, DocumentStore
from ..domain.urls import canonical_page_url
from ..logging_setup import get_logger
from ..manifest.content_cache import ContentCache
from ..manifest.models import BundleState, SourceState, SyncManifest
from ..normalize.aws_docs import AwsDocsNormalizer
from ..normalize.common import title_from_url
from .planner import decide_action, plan_bundle_documents
from .results import BundleResult, DocumentPlan, SourceResult, SyncReport

log = get_logger("sync")


class SyncService:
    """Runs fetch -> normalize -> diff -> bundle -> store for a set of bundles."""

    def __init__(
        self,
        *,
        settings: Settings,
        fetcher: DocumentFetcher,
        normalizer: AwsDocsNormalizer,
        manifest_repository,
        store: DocumentStore | None = None,
        rss: RssChangeCandidates | None = None,
        builder: BundleBuilder | None = None,
        content_cache: ContentCache | None = None,
    ) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._normalizer = normalizer
        self._manifests = manifest_repository
        self._store = store
        self._rss = rss
        self._builder = builder or BundleBuilder()
        self._detector = HashChangeDetector()
        # Absent a cache, conditional requests are simply never issued: a 304
        # whose content cannot be recovered is worse than not asking.
        self._cache = content_cache or ContentCache(Path(".state/content-cache"), enabled=False)

    # -- public API --------------------------------------------------------------

    def run(
        self,
        bundles: list[Bundle],
        *,
        dry_run: bool = False,
        full_scan: bool = False,
        allow_partial: bool = False,
        write: bool = True,
    ) -> SyncReport:
        """Synchronize ``bundles``.

        Args:
            dry_run: report the plan without writing to Drive or the manifest.
            full_scan: ignore RSS hints and re-hash every source (the safety net).
            allow_partial: build a document even though some sources failed.
                Off by default -- silently shipping an incomplete knowledge source
                is worse than shipping a stale one.
            write: when false, sources are fetched and diffed but no document is
                rendered or stored (used by ``fetch`` and ``scan``).
        """
        # The lock spans load-modify-save: holding it only around the write
        # would still let two runs read the same state and clobber each other.
        lock = getattr(self._manifests, "lock", None)
        with lock() if callable(lock) else nullcontext():
            return self._run_locked(
                bundles,
                dry_run=dry_run,
                full_scan=full_scan,
                allow_partial=allow_partial,
                write=write,
            )

    def _run_locked(
        self,
        bundles: list[Bundle],
        *,
        dry_run: bool,
        full_scan: bool,
        allow_partial: bool,
        write: bool,
    ) -> SyncReport:
        manifest = self._manifests.load()
        report = SyncReport(dry_run=dry_run)

        for bundle in bundles:
            # The result is created here, not inside the bundle run, so that an
            # unexpected failure still reports the sources that did succeed
            # instead of discarding the whole bundle's findings.
            result = BundleResult(
                bundle_id=bundle.id,
                collection_id=bundle.collection_id,
                output=bundle.output,
            )
            try:
                self._sync_bundle(
                    bundle,
                    result=result,
                    manifest=manifest,
                    dry_run=dry_run,
                    full_scan=full_scan,
                    allow_partial=allow_partial,
                    write=write,
                )
            except Exception as exc:  # a bundle must not take down the run
                log.exception("bundle_sync_failed", extra={"bundle_id": bundle.id})
                result.documents.append(
                    DocumentPlan(
                        name=bundle.output,
                        bundle_id=bundle.id,
                        action=SyncAction.ERROR,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                report.errors.append(f"{bundle.id}: {type(exc).__name__}: {exc}")
            report.bundles.append(result)

        self._mark_registry_orphans(manifest, bundles)

        if not dry_run:
            self._manifests.save(manifest)
            if self._settings.app.manifest.prune_content_cache:
                self._cache.prune({s.sha256 for s in manifest.sources.values() if s.sha256})
        else:
            log.info("manifest_write_skipped", extra={"reason": "dry_run"})

        log.info("sync_completed", extra=report.summary())
        return report

    # -- bundle ------------------------------------------------------------------

    def _sync_bundle(
        self,
        bundle: Bundle,
        *,
        result: BundleResult,
        manifest: SyncManifest,
        dry_run: bool,
        full_scan: bool,
        allow_partial: bool,
        write: bool,
    ) -> None:
        log.info(
            "bundle_sync_started",
            extra={
                "bundle_id": bundle.id,
                "collection_id": bundle.collection_id,
                "sources": len(bundle.sources),
                "full_scan": full_scan,
            },
        )

        if not full_scan and self._can_skip_via_rss(bundle, manifest):
            result.skipped_reason = "no document-history RSS activity since the last sync"
            state = manifest.bundle(bundle.id)
            for name, document_id in (state.document_ids if state else {}).items():
                result.documents.append(
                    DocumentPlan(
                        name=name,
                        bundle_id=bundle.id,
                        action=SyncAction.NO_CHANGE,
                        document_id=document_id,
                        composition_hash=(state.composition_hashes.get(name, "") if state else ""),
                        reason=result.skipped_reason,
                    )
                )
            for source in bundle.sources:
                result.sources.append(
                    SourceResult(
                        source_url=canonical_page_url(source.url),
                        bundle_id=bundle.id,
                        status=SourceStatus.NOT_CHECKED,
                    )
                )
            log.info("bundle_skipped_rss", extra={"bundle_id": bundle.id})
            return

        documents, retrieved_at = self._collect_sources(
            bundle, manifest, result, full_scan=full_scan, dry_run=dry_run
        )

        if write:
            self._render_and_store(
                bundle,
                documents,
                retrieved_at,
                manifest=manifest,
                result=result,
                dry_run=dry_run,
                allow_partial=allow_partial,
            )

    def _collect_sources(
        self,
        bundle: Bundle,
        manifest: SyncManifest,
        result: BundleResult,
        *,
        full_scan: bool = False,
        dry_run: bool = False,
    ) -> tuple[list[NormalizedDocument], dict[str, object]]:
        """Fetch, normalize, and diff every source. Errors stay local to a source."""
        documents: list[NormalizedDocument] = []
        retrieved_at: dict[str, object] = {}
        now = datetime.now(UTC)

        for source in bundle.sources:
            canonical = canonical_page_url(source.url)
            previous = manifest.source(canonical)
            validators = None if full_scan else self._validators_for(previous)

            try:
                document, from_cache = self._retrieve(source, previous, validators)
            except NotFoundError as exc:
                # Definitively gone upstream. Recorded, never deleted; the bundle
                # is still complete without it.
                log.warning(
                    "source_orphaned", extra={"source_url": canonical, "reason": str(exc)}
                )
                manifest.put_source(
                    _orphan_state(previous, canonical, bundle, now, str(exc))
                )
                result.sources.append(
                    SourceResult(
                        source_url=canonical,
                        bundle_id=bundle.id,
                        status=SourceStatus.ORPHANED,
                        error=str(exc),
                    )
                )
                continue
            except SourceError as exc:
                log.error(
                    "source_fetch_failed",
                    extra={"source_url": canonical, "bundle_id": bundle.id, "reason": str(exc)},
                )
                if previous is not None:
                    previous.last_checked = now
                    previous.last_error = str(exc)
                    manifest.put_source(previous)
                result.sources.append(
                    SourceResult(
                        source_url=canonical,
                        bundle_id=bundle.id,
                        status=SourceStatus.FAILED,
                        error=str(exc),
                    )
                )
                continue
            except Exception as exc:  # unexpected: still isolated to this source
                log.exception("source_fetch_failed", extra={"source_url": canonical})
                if previous is not None:
                    # Same bookkeeping as the expected-failure branch: without it
                    # stored state claims the source was last checked long ago
                    # and never records why it is failing.
                    previous.last_checked = now
                    previous.last_error = f"{type(exc).__name__}: {exc}"
                    manifest.put_source(previous)
                result.sources.append(
                    SourceResult(
                        source_url=canonical,
                        bundle_id=bundle.id,
                        status=SourceStatus.FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            state, decision = self._detector.apply(
                previous=previous,
                document=document,
                bundle_id=bundle.id,
                collection_id=bundle.collection_id,
                now=now,
            )
            manifest.put_source(state)
            documents.append(document)
            retrieved_at[document.source_url] = state.content_retrieved_at or document.retrieved_at

            if not from_cache and not dry_run:
                # A dry run leaves no local state behind, cache included.
                self._cache.put(document.content_hash, document.content)

            result.sources.append(
                SourceResult(
                    source_url=canonical,
                    bundle_id=bundle.id,
                    status=decision.status,
                    title=document.title,
                    content_hash=document.content_hash,
                    fetcher=document.fetcher,
                    chars=document.char_count,
                    not_modified=from_cache,
                )
            )

        return documents, retrieved_at

    # -- retrieval ---------------------------------------------------------------

    def _validators_for(self, previous: SourceState | None) -> HttpValidators | None:
        """Cache validators worth sending for this source, if any.

        Deliberately conservative. Validators are only offered when the content
        behind the stored hash is still in the cache, because a 304 we cannot act
        on costs an extra round trip and saves nothing.
        """
        if not self._settings.app.change_detection.use_conditional_requests:
            return None
        if previous is None or previous.orphaned:
            return None
        if not previous.sha256 or not previous.fetched_from:
            return None
        if not self._cache.has(previous.sha256):
            return None

        validators = HttpValidators(
            url=previous.fetched_from,
            etag=previous.etag,
            last_modified=previous.last_modified,
        )
        return validators if validators.usable else None

    def _retrieve(
        self,
        source: DocumentSource,
        previous: SourceState | None,
        validators: HttpValidators | None,
    ) -> tuple[NormalizedDocument, bool]:
        """Return the normalized document and whether it came from the cache."""
        outcome = self._fetcher.fetch(source, validators)

        if isinstance(outcome, NotModified):
            cached = self._from_cache(previous, outcome)
            if cached is not None:
                return cached, True
            # The entry vanished between the check and the read. Nothing is
            # broken -- just ask for the document properly this time.
            log.info(
                "source_cache_miss",
                extra={"source_url": source.url, "effect": "refetching unconditionally"},
            )
            outcome = self._fetcher.fetch(source)
            if isinstance(outcome, NotModified):  # pragma: no cover - defensive
                raise FetchError(
                    f"backend answered 304 for an unconditional request: {source.url}",
                    source_url=source.url,
                )

        return self._normalizer.normalize(outcome), False

    def _from_cache(
        self, previous: SourceState | None, not_modified: NotModified
    ) -> NormalizedDocument | None:
        """Rebuild the last known document from cached content, if available.

        The hash is not recomputed from scratch here: the cache verifies content
        against its own key on read, so content that comes back at all is content
        that already hashes to ``previous.sha256``.
        """
        if previous is None or not previous.sha256:
            return None
        content = self._cache.get(previous.sha256)
        if content is None:
            return None

        return NormalizedDocument(
            source_url=previous.source_url,
            title=previous.title or title_from_url(previous.source_url),
            # The provenance timestamp belongs to the content, not to this check.
            retrieved_at=previous.content_retrieved_at or not_modified.checked_at,
            content_hash=previous.sha256,
            content=content,
            fetched_from=not_modified.fetched_from,
            fetcher=previous.fetcher or not_modified.fetcher,
            etag=not_modified.validators.etag,
            last_modified=not_modified.validators.last_modified,
        )

    # -- rendering and storage ---------------------------------------------------

    def _render_and_store(
        self,
        bundle: Bundle,
        documents: list[NormalizedDocument],
        retrieved_at: dict[str, object],
        *,
        manifest: SyncManifest,
        result: BundleResult,
        dry_run: bool,
        allow_partial: bool,
    ) -> None:
        state = manifest.bundle(bundle.id) or BundleState(
            bundle_id=bundle.id, collection_id=bundle.collection_id, output=bundle.output
        )
        # Captured before the new name overwrites it: a split transition has to
        # reconstruct the name each part had last run, which is built from the
        # output name that was in force then.
        previous_output = state.output or bundle.output
        state.output = bundle.output

        if not documents:
            reason = "no source content could be retrieved"
            log.error("bundle_rebuild_skipped", extra={"bundle_id": bundle.id, "reason": reason})
            result.documents.append(
                DocumentPlan(
                    name=bundle.output,
                    bundle_id=bundle.id,
                    action=SyncAction.ERROR,
                    reason=reason,
                    error=reason,
                )
            )
            state.last_error = reason
            manifest.put_bundle(state)
            return

        log.info(
            "bundle_rebuild_started",
            extra={"bundle_id": bundle.id, "sections": len(documents)},
        )
        rendered = plan_bundle_documents(
            bundle,
            documents,
            retrieved_at,
            builder=self._builder,
            target_max_chars=self._settings.google_docs.target_max_chars,
            hard_max_chars=self._settings.google_docs.hard_max_chars,
            part_suffix_format=self._settings.google_docs.part_suffix_format,
        )

        previous_part_count = state.part_count
        for document in rendered:
            plan = self._apply_document(
                document,
                state=state,
                previous_output=previous_output,
                previous_part_count=previous_part_count,
                result=result,
                dry_run=dry_run,
                allow_partial=allow_partial,
            )
            result.documents.append(plan)

        # A name this bundle no longer produces is retired, not deleted. Moving
        # it out of document_ids means the warning fires once, when it actually
        # happens, instead of on every run forever -- a warning that never stops
        # is one an operator learns to scroll past. Re-adoption still works if
        # the name comes back: the store is searched by name before creating.
        #
        # Skipped when nothing was written. An incomplete bundle renders from
        # only the sources that survived, so it can render fewer parts than it
        # really has -- retiring on that basis would record live documents as
        # retired while leaving them untouched in Drive.
        wrote_anything = not any(
            p.action is SyncAction.SKIPPED_INCOMPLETE for p in result.documents
        )
        current_names = {d.name for d in rendered}
        for stale in sorted(set(state.document_ids) - current_names) if wrote_anything else []:
            document_id = state.document_ids.pop(stale)
            state.composition_hashes.pop(stale, None)
            state.orphaned_documents[stale] = document_id
            log.warning(
                "google_doc_retired",
                extra={
                    "bundle_id": bundle.id,
                    "document_name": stale,
                    "document_id": document_id,
                    "reason": "no longer produced by this bundle",
                },
            )

        state.part_count = len(rendered)
        state.last_synced = datetime.now(UTC)
        if any(p.action in (SyncAction.CREATE, SyncAction.UPDATE) for p in result.documents):
            state.last_changed = state.last_synced
        state.last_error = None if result.complete else "one or more sources failed"
        manifest.put_bundle(state)

    def _predecessor_name(
        self, document: BundleDocument, previous_output: str, previous_part_count: int
    ) -> str | None:
        """The name this part had under the previous split layout, if it differs.

        A bundle crossing the size threshold renames ``AWS_X`` to ``AWS_X_01``.
        Without this, the renamed part looks like a brand new document: a second
        copy is created and the original -- the one every Gemini Notebook
        already references by id -- is left behind. Carrying the id over instead
        turns the transition into a rename, which Drive performs in place.
        """
        if previous_part_count == document.part_count or previous_part_count < 1:
            return None
        if document.part > previous_part_count:
            return None  # a genuinely new part, nothing to inherit

        previous = document_name(
            previous_output,
            part=document.part,
            part_count=previous_part_count,
            suffix_format=self._settings.google_docs.part_suffix_format,
        )
        return previous if previous != document.name else None

    def _apply_document(
        self,
        document: BundleDocument,
        *,
        state: BundleState,
        previous_output: str,
        previous_part_count: int,
        result: BundleResult,
        dry_run: bool,
        allow_partial: bool,
    ) -> DocumentPlan:
        store = self._store
        existing = None
        recorded_id = state.document_ids.get(document.name)

        renamed_from: str | None = None
        if recorded_id is None:
            renamed_from = self._predecessor_name(
                document, previous_output, previous_part_count
            )
            if renamed_from is not None:
                recorded_id = state.document_ids.get(renamed_from)
                if recorded_id is not None:
                    log.info(
                        "google_doc_rename_planned",
                        extra={
                            "bundle_id": document.bundle_id,
                            "from_name": renamed_from,
                            "to_name": document.name,
                            "document_id": recorded_id,
                        },
                    )
                else:
                    renamed_from = None

        if store is not None:
            try:
                if recorded_id:
                    existing = store.find_by_id(recorded_id)
                    if existing is None:
                        log.warning(
                            "google_doc_missing",
                            extra={"document_name": document.name, "document_id": recorded_id},
                        )
                if existing is None:
                    # Manifest lost or never existed: adopt by name so a rerun
                    # cannot create a second copy of the same knowledge source.
                    existing = store.find_by_name(document.name)
            except StoreError as exc:
                return DocumentPlan(
                    name=document.name,
                    bundle_id=document.bundle_id,
                    action=SyncAction.ERROR,
                    chars=document.char_count,
                    composition_hash=document.composition_hash,
                    error=str(exc),
                    document=document,
                )
        elif recorded_id:
            # No store configured (offline dry run): trust the manifest's record.
            from ..domain.models import StoredDocument

            existing = StoredDocument(id=recorded_id, name=document.name)

        action, reason = decide_action(
            document,
            existing=existing,
            previous_hash=state.composition_hashes.get(document.name),
            bundle_complete=result.complete,
            allow_partial=allow_partial,
        )

        plan = DocumentPlan(
            name=document.name,
            bundle_id=document.bundle_id,
            action=action,
            document_id=existing.id if existing else None,
            web_view_link=existing.web_view_link if existing else None,
            chars=document.char_count,
            composition_hash=document.composition_hash,
            reason=reason,
            document=document,
        )

        if action is SyncAction.NO_CHANGE:
            log.info(
                "google_doc_unchanged",
                extra={"document_name": document.name, "document_id": plan.document_id},
            )
            return plan

        if action is SyncAction.SKIPPED_INCOMPLETE:
            log.warning(
                "google_doc_update_skipped",
                extra={
                    "document_name": document.name,
                    "reason": reason,
                    "failed_sources": len(result.failed_sources),
                },
            )
            return plan

        if dry_run or store is None:
            log.info(
                "google_doc_planned",
                extra={"document_name": document.name, "action": action.value, "dry_run": dry_run},
            )
            return plan

        try:
            if action is SyncAction.CREATE:
                stored = store.create(document.name, document.markdown)
            else:
                assert existing is not None
                # files.update carries the new name and keeps the file id, so a
                # split transition is a rename rather than a replacement.
                stored = store.update(existing.id, document.name, document.markdown)
        except StoreError as exc:
            log.error(
                "google_doc_write_failed",
                extra={"document_name": document.name, "action": action.value, "reason": str(exc)},
            )
            plan.action = SyncAction.ERROR
            plan.error = str(exc)
            return plan

        plan.document_id = stored.id
        plan.web_view_link = stored.web_view_link
        state.document_ids[document.name] = stored.id

        if renamed_from is not None:
            # The file was renamed in place, so the old key no longer describes
            # anything. Dropping it keeps it out of the orphan report.
            state.document_ids.pop(renamed_from, None)
            state.composition_hashes.pop(renamed_from, None)

        # The composition hash is recorded only once the write is known to have
        # produced a real document. Recording it first and checking afterwards
        # would leave an empty document remembered as synced, and every later
        # run would compare equal and refuse to repair it.
        if self._is_usable(stored.id, document, plan):
            state.composition_hashes[document.name] = document.composition_hash
        else:
            plan.action = SyncAction.ERROR
            state.composition_hashes.pop(document.name, None)

        return plan

    def _is_usable(
        self, document_id: str, document: BundleDocument, plan: DocumentPlan
    ) -> bool:
        """Whether the written document actually contains what was uploaded.

        A Drive write whose conversion produces an empty body still reports
        success. Answering False here is what stops that document's composition
        hash being recorded, so the next run sees a mismatch and rewrites it
        instead of comparing equal forever.

        A store that cannot read back, and a read that fails, both answer True:
        neither is evidence the document is broken, and refusing to record a
        hash on missing evidence would rewrite every document on every run.
        """
        inspect = getattr(self._store, "inspect", None)
        if inspect is None:
            return True
        try:
            shape = inspect(document_id)
        except StoreError as exc:
            log.warning(
                "google_doc_verify_failed",
                extra={"document_id": document_id, "reason": str(exc)},
            )
            return True

        if shape.is_empty:
            plan.error = (
                "document body is empty after conversion; the hash was not "
                "recorded so the next sync will rewrite it"
            )
            log.error(
                "google_doc_verify_empty",
                extra={"document_id": document_id, "document_name": document.name},
            )
            return False
        return True

    # -- RSS fast path -----------------------------------------------------------

    def _can_skip_via_rss(self, bundle: Bundle, manifest: SyncManifest) -> bool:
        """Whether RSS says this whole bundle is worth skipping.

        Only ever returns True when *all* of the following hold, because a false
        positive here means silently serving stale knowledge:

        * RSS use is enabled and the bundle's feeds cover every source in it;
        * every source in the bundle already has a stored hash;
        * every document for the bundle already exists with a recorded hash;
        * no feed entry in the lookback window mentions any of those sources.

        ``scan`` bypasses this entirely, which is the safety net that makes the
        optimization acceptable.
        """
        if not self._settings.app.change_detection.use_rss or self._rss is None:
            return False
        if not self._rss.covers(bundle):
            return False

        state = manifest.bundle(bundle.id)
        if state is None or not state.document_ids:
            return False
        if any(name not in state.composition_hashes for name in state.document_ids):
            return False

        for source in bundle.sources:
            stored = manifest.source(canonical_page_url(source.url))
            if stored is None or not stored.sha256:
                return False  # never seen: always fetch

        candidates = self._rss.candidates(bundle)
        return not candidates

    # -- orphans -----------------------------------------------------------------

    def _mark_registry_orphans(self, manifest: SyncManifest, bundles: list[Bundle]) -> None:
        """Flag manifest sources that the registry no longer lists.

        Recorded only. Removing a page from the registry is a statement about what
        to sync from now on, not an instruction to destroy what was already
        published.
        """
        touched_bundles = {b.id for b in bundles}
        registered = {
            canonical_page_url(s.url) for b in bundles for s in b.sources
        }
        now = datetime.now(UTC)

        for url, state in manifest.sources.items():
            if state.bundle_id not in touched_bundles:
                continue
            if url in registered or state.orphaned:
                continue
            state.orphaned = True
            state.orphaned_at = now
            state.last_error = "removed from the source registry"
            log.warning(
                "source_orphaned",
                extra={"source_url": url, "bundle_id": state.bundle_id, "reason": "deregistered"},
            )


def _orphan_state(
    previous: SourceState | None,
    canonical: str,
    bundle: Bundle,
    now: datetime,
    reason: str,
) -> SourceState:
    state = previous or SourceState(
        source_url=canonical, bundle_id=bundle.id, collection_id=bundle.collection_id
    )
    state.orphaned = True
    state.orphaned_at = state.orphaned_at or now
    state.last_checked = now
    state.last_error = reason
    return state
