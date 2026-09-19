"""Drive adapter behaviour, exercised against a hand-built fake API surface.

No googleapiclient, no credentials, no network: the adapter is driven through the
same ``.files().create(...).execute()`` shape the real client exposes.
"""

from __future__ import annotations

import pytest

from aws_doc_sync.domain.errors import StoreError
from aws_doc_sync.google.docs import GoogleDocsInspector
from aws_doc_sync.google.drive import GOOGLE_DOC_MIME, MARKDOWN_MIME, PLAIN_MIME, DriveDocumentStore

FOLDER = "folder-123"


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeFiles:
    def __init__(self, store: FakeDrive):
        self.store = store

    def get(self, **kwargs):
        file_id = kwargs["fileId"]
        if file_id not in self.store.items:
            return _Call(RuntimeError("HTTP 404 not found"))
        return _Call(self.store.items[file_id])

    def list(self, **kwargs):
        self.store.queries.append(kwargs.get("q", ""))
        return _Call({"files": list(self.store.items.values())})

    def create(self, **kwargs):
        self.store.creates.append(kwargs)
        file_id = f"doc-{len(self.store.items) + 1}"
        payload = {
            "id": file_id,
            "name": kwargs["body"]["name"],
            "webViewLink": f"https://docs.google.com/document/d/{file_id}",
            "trashed": False,
        }
        self.store.items[file_id] = payload
        return _Call(payload)

    def update(self, **kwargs):
        self.store.updates.append(kwargs)
        payload = self.store.items[kwargs["fileId"]]
        payload["name"] = kwargs["body"]["name"]
        return _Call(payload)


class FakeDrive:
    def __init__(self, import_formats: dict | None = None):
        self.items: dict[str, dict] = {}
        self.creates: list[dict] = []
        self.updates: list[dict] = []
        self.queries: list[str] = []
        self._import_formats = (
            import_formats
            if import_formats is not None
            else {MARKDOWN_MIME: [GOOGLE_DOC_MIME]}
        )

    def files(self):
        return FakeFiles(self)

    def about(self):
        outer = self

        class _About:
            def get(self, **kwargs):
                return _Call({"importFormats": outer._import_formats})

        return _About()


def make_store(drive: FakeDrive, **kwargs) -> DriveDocumentStore:
    return DriveDocumentStore(
        credential_provider=None, folder_id=FOLDER, drive_service=drive, **kwargs
    )


def test_markdown_is_chosen_when_drive_supports_it():
    assert make_store(FakeDrive()).upload_mime_type() == MARKDOWN_MIME


def test_falls_back_to_plain_text_when_markdown_import_is_unavailable():
    # Google documents importFormats as the authoritative, dynamic answer.
    assert make_store(FakeDrive(import_formats={})).upload_mime_type() == PLAIN_MIME


def test_probe_result_is_cached():
    drive = FakeDrive()
    store = make_store(drive)
    store.upload_mime_type()
    store.upload_mime_type()
    assert store._upload_mime == MARKDOWN_MIME


def test_an_explicit_mime_type_skips_the_probe():
    store = make_store(FakeDrive(import_formats={}), import_mime_type=MARKDOWN_MIME)
    assert store.upload_mime_type() == MARKDOWN_MIME


def test_create_targets_the_configured_folder_and_converts_to_a_google_doc():
    drive = FakeDrive()
    document = make_store(drive).create("AWS_Example", "# hi\n")

    body = drive.creates[0]["body"]
    assert body["parents"] == [FOLDER]
    assert body["mimeType"] == GOOGLE_DOC_MIME
    assert document.id == "doc-1"


def test_update_keeps_the_file_id():
    """Replacing the file would detach every Gemini Notebook that references it."""
    drive = FakeDrive()
    store = make_store(drive)
    created = store.create("AWS_Example", "# v1\n")
    updated = store.update(created.id, "AWS_Example", "# v2\n")

    assert updated.id == created.id
    assert drive.updates[0]["fileId"] == created.id
    assert len(drive.creates) == 1


def test_no_delete_method_is_exposed():
    # Removing a knowledge source must stay a human decision.
    assert not hasattr(DriveDocumentStore, "delete")


def test_find_by_name_scopes_the_query_to_the_folder_and_excludes_trash():
    drive = FakeDrive()
    store = make_store(drive)
    store.create("AWS_Example", "# hi\n")
    store.find_by_name("AWS_Example")

    query = drive.queries[-1]
    assert f"'{FOLDER}' in parents" in query
    assert "trashed = false" in query
    assert f"mimeType = '{GOOGLE_DOC_MIME}'" in query


def test_find_by_name_escapes_quotes_in_the_name():
    drive = FakeDrive()
    make_store(drive).find_by_name("Weird'Name")
    assert "Weird\\'Name" in drive.queries[-1]


def test_find_by_id_returns_none_for_a_deleted_document():
    assert make_store(FakeDrive()).find_by_id("gone") is None


def test_api_errors_are_wrapped_as_store_errors():
    class Exploding(FakeDrive):
        def files(self):
            class _Files:
                def list(self, **kwargs):
                    return _Call(RuntimeError("boom"))

            return _Files()

    with pytest.raises(StoreError, match="Drive search failed"):
        make_store(Exploding()).find_by_name("x")


# -- Docs API verification -----------------------------------------------------------


class FakeDocs:
    def __init__(self, payload):
        self._payload = payload

    def documents(self):
        outer = self

        class _Documents:
            def get(self, **kwargs):
                return _Call(outer._payload)

        return _Documents()


def test_inspector_reports_a_populated_body():
    docs = FakeDocs(
        {"title": "AWS_Example", "revisionId": "r1", "body": {"content": [{"endIndex": 4200}]}}
    )
    shape = GoogleDocsInspector(docs).shape("doc-1")
    assert shape.end_index == 4200
    assert shape.is_empty is False


def test_inspector_flags_an_empty_body():
    """A conversion that yields nothing would otherwise look like a success."""
    docs = FakeDocs(
        {"title": "AWS_Example", "revisionId": "r1", "body": {"content": [{"endIndex": 1}]}}
    )
    assert GoogleDocsInspector(docs).shape("doc-1").is_empty is True
