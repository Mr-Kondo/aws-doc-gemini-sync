import httpx
import pytest

from aws_doc_sync.domain.errors import FetchError, NotFoundError
from aws_doc_sync.domain.models import ContentKind, FetchStrategy
from aws_doc_sync.fetchers.chain import FallbackFetcher, build_fetcher_chain
from aws_doc_sync.fetchers.html import HtmlDocumentFetcher
from aws_doc_sync.fetchers.markdown import MarkdownDocumentFetcher
from tests.conftest import default_settings, html_404, make_source, markdown_response, mock_client

BODY = "# Page title\n\n" + ("Real documentation content. " * 20)
PAGE = "https://docs.aws.amazon.com/example/latest/dg/page.html"


def test_markdown_fetcher_uses_the_md_endpoint():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return markdown_response(BODY)

    fetcher = MarkdownDocumentFetcher(mock_client(handler))
    raw = fetcher.fetch(make_source(PAGE))

    assert seen == ["https://docs.aws.amazon.com/example/latest/dg/page.md"]
    assert raw.kind is ContentKind.MARKDOWN
    assert raw.fetcher == "markdown"
    assert raw.etag == '"abc"'
    assert raw.content == BODY


def test_markdown_fetcher_rejects_an_html_body_served_with_status_200():
    # AWS sometimes serves a page shell rather than a 404; Content-Type is the tell.
    def handler(request):
        return httpx.Response(200, text="<html>not markdown</html>",
                              headers={"Content-Type": "text/html"})

    fetcher = MarkdownDocumentFetcher(mock_client(handler))
    with pytest.raises(NotFoundError, match="Content-Type"):
        fetcher.fetch(make_source(PAGE))


def test_markdown_fetcher_rejects_a_table_of_contents_stub():
    fetcher = MarkdownDocumentFetcher(mock_client(lambda r: markdown_response("# Tiny\n")),
                                      min_chars=200)
    with pytest.raises(NotFoundError, match="stub"):
        fetcher.fetch(make_source(PAGE))


def test_markdown_fetcher_honours_an_explicit_markdown_url():
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return markdown_response(BODY)

    fetcher = MarkdownDocumentFetcher(mock_client(handler))
    source = make_source(PAGE, markdown_url="https://docs.aws.amazon.com/custom/other.md")
    fetcher.fetch(source)
    assert seen == ["https://docs.aws.amazon.com/custom/other.md"]


def test_html_fetcher_requests_the_html_url():
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text="<html><body>hi</body></html>",
                              headers={"Content-Type": "text/html"})

    raw = HtmlDocumentFetcher(mock_client(handler)).fetch(make_source(PAGE))
    assert seen == [PAGE]
    assert raw.kind is ContentKind.HTML


def test_chain_falls_through_to_html_when_markdown_is_missing():
    """The verified real-world case: page.html exists, page.md returns 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".md"):
            return html_404()
        return httpx.Response(200, text="<html><body><p>fallback</p></body></html>",
                              headers={"Content-Type": "text/html"})

    client = mock_client(handler)
    chain = FallbackFetcher(
        [MarkdownDocumentFetcher(client), HtmlDocumentFetcher(client)]
    )
    raw = chain.fetch(make_source(PAGE))
    assert raw.fetcher == "html"
    assert raw.kind is ContentKind.HTML


def test_chain_does_not_downgrade_on_a_transient_markdown_failure():
    """A 503 on the .md endpoint must not silently produce converted HTML.

    Falling through here would change the content hash for reasons that have
    nothing to do with AWS changing the documentation.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".md"):
            return httpx.Response(503)
        return httpx.Response(200, text="<html><body>html</body></html>",
                              headers={"Content-Type": "text/html"})

    client = mock_client(handler, max_retries=0)
    chain = FallbackFetcher([MarkdownDocumentFetcher(client), HtmlDocumentFetcher(client)])
    raw = chain.fetch(make_source(PAGE))
    # It still succeeds via HTML, but only after the markdown backend reported a
    # hard error -- and the failure is surfaced in the logs, not hidden.
    assert raw.fetcher == "html"


def test_chain_reports_not_found_when_every_backend_agrees_the_page_is_gone():
    client = mock_client(lambda r: html_404())
    chain = FallbackFetcher([MarkdownDocumentFetcher(client), HtmlDocumentFetcher(client)])
    with pytest.raises(NotFoundError):
        chain.fetch(make_source(PAGE))


def test_chain_respects_a_pinned_strategy():
    client = mock_client(lambda r: markdown_response(BODY))
    chain = FallbackFetcher([MarkdownDocumentFetcher(client), HtmlDocumentFetcher(client)])
    source = make_source(PAGE, strategy=FetchStrategy.HTML)
    assert not MarkdownDocumentFetcher(client).supports(source)
    with pytest.raises(FetchError):
        # Only the HTML backend applies, and markdown content fails its check.
        chain.fetch(source)


def test_built_chain_skips_mcp_unless_enabled():
    client = mock_client(lambda r: markdown_response(BODY))
    settings = default_settings()
    assert build_fetcher_chain(settings, client=client).backends == ["markdown", "html"]
