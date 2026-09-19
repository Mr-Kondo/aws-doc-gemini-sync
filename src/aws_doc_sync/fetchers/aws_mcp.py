"""Priority-2 backend: AWS Labs' AWS Documentation MCP Server.

The server exposes ``read_documentation(url, max_length=5000, start_index=0)``,
which fetches an AWS documentation page and returns Markdown.

Three properties of that tool, verified against the live server, shape this
module -- each of them silently loses content if ignored:

* **``max_length`` defaults to 5000 characters.** A caller that does not pass it
  receives the first 5000 characters of every page and no indication that the
  rest exists. For a pipeline whose entire purpose is to preserve documentation
  verbatim, that is the worst possible failure: quiet, total, and reported as
  success. This module always passes an explicit ``max_length`` and keeps
  paginating with ``start_index`` until the server says there is nothing left.
* **Every chunk is prefixed** with ``AWS Documentation from <url>:``. That is a
  wrapper the tool adds, not page content, so it is stripped from each chunk.
  Leaving it in would also make the content hash disagree with the Markdown
  backend for byte-identical documentation.
* **Exhaustion is signalled in-band** as ``<e>No more content available.</e>``,
  not by an empty response.

Design constraints this module also satisfies:

* The pipeline must run on a machine with no MCP runtime, so the ``mcp`` SDK is
  imported lazily and the backend is disabled by default.
* MCP is a *backend*, not an architecture. Everything above sees ``DocumentFetcher``.

The SDK is async and the pipeline is synchronous. One stdio session is held open
on a dedicated event-loop thread, which avoids paying a server process spawn per
page (measured: 5.5s for the first fetch, 0.2s for the next on the same session).
"""

from __future__ import annotations

import asyncio
import re
import threading
from concurrent.futures import Future
from datetime import UTC, datetime
from typing import Any

from ..domain.errors import FetchError
from ..domain.models import (
    ContentKind,
    DocumentSource,
    FetchStrategy,
    HttpValidators,
    RawDocument,
)
from ..domain.urls import html_url_for
from ..logging_setup import get_logger

log = get_logger("fetch.mcp")

#: The wrapper the tool puts in front of every chunk: ``...<url>:`` then exactly
#: one blank line. The trailing newlines are matched exactly rather than with
#: ``\s*\n+``: a greedy match also eats a newline that belongs to the page when a
#: chunk boundary lands on one, losing a character per boundary. If the server
#: ever changes the separator this stops matching, which leaves a visible stray
#: line in the document -- far better than silently deleting content.
PREAMBLE_RE = re.compile(r"^AWS Documentation from\s+\S+:[ \t]*\r?\n\r?\n", re.IGNORECASE)

#: In-band end-of-content marker.
NO_MORE_CONTENT = "<e>No more content available.</e>"
NO_MORE_RE = re.compile(r"\s*<e>\s*No more content available\.\s*</e>\s*$", re.IGNORECASE)

#: A truncated chunk ends with a marker carrying the *authoritative* next offset:
#: ``<e>Content truncated. Call the read_documentation tool with start_index=5000
#: to get more content.</e>``. Pagination follows that number rather than adding
#: up received lengths -- the marker is part of the response, so a chunk is
#: longer than the content it carries, and advancing by the response length skips
#: real text. The leading ``\s*`` matters too: the server inserts a blank line
#: before the marker. Exactly that blank line is consumed -- not ``\s*`` -- so a
#: boundary that lands where the page itself ends in a newline keeps it. Greedy
#: whitespace stripping there costs one character per boundary, which is both
#: invisible and enough to make the content hash depend on ``max_length``.
TRUNCATED_RE = re.compile(
    r"(?:\r?\n\r?\n)?<e>\s*Content truncated\.\s*Call the read_documentation tool with\s+"
    r"start_index=(\d+)\s+to get more content\.\s*</e>\s*$",
    re.IGNORECASE,
)

#: The tool's own ceiling is exclusive of 1_000_000.
MAX_LENGTH_CEILING = 999_999

#: Refuse to loop forever if the server never signals exhaustion.
MAX_CHUNKS = 200


class _SessionThread:
    """Owns the MCP session on its own event loop.

    The session is opened and closed **inside a single task**. anyio cancel
    scopes may only be exited by the task that entered them, so closing an
    exit stack from a different task raises "Attempted to exit cancel scope in a
    different task than it was entered in" and leaves the subprocess behind.
    """

    def __init__(self, params: Any, startup_timeout: float) -> None:
        self._params = params
        self._startup_timeout = startup_timeout
        self._loop = asyncio.new_event_loop()
        self._stop: asyncio.Event | None = None
        self._session: Any = None
        self._finished = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="aws-doc-sync-mcp", daemon=True
        )

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> Any:
        ready: Future = Future()
        self._thread.start()
        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(self._serve(ready))
        )
        self._session = ready.result(timeout=self._startup_timeout)
        return self._session

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _serve(self, ready: Future) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(self._params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                ready.set_result(session)
                await self._stop.wait()
        except Exception as exc:  # surfaced to the caller through `ready`
            if not ready.done():
                ready.set_exception(exc)
            else:
                log.warning("mcp_session_ended", extra={"reason": str(exc)})
        finally:
            self._finished.set()

    def call(self, coro: Any, timeout: float) -> Any:
        future: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
            self._finished.wait(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._loop.is_closed():
            self._loop.close()
        self._session = None


class AwsDocumentationMcpFetcher:
    """Retrieves page Markdown through the AWS Documentation MCP Server."""

    name = "aws_mcp"
    strategy = FetchStrategy.MCP

    def __init__(
        self,
        *,
        command: str = "uvx",
        args: tuple[str, ...] = ("awslabs.aws-documentation-mcp-server@latest",),
        tool_name: str = "read_documentation",
        env: dict[str, str] | None = None,
        startup_timeout_seconds: float = 60.0,
        request_timeout_seconds: float = 120.0,
        min_chars: int = 200,
        max_length: int = 100_000,
    ) -> None:
        self._command = command
        self._args = list(args)
        self._tool_name = tool_name
        self._env = env or {"FASTMCP_LOG_LEVEL": "ERROR", "AWS_DOCUMENTATION_PARTITION": "aws"}
        self._startup_timeout = startup_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._min_chars = min_chars
        self._max_length = min(max(1, max_length), MAX_LENGTH_CEILING)

        self._thread: _SessionThread | None = None
        self._session: Any = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------------

    def _connect(self) -> None:
        try:
            from mcp import StdioServerParameters
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise FetchError(
                "the AWS Documentation MCP backend requires the 'mcp' package "
                "(install with: uv sync --extra mcp), or set mcp.enabled: false"
            ) from exc

        params = StdioServerParameters(command=self._command, args=self._args, env=self._env)
        thread = _SessionThread(params, self._startup_timeout)
        try:
            self._session = thread.start()
        except Exception as exc:
            thread.close()
            raise FetchError(f"could not start MCP server {self._command}: {exc}") from exc

        self._thread = thread
        log.info("mcp_session_started", extra={"command": self._command})

    def close(self) -> None:
        with self._lock:
            if self._thread is None:
                return
            self._thread.close()
            self._thread = None
            self._session = None
        log.info("mcp_session_closed")

    # -- fetcher protocol --------------------------------------------------------

    def supports(self, source: DocumentSource) -> bool:
        return source.strategy in (FetchStrategy.AUTO, FetchStrategy.MCP)

    def fetch(
        self, source: DocumentSource, validators: HttpValidators | None = None
    ) -> RawDocument:
        """Retrieve through the MCP server.

        ``validators`` are accepted and ignored: the tool exposes no conditional
        request surface, so this backend always returns a document and never
        ``NotModified``.
        """
        url = html_url_for(source.url)

        with self._lock:
            if self._session is None:
                self._connect()
            session, thread = self._session, self._thread

        if session is None or thread is None:  # pragma: no cover - defensive
            raise FetchError("MCP session unavailable", source_url=source.url)

        text = self._read_whole_document(session, thread, url, source)

        if len(text.strip()) < self._min_chars:
            raise FetchError(
                f"MCP returned {len(text.strip())} chars (< {self._min_chars}) for {url}",
                source_url=source.url,
            )

        log.info(
            "source_fetch_completed",
            extra={
                "source_url": source.url,
                "fetcher": self.name,
                "fetched_from": url,
                "bytes": len(text),
            },
        )
        return RawDocument(
            source=source,
            content=text,
            kind=ContentKind.MARKDOWN,
            fetched_from=url,
            fetcher=self.name,
            retrieved_at=datetime.now(UTC),
        )

    def _read_whole_document(
        self, session: Any, thread: _SessionThread, url: str, source: DocumentSource
    ) -> str:
        """Page through the document until the server reports exhaustion.

        Both the continuation offset and the end of the document come from the
        server's own in-band markers; no arithmetic on received lengths is done
        anywhere. That is not fastidiousness. A truncated chunk *ends with* a
        marker, so its length exceeds the content it carries, and advancing by
        that length skips a hundred characters of real documentation at every
        boundary while splicing the marker text into the page -- a corruption
        that preserves the total length, and so hides from every check that only
        counts characters.

        Never returns a partial document quietly: either the server signals the
        end, or this raises.
        """
        chunks: list[str] = []
        start_index = 0
        seen_offsets: set[int] = set()

        for _ in range(MAX_CHUNKS):
            chunk = self._read_chunk(session, thread, url, source, start_index)
            if NO_MORE_RE.search(chunk) or not chunk:
                break

            truncated = TRUNCATED_RE.search(chunk)
            if truncated is None:
                chunks.append(chunk)  # the final chunk carries no marker
                break

            chunks.append(chunk[: truncated.start()])
            start_index = int(truncated.group(1))
            if start_index in seen_offsets:
                raise FetchError(
                    f"MCP pagination stalled at start_index={start_index} for {url}",
                    source_url=source.url,
                )
            seen_offsets.add(start_index)
        else:
            raise FetchError(
                f"MCP pagination did not terminate after {MAX_CHUNKS} chunks for {url}",
                source_url=source.url,
            )

        if len(chunks) > 1:
            log.info(
                "mcp_document_paginated",
                extra={"source_url": source.url, "chunks": len(chunks)},
            )
        return "".join(chunks)

    def _read_chunk(
        self,
        session: Any,
        thread: _SessionThread,
        url: str,
        source: DocumentSource,
        start_index: int,
    ) -> str:
        """One ``read_documentation`` call, with the preamble removed.

        Any in-band markers are left in place; deciding what they mean is the
        caller's job.
        """
        arguments = {
            "url": url,
            "max_length": self._max_length,
            "start_index": start_index,
        }
        try:
            result = thread.call(
                session.call_tool(self._tool_name, arguments),
                timeout=self._request_timeout,
            )
        except Exception as exc:
            raise FetchError(
                f"MCP call_tool failed for {url}: {exc}", source_url=source.url
            ) from exc

        # The SDK renamed this field; check both so an error is never missed.
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            detail = _extract_text(result, source_url=source.url)[:200]
            raise FetchError(
                f"MCP server reported an error for {url}: {detail}",
                source_url=source.url,
            )

        return _strip_preamble(_extract_text(result, source_url=source.url))


def _strip_preamble(text: str) -> str:
    """Remove the ``AWS Documentation from <url>:`` wrapper the tool prepends."""
    return PREAMBLE_RE.sub("", text, count=1)


def _extract_text(result: Any, *, source_url: str | None = None) -> str:
    """Flatten an MCP ``CallToolResult`` into text.

    Tolerant of shape differences across SDK versions: reads ``content`` items
    that carry ``.text``, and falls back to ``structuredContent``.
    """
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    if parts:
        return "\n".join(parts)

    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if isinstance(structured, dict):
        for key in ("result", "text", "content", "markdown"):
            value = structured.get(key)
            if isinstance(value, str):
                return value
    if isinstance(result, str):
        return result
    raise FetchError("MCP result contained no text content", source_url=source_url)
