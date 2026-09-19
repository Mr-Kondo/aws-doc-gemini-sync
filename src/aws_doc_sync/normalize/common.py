"""Shared, deterministic text utilities for normalization.

Two invariants drive everything in this module:

1. **Nothing inside a fenced code block is ever touched.** Whitespace inside an
   IAM policy or a shell snippet is content, and AWS really does emit trailing
   tabs inside JSON examples.
2. **The same input always produces the same output.** The content hash is the
   change-detection source of truth, so any nondeterminism here shows up as a
   phantom update and rewrites a Google Doc for no reason.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

FENCE_RE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})(.*)$")


def normalize_line_endings(text: str) -> str:
    """CRLF/CR -> LF, and strip a UTF-8 BOM."""
    return text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")


def iter_lines_with_code_flag(text: str) -> Iterator[tuple[str, bool]]:
    """Yield ``(line, inside_code_block)`` for each line.

    A fence closes only on a matching marker of at least the opening length, per
    CommonMark, so a ```` ``` ```` inside a ```` ```` ```` block does not end it.
    """
    fence_char: str | None = None
    fence_len = 0

    for line in text.split("\n"):
        match = FENCE_RE.match(line)
        if match:
            marker = match.group(2)
            char, length = marker[0], len(marker)
            if fence_char is None:
                fence_char, fence_len = char, length
                yield line, True  # the opening fence belongs to the block
                continue
            if char == fence_char and length >= fence_len and not match.group(3).strip():
                yield line, True  # the closing fence too
                fence_char, fence_len = None, 0
                continue
        yield line, fence_char is not None


def strip_trailing_whitespace(line: str) -> str:
    """Trim trailing spaces while keeping Markdown hard line breaks.

    AWS renders admonitions as ``**Note**`` followed by two spaces; dropping them
    would merge the label into the body paragraph.
    """
    stripped = line.rstrip()
    if not stripped:
        return ""
    if len(line) - len(stripped) >= 2:
        return stripped + "  "
    return stripped


def collapse_blank_lines(lines: list[tuple[str, bool]], max_blank: int) -> list[str]:
    """Cap runs of blank lines outside code blocks."""
    out: list[str] = []
    blank_run = 0
    for line, in_code in lines:
        if in_code:
            out.append(line)
            blank_run = 0
            continue
        if line.strip():
            blank_run = 0
            out.append(line)
        else:
            blank_run += 1
            if blank_run <= max_blank:
                out.append("")
    return out


def trim_document(lines: list[str]) -> str:
    """Drop leading/trailing blank lines and guarantee one trailing newline."""
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end]) + "\n" if end > start else ""


def compute_content_hash(content: str) -> str:
    """``sha256:<hex>`` over the normalized content.

    Prefixed with the algorithm so a future migration can be detected in stored
    manifests instead of silently comparing incompatible digests.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def extract_title(markdown: str) -> str | None:
    """First ATX heading in the document, if any."""
    for line, in_code in iter_lines_with_code_flag(markdown):
        if in_code:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    return None


def title_from_url(url: str) -> str:
    """Readable fallback title derived from the page slug."""
    from ..domain.urls import canonical_page_url

    tail = canonical_page_url(url).rstrip("/").rsplit("/", 1)[-1]
    tail = tail.removesuffix(".html")
    cleaned = re.sub(r"[-_]+", " ", tail).strip()
    return cleaned or url
