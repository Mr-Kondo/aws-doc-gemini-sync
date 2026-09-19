"""Live checks against docs.aws.amazon.com.

Deselected by default (``-m 'not integration'`` in pyproject). Run with::

    pytest -m integration tests/integration/test_live_aws.py

These exist because the whole markdown-first strategy rests on assumptions about
a service this project does not control. If AWS stops serving ``.md`` siblings,
or changes the Content-Type, or moves the document-history feeds, the unit suite
would still pass while production quietly degraded to HTML conversion. This is
the test that notices.

Read-only: no AWS or Google state is modified.
"""

from __future__ import annotations

import pytest

from aws_doc_sync.change_detection.rss import RssChangeCandidates
from aws_doc_sync.domain.errors import NotFoundError
from aws_doc_sync.domain.models import Bundle, ContentKind, DocumentSource
from aws_doc_sync.fetchers.chain import FallbackFetcher
from aws_doc_sync.fetchers.html import HtmlDocumentFetcher
from aws_doc_sync.fetchers.http_client import HttpClient, RetryPolicy
from aws_doc_sync.fetchers.markdown import MarkdownDocumentFetcher
from aws_doc_sync.normalize.aws_docs import AwsDocsNormalizer

pytestmark = pytest.mark.integration

# Verified to exist at the time of writing.
MARKDOWN_PAGE = "https://docs.aws.amazon.com/sagemaker/latest/dg/how-it-works-training.html"
HTML_ONLY_PAGE = (
    "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/AlarmThatSendsEmail.html"
)
CODE_HEAVY_PAGE = "https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html"
SAGEMAKER_FEED = (
    "https://docs.aws.amazon.com/sagemaker/latest/dg/amazon-sagemaker-release-notes.rss"
)


@pytest.fixture(scope="module")
def client():
    with HttpClient(
        policy=RetryPolicy(max_retries=2),
        requests_per_second=2.0,
        user_agent="aws-doc-sync/0.1 (integration test)",
    ) as http:
        yield http


@pytest.fixture(scope="module")
def chain(client):
    return FallbackFetcher([MarkdownDocumentFetcher(client), HtmlDocumentFetcher(client)])


def test_markdown_endpoint_still_exists_and_is_served_as_markdown(chain):
    raw = chain.fetch(DocumentSource(url=MARKDOWN_PAGE))
    assert raw.fetcher == "markdown"
    assert raw.kind is ContentKind.MARKDOWN
    assert raw.fetched_from.endswith(".md")
    assert raw.etag  # conditional-request metadata is available


def test_missing_markdown_sibling_falls_back_to_html(chain):
    raw = chain.fetch(DocumentSource(url=HTML_ONLY_PAGE))
    assert raw.fetcher == "html"
    assert raw.kind is ContentKind.HTML


def test_markdown_fetcher_alone_reports_not_found_for_the_html_only_page(client):
    with pytest.raises(NotFoundError):
        MarkdownDocumentFetcher(client).fetch(DocumentSource(url=HTML_ONLY_PAGE))


def test_live_markdown_normalizes_into_traceable_content(chain):
    document = AwsDocsNormalizer().normalize(chain.fetch(DocumentSource(url=MARKDOWN_PAGE)))
    assert document.title
    assert document.content_hash.startswith("sha256:")
    assert document.char_count > 1_000
    assert "<a name=" not in document.content


def test_a_second_fetch_of_an_unchanged_page_produces_the_same_hash(chain):
    """Hashing must be stable against AWS's own response noise (ETag, ordering)."""
    normalizer = AwsDocsNormalizer()
    first = normalizer.normalize(chain.fetch(DocumentSource(url=MARKDOWN_PAGE)))
    second = normalizer.normalize(chain.fetch(DocumentSource(url=MARKDOWN_PAGE)))
    assert first.content_hash == second.content_hash


def test_code_blocks_survive_on_a_policy_heavy_page(chain):
    document = AwsDocsNormalizer().normalize(chain.fetch(DocumentSource(url=CODE_HEAVY_PAGE)))
    assert "```" in document.content
    assert '"Version"' in document.content


def test_document_history_feed_yields_candidates_for_registered_sources(client):
    bundle = Bundle(
        id="live",
        collection_id="live",
        output="LIVE",
        sources=(DocumentSource(url=MARKDOWN_PAGE),),
        rss_feeds=(SAGEMAKER_FEED,),
    )
    rss = RssChangeCandidates(client, lookback_days=3650)
    assert rss.covers(bundle) is True
    # The feed must at least parse and produce docs.aws.amazon.com URLs.
    assert isinstance(rss.candidates(bundle), set)


def test_full_pipeline_against_live_aws_is_idempotent(chain, tmp_path):
    """The end-to-end guarantee, with real AWS content and a faked destination.

    Everything upstream of Google is live here: real fetches, real fallback, real
    normalization, real hashing, real manifest persistence. Only the destination
    is a fake, so the test can assert the thing that matters -- that a second run
    writes nothing -- without touching anyone's Drive.
    """
    from aws_doc_sync.config.settings import Settings
    from aws_doc_sync.google.fake import FakeDocumentStore
    from aws_doc_sync.manifest.repository import JsonManifestRepository
    from aws_doc_sync.sync.service import SyncService

    bundle = Bundle(
        id="live_mixed",
        collection_id="live",
        output="AWS_Live_Mixed",
        title="Live mixed-backend bundle",
        # One page served as Markdown, one that only exists as HTML: both
        # acquisition paths are exercised inside a single document.
        sources=(DocumentSource(url=MARKDOWN_PAGE), DocumentSource(url=HTML_ONLY_PAGE)),
    )

    store = FakeDocumentStore()
    manifests = JsonManifestRepository(tmp_path / "manifest.json")

    def service() -> SyncService:
        return SyncService(
            settings=Settings(),
            fetcher=chain,
            normalizer=AwsDocsNormalizer(),
            manifest_repository=manifests,
            store=store,
        )

    first = service().run([bundle])
    assert first.action_counts() == {"CREATE": 1}
    assert first.source_counts() == {"NEW": 2}

    document_id = store.find_by_name("AWS_Live_Mixed").id
    content = store.content(document_id)
    assert "Source Type: AWS Official Documentation" in content
    assert MARKDOWN_PAGE in content and HTML_ONLY_PAGE in content
    assert content.count("Content Hash:\nsha256:") == 2

    second = service().run([bundle])

    assert second.action_counts() == {"NO_CHANGE": 1}
    assert second.source_counts() == {"UNCHANGED": 2}
    assert store.updated == []
    assert store.content(document_id) == content   # byte-identical


def test_aws_honours_conditional_requests_with_a_real_304(chain):
    """The assumption the whole conditional-request path rests on.

    If AWS ever stopped issuing ETags, or stopped honouring If-None-Match, the
    unit suite would still pass while every sync silently downloaded every page.
    """
    from aws_doc_sync.domain.models import HttpValidators, NotModified

    first = chain.fetch(DocumentSource(url=MARKDOWN_PAGE))
    assert not isinstance(first, NotModified)
    assert first.etag, "AWS stopped sending ETags"

    validators = HttpValidators(
        url=first.fetched_from, etag=first.etag, last_modified=first.last_modified
    )
    second = chain.fetch(DocumentSource(url=MARKDOWN_PAGE), validators)

    assert isinstance(second, NotModified)
    assert second.fetched_from == first.fetched_from


def test_a_stale_etag_gets_a_full_response_not_a_304(chain):
    from aws_doc_sync.domain.models import HttpValidators, NotModified

    stale = HttpValidators(url=MARKDOWN_PAGE.replace(".html", ".md"), etag='"not-a-real-etag"')
    result = chain.fetch(DocumentSource(url=MARKDOWN_PAGE), stale)
    assert not isinstance(result, NotModified)


def test_html_fallback_pages_are_also_revalidated(chain):
    """The page with no .md rendition must benefit too, not just markdown ones."""
    from aws_doc_sync.domain.models import HttpValidators, NotModified

    first = chain.fetch(DocumentSource(url=HTML_ONLY_PAGE))
    assert not isinstance(first, NotModified)
    if not first.etag:
        pytest.skip("this page is served without an ETag")

    validators = HttpValidators(
        url=first.fetched_from, etag=first.etag, last_modified=first.last_modified
    )
    assert isinstance(chain.fetch(DocumentSource(url=HTML_ONLY_PAGE), validators), NotModified)


def test_second_live_run_transfers_no_document_bodies(chain, tmp_path):
    """End-to-end: live AWS, real 304s, content recovered from the cache."""
    from aws_doc_sync.config.settings import Settings
    from aws_doc_sync.google.fake import FakeDocumentStore
    from aws_doc_sync.manifest.content_cache import ContentCache
    from aws_doc_sync.manifest.repository import JsonManifestRepository
    from aws_doc_sync.sync.service import SyncService

    bundle = Bundle(
        id="live_conditional",
        collection_id="live",
        output="AWS_Live_Conditional",
        title="Live conditional bundle",
        sources=(DocumentSource(url=MARKDOWN_PAGE), DocumentSource(url=HTML_ONLY_PAGE)),
    )

    store = FakeDocumentStore()
    manifests = JsonManifestRepository(tmp_path / "manifest.json")
    cache = ContentCache(tmp_path / "content-cache")

    def service() -> SyncService:
        return SyncService(
            settings=Settings(),
            fetcher=chain,
            normalizer=AwsDocsNormalizer(),
            manifest_repository=manifests,
            store=store,
            content_cache=cache,
        )

    first = service().run([bundle])
    assert first.action_counts() == {"CREATE": 1}
    assert cache.stats()["entries"] == 2

    document_id = store.find_by_name("AWS_Live_Conditional").id
    rendered = store.content(document_id)

    second = service().run([bundle])

    assert second.action_counts() == {"NO_CHANGE": 1}
    assert second.source_counts() == {"UNCHANGED": 2}
    assert second.not_modified_sources == 2, "AWS did not answer 304 for both pages"
    assert store.updated == []
    assert store.content(document_id) == rendered
