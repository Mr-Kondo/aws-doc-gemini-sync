"""Fetcher priority chain.

Implements the acquisition order (Markdown -> MCP -> HTML) as composition rather
than as branching inside a fetcher, so adding or reordering a backend is a config
change and never an edit to retrieval logic.

Fall-through rule: only a *this backend cannot serve this page* failure advances
the chain. A transient network failure has already been retried by the transport;
letting it fall through would silently downgrade a page from Markdown to
converted HTML and produce a spurious content-hash change.

A ``NotModified`` answer ends the chain immediately. It is a successful outcome,
and trying the next backend after one would throw away the saving the conditional
request just earned.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..config.settings import Settings
from ..domain.errors import FetchError, NotFoundError
from ..domain.models import (
    DocumentSource,
    FetchStrategy,
    HttpValidators,
    NotModified,
    RawDocument,
)
from ..domain.protocols import DocumentFetcher
from ..logging_setup import get_logger
from .html import HtmlDocumentFetcher
from .http_client import HttpClient
from .markdown import MarkdownDocumentFetcher

log = get_logger("fetch.chain")


class FallbackFetcher:
    """Tries each backend in order until one produces a document."""

    name = "chain"

    def __init__(self, fetchers: Sequence[DocumentFetcher]) -> None:
        if not fetchers:
            raise ValueError("FallbackFetcher requires at least one backend")
        self._fetchers = list(fetchers)

    @property
    def backends(self) -> list[str]:
        return [f.name for f in self._fetchers]

    def supports(self, source: DocumentSource) -> bool:
        return any(f.supports(source) for f in self._fetchers)

    def fetch(
        self, source: DocumentSource, validators: HttpValidators | None = None
    ) -> RawDocument | NotModified:
        log.info(
            "source_fetch_started",
            extra={
                "source_url": source.url,
                "strategy": source.strategy.value,
                "conditional": validators is not None and validators.usable,
            },
        )

        applicable = [f for f in self._fetchers if f.supports(source)]
        if not applicable:
            raise FetchError(
                f"no fetch backend supports strategy '{source.strategy.value}' for {source.url}",
                source_url=source.url,
            )

        attempts: list[str] = []
        last_error: Exception | None = None

        for index, fetcher in enumerate(applicable):
            is_last = index == len(applicable) - 1
            try:
                return fetcher.fetch(source, validators)
            except NotFoundError as exc:
                # "Not available from this backend" -- the one case that should
                # advance the chain.
                attempts.append(f"{fetcher.name}:not_found")
                last_error = exc
                if not is_last:
                    log.info(
                        "source_fetch_fallback",
                        extra={
                            "source_url": source.url,
                            "from_fetcher": fetcher.name,
                            "to_fetcher": applicable[index + 1].name,
                            "reason": str(exc),
                        },
                    )
                    continue
            except FetchError as exc:
                attempts.append(f"{fetcher.name}:error")
                last_error = exc
                if not is_last:
                    log.warning(
                        "source_fetch_backend_failed",
                        extra={
                            "source_url": source.url,
                            "fetcher": fetcher.name,
                            "reason": str(exc),
                        },
                    )
                    continue

        message = f"all fetch backends failed for {source.url} (tried: {', '.join(attempts)})"
        if isinstance(last_error, NotFoundError) and all(
            a.endswith(":not_found") for a in attempts
        ):
            # Every backend agrees the page is gone -> orphan candidate, not a bug.
            raise NotFoundError(message, source_url=source.url)
        raise FetchError(message, source_url=source.url)


def build_fetcher_chain(settings: Settings, *, client: HttpClient) -> FallbackFetcher:
    """Assemble the chain described by ``settings.fetch.strategy_order``."""
    built: list[DocumentFetcher] = []
    for strategy in settings.fetch.strategy_order:
        if strategy is FetchStrategy.MARKDOWN:
            built.append(
                MarkdownDocumentFetcher(
                    client,
                    require_content_type=settings.fetch.require_markdown_content_type,
                    min_chars=settings.fetch.min_markdown_chars,
                )
            )
        elif strategy is FetchStrategy.HTML:
            built.append(HtmlDocumentFetcher(client))
        elif strategy is FetchStrategy.MCP:
            if not settings.app.mcp.enabled:
                continue
            from .aws_mcp import AwsDocumentationMcpFetcher

            built.append(
                AwsDocumentationMcpFetcher(
                    command=settings.app.mcp.command,
                    args=settings.app.mcp.args,
                    tool_name=settings.app.mcp.tool_name,
                    startup_timeout_seconds=settings.app.mcp.startup_timeout_seconds,
                    request_timeout_seconds=settings.app.mcp.request_timeout_seconds,
                    min_chars=settings.fetch.min_markdown_chars,
                    max_length=settings.app.mcp.max_length,
                )
            )

    if not built:
        raise ValueError("fetch.strategy_order produced no usable backends")
    return FallbackFetcher(built)
