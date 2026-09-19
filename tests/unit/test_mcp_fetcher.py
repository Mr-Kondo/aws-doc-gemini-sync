"""AWS Documentation MCP backend.

Driven through a fake session, so the default suite needs no MCP runtime and no
network. The behaviours pinned here are the ones that silently destroyed content
in the first implementation: an unspecified ``max_length`` truncating every page
at 5000 characters, a wrapper line leaking into the body, and an error field
whose name did not match the SDK's.
"""

from __future__ import annotations

import pytest

from aws_doc_sync.domain.errors import FetchError
from aws_doc_sync.domain.models import ContentKind, DocumentSource
from aws_doc_sync.fetchers.aws_mcp import (
    NO_MORE_CONTENT,
    AwsDocumentationMcpFetcher,
    _strip_preamble,
)

URL = "https://docs.aws.amazon.com/example/latest/dg/page.html"
PREAMBLE = f"AWS Documentation from {URL}:\n\n"


class FakeContent:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeResult:
    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.content = [FakeContent(text)]
        self.is_error = is_error


class FakeSession:
    """Mimics the real server, including the parts that are inconvenient.

    A truncated chunk does not simply end -- the server appends a blank line and
    a marker carrying the next offset:

        ...page text\n\n<e>Content truncated. Call the read_documentation tool
        with start_index=5000 to get more content.</e>

    A fake that returns clean slices hides three separate bugs at once: marker
    text spliced into the page, content skipped because the response is longer
    than the content it carries, and a newline eaten at every boundary. All three
    were shipped against a kinder fake than this one.
    """

    def __init__(
        self,
        body: str,
        *,
        page_size: int = 5000,
        is_error: bool = False,
    ) -> None:
        self.body = body
        self.page_size = page_size
        self.is_error = is_error
        self.calls: list[dict] = []

    def call_tool(self, name: str, arguments: dict):
        self.calls.append(dict(arguments))
        if self.is_error:
            return FakeResult("Error executing tool read_documentation", is_error=True)

        start = arguments.get("start_index", 0)
        if start >= len(self.body):
            return FakeResult(PREAMBLE + NO_MORE_CONTENT)

        limit = min(arguments.get("max_length", 5000), self.page_size)
        chunk = self.body[start : start + limit]
        following = start + len(chunk)
        if following < len(self.body):
            chunk += (
                f"\n\n<e>Content truncated. Call the read_documentation tool "
                f"with start_index={following} to get more content.</e>"
            )
        return FakeResult(PREAMBLE + chunk)


class FakeThread:
    def call(self, coro, timeout):
        return coro  # FakeSession.call_tool returns a value, not a coroutine


def fetcher_with(session, **kwargs) -> AwsDocumentationMcpFetcher:
    fetcher = AwsDocumentationMcpFetcher(**kwargs)
    fetcher._session = session
    fetcher._thread = FakeThread()
    return fetcher


def test_preamble_is_stripped():
    assert _strip_preamble(PREAMBLE + "# Title\n\nBody.") == "# Title\n\nBody."


def test_preamble_stripping_leaves_ordinary_content_alone():
    body = "# AWS Documentation from somewhere\n\nBody."
    assert _strip_preamble(body) == body


def test_max_length_is_always_sent_explicitly():
    """The regression that matters most.

    The tool's default is 5000 characters. Omitting the argument returns the
    first 5000 characters of every page with no indication the rest exists --
    silent, total content loss reported as success.
    """
    session = FakeSession("x" * 400, page_size=100_000)
    fetcher_with(session, max_length=100_000).fetch(DocumentSource(url=URL))

    assert session.calls[0]["max_length"] == 100_000
    assert "max_length" in session.calls[0]


def test_a_long_document_is_assembled_from_every_chunk():
    body = "".join(f"line {i:05d}\n" for i in range(2000))  # ~24k chars
    session = FakeSession(body, page_size=5000)

    raw = fetcher_with(session, max_length=5000).fetch(DocumentSource(url=URL))

    assert raw.content == body, "content was lost or duplicated across chunks"
    assert len(session.calls) > 1

    # start_index follows what was actually received, not what was requested --
    # the server returns more than max_length, so assuming otherwise would
    # re-read or skip text at every boundary.
    indices = [c["start_index"] for c in session.calls]
    assert indices[0] == 0
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)


def test_a_chunk_shorter_than_max_length_does_not_end_pagination():
    """The silent-truncation regression.

    Measured against the live server, a request for 5000 characters comes back
    with 5103. The returned length is not a function of the requested one, so
    treating "shorter than requested" as the end is a guess -- and being wrong
    about it drops the rest of the page with no error.
    """
    body = "b" * 12_000
    session = FakeSession(body, page_size=4_000)

    raw = fetcher_with(session, max_length=5_000).fetch(DocumentSource(url=URL))

    assert raw.content == body
    assert len(raw.content) == 12_000


def test_pagination_stops_on_the_end_sentinel():
    body = "y" * 10_000
    session = FakeSession(body, page_size=5000)
    raw = fetcher_with(session, max_length=5000).fetch(DocumentSource(url=URL))
    assert raw.content == body
    assert NO_MORE_CONTENT not in raw.content


def test_a_document_that_fits_costs_exactly_one_call():
    """No marker means no more content, so no confirmation round trip is needed.

    The end of the document is still the server's statement, not an inference
    from the chunk's length.
    """
    session = FakeSession("z" * 900, page_size=100_000)
    raw = fetcher_with(session, max_length=100_000).fetch(DocumentSource(url=URL))

    assert raw.content == "z" * 900
    assert len(session.calls) == 1


def test_the_result_is_markdown():
    raw = fetcher_with(FakeSession("m" * 900, page_size=100_000)).fetch(DocumentSource(url=URL))
    assert raw.kind is ContentKind.MARKDOWN
    assert raw.fetcher == "aws_mcp"


@pytest.mark.parametrize("field", ["is_error", "isError"])
def test_a_server_error_is_detected_under_either_field_name(field):
    """The SDK renamed this field; checking only one spelling hid every error."""

    class Result:
        def __init__(self):
            self.content = [FakeContent("Error executing tool read_documentation")]
            setattr(self, field, True)

    class Session:
        def call_tool(self, name, arguments):
            return Result()

    with pytest.raises(FetchError, match="reported an error"):
        fetcher_with(Session()).fetch(DocumentSource(url=URL))


def test_a_document_below_the_minimum_is_rejected():
    session = FakeSession("tiny", page_size=100_000)
    with pytest.raises(FetchError, match="chars"):
        fetcher_with(session, min_chars=200).fetch(DocumentSource(url=URL))


def test_pagination_that_never_terminates_raises_instead_of_looping():
    """A server that keeps advancing the offset forever must not hang the run."""

    class Endless:
        def __init__(self):
            self.offset = 0

        def call_tool(self, name, arguments):
            self.offset += 10
            return FakeResult(
                PREAMBLE + "q" * 10 + f"\n\n<e>Content truncated. Call the "
                f"read_documentation tool with start_index={self.offset} to get "
                f"more content.</e>"
            )

    with pytest.raises(FetchError, match="did not terminate"):
        fetcher_with(Endless(), max_length=10).fetch(DocumentSource(url=URL))


def test_max_length_is_clamped_to_the_tools_ceiling():
    fetcher = AwsDocumentationMcpFetcher(max_length=5_000_000)
    assert fetcher._max_length == 999_999


def test_validators_are_accepted_and_ignored():
    from aws_doc_sync.domain.models import HttpValidators

    session = FakeSession("v" * 900, page_size=100_000)
    raw = fetcher_with(session).fetch(
        DocumentSource(url=URL), HttpValidators(url=URL, etag='"x"')
    )
    # No conditional surface exists, so a document always comes back.
    assert raw.content == "v" * 900


def test_stripping_the_preamble_never_eats_page_content():
    """A chunk boundary can land on a newline.

    The preamble ends with a blank line, and the chunk that follows it can
    legitimately start with one too. Matching the separator greedily removes
    that leading newline, losing one character at every chunk boundary --
    invisible in a diff, and cumulative across a long document.
    """
    assert _strip_preamble(PREAMBLE + "\n# Heading") == "\n# Heading"
    assert _strip_preamble(PREAMBLE + "\n\nbody") == "\n\nbody"
    assert _strip_preamble(PREAMBLE + "   indented") == "   indented"


def test_content_is_reassembled_exactly_when_boundaries_fall_on_newlines():
    body = "".join(f"{i:04d}\n" for i in range(3000))  # every 5th char is a newline
    session = FakeSession(body, page_size=1_000)

    raw = fetcher_with(session, max_length=1_000).fetch(DocumentSource(url=URL))

    assert len(raw.content) == len(body)
    assert raw.content == body


def test_the_truncation_marker_never_reaches_the_document():
    """The marker is protocol, not documentation.

    Splicing it into the page corrupts the content *and* keeps the length
    plausible, so any check that only counts characters sees nothing wrong.
    """
    body = "c" * 12_000
    session = FakeSession(body, page_size=5_000)

    raw = fetcher_with(session, max_length=5_000).fetch(DocumentSource(url=URL))

    assert raw.content == body
    assert "<e>" not in raw.content
    assert "Content truncated" not in raw.content
    assert "read_documentation" not in raw.content


def test_pagination_follows_the_offset_the_server_supplies():
    """Advancing by the response length skips the marker's worth of real text."""
    body = "d" * 11_000
    session = FakeSession(body, page_size=4_000)

    fetcher_with(session, max_length=4_000).fetch(DocumentSource(url=URL))

    # 11,000 chars at 4,000 per page: the third chunk is the remainder and
    # carries no marker, so it ends the read without a further call.
    assert [c["start_index"] for c in session.calls] == [0, 4_000, 8_000]


def test_assembled_content_does_not_depend_on_the_page_size():
    """The content hash must not move when a tuning knob does."""
    body = "".join(f"{i:04d}\n" for i in range(2_000))
    outputs = {
        size: fetcher_with(FakeSession(body, page_size=size), max_length=size)
        .fetch(DocumentSource(url=URL))
        .content
        for size in (700, 1_000, 3_000, 50_000)
    }
    assert len(set(outputs.values())) == 1
    assert next(iter(outputs.values())) == body


def test_a_stalled_offset_raises_instead_of_looping():
    class Stuck:
        def call_tool(self, name, arguments):
            return FakeResult(
                PREAMBLE + "text\n\n<e>Content truncated. Call the read_documentation "
                "tool with start_index=42 to get more content.</e>"
            )

    with pytest.raises(FetchError, match="stalled"):
        fetcher_with(Stuck()).fetch(DocumentSource(url=URL))
