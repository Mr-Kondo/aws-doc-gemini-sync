"""Deterministic normalization of AWS documentation.

No summarization, ever. AWS documentation is the canonical source and this
pipeline is a synchronizing cache; an LLM rewrite would silently drop quotas,
Region constraints, and IAM policy details that a reader later depends on.

What is removed is limited to site chrome and rendering artifacts:
navigation, breadcrumbs, feedback widgets, copy-to-clipboard buttons, the
JavaScript-disabled notice, empty-bold artifacts, and HTML anchor stubs.

What is preserved verbatim: body text, all heading levels, Note/Warning/
Important/Tip admonitions, code blocks in every language, tables, parameter
lists, quotas, Region constraints, deprecation and version notices.
"""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlsplit

from ..domain.errors import NormalizationError
from ..domain.models import ContentKind, NormalizedDocument, RawDocument
from ..domain.urls import canonical_page_url
from ..logging_setup import get_logger
from .common import (
    collapse_blank_lines,
    compute_content_hash,
    extract_title,
    iter_lines_with_code_flag,
    normalize_line_endings,
    strip_trailing_whitespace,
    title_from_url,
    trim_document,
)
from .html_to_markdown import html_to_markdown

log = get_logger("normalize")

# ``<a name="section-id"></a>`` anchors: navigational scaffolding with no content.
ANCHOR_RE = re.compile(r'<a\s+(?:name|id)\s*=\s*"[^"]*"\s*>\s*</a>', re.IGNORECASE)

# ``****`` -- an empty bold run left behind by AWS's HTML-to-Markdown renderer
# where the source had an untitled example caption.
EMPTY_EMPHASIS_RE = re.compile(r"^\*{2,}\s*$")

# Markdown inline link / image targets.
LINK_RE = re.compile(r"(!?\[[^\]]*\])\(\s*<?([^)\s>]+)>?((?:\s+\"[^\"]*\")?)\s*\)")

# Chrome that occasionally survives into the Markdown rendition.
CHROME_LINE_RES = (
    re.compile(r"^\s*javascript is disabled or is unavailable in your browser\.?\s*$", re.I),
    re.compile(
        r"^\s*to use the (aws|amazon) documentation,? javascript must be enabled\.?\s*$",
        re.I,
    ),
    re.compile(r"^\s*(thanks for letting us know|did this page help you\??)", re.I),
    re.compile(r"^\s*please (tell us|refer to your browser's help pages)", re.I),
    re.compile(r"^\s*we're sorry we let you down\.?\s*$", re.I),
    re.compile(r"^\s*\[document conventions\]", re.I),
)


class AwsDocsNormalizer:
    """Turns a ``RawDocument`` of either wire format into normalized Markdown."""

    def __init__(
        self,
        *,
        strip_anchor_tags: bool = True,
        max_consecutive_blank_lines: int = 1,
        absolutize_links: bool = True,
    ) -> None:
        self._strip_anchors = strip_anchor_tags
        self._max_blank = max_consecutive_blank_lines
        self._absolutize = absolutize_links

    def normalize(self, raw: RawDocument) -> NormalizedDocument:
        try:
            if raw.kind is ContentKind.MARKDOWN:
                markdown = raw.content
            elif raw.kind is ContentKind.HTML:
                markdown = html_to_markdown(
                    raw.content, base_url=canonical_page_url(raw.source.url)
                )
            else:  # pragma: no cover - StrEnum is exhaustive
                raise NormalizationError(f"unsupported content kind: {raw.kind}")

            content = self.normalize_markdown(markdown, base_url=raw.source.url)
        except NormalizationError:
            raise
        except Exception as exc:
            raise NormalizationError(
                f"normalization failed for {raw.source.url}: {exc}", source_url=raw.source.url
            ) from exc

        if not content.strip():
            raise NormalizationError(
                f"normalization produced empty content for {raw.source.url}",
                source_url=raw.source.url,
            )

        title = (
            raw.source.title_override
            or extract_title(content)
            or title_from_url(raw.source.url)
        )

        document = NormalizedDocument(
            source_url=canonical_page_url(raw.source.url),
            title=title,
            retrieved_at=raw.retrieved_at,
            content_hash=compute_content_hash(content),
            content=content,
            fetched_from=raw.fetched_from,
            fetcher=raw.fetcher,
            etag=raw.etag,
            last_modified=raw.last_modified,
        )
        log.info(
            "source_normalized",
            extra={
                "source_url": document.source_url,
                "title": document.title,
                "chars": document.char_count,
                "content_hash": document.content_hash,
                "fetcher": raw.fetcher,
            },
        )
        return document

    # -- markdown pipeline -------------------------------------------------------

    def normalize_markdown(self, markdown: str, *, base_url: str) -> str:
        """Apply every cleanup rule, leaving fenced code untouched."""
        text = normalize_line_endings(markdown)
        base = canonical_page_url(base_url)

        processed: list[tuple[str, bool]] = []
        for line, in_code in iter_lines_with_code_flag(text):
            if in_code:
                processed.append((line, True))
                continue

            if self._strip_anchors:
                line = ANCHOR_RE.sub("", line)

            if _is_chrome(line):
                continue
            if EMPTY_EMPHASIS_RE.match(line):
                continue

            if self._absolutize:
                line = _absolutize_links(line, base)

            processed.append((strip_trailing_whitespace(line), False))

        return trim_document(collapse_blank_lines(processed, self._max_blank))


def _is_chrome(line: str) -> bool:
    return any(pattern.match(line) for pattern in CHROME_LINE_RES)


def _absolutize_links(line: str, base_url: str) -> str:
    """Make relative links absolute and point ``.md`` targets at ``.html``.

    Bundled documents are read far away from docs.aws.amazon.com, where a
    relative ``canvas-build-model.md`` resolves to nothing. AWS's own Markdown
    mixes relative ``.md`` and absolute ``.html`` links within a single page.
    """

    def _replace(match: re.Match[str]) -> str:
        label, target, title = match.group(1), match.group(2), match.group(3)
        if not target or target.startswith(("mailto:", "tel:", "data:")):
            return match.group(0)
        if target.startswith("#"):
            # A same-page anchor is dead once the page is bundled with others.
            # Pointing it at the original page keeps the reference traceable.
            return f"{label}({base_url}{target}{title})"
        scheme = urlsplit(target).scheme
        if scheme and scheme not in ("http", "https"):
            return match.group(0)

        if not scheme:
            target = urljoin(base_url, target)

        path, fragment = urldefrag(target)
        if path.endswith(".md"):
            path = path[: -len(".md")] + ".html"
        rebuilt = f"{path}#{fragment}" if fragment else path
        return f"{label}({rebuilt}{title})"

    return LINK_RE.sub(_replace, line)
