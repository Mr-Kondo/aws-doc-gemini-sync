"""End-to-end behaviour of the sync pipeline, with Google and AWS faked out."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aws_doc_sync.domain.errors import FetchError, NotFoundError
from aws_doc_sync.domain.models import (
    Bundle,
    ContentKind,
    DocumentSource,
    HttpValidators,
    NotModified,
    RawDocument,
    SourceStatus,
    SyncAction,
)
from aws_doc_sync.google.fake import FakeDocumentStore
from aws_doc_sync.manifest.repository import InMemoryManifestRepository, JsonManifestRepository
from aws_doc_sync.normalize.aws_docs import AwsDocsNormalizer
from aws_doc_sync.sync.service import SyncService
from tests.conftest import default_settings

BASE = "https://docs.aws.amazon.com/example/latest/dg"


class ScriptedFetcher:
    """Serves canned content per URL; raises what the script says to raise.

    Also models a conditional origin: when the caller sends validators whose
    ETag matches the one this URL currently serves, it answers 304 the way
    docs.aws.amazon.com would.
    """

    name = "scripted"

    def __init__(self, bodies: dict[str, str | Exception]) -> None:
        self.bodies = bodies
        self.calls: list[str] = []
        self.conditional_calls: list[str] = []
        #: URL -> ETag currently served. Change it to simulate an edit upstream.
        self.etags: dict[str, str] = dict.fromkeys(bodies, '"v1"')

    def supports(self, source: DocumentSource) -> bool:
        return True

    def fetch(
        self, source: DocumentSource, validators: HttpValidators | None = None
    ) -> RawDocument | NotModified:
        self.calls.append(source.url)
        etag = self.etags.get(source.url)

        if validators is not None and validators.applies_to(source.url):
            self.conditional_calls.append(source.url)
            if etag is not None and validators.etag == etag:
                return NotModified(
                    source=source,
                    fetched_from=source.url,
                    fetcher=self.name,
                    checked_at=datetime.now(UTC),
                    validators=HttpValidators(url=source.url, etag=etag),
                )

        body = self.bodies[source.url]
        if isinstance(body, Exception):
            raise body
        return RawDocument(
            source=source,
            content=body,
            kind=ContentKind.MARKDOWN,
            fetched_from=source.url,
            fetcher="markdown",
            retrieved_at=datetime.now(UTC),
            etag=etag,
        )

    def edit(self, url: str, body: str) -> None:
        """Change a page upstream, the way a real edit would: new body, new ETag."""
        self.bodies[url] = body
        self.etags[url] = f'"v{len(self.calls)}"' 


def bundle(slugs, *, bundle_id="b", output="AWS_Example", rss=()) -> Bundle:
    return Bundle(
        id=bundle_id,
        collection_id="c",
        output=output,
        title="AWS Example",
        sources=tuple(DocumentSource(url=f"{BASE}/{s}.html", bundle_id=bundle_id) for s in slugs),
        rss_feeds=tuple(rss),
    )


def body(slug: str, revision: str = "v1") -> str:
    return f"# {slug.title()}\n\nContent for {slug}, revision {revision}.\n"


def service(fetcher, store, manifests, settings=None) -> SyncService:
    return SyncService(
        settings=settings or default_settings(),
        fetcher=fetcher,
        normalizer=AwsDocsNormalizer(),
        manifest_repository=manifests,
        store=store,
    )


@pytest.fixture
def setup():
    slugs = ["alpha", "beta"]
    fetcher = ScriptedFetcher({f"{BASE}/{s}.html": body(s) for s in slugs})
    store = FakeDocumentStore()
    manifests = InMemoryManifestRepository()
    return bundle(slugs), fetcher, store, manifests


# -- idempotency -------------------------------------------------------------------


def test_first_run_creates_one_document_per_bundle(setup):
    b, fetcher, store, manifests = setup
    report = service(fetcher, store, manifests).run([b])

    assert report.action_counts() == {"CREATE": 1}
    assert report.source_counts() == {"NEW": 2}
    assert store.created == ["AWS_Example"]
    assert report.exit_code() == 0


def test_second_run_with_identical_content_writes_nothing(setup):
    """The core idempotency guarantee."""
    b, fetcher, store, manifests = setup
    svc = service(fetcher, store, manifests)

    svc.run([b])
    store.created.clear()
    store.updated.clear()

    second = svc.run([b])

    assert second.action_counts() == {"NO_CHANGE": 1}
    assert second.source_counts() == {"UNCHANGED": 2}
    assert store.created == [] and store.updated == []


def test_third_run_is_also_a_no_op(setup):
    b, fetcher, store, manifests = setup
    svc = service(fetcher, store, manifests)
    svc.run([b])
    svc.run([b])
    store.created.clear()
    store.updated.clear()
    assert svc.run([b]).action_counts() == {"NO_CHANGE": 1}
    assert store.created == [] and store.updated == []


def test_a_changed_source_updates_in_place_keeping_the_document_id(setup):
    b, fetcher, store, manifests = setup
    svc = service(fetcher, store, manifests)
    first = svc.run([b])
    original_id = first.all_documents[0].document_id

    fetcher.bodies[f"{BASE}/alpha.html"] = body("alpha", revision="v2")
    second = svc.run([b])

    assert second.action_counts() == {"UPDATE": 1}
    assert second.source_counts() == {"CHANGED": 1, "UNCHANGED": 1}
    # The id must survive: a Gemini Notebook references the document by id.
    assert second.all_documents[0].document_id == original_id
    assert store.created == ["AWS_Example"]  # never created twice
    assert "revision v2" in store.content(original_id)


def test_rendered_document_is_byte_identical_across_no_op_runs(setup):
    b, fetcher, store, manifests = setup
    svc = service(fetcher, store, manifests)
    svc.run([b])
    document_id = store.find_by_name("AWS_Example").id
    first_content = store.content(document_id)

    svc.run([b])
    assert store.content(document_id) == first_content


# -- manifest loss and adoption -----------------------------------------------------


def test_lost_manifest_adopts_the_existing_document_instead_of_duplicating(setup):
    b, fetcher, store, _ = setup
    service(fetcher, store, InMemoryManifestRepository()).run([b])
    assert len(store.list_documents()) == 1

    # A fresh machine with no manifest at all.
    report = service(fetcher, store, InMemoryManifestRepository()).run([b])

    assert report.action_counts() == {"UPDATE": 1}
    assert len(store.list_documents()) == 1
    assert store.created == ["AWS_Example"]


def test_document_deleted_from_drive_is_recreated(setup):
    b, fetcher, store, manifests = setup
    svc = service(fetcher, store, manifests)
    svc.run([b])

    store._documents.clear()  # someone emptied the Drive folder
    report = svc.run([b])

    assert report.action_counts() == {"CREATE": 1}


# -- failure isolation ---------------------------------------------------------------


def test_one_failing_source_does_not_stop_the_others(setup):
    b, fetcher, store, manifests = setup
    b = bundle(["alpha", "beta", "gamma"])
    fetcher = ScriptedFetcher(
        {
            f"{BASE}/alpha.html": body("alpha"),
            f"{BASE}/beta.html": body("beta"),
            f"{BASE}/gamma.html": FetchError("503 from AWS", source_url=f"{BASE}/gamma.html"),
        }
    )
    report = service(fetcher, store, manifests).run([b])

    assert report.succeeded_sources == 2
    assert report.failed_sources == 1
    # Every source was attempted; the failure did not abort the loop.
    assert len(fetcher.calls) == 3


def test_an_incomplete_bundle_never_overwrites_a_good_document():
    """The rule that protects existing knowledge from a transient AWS outage."""
    b = bundle(["alpha", "beta"])
    good = {f"{BASE}/{s}.html": body(s) for s in ("alpha", "beta")}
    fetcher = ScriptedFetcher(dict(good))
    store = FakeDocumentStore()
    manifests = InMemoryManifestRepository()

    service(fetcher, store, manifests).run([b])
    document_id = store.find_by_name("AWS_Example").id
    intact = store.content(document_id)

    fetcher.bodies[f"{BASE}/beta.html"] = FetchError("503", source_url=f"{BASE}/beta.html")
    fetcher.bodies[f"{BASE}/alpha.html"] = body("alpha", revision="v2")
    report = service(fetcher, store, manifests).run([b])

    assert report.action_counts() == {"SKIPPED_INCOMPLETE": 1}
    assert store.content(document_id) == intact   # untouched
    assert report.exit_code() == 1                # but the run is not "clean"


def test_allow_partial_writes_the_incomplete_document_when_asked():
    b = bundle(["alpha", "beta"])
    fetcher = ScriptedFetcher(
        {
            f"{BASE}/alpha.html": body("alpha"),
            f"{BASE}/beta.html": FetchError("503", source_url=f"{BASE}/beta.html"),
        }
    )
    store = FakeDocumentStore()
    report = service(fetcher, store, InMemoryManifestRepository()).run(
        [b], allow_partial=True
    )
    assert report.action_counts() == {"CREATE": 1}
    assert "Sources in this document: 1" in store.content_by_name("AWS_Example")


def test_a_bundle_whose_sources_all_fail_is_an_error_not_an_empty_document():
    b = bundle(["alpha"])
    fetcher = ScriptedFetcher({f"{BASE}/alpha.html": FetchError("down")})
    store = FakeDocumentStore()
    report = service(fetcher, store, InMemoryManifestRepository()).run([b])

    assert report.action_counts() == {"ERROR": 1}
    assert store.created == []


def test_one_failing_bundle_does_not_stop_the_next_one():
    ok = bundle(["alpha"], bundle_id="ok", output="AWS_Ok")
    broken = bundle(["beta"], bundle_id="broken", output="AWS_Broken")
    fetcher = ScriptedFetcher(
        {f"{BASE}/alpha.html": body("alpha"), f"{BASE}/beta.html": FetchError("down")}
    )
    store = FakeDocumentStore()
    report = service(fetcher, store, InMemoryManifestRepository()).run([broken, ok])

    assert store.created == ["AWS_Ok"]
    assert len(report.bundles) == 2


# -- orphans -------------------------------------------------------------------------


def test_a_page_that_404s_is_recorded_as_orphaned_and_never_deleted():
    b = bundle(["alpha", "beta"])
    fetcher = ScriptedFetcher(
        {f"{BASE}/alpha.html": body("alpha"), f"{BASE}/beta.html": body("beta")}
    )
    store = FakeDocumentStore()
    manifests = InMemoryManifestRepository()
    service(fetcher, store, manifests).run([b])

    fetcher.bodies[f"{BASE}/beta.html"] = NotFoundError("410 gone", source_url=f"{BASE}/beta.html")
    report = service(fetcher, store, manifests).run([b])

    assert SourceStatus.ORPHANED in {s.status for s in report.all_sources}
    # An upstream removal is a real change, so the bundle is rebuilt without it.
    assert report.action_counts() == {"UPDATE": 1}
    assert manifests.load().source(f"{BASE}/beta.html").orphaned is True
    assert len(store.list_documents()) == 1


def test_removing_a_source_from_the_registry_records_an_orphan():
    fetcher = ScriptedFetcher(
        {f"{BASE}/alpha.html": body("alpha"), f"{BASE}/beta.html": body("beta")}
    )
    store = FakeDocumentStore()
    manifests = InMemoryManifestRepository()
    service(fetcher, store, manifests).run([bundle(["alpha", "beta"])])

    service(fetcher, store, manifests).run([bundle(["alpha"])])

    state = manifests.load().source(f"{BASE}/beta.html")
    assert state.orphaned is True
    assert "registry" in (state.last_error or "")


# -- dry run -------------------------------------------------------------------------


def test_dry_run_writes_neither_drive_nor_the_manifest(setup):
    b, fetcher, store, manifests = setup
    report = service(fetcher, store, manifests).run([b], dry_run=True)

    assert report.action_counts() == {"CREATE": 1}
    assert report.dry_run is True
    assert store.created == [] and store.updated == []
    assert manifests.saves == 0
    assert manifests.load().sources == {}


def test_dry_run_against_a_read_only_store_cannot_write(setup):
    from aws_doc_sync.google.fake import ReadOnlyStore

    b, fetcher, store, manifests = setup
    guard = ReadOnlyStore(store)
    report = service(fetcher, guard, manifests).run([b], dry_run=True)

    assert report.action_counts() == {"CREATE": 1}
    assert guard.blocked == []   # the planner never even attempted a write


# -- RSS fast path --------------------------------------------------------------------


class StubRss:
    def __init__(self, candidates: set[str], covers: bool = True) -> None:
        self._candidates = candidates
        self._covers = covers
        self.asked = 0

    def covers(self, bundle) -> bool:
        return self._covers

    def candidates(self, bundle) -> set[str]:
        self.asked += 1
        return self._candidates


def test_quiet_feed_skips_the_bundle_without_refetching():
    b = bundle(["alpha"], rss=[f"{BASE}/feed.rss"])
    fetcher = ScriptedFetcher({f"{BASE}/alpha.html": body("alpha")})
    store = FakeDocumentStore()
    manifests = InMemoryManifestRepository()

    svc = SyncService(
        settings=default_settings(), fetcher=fetcher, normalizer=AwsDocsNormalizer(),
        manifest_repository=manifests, store=store, rss=StubRss(set()),
    )
    svc.run([b])                       # establishes state
    calls_after_first = len(fetcher.calls)

    report = svc.run([b])

    assert len(fetcher.calls) == calls_after_first    # nothing refetched
    assert report.action_counts() == {"NO_CHANGE": 1}
    assert report.source_counts() == {"NOT_CHECKED": 1}


def test_full_scan_ignores_the_rss_fast_path():
    """The safety net: hashing is authoritative and can always be forced."""
    b = bundle(["alpha"], rss=[f"{BASE}/feed.rss"])
    fetcher = ScriptedFetcher({f"{BASE}/alpha.html": body("alpha")})
    manifests = InMemoryManifestRepository()
    svc = SyncService(
        settings=default_settings(), fetcher=fetcher, normalizer=AwsDocsNormalizer(),
        manifest_repository=manifests, store=FakeDocumentStore(), rss=StubRss(set()),
    )
    svc.run([b])
    before = len(fetcher.calls)

    svc.run([b], full_scan=True)
    assert len(fetcher.calls) == before + 1


def test_rss_is_not_trusted_for_a_bundle_it_cannot_observe():
    b = bundle(["alpha"], rss=[f"{BASE}/feed.rss"])
    fetcher = ScriptedFetcher({f"{BASE}/alpha.html": body("alpha")})
    manifests = InMemoryManifestRepository()
    rss = StubRss(set(), covers=False)
    svc = SyncService(
        settings=default_settings(), fetcher=fetcher, normalizer=AwsDocsNormalizer(),
        manifest_repository=manifests, store=FakeDocumentStore(), rss=rss,
    )
    svc.run([b])
    before = len(fetcher.calls)
    svc.run([b])
    assert len(fetcher.calls) == before + 1   # refetched despite the quiet feed


def test_an_unseen_source_is_always_fetched_even_when_the_feed_is_quiet():
    b = bundle(["alpha"], rss=[f"{BASE}/feed.rss"])
    fetcher = ScriptedFetcher({f"{BASE}/alpha.html": body("alpha")})
    svc = SyncService(
        settings=default_settings(), fetcher=fetcher, normalizer=AwsDocsNormalizer(),
        manifest_repository=InMemoryManifestRepository(), store=FakeDocumentStore(),
        rss=StubRss(set()),
    )
    svc.run([b])
    assert fetcher.calls == [f"{BASE}/alpha.html"]


# -- persistence -------------------------------------------------------------------


def test_state_survives_a_process_restart(tmp_path):
    b = bundle(["alpha"])
    fetcher = ScriptedFetcher({f"{BASE}/alpha.html": body("alpha")})
    store = FakeDocumentStore()
    path = tmp_path / "manifest.json"

    service(fetcher, store, JsonManifestRepository(path)).run([b])
    # A brand new repository object, reading the file written by the first run.
    report = service(fetcher, store, JsonManifestRepository(path)).run([b])

    assert report.action_counts() == {"NO_CHANGE": 1}
    assert store.updated == []


# -- conditional requests ------------------------------------------------------------


def cached_service(fetcher, store, manifests, cache, settings=None) -> SyncService:
    return SyncService(
        settings=settings or default_settings(),
        fetcher=fetcher,
        normalizer=AwsDocsNormalizer(),
        manifest_repository=manifests,
        store=store,
        content_cache=cache,
    )


@pytest.fixture
def conditional(tmp_path):
    from aws_doc_sync.manifest.content_cache import ContentCache

    slugs = ["alpha", "beta"]
    fetcher = ScriptedFetcher({f"{BASE}/{s}.html": body(s) for s in slugs})
    return (
        bundle(slugs),
        fetcher,
        FakeDocumentStore(),
        InMemoryManifestRepository(),
        ContentCache(tmp_path / "content-cache"),
    )


def test_first_run_has_nothing_to_validate_against(conditional):
    b, fetcher, store, manifests, cache = conditional
    cached_service(fetcher, store, manifests, cache).run([b])
    assert fetcher.conditional_calls == []


def test_second_run_validates_and_is_served_from_the_cache(conditional):
    """The payoff: a full sync with no document bodies transferred."""
    b, fetcher, store, manifests, cache = conditional
    svc = lambda: cached_service(fetcher, store, manifests, cache)  # noqa: E731

    svc().run([b])
    store.created.clear()

    report = svc().run([b])

    assert fetcher.conditional_calls == [f"{BASE}/alpha.html", f"{BASE}/beta.html"]
    assert report.source_counts() == {"UNCHANGED": 2}
    assert report.action_counts() == {"NO_CHANGE": 1}
    assert report.not_modified_sources == 2
    assert report.summary()["sources_not_modified"] == 2
    assert store.created == [] and store.updated == []


def test_a_304_served_document_keeps_its_original_provenance(conditional):
    """The rendered document must not change just because it was revalidated."""
    b, fetcher, store, manifests, cache = conditional
    cached_service(fetcher, store, manifests, cache).run([b])
    document_id = store.find_by_name("AWS_Example").id
    original = store.content(document_id)

    cached_service(fetcher, store, manifests, cache).run([b])
    assert store.content(document_id) == original


def test_an_upstream_edit_changes_the_etag_and_forces_a_real_fetch(conditional):
    b, fetcher, store, manifests, cache = conditional
    svc = lambda: cached_service(fetcher, store, manifests, cache)  # noqa: E731
    svc().run([b])

    fetcher.edit(f"{BASE}/alpha.html", body("alpha", revision="v2"))
    report = svc().run([b])

    assert report.source_counts() == {"CHANGED": 1, "UNCHANGED": 1}
    assert report.action_counts() == {"UPDATE": 1}
    assert report.not_modified_sources == 1          # beta still revalidated
    assert "revision v2" in store.content_by_name("AWS_Example")


def test_full_scan_never_sends_validators(conditional):
    """The safety net. A wrong ETag upstream cannot outlive one full pass."""
    b, fetcher, store, manifests, cache = conditional
    svc = lambda: cached_service(fetcher, store, manifests, cache)  # noqa: E731
    svc().run([b])

    report = svc().run([b], full_scan=True)

    assert fetcher.conditional_calls == []
    assert report.source_counts() == {"UNCHANGED": 2}
    assert report.not_modified_sources == 0


def test_settings_can_turn_conditional_requests_off(conditional):
    from aws_doc_sync.config.settings import ChangeDetectionSettings

    b, fetcher, store, manifests, cache = conditional
    settings = default_settings(
        change_detection=ChangeDetectionSettings(use_conditional_requests=False)
    )
    cached_service(fetcher, store, manifests, cache, settings).run([b])
    cached_service(fetcher, store, manifests, cache, settings).run([b])
    assert fetcher.conditional_calls == []


def test_no_validators_are_sent_when_the_cache_cannot_answer(conditional):
    """A 304 we cannot act on costs a round trip and saves nothing."""
    b, fetcher, store, manifests, cache = conditional
    cached_service(fetcher, store, manifests, cache).run([b])

    cache.prune(set())   # wipe the cache, keep the manifest
    report = cached_service(fetcher, store, manifests, cache).run([b])

    assert fetcher.conditional_calls == []
    assert report.source_counts() == {"UNCHANGED": 2}


def test_an_entry_lost_after_the_check_falls_back_to_a_full_fetch(conditional):
    """The narrow race: has() says yes, get() finds nothing."""
    b, fetcher, store, manifests, cache = conditional
    cached_service(fetcher, store, manifests, cache).run([b])

    class VanishingCache:
        def __init__(self, inner):
            self.inner = inner

        def has(self, content_hash):
            return True

        def get(self, content_hash):
            return None

        def put(self, content_hash, content):
            self.inner.put(content_hash, content)

        def prune(self, keep):
            return 0

    fetcher.calls.clear()
    report = cached_service(fetcher, store, manifests, VanishingCache(cache)).run([b])

    # Each source: one conditional 304, then one real fetch.
    assert len(fetcher.calls) == 4
    assert report.source_counts() == {"UNCHANGED": 2}
    assert report.not_modified_sources == 0


def test_a_dry_run_leaves_the_cache_untouched(conditional):
    b, fetcher, store, manifests, cache = conditional
    cached_service(fetcher, store, manifests, cache).run([b], dry_run=True)
    assert cache.stats()["entries"] == 0


def test_the_cache_is_pruned_to_what_the_manifest_still_references(conditional):
    b, fetcher, store, manifests, cache = conditional
    svc = lambda: cached_service(fetcher, store, manifests, cache)  # noqa: E731
    svc().run([b])
    assert cache.stats()["entries"] == 2

    fetcher.edit(f"{BASE}/alpha.html", body("alpha", revision="v2"))
    svc().run([b])

    # Two live sources; the superseded revision of alpha is gone.
    assert cache.stats()["entries"] == 2
    manifest = manifests.load()
    for state in manifest.sources.values():
        assert cache.get(state.sha256) is not None


def test_validators_are_recorded_against_the_url_that_produced_them(conditional):
    b, fetcher, store, manifests, cache = conditional
    cached_service(fetcher, store, manifests, cache).run([b])

    state = manifests.load().source(f"{BASE}/alpha.html")
    assert state.fetched_from == f"{BASE}/alpha.html"
    assert state.etag == '"v1"'


def test_an_orphaned_source_is_not_revalidated(conditional):
    b, fetcher, store, manifests, cache = conditional
    cached_service(fetcher, store, manifests, cache).run([b])

    fetcher.bodies[f"{BASE}/beta.html"] = NotFoundError("gone", source_url=f"{BASE}/beta.html")
    fetcher.etags.pop(f"{BASE}/beta.html", None)
    cached_service(fetcher, store, manifests, cache).run([b])

    fetcher.conditional_calls.clear()
    cached_service(fetcher, store, manifests, cache).run([b])
    # alpha is still revalidated; the orphan is not.
    assert fetcher.conditional_calls == [f"{BASE}/alpha.html"]


# -- document splitting across runs ---------------------------------------------------


def split_setup(tmp_path, *, page_chars: int):
    """A bundle whose size can be pushed over the split threshold on demand."""
    from aws_doc_sync.config.settings import GoogleDocsSettings
    from aws_doc_sync.manifest.content_cache import ContentCache

    slugs = ["alpha", "beta"]
    fetcher = ScriptedFetcher(
        {f"{BASE}/{s}.html": f"# {s.title()}\n\n{'x' * page_chars}\n" for s in slugs}
    )
    settings = default_settings(
        google_docs=GoogleDocsSettings(target_max_chars=250_000, hard_max_chars=500_000)
    )
    return (
        bundle(slugs),
        fetcher,
        FakeDocumentStore(),
        InMemoryManifestRepository(),
        ContentCache(tmp_path / "cache"),
        settings,
    )


def test_growing_past_the_split_threshold_renames_instead_of_duplicating(tmp_path):
    """The document id must survive a split -- a notebook references it by id.

    Creating fresh documents here would silently detach every Gemini Notebook
    pointing at the original and leave an orphan behind.
    """
    from aws_doc_sync.config.settings import GoogleDocsSettings

    b, fetcher, store, manifests, cache, settings = split_setup(tmp_path, page_chars=2_000)

    first = cached_service(fetcher, store, manifests, cache, settings).run([b])
    assert [d.name for d in first.all_documents] == ["AWS_Example"]
    original_id = first.all_documents[0].document_id

    # The same sources, now too large for one document.
    tight = default_settings(
        google_docs=GoogleDocsSettings(target_max_chars=4_000, hard_max_chars=500_000)
    )
    second = cached_service(fetcher, store, manifests, cache, tight).run([b], full_scan=True)

    names = [d.name for d in second.all_documents]
    assert names == ["AWS_Example_01", "AWS_Example_02"]

    # Part 1 kept the original file, renamed in place.
    part_one = next(d for d in second.all_documents if d.name == "AWS_Example_01")
    assert part_one.action is SyncAction.UPDATE
    assert part_one.document_id == original_id

    # Exactly one new document was created, not two.
    assert store.created == ["AWS_Example", "AWS_Example_02"]
    assert len(store.list_documents()) == 2


def test_the_old_name_is_not_reported_as_an_orphan_after_a_rename(tmp_path):
    from aws_doc_sync.config.settings import GoogleDocsSettings

    b, fetcher, store, manifests, cache, settings = split_setup(tmp_path, page_chars=2_000)
    cached_service(fetcher, store, manifests, cache, settings).run([b])

    tight = default_settings(
        google_docs=GoogleDocsSettings(target_max_chars=4_000, hard_max_chars=500_000)
    )
    cached_service(fetcher, store, manifests, cache, tight).run([b], full_scan=True)

    state = manifests.load().bundle("b")
    assert set(state.document_ids) == {"AWS_Example_01", "AWS_Example_02"}
    assert "AWS_Example" not in state.document_ids
    assert state.part_count == 2


def test_a_split_bundle_is_still_idempotent(tmp_path):
    from aws_doc_sync.config.settings import GoogleDocsSettings

    b, fetcher, store, manifests, cache, _ = split_setup(tmp_path, page_chars=2_000)
    tight = default_settings(
        google_docs=GoogleDocsSettings(target_max_chars=4_000, hard_max_chars=500_000)
    )
    cached_service(fetcher, store, manifests, cache, tight).run([b])
    store.created.clear()
    store.updated.clear()

    report = cached_service(fetcher, store, manifests, cache, tight).run([b], full_scan=True)

    assert report.action_counts() == {"NO_CHANGE": 2}
    assert store.created == [] and store.updated == []


def test_a_retired_document_is_reported_once_not_on_every_run(tmp_path):
    """A warning that never stops is one an operator learns to scroll past.

    Shrinking back below the split threshold retires the extra part. That is
    worth saying when it happens, and worth never saying again.
    """
    from aws_doc_sync.config.settings import GoogleDocsSettings

    b, fetcher, store, manifests, cache, _ = split_setup(tmp_path, page_chars=2_000)
    tight = default_settings(
        google_docs=GoogleDocsSettings(target_max_chars=4_000, hard_max_chars=500_000)
    )
    wide = default_settings(
        google_docs=GoogleDocsSettings(target_max_chars=250_000, hard_max_chars=500_000)
    )

    cached_service(fetcher, store, manifests, cache, tight).run([b])
    assert set(manifests.load().bundle("b").document_ids) == {
        "AWS_Example_01",
        "AWS_Example_02",
    }

    cached_service(fetcher, store, manifests, cache, wide).run([b], full_scan=True)

    state = manifests.load().bundle("b")
    assert set(state.document_ids) == {"AWS_Example"}
    # The retired name is recorded so it can be cleaned up deliberately...
    assert "AWS_Example_02" in state.orphaned_documents
    # ...and no longer looks like a live document that vanished.
    assert "AWS_Example_02" not in state.composition_hashes

    # A further run has nothing left to report and writes nothing.
    store.updated.clear()
    report = cached_service(fetcher, store, manifests, cache, wide).run([b], full_scan=True)
    assert report.action_counts() == {"NO_CHANGE": 1}
    assert store.updated == []
    assert set(manifests.load().bundle("b").orphaned_documents) == {"AWS_Example_02"}


def test_a_retired_document_is_re_adopted_if_its_name_comes_back(tmp_path):
    """Growing again must reuse the retired file, not create a third one."""
    from aws_doc_sync.config.settings import GoogleDocsSettings

    b, fetcher, store, manifests, cache, _ = split_setup(tmp_path, page_chars=2_000)
    tight = default_settings(
        google_docs=GoogleDocsSettings(target_max_chars=4_000, hard_max_chars=500_000)
    )
    wide = default_settings(
        google_docs=GoogleDocsSettings(target_max_chars=250_000, hard_max_chars=500_000)
    )

    cached_service(fetcher, store, manifests, cache, tight).run([b])
    part_two_id = store.find_by_name("AWS_Example_02").id

    cached_service(fetcher, store, manifests, cache, wide).run([b], full_scan=True)
    store.created.clear()
    cached_service(fetcher, store, manifests, cache, tight).run([b], full_scan=True)

    assert store.created == [], "a retired document was duplicated instead of reused"
    assert store.find_by_name("AWS_Example_02").id == part_two_id
