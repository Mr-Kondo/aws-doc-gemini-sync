"""Bundle rendering.

Turns the normalized documents of one bundle into the Markdown that becomes a
Google Doc, with per-source provenance so a human can trace any sentence in a
Gemini answer back to the exact AWS page and revision it came from.

Two rules keep repeat syncs idempotent:

* **No wall-clock time is rendered.** Every timestamp comes from the manifest's
  ``content_retrieved_at``, which moves only when the content hash moves. Putting
  ``datetime.now()`` in the header would make every run produce a different
  document and defeat the entire change-detection layer.
* **The composition hash covers inputs, not output.** It is computed over the
  ordered ``(source_url, title, content_hash)`` triples plus ``RENDER_VERSION``,
  so a template change forces a rebuild while a re-render of identical inputs
  does not.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from ..domain.models import Bundle, BundleDocument, NormalizedDocument
from ..logging_setup import get_logger
from ..normalize.common import iter_lines_with_code_flag

log = get_logger("bundle")

#: Bump whenever the rendered layout changes, so existing docs are rebuilt once.
RENDER_VERSION = 1

SEPARATOR = "---"


@dataclass(frozen=True)
class SourceSection:
    """One source's contribution to a bundle document."""

    source_url: str
    title: str
    content_hash: str
    retrieved_at: datetime
    markdown: str

    @property
    def char_count(self) -> int:
        return len(self.markdown)


class BundleBuilder:
    """Renders bundles into provenance-carrying Markdown documents."""

    def __init__(self, *, generator: str = "aws-doc-gemini-sync") -> None:
        self._generator = generator

    # -- sections ----------------------------------------------------------------

    def section(self, document: NormalizedDocument, *, retrieved_at: datetime) -> SourceSection:
        """Render one source: provenance block, separator, then the body.

        ``retrieved_at`` is supplied by the caller (from the manifest) rather than
        read off the document, because a re-fetch that produced identical content
        must not change what this section renders.
        """
        body = _demote_headings(_drop_leading_title(document.content, document.title))

        parts = [
            f"## {document.title}",
            "",
            "Source URL:",
            document.source_url,
            "",
            "Retrieved:",
            _iso(retrieved_at),
            "",
            "Content Hash:",
            document.content_hash,
            "",
            SEPARATOR,
            "",
            body.rstrip("\n"),
        ]
        return SourceSection(
            source_url=document.source_url,
            title=document.title,
            content_hash=document.content_hash,
            retrieved_at=retrieved_at,
            markdown="\n".join(parts) + "\n",
        )

    # -- documents ---------------------------------------------------------------

    def header(
        self,
        bundle: Bundle,
        sections: list[SourceSection],
        *,
        part: int,
        part_count: int,
    ) -> str:
        generated = max((s.retrieved_at for s in sections), default=None)
        lines = [
            f"# {bundle.display_title}",
            "",
            "Source Type: AWS Official Documentation",
            f"Collection: {bundle.collection_id}",
            f"Bundle: {bundle.id}",
            f"Sources in this document: {len(sections)}",
        ]
        if generated is not None:
            # Date of the newest source revision included here -- not the time this
            # file was written, which would change on every run.
            lines.append(f"Generated (newest source revision): {generated.date().isoformat()}")
        if part_count > 1:
            lines.append(f"Part: {part} of {part_count}")
        lines.append(f"Generator: {self._generator}")

        if bundle.description:
            lines += ["", bundle.description]

        lines += [
            "",
            "This document is a synchronized copy of AWS official documentation. "
            "The content is reproduced without summarization. Each section below records "
            "the source URL, the retrieval time, and the SHA-256 hash of the normalized "
            "text so that any statement can be traced back to its origin.",
            "",
            "Contents:",
        ]
        lines += [f"{index}. {s.title}" for index, s in enumerate(sections, start=1)]
        lines += ["", SEPARATOR, "", ""]
        return "\n".join(lines)

    def document(
        self,
        bundle: Bundle,
        sections: list[SourceSection],
        *,
        name: str,
        part: int = 1,
        part_count: int = 1,
    ) -> BundleDocument:
        header = self.header(bundle, sections, part=part, part_count=part_count)
        body = f"\n\n{SEPARATOR}\n\n".join(s.markdown.rstrip("\n") for s in sections)
        markdown = header + body + "\n"

        title = bundle.display_title
        if part_count > 1:
            title = f"{title} ({part}/{part_count})"

        return BundleDocument(
            bundle_id=bundle.id,
            collection_id=bundle.collection_id,
            name=name,
            part=part,
            part_count=part_count,
            title=title,
            markdown=markdown,
            composition_hash=composition_hash(bundle, sections, part=part, part_count=part_count),
            source_urls=tuple(s.source_url for s in sections),
        )


def composition_hash(
    bundle: Bundle, sections: list[SourceSection], *, part: int, part_count: int
) -> str:
    """Stable digest of everything that determines a document's content."""
    payload = {
        "render_version": RENDER_VERSION,
        "bundle_id": bundle.id,
        "collection_id": bundle.collection_id,
        "output": bundle.output,
        "title": bundle.display_title,
        "description": bundle.description,
        "part": part,
        "part_count": part_count,
        "sections": [
            {"source_url": s.source_url, "title": s.title, "content_hash": s.content_hash}
            for s in sections
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Markdown structure helpers
# --------------------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    """UTC ISO-8601, second precision.

    Rendered into the document, so the format must never depend on the local
    timezone of whichever machine happens to run the sync.
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _drop_leading_title(markdown: str, title: str) -> str:
    """Remove the body's own H1 when it repeats the section heading.

    Keeping both would give the reader the same sentence twice and, in Google
    Docs, two competing outline entries for one section.
    """
    lines = markdown.split("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("# ") and stripped[2:].strip() == title.strip():
            return "\n".join(lines[index + 1 :]).lstrip("\n")
        return markdown
    return markdown


def _demote_headings(markdown: str, *, by: int = 2, max_level: int = 6) -> str:
    """Push body headings below the bundle (H1) and section (H2) levels.

    Without this a bundle document would contain several competing H1s and the
    Google Docs outline would be unusable. Levels saturate at ``max_level``
    rather than overflowing into invalid Markdown.
    """
    out: list[str] = []
    for line, in_code in iter_lines_with_code_flag(markdown):
        if in_code:
            out.append(line)
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            rest = stripped[hashes:]
            if 1 <= hashes <= 6 and (rest.startswith(" ") or not rest):
                level = min(hashes + by, max_level)
                out.append("#" * level + rest)
                continue
        out.append(line)
    return "\n".join(out)
