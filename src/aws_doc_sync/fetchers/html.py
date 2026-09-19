"""Priority-3 backend: HTML fallback.

Used only when no Markdown rendition is available. Several real pages need it --
for example ``AlarmThatSendsEmail.html`` exists while ``AlarmThatSendsEmail.md``
returns 404.

This module only *retrieves*; turning HTML into Markdown is the normalizer's job.
Keeping the two apart means the conversion rules can be unit-tested against
fixture HTML with no network at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..domain.errors import FetchError
from ..domain.models import (
    ContentKind,
    DocumentSource,
    FetchStrategy,
    HttpValidators,
    NotModified,
    RawDocument,
)
from ..domain.urls import html_url_for
from ..logging_setup import get_logger
from .http_client import HttpClient, refresh_validators

log = get_logger("fetch.html")


class HtmlDocumentFetcher:
    """Fetches the rendered HTML page."""

    name = "html"
    strategy = FetchStrategy.HTML

    def __init__(self, client: HttpClient) -> None:
        self._client = client

    def supports(self, source: DocumentSource) -> bool:
        return source.strategy in (FetchStrategy.AUTO, FetchStrategy.HTML)

    def fetch(
        self, source: DocumentSource, validators: HttpValidators | None = None
    ) -> RawDocument | NotModified:
        url = html_url_for(source.url)
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
        if content_type and not content_type.startswith(("text/html", "application/xhtml")):
            raise FetchError(
                f"expected HTML but got Content-Type {content_type!r} for {url}",
                source_url=source.url,
            )

        text = response.text
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
            kind=ContentKind.HTML,
            fetched_from=url,
            fetcher=self.name,
            retrieved_at=datetime.now(UTC),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )
