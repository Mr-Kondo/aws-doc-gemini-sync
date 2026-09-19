"""Google Docs API adapter.

Drive owns writing (it performs the Markdown conversion); the Docs API is used to
*read back* what the conversion produced. That read is not ceremony: an upload can
succeed at the Drive layer and still leave a document whose body is empty, and an
empty knowledge source is indistinguishable from a working one until someone asks
Gemini a question and gets nothing.

Kept in its own module so that the two Google APIs stay independently
replaceable -- a future version that writes through ``documents.batchUpdate``
changes this file and the store's write calls, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.errors import StoreError
from ..logging_setup import get_logger

log = get_logger("google.docs")

#: A converted document always contains at least a body paragraph; anything at or
#: below this many characters is empty in practice.
EMPTY_BODY_THRESHOLD = 2


@dataclass(frozen=True)
class DocumentShape:
    """Structural facts about a stored Google Doc."""

    document_id: str
    title: str
    revision_id: str
    end_index: int

    @property
    def is_empty(self) -> bool:
        return self.end_index <= EMPTY_BODY_THRESHOLD


class GoogleDocsInspector:
    """Reads document structure through the Docs API."""

    def __init__(self, docs_service: Any) -> None:
        self._docs = docs_service

    def shape(self, document_id: str) -> DocumentShape:
        try:
            payload = (
                self._docs.documents()
                .get(documentId=document_id, fields="title,revisionId,body(content(endIndex))")
                .execute()
            )
        except Exception as exc:
            raise StoreError(f"Docs API read failed for {document_id}: {exc}") from exc

        content = (payload.get("body", {}) or {}).get("content", []) or []
        end_index = max((int(c.get("endIndex", 0)) for c in content), default=0)

        shape = DocumentShape(
            document_id=document_id,
            title=str(payload.get("title", "")),
            revision_id=str(payload.get("revisionId", "")),
            end_index=end_index,
        )
        if shape.is_empty:
            log.warning(
                "google_doc_body_empty",
                extra={"document_id": document_id, "end_index": end_index},
            )
        return shape
