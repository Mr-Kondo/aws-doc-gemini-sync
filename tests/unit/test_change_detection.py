from datetime import UTC, datetime, timedelta

import httpx

from aws_doc_sync.change_detection.hash import HashChangeDetector, evaluate_change
from aws_doc_sync.change_detection.rss import RssChangeCandidates
from aws_doc_sync.domain.models import Bundle, DocumentSource, SourceStatus
from aws_doc_sync.normalize.aws_docs import AwsDocsNormalizer
from tests.conftest import fixture_text, make_raw, mock_client

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 2, 1, tzinfo=UTC)


def normalize(text: str, *, retrieved_at: datetime):
    return AwsDocsNormalizer().normalize(make_raw(text, retrieved_at=retrieved_at))


def test_first_sight_of_a_source_is_new():
    document = normalize("# T\n\nBody.\n", retrieved_at=T0)
    assert evaluate_change(None, document).status is SourceStatus.NEW


def test_identical_content_is_unchanged():
    detector = HashChangeDetector()
    first, _ = detector.apply(
        previous=None, document=normalize("# T\n\nBody.\n", retrieved_at=T0),
        bundle_id="b", now=T0,
    )
    _, decision = detector.apply(
        previous=first, document=normalize("# T\n\nBody.\n", retrieved_at=T1),
        bundle_id="b", now=T1,
    )
    assert decision.status is SourceStatus.UNCHANGED
    assert not decision.changed


def test_different_content_is_changed():
    detector = HashChangeDetector()
    first, _ = detector.apply(
        previous=None, document=normalize("# T\n\nBody.\n", retrieved_at=T0),
        bundle_id="b", now=T0,
    )
    _, decision = detector.apply(
        previous=first, document=normalize("# T\n\nBody changed.\n", retrieved_at=T1),
        bundle_id="b", now=T1,
    )
    assert decision.status is SourceStatus.CHANGED
    assert decision.previous_hash != decision.current_hash


def test_unchanged_content_does_not_advance_the_provenance_timestamp():
    """The timestamp rendered into the document must be content-derived.

    If ``content_retrieved_at`` moved on every check, the rendered bundle would
    differ on every run and nothing would ever report NO_CHANGE.
    """
    detector = HashChangeDetector()
    first, _ = detector.apply(
        previous=None, document=normalize("# T\n\nBody.\n", retrieved_at=T0),
        bundle_id="b", now=T0,
    )
    second, _ = detector.apply(
        previous=first, document=normalize("# T\n\nBody.\n", retrieved_at=T1),
        bundle_id="b", now=T1,
    )
    assert second.content_retrieved_at == T0     # pinned to the content
    assert second.last_changed == first.last_changed
    assert second.last_checked == T1             # but the check did happen


def test_changed_content_does_advance_the_provenance_timestamp():
    detector = HashChangeDetector()
    first, _ = detector.apply(
        previous=None, document=normalize("# T\n\nBody.\n", retrieved_at=T0),
        bundle_id="b", now=T0,
    )
    second, _ = detector.apply(
        previous=first, document=normalize("# T\n\nNew body.\n", retrieved_at=T1),
        bundle_id="b", now=T1,
    )
    assert second.content_retrieved_at == T1
    assert second.last_changed == T1


def test_a_cleared_error_and_orphan_flag_are_reset_on_a_good_fetch():
    detector = HashChangeDetector()
    first, _ = detector.apply(
        previous=None, document=normalize("# T\n\nBody.\n", retrieved_at=T0),
        bundle_id="b", now=T0,
    )
    first.orphaned = True
    first.last_error = "was gone"
    second, _ = detector.apply(
        previous=first, document=normalize("# T\n\nBody.\n", retrieved_at=T1),
        bundle_id="b", now=T1,
    )
    assert second.orphaned is False
    assert second.last_error is None


# -- RSS ---------------------------------------------------------------------------


def bundle_with(urls, feeds=("https://docs.aws.amazon.com/example/latest/dg/feed.rss",)):
    return Bundle(
        id="b",
        collection_id="c",
        output="OUT",
        sources=tuple(DocumentSource(url=u) for u in urls),
        rss_feeds=tuple(feeds),
    )


def rss_client():
    return mock_client(
        lambda r: httpx.Response(
            200, text=fixture_text("doc_history.rss"),
            headers={"Content-Type": "application/rss+xml"},
        )
    )


def test_rss_matches_urls_found_in_the_item_description():
    """Real AWS feeds put the changed page in <description>, not <link>.

    The <link> element points at the guide's doc-history page for every item, so
    reading only that would produce zero candidates forever.
    """
    changed = "https://docs.aws.amazon.com/example/latest/dg/changed-page.html"
    quiet = "https://docs.aws.amazon.com/example/latest/dg/quiet-page.html"
    rss = RssChangeCandidates(rss_client())
    assert rss.candidates(bundle_with([changed, quiet])) == {changed}


def test_rss_only_reports_pages_the_bundle_actually_tracks():
    # The feed also mentions /other/latest/dg/elsewhere.html, which this bundle
    # does not register. Reporting it would trigger a pointless fetch.
    tracked = "https://docs.aws.amazon.com/example/latest/dg/quiet-page.html"
    rss = RssChangeCandidates(rss_client())
    assert rss.candidates(bundle_with([tracked])) == set()


def test_bundle_without_feeds_yields_no_candidates_and_is_not_covered():
    rss = RssChangeCandidates(rss_client())
    bundle = bundle_with(["https://docs.aws.amazon.com/example/latest/dg/a.html"], feeds=())
    assert rss.candidates(bundle) == set()
    assert rss.covers(bundle) is False


def test_coverage_requires_a_feed_for_every_guide_in_the_bundle():
    """A feed cannot report a change in a guide it does not cover.

    Without this check, mixing a watched guide with an unwatched one would make
    the bundle look permanently quiet.
    """
    rss = RssChangeCandidates(rss_client())
    same_guide = bundle_with(
        ["https://docs.aws.amazon.com/example/latest/dg/a.html",
         "https://docs.aws.amazon.com/example/latest/dg/b.html"]
    )
    mixed = bundle_with(
        ["https://docs.aws.amazon.com/example/latest/dg/a.html",
         "https://docs.aws.amazon.com/other/latest/dg/b.html"]
    )
    assert rss.covers(same_guide) is True
    assert rss.covers(mixed) is False


def test_a_dead_feed_does_not_raise():
    # A broken feed must degrade to "no hint", never fail the bundle.
    rss = RssChangeCandidates(mock_client(lambda r: httpx.Response(500)))
    bundle = bundle_with(["https://docs.aws.amazon.com/example/latest/dg/a.html"])
    assert rss.candidates(bundle) == set()


def test_entries_older_than_the_lookback_window_are_ignored():
    old = datetime.now(UTC) - timedelta(days=400)
    feed = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
      <item><title>old</title>
      <link>https://docs.aws.amazon.com/example/latest/dg/changed-page.html</link>
      <pubDate>{old.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate></item>
    </channel></rss>"""
    rss = RssChangeCandidates(
        mock_client(lambda r: httpx.Response(200, text=feed,
                                             headers={"Content-Type": "application/rss+xml"})),
        lookback_days=30,
    )
    url = "https://docs.aws.amazon.com/example/latest/dg/changed-page.html"
    assert rss.candidates(bundle_with([url])) == set()
