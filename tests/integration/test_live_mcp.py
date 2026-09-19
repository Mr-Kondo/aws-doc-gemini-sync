"""Live checks against the AWS Documentation MCP Server.

Deselected by default. Requires the optional extra and a working ``uvx``::

    uv sync --extra mcp
    pytest -m integration tests/integration/test_live_mcp.py

These exist because the tool's defaults are hostile to this pipeline: omit
``max_length`` and every page comes back truncated at 5000 characters, with no
error and no indication that anything is missing. A unit test with a fake server
cannot notice AWS changing that default; this can.
"""

from __future__ import annotations

import pytest

from aws_doc_sync.domain.models import ContentKind, DocumentSource
from aws_doc_sync.fetchers.http_client import HttpClient, RetryPolicy
from aws_doc_sync.fetchers.markdown import MarkdownDocumentFetcher
from aws_doc_sync.normalize.aws_docs import AwsDocsNormalizer

pytestmark = pytest.mark.integration

pytest.importorskip("mcp", reason="install the optional extra: uv sync --extra mcp")

SHORT_PAGE = "https://docs.aws.amazon.com/sagemaker/latest/dg/how-it-works-training.html"
LONG_PAGE = "https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html"


@pytest.fixture(scope="module")
def mcp_fetcher():
    from aws_doc_sync.fetchers.aws_mcp import AwsDocumentationMcpFetcher

    fetcher = AwsDocumentationMcpFetcher(max_length=100_000)
    try:
        yield fetcher
    finally:
        fetcher.close()


def test_the_session_starts_and_returns_markdown(mcp_fetcher):
    raw = mcp_fetcher.fetch(DocumentSource(url=SHORT_PAGE))
    assert raw.kind is ContentKind.MARKDOWN
    assert raw.fetcher == "aws_mcp"


def test_the_tool_wrapper_never_reaches_the_content(mcp_fetcher):
    raw = mcp_fetcher.fetch(DocumentSource(url=SHORT_PAGE))
    assert not raw.content.lstrip().startswith("AWS Documentation from")
    assert "No more content available" not in raw.content


def test_a_long_page_comes_back_whole(mcp_fetcher):
    """The truncation regression, measured against the Markdown endpoint.

    5000 characters is the tool's default page size. Anything near it means the
    pagination broke and content is being lost silently.
    """
    raw = mcp_fetcher.fetch(DocumentSource(url=LONG_PAGE))

    with HttpClient(policy=RetryPolicy(max_retries=2), requests_per_second=2.0) as http:
        reference = MarkdownDocumentFetcher(http).fetch(DocumentSource(url=LONG_PAGE))

    assert len(raw.content) > 20_000, "far shorter than the page: pagination is broken"
    assert len(raw.content) >= len(reference.content) * 0.85


def test_the_result_normalizes_like_any_other_backend(mcp_fetcher):
    document = AwsDocsNormalizer().normalize(mcp_fetcher.fetch(DocumentSource(url=SHORT_PAGE)))
    assert document.title
    assert document.content_hash.startswith("sha256:")
    assert document.fetcher == "aws_mcp"


def test_the_session_is_reused_across_fetches(mcp_fetcher):
    """One server process for the whole run, not one per page."""
    import time

    mcp_fetcher.fetch(DocumentSource(url=SHORT_PAGE))  # warm
    start = time.monotonic()
    mcp_fetcher.fetch(DocumentSource(url=SHORT_PAGE))
    assert time.monotonic() - start < 5.0


def test_an_unsupported_url_is_reported_as_an_error(mcp_fetcher):
    from aws_doc_sync.domain.errors import FetchError

    with pytest.raises(FetchError):
        mcp_fetcher.fetch(DocumentSource(url="https://example.com/not-aws.html"))


@pytest.mark.parametrize("page_size", [1_500, 5_000, 500_000])
def test_assembly_is_identical_at_every_page_size(page_size):
    """The content hash must not depend on a tuning knob.

    Three separate defects hid behind length-based checks here: marker text
    spliced into the page, content skipped because a truncated response is
    longer than the content it carries, and a newline eaten at each boundary.
    Comparing a heavily paginated read against a single-call read is what
    surfaces all of them, because the corruption preserved the total length.
    """
    from aws_doc_sync.fetchers.aws_mcp import AwsDocumentationMcpFetcher

    reference = AwsDocumentationMcpFetcher(max_length=500_000)
    paged = AwsDocumentationMcpFetcher(max_length=page_size)
    try:
        whole = reference.fetch(DocumentSource(url=LONG_PAGE)).content
        chunked = paged.fetch(DocumentSource(url=LONG_PAGE)).content
    finally:
        reference.close()
        paged.close()

    assert chunked == whole
    assert "<e>" not in chunked, "an in-band protocol marker reached the document"


def test_no_protocol_marker_survives_into_the_content(mcp_fetcher):
    content = mcp_fetcher.fetch(DocumentSource(url=LONG_PAGE)).content
    for marker in ("<e>", "Content truncated", "No more content", "read_documentation"):
        assert marker not in content
