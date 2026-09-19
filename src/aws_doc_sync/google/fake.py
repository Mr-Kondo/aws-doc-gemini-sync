"""In-memory ``DocumentStore``.

Backs both ``--dry-run`` and the unit tests, which is the point: the planning and
orchestration code that runs against real Drive is the same code exercised by the
test suite, so a dry run is a genuine rehearsal rather than a separate code path
that can drift out of agreement with reality.

It also makes the "never call Google from unit tests" rule enforceable -- the
tests have no credentials and no network, and still cover CREATE / UPDATE /
NO_CHANGE decisions end to end.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from datetime import UTC, datetime

from ..domain.errors import StoreError
from ..domain.models import StoredDocument


class FakeDocumentStore:
    """Records what would have happened, without touching Google."""

    def __init__(self, *, read_only: bool = False, existing: Sequence[StoredDocument] = ()) -> None:
        #: When true, writes raise instead of mutating -- used to prove that a dry
        #: run cannot modify anything even if the planner had a bug.
        self.read_only = read_only
        self._documents: dict[str, StoredDocument] = {d.id: d for d in existing}
        self._contents: dict[str, str] = {}
        self._ids = itertools.count(1)
        self.created: list[str] = []
        self.updated: list[str] = []

    # -- DocumentStore -----------------------------------------------------------

    def find_by_id(self, document_id: str) -> StoredDocument | None:
        document = self._documents.get(document_id)
        if document is None or document.trashed:
            return None
        return document

    def find_by_name(self, name: str) -> StoredDocument | None:
        for document in self._documents.values():
            if document.name == name and not document.trashed:
                return document
        return None

    def list_documents(self) -> Sequence[StoredDocument]:
        return [d for d in self._documents.values() if not d.trashed]

    def create(self, name: str, markdown: str) -> StoredDocument:
        if self.read_only:
            raise StoreError(f"dry run attempted to create '{name}'")
        document = StoredDocument(
            id=f"fake-doc-{next(self._ids):04d}",
            name=name,
            web_view_link=f"https://docs.google.com/document/d/fake-{name}",
            modified_time=datetime.now(UTC),
        )
        self._documents[document.id] = document
        self._contents[document.id] = markdown
        self.created.append(name)
        return document

    def update(self, document_id: str, name: str, markdown: str) -> StoredDocument:
        if self.read_only:
            raise StoreError(f"dry run attempted to update '{name}'")
        if document_id not in self._documents:
            raise StoreError(f"no such document: {document_id}")
        document = self._documents[document_id].model_copy(
            update={"name": name, "modified_time": datetime.now(UTC)}
        )
        self._documents[document_id] = document
        self._contents[document_id] = markdown
        self.updated.append(name)
        return document

    # -- test helpers ------------------------------------------------------------

    def content(self, document_id: str) -> str:
        return self._contents.get(document_id, "")

    def content_by_name(self, name: str) -> str:
        document = self.find_by_name(name)
        return self.content(document.id) if document else ""


class ReadOnlyStore:
    """Wraps a real store, allowing reads and refusing writes.

    Lets ``--dry-run`` produce a truthful plan against live Drive state -- the
    document ids and names it reports are the real ones -- while making an
    accidental write impossible rather than merely unlikely.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.blocked: list[str] = []

    def find_by_id(self, document_id: str) -> StoredDocument | None:
        return self._inner.find_by_id(document_id)  # type: ignore[attr-defined]

    def find_by_name(self, name: str) -> StoredDocument | None:
        return self._inner.find_by_name(name)  # type: ignore[attr-defined]

    def list_documents(self) -> Sequence[StoredDocument]:
        return self._inner.list_documents()  # type: ignore[attr-defined]

    def create(self, name: str, markdown: str) -> StoredDocument:
        self.blocked.append(f"create:{name}")
        raise StoreError(f"dry run: refusing to create '{name}'")

    def update(self, document_id: str, name: str, markdown: str) -> StoredDocument:
        self.blocked.append(f"update:{name}")
        raise StoreError(f"dry run: refusing to update '{name}'")
