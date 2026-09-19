"""Live checks against Google Drive and Docs.

Deselected by default and additionally gated on an explicit folder id, because
these tests *create and modify real files*. Run with::

    export AWS_DOC_SYNC_IT_FOLDER_ID=<a throwaway Drive folder>
    pytest -m integration tests/integration/test_google_drive.py

Never point this at the folder your real Gemini Notebook reads. The tests create
their own uniquely named documents, and they never delete anything -- clean up by
hand, deliberately, which is the same rule the pipeline itself follows.
"""

from __future__ import annotations

import os
import uuid

import pytest

from aws_doc_sync.config.settings import GoogleSettings
from aws_doc_sync.google.auth import build_credential_provider
from aws_doc_sync.google.drive import MARKDOWN_MIME, PLAIN_MIME, DriveDocumentStore

pytestmark = pytest.mark.integration

FOLDER_ID = os.environ.get("AWS_DOC_SYNC_IT_FOLDER_ID")

requires_drive = pytest.mark.skipif(
    not FOLDER_ID,
    reason="set AWS_DOC_SYNC_IT_FOLDER_ID to a throwaway Drive folder to run these",
)

SAMPLE = """# Integration Test Document

Source Type: AWS Official Documentation

---

## Section one

Source URL:
https://docs.aws.amazon.com/example/latest/dg/page.html

---

Body paragraph.

| Resource | Quota |
| --- | --- |
| Endpoints | 100 |

```json
{"Version": "2012-10-17"}
```
"""


@pytest.fixture(scope="module")
def store():
    settings = GoogleSettings()
    problems = settings.validate_for_sync()
    if problems:
        pytest.skip("; ".join(problems))
    return DriveDocumentStore(
        credential_provider=build_credential_provider(settings),
        folder_id=FOLDER_ID or "",
    )


@requires_drive
def test_drive_reports_whether_it_can_import_markdown(store):
    """The assumption the whole write path rests on, checked against live Drive."""
    assert store.upload_mime_type() in (MARKDOWN_MIME, PLAIN_MIME)


@requires_drive
def test_create_update_and_verify_keeps_one_stable_document_id(store):
    name = f"AWS_DocSync_IT_{uuid.uuid4().hex[:8]}"

    created = store.create(name, SAMPLE)
    assert created.id and created.name == name

    # The id must not change on update: Gemini Notebook references it.
    updated = store.update(created.id, name, SAMPLE.replace("Body paragraph.", "Rewritten."))
    assert updated.id == created.id

    # And the conversion must have produced an actual body, not an empty doc.
    shape = store.inspect(created.id)
    assert not shape.is_empty
    assert shape.revision_id

    # Adoption by name works, which is what protects a lost manifest.
    assert store.find_by_name(name).id == created.id

    print(f"\ncreated test document (delete manually): "
          f"https://docs.google.com/document/d/{created.id}")


@requires_drive
def test_find_by_name_returns_none_for_an_absent_document(store):
    assert store.find_by_name(f"AWS_DocSync_IT_absent_{uuid.uuid4().hex}") is None
