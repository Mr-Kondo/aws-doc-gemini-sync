"""Google Drive adapter.

Writes one Google Doc per bundle. The content is uploaded as Markdown and
converted by Drive itself (``mimeType: application/vnd.google-apps.document``),
which is how headings, tables, and code blocks survive the trip. Google documents
Markdown as a supported import format and recommends confirming support at
runtime through ``about.importFormats``; this adapter does exactly that and falls
back to ``text/plain`` if the conversion is not offered.

The rule that matters most: **update in place, never replace.** ``files.update``
keeps the file id, so a Gemini Notebook that already references the document keeps
working. Deleting and recreating would silently detach every notebook and lose the
revision history.

There is no delete method here at all. Removing a knowledge source is a human
decision; orphans are reported, never actioned.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ..domain.errors import StoreError
from ..domain.models import StoredDocument
from ..logging_setup import get_logger
from .docs import DocumentShape, GoogleDocsInspector

log = get_logger("google.drive")

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
MARKDOWN_MIME = "text/markdown"
PLAIN_MIME = "text/plain"

_FILE_FIELDS = "id, name, webViewLink, modifiedTime, trashed"


class DriveDocumentStore:
    """``DocumentStore`` backed by Google Drive."""

    def __init__(
        self,
        *,
        credential_provider: Any,
        folder_id: str,
        import_mime_type: str = "auto",
        drive_service: Any = None,
        docs_service: Any = None,
    ) -> None:
        if not folder_id:
            raise StoreError("a destination Drive folder id is required")
        self._provider = credential_provider
        self._folder_id = folder_id
        self._configured_mime = import_mime_type
        self._drive = drive_service
        self._docs = docs_service
        self._upload_mime: str | None = None

    # -- services ----------------------------------------------------------------

    @property
    def drive(self) -> Any:
        if self._drive is None:
            self._drive = self._build("drive", "v3")
        return self._drive

    @property
    def docs(self) -> Any:
        if self._docs is None:
            self._docs = self._build("docs", "v1")
        return self._docs

    def _build(self, name: str, version: str) -> Any:
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise StoreError(f"google-api-python-client is not installed: {exc}") from exc

        from .auth import DRIVE_SCOPES

        credentials = self._provider.credentials(DRIVE_SCOPES)
        return build(name, version, credentials=credentials, cache_discovery=False)

    # -- upload format -----------------------------------------------------------

    def upload_mime_type(self) -> str:
        """Decide the format to upload in, probing Drive when set to ``auto``.

        Markdown is strongly preferred: uploading plain text would flatten every
        heading and table into an undifferentiated wall of characters, which is
        precisely the fidelity loss this pipeline exists to avoid.
        """
        if self._upload_mime is not None:
            return self._upload_mime

        if self._configured_mime != "auto":
            self._upload_mime = self._configured_mime
            return self._upload_mime

        try:
            about = self.drive.about().get(fields="importFormats").execute()
            formats = about.get("importFormats", {}) or {}
            supported = GOOGLE_DOC_MIME in (formats.get(MARKDOWN_MIME) or [])
        except Exception as exc:
            log.warning("drive_import_formats_probe_failed", extra={"reason": str(exc)})
            supported = False

        self._upload_mime = MARKDOWN_MIME if supported else PLAIN_MIME
        log.info(
            "drive_import_format_selected",
            extra={"mime_type": self._upload_mime, "probed": True},
        )
        return self._upload_mime

    # -- DocumentStore -----------------------------------------------------------

    def find_by_id(self, document_id: str) -> StoredDocument | None:
        try:
            payload = (
                self.drive.files()
                .get(fileId=document_id, fields=_FILE_FIELDS, supportsAllDrives=True)
                .execute()
            )
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise StoreError(f"Drive lookup failed for {document_id}: {exc}") from exc
        return _to_document(payload)

    def find_by_name(self, name: str) -> StoredDocument | None:
        """Locate an existing doc by exact name inside the destination folder.

        This is the recovery path when the manifest has been lost or was never
        committed: without it, a fresh manifest would create a second copy of
        every document and quietly double the notebook's sources.
        """
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name = '{escaped}' and '{self._folder_id}' in parents "
            f"and mimeType = '{GOOGLE_DOC_MIME}' and trashed = false"
        )
        try:
            response = (
                self.drive.files()
                .list(
                    q=query,
                    fields=f"files({_FILE_FIELDS})",
                    pageSize=10,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except Exception as exc:
            raise StoreError(f"Drive search failed for name '{name}': {exc}") from exc

        files = response.get("files", []) or []
        if not files:
            return None
        if len(files) > 1:
            # Ambiguous: adopting an arbitrary one could overwrite the wrong doc.
            log.warning(
                "drive_duplicate_name",
                extra={"document_name": name, "count": len(files), "folder_id": self._folder_id},
            )
        return _to_document(files[0])

    def list_documents(self) -> Sequence[StoredDocument]:
        query = (
            f"'{self._folder_id}' in parents and mimeType = '{GOOGLE_DOC_MIME}' "
            f"and trashed = false"
        )
        out: list[StoredDocument] = []
        page_token: str | None = None
        try:
            while True:
                response = (
                    self.drive.files()
                    .list(
                        q=query,
                        fields=f"nextPageToken, files({_FILE_FIELDS})",
                        pageSize=100,
                        pageToken=page_token,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
                out.extend(_to_document(f) for f in response.get("files", []) or [])
                page_token = response.get("nextPageToken")
                if not page_token:
                    return out
        except Exception as exc:
            raise StoreError(f"Drive listing failed for folder {self._folder_id}: {exc}") from exc

    def create(self, name: str, markdown: str) -> StoredDocument:
        media = self._media(markdown)
        metadata = {"name": name, "mimeType": GOOGLE_DOC_MIME, "parents": [self._folder_id]}
        try:
            payload = (
                self.drive.files()
                .create(
                    body=metadata,
                    media_body=media,
                    fields=_FILE_FIELDS,
                    supportsAllDrives=True,
                )
                .execute()
            )
        except Exception as exc:
            raise StoreError(f"Drive create failed for '{name}': {exc}") from exc

        document = _to_document(payload)
        log.info(
            "google_doc_created",
            extra={"document_name": name, "document_id": document.id, "chars": len(markdown)},
        )
        return document

    def update(self, document_id: str, name: str, markdown: str) -> StoredDocument:
        """Replace the document's content while keeping its id."""
        media = self._media(markdown)
        try:
            payload = (
                self.drive.files()
                .update(
                    fileId=document_id,
                    body={"name": name, "mimeType": GOOGLE_DOC_MIME},
                    media_body=media,
                    fields=_FILE_FIELDS,
                    supportsAllDrives=True,
                )
                .execute()
            )
        except Exception as exc:
            raise StoreError(f"Drive update failed for {document_id}: {exc}") from exc

        document = _to_document(payload)
        log.info(
            "google_doc_updated",
            extra={"document_name": name, "document_id": document.id, "chars": len(markdown)},
        )
        return document

    # -- verification (Google Docs API) ------------------------------------------

    def inspect(self, document_id: str) -> DocumentShape:
        """Read back a document's shape after a write.

        Confirms Drive's Markdown conversion actually produced a body. A write
        that succeeds at the Drive layer but yields an empty document looks like
        success while having destroyed a knowledge source.
        """
        return GoogleDocsInspector(self.docs).shape(document_id)

    # -- helpers -----------------------------------------------------------------

    def _media(self, markdown: str) -> Any:
        try:
            from googleapiclient.http import MediaIoBaseUpload
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise StoreError(f"google-api-python-client is not installed: {exc}") from exc

        return MediaIoBaseUpload(
            io.BytesIO(markdown.encode("utf-8")),
            mimetype=self.upload_mime_type(),
            resumable=False,
        )


def _to_document(payload: dict[str, Any]) -> StoredDocument:
    return StoredDocument(
        id=payload.get("id", ""),
        name=payload.get("name", ""),
        web_view_link=payload.get("webViewLink"),
        modified_time=_parse_time(payload.get("modifiedTime")),
        trashed=bool(payload.get("trashed", False)),
    )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_not_found(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status == 404 or "404" in str(exc)
