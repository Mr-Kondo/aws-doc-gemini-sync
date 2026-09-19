"""Ports.

Every external system this package talks to is reached through one of these
Protocols. Business logic depends on the Protocol, never on the concrete adapter,
so a vendor API change is contained to one module under ``fetchers/`` or
``google/``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from .models import (
    DocumentSource,
    HttpValidators,
    NotModified,
    RawDocument,
    StoredDocument,
)


@runtime_checkable
class DocumentFetcher(Protocol):
    """Retrieves the bytes of one AWS documentation page."""

    name: str

    def supports(self, source: DocumentSource) -> bool:
        """Whether this backend can even attempt ``source``.

        Checked before ``fetch`` so that an unusable backend costs no network I/O
        and produces no misleading error in the report.
        """
        ...

    def fetch(
        self, source: DocumentSource, validators: HttpValidators | None = None
    ) -> RawDocument | NotModified:
        """Retrieve ``source``.

        When ``validators`` are supplied and apply to the URL this backend would
        request, the request is made conditional and may answer ``NotModified``
        instead of a document. Backends that cannot make conditional requests
        (the MCP server, for one) ignore the argument and always return a
        document -- so a caller that passes validators must handle both, and a
        caller that passes none never sees ``NotModified``.

        Raises:
            NotFoundError: the page does not exist upstream.
            FetchError: any other retrieval failure.
        """
        ...


@runtime_checkable
class ManifestRepository(Protocol):
    """Persistence for sync state (hashes, doc ids, timestamps)."""

    def load(self) -> object: ...
    def save(self, manifest: object) -> None: ...


@runtime_checkable
class DocumentStore(Protocol):
    """Destination for rendered bundle documents.

    Intentionally narrow, and intentionally without a ``delete``: removing a
    knowledge source is never something this pipeline should do on its own.
    """

    def find_by_id(self, document_id: str) -> StoredDocument | None: ...

    def find_by_name(self, name: str) -> StoredDocument | None: ...

    def list_documents(self) -> Sequence[StoredDocument]: ...

    def create(self, name: str, markdown: str) -> StoredDocument: ...

    def update(self, document_id: str, name: str, markdown: str) -> StoredDocument: ...


@runtime_checkable
class CredentialProvider(Protocol):
    """Supplies Google API credentials.

    Exists so that OAuth (local development) and service accounts (unattended
    execution) are swappable without touching the Drive/Docs adapters.
    """

    def credentials(self, scopes: Iterable[str]) -> object: ...
