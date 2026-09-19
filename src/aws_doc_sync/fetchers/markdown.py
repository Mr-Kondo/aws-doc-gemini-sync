"""Priority-1 backend: AWS Documentation's native Markdown endpoint.

docs.aws.amazon.com serves a Markdown rendition of most guide pages at the same
path with a ``.md`` suffix. Using it avoids an HTML->Markdown conversion step
entirely, which is the single biggest source of fidelity loss for code blocks and
tables.

The rewrite is *not* universal, so every response is validated three ways before
it is accepted: HTTP status, Content-Type, and a minimum body size. A missing
``.md`` sibling is served as an HTML 404 page, which the status check catches;
the other two guard against stub pages that exist but contain only a table of
contents.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..domain.errors import FetchError, NotFoundError
from ..domain.models import (
    ContentKind,
    DocumentSource,
    FetchStrategy,
    HttpValidators,
    NotModified,
    RawDocument,
)
from ..domain.urls import markdown_url_for
from ..logging_setup import get_logger
from .http_client import HttpClient, refresh_validators

log = get_logger("fetch.markdown")

_MARKDOWN_CONTENT_TYPES = ("text/markdown", "text/plain", "text/x-markdown")


class MarkdownDocumentFetcher:
    """Fetches the ``.md`` rendition of an AWS documentation page."""

    name = "markdown"
    strategy = FetchStrategy.MARKDOWN

    def __init__(
        self,
        client: HttpClient,
        *,
        require_content_type: bool = True,
        min_chars: int = 200,
    ) -> None:
        self._client = client
        self._require_content_type = require_content_type
        self._min_chars = min_chars

    def supports(self, source: DocumentSource) -> bool:
        if source.strategy not in (FetchStrategy.AUTO, FetchStrategy.MARKDOWN):
            return False
        return bool(source.markdown_url or markdown_url_for(source.url))

    def fetch(
        self, source: DocumentSource, validators: HttpValidators | None = None
    ) -> RawDocument | NotModified:
        url = source.markdown_url or markdown_url_for(source.url)
        if not url:
            raise FetchError(
                f"no markdown endpoint can be derived from {source.url}", source_url=source.url
            )

        response = self._client.get(url, validators=validators)

        if response.status_code == 304:
            log.info(
                "source_not_modified",
                extra={"source_url": source.url, "fetcher": self.name, "fetched_from": url},
            )
            return NotModified(
                source=source,
                fetched_from=url,
                fetcher=self.name,
                checked_at=datetime.now(UTC),
                validators=refresh_validators(response, url, validators),
            )

        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()

        if self._require_content_type and content_type not in _MARKDOWN_CONTENT_TYPES:
            # A 200 with text/html here means AWS served a page shell, not markdown.
            raise NotFoundError(
                f"markdown endpoint returned Content-Type {content_type!r} for {url}",
                source_url=source.url,
            )

        text = response.text
        if len(text.strip()) < self._min_chars:
            raise NotFoundError(
                f"markdown endpoint returned {len(text.strip())} chars "
                f"(< {self._min_chars}) for {url}; treating as a stub",
                source_url=source.url,
            )

        log.info(
            "source_fetch_completed",
            extra={
                "source_url": source.url,
                "fetcher": self.name,
                "fetched_from": url,
                "bytes": len(text),
                "status": response.status_code,
            },
        )
        return RawDocument(
            source=source,
            content=text,
            kind=ContentKind.MARKDOWN,
            fetched_from=url,
            fetcher=self.name,
            retrieved_at=datetime.now(UTC),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )

