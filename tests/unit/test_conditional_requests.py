"""Conditional requests: If-None-Match / If-Modified-Since and HTTP 304.

The behaviour being protected here is narrow but easy to get wrong: a validator
belongs to one resource, a 304 is a success and not a failure, and a 304 must not
send the fetcher chain looking for another backend.
"""

from __future__ import annotations

import httpx
import pytest

from aws_doc_sync.domain.errors import FetchError
from aws_doc_sync.domain.models import HttpValidators, NotModified, RawDocument
from aws_doc_sync.fetchers.chain import FallbackFetcher
from aws_doc_sync.fetchers.html import HtmlDocumentFetcher
from aws_doc_sync.fetchers.http_client import refresh_validators, validators_from
from aws_doc_sync.fetchers.markdown import MarkdownDocumentFetcher
from tests.conftest import html_404, make_source, markdown_response, mock_client

PAGE = "https://docs.aws.amazon.com/example/latest/dg/page.html"
MD = "https://docs.aws.amazon.com/example/latest/dg/page.md"
BODY = "# Page title\n\n" + ("Documentation content. " * 20)


# -- HttpValidators ------------------------------------------------------------------


def test_headers_are_built_per_rfc_9110():
    validators = HttpValidators(url=MD, etag='"abc"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT")
    assert validators.headers() == {
        "If-None-Match": '"abc"',
        "If-Modified-Since": "Wed, 21 Oct 2026 07:28:00 GMT",
    }


def test_validators_without_either_field_are_not_usable():
    assert HttpValidators(url=MD).usable is False
    assert HttpValidators(url=MD).applies_to(MD) is False


def test_validators_apply_only_to_the_url_that_issued_them():
    """An ETag identifies a representation of one resource.

    The same page is reachable at page.md and page.html, and those are different
    resources with different ETags.
    """
    validators = HttpValidators(url=MD, etag='"abc"')
    assert validators.applies_to(MD) is True
    assert validators.applies_to(PAGE) is False


def test_validators_are_read_off_a_response():
    response = httpx.Response(200, headers={"ETag": '"x"', "Last-Modified": "yesterday"})
    read = validators_from(response, MD)
    assert (read.url, read.etag, read.last_modified) == (MD, '"x"', "yesterday")


def test_a_304_carrying_a_new_etag_refreshes_the_stored_one():
    sent = HttpValidators(url=MD, etag='"old"')
    response = httpx.Response(304, headers={"ETag": '"new"'})
    assert refresh_validators(response, MD, sent).etag == '"new"'


def test_a_304_without_an_etag_keeps_the_one_that_was_sent():
    # Otherwise the next request would silently stop being conditional forever.
    sent = HttpValidators(url=MD, etag='"old"')
    assert refresh_validators(httpx.Response(304), MD, sent).etag == '"old"'


# -- HttpClient ----------------------------------------------------------------------


def test_client_sends_conditional_headers_when_validators_apply():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(304)

    client = mock_client(handler)
    response = client.get(MD, validators=HttpValidators(url=MD, etag='"abc"'))

    assert response.status_code == 304
    assert seen[0]["if-none-match"] == '"abc"'


def test_client_omits_conditional_headers_for_a_different_url():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return markdown_response(BODY)

    client = mock_client(handler)
    client.get(MD, validators=HttpValidators(url=PAGE, etag='"abc"'))
    assert "if-none-match" not in seen[0]


def test_client_sends_nothing_extra_without_validators():
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return markdown_response(BODY)

    mock_client(handler).get(MD)
    assert "if-none-match" not in seen[0]
    assert "if-modified-since" not in seen[0]


def test_a_304_is_never_retried():
    """It is the answer to the question, not a failure."""
    sleeps: list[float] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(304)

    client = mock_client(handler, max_retries=3, sleeps=sleeps)
    client.get(MD, validators=HttpValidators(url=MD, etag='"abc"'))
    assert calls["n"] == 1
    assert sleeps == []


def test_an_unrequested_304_is_still_treated_as_wrong():
    # Nothing should produce this, but a silent empty body would be far worse
    # than a loud error.
    client = mock_client(lambda r: httpx.Response(304), max_retries=0)
    with pytest.raises(FetchError, match="304"):
        client.get(MD)


# -- fetchers ------------------------------------------------------------------------


def test_markdown_fetcher_reports_not_modified():
    client = mock_client(lambda r: httpx.Response(304, headers={"ETag": '"v2"'}))
    fetcher = MarkdownDocumentFetcher(client)
    result = fetcher.fetch(make_source(PAGE), HttpValidators(url=MD, etag='"v1"'))

    assert isinstance(result, NotModified)
    assert result.fetched_from == MD
    assert result.fetcher == "markdown"
    assert result.validators.etag == '"v2"'


def test_html_fetcher_reports_not_modified():
    fetcher = HtmlDocumentFetcher(mock_client(lambda r: httpx.Response(304)))
    result = fetcher.fetch(make_source(PAGE), HttpValidators(url=PAGE, etag='"v1"'))

    assert isinstance(result, NotModified)
    assert result.fetched_from == PAGE
    assert result.fetcher == "html"


def test_fetchers_return_a_document_when_the_content_changed():
    fetcher = MarkdownDocumentFetcher(mock_client(lambda r: markdown_response(BODY)))
    result = fetcher.fetch(make_source(PAGE), HttpValidators(url=MD, etag='"stale"'))
    assert isinstance(result, RawDocument)


# -- chain ---------------------------------------------------------------------------


def test_chain_stops_at_a_not_modified_answer():
    """Falling through would discard the saving the conditional request earned."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(304)

    client = mock_client(handler)
    chain = FallbackFetcher([MarkdownDocumentFetcher(client), HtmlDocumentFetcher(client)])
    result = chain.fetch(make_source(PAGE), HttpValidators(url=MD, etag='"v1"'))

    assert isinstance(result, NotModified)
    assert calls == [MD]  # the HTML backend was never consulted


def test_html_validators_survive_a_markdown_404_and_are_used_on_fallback():
    """The realistic fallback case for a page with no .md rendition.

    The stored validators describe page.html, so the .md probe goes out
    unconditionally and the .html request carries If-None-Match.
    """
    conditional_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "if-none-match" in request.headers:
            conditional_urls.append(url)
        if url.endswith(".md"):
            return html_404()
        return httpx.Response(304)

    client = mock_client(handler)
    chain = FallbackFetcher([MarkdownDocumentFetcher(client), HtmlDocumentFetcher(client)])
    result = chain.fetch(make_source(PAGE), HttpValidators(url=PAGE, etag='"v1"'))

    assert isinstance(result, NotModified)
    assert conditional_urls == [PAGE]
