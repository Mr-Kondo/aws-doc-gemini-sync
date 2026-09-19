"""Shared test helpers.

Everything here is offline. No test in the default suite performs network or
Google I/O; the fetchers are driven through ``httpx.MockTransport`` and the
destination is ``FakeDocumentStore``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from aws_doc_sync.config.settings import GoogleSettings, Settings
from aws_doc_sync.domain.models import ContentKind, DocumentSource, RawDocument
from aws_doc_sync.fetchers.http_client import HttpClient, RetryPolicy

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _quiet_logging():
    """Keep structured log output out of test reports."""
    logging.getLogger("aws_doc_sync").setLevel(logging.CRITICAL)
    yield


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch):
    """Keep the developer's own credentials out of the test run.

    ``GoogleSettings`` reads ``.env`` from the working directory, so a real
    ``.env`` in the repository root would leak into every test that builds
    settings -- results would depend on whose machine the suite ran on, and a
    failure would look like a code bug. Tests that need these values set them
    explicitly with monkeypatch.
    """
    monkeypatch.setitem(GoogleSettings.model_config, "env_file", None)
    for name in (
        "GOOGLE_AUTH_METHOD",
        "GOOGLE_CLIENT_SECRETS_FILE",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_TOKEN_PATH",
        "GOOGLE_DRIVE_FOLDER_ID",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "GOOGLE_IMPERSONATE_SUBJECT",
        "AWS_DOC_SYNC_MANIFEST_PATH",
        "AWS_DOC_SYNC_LOG_LEVEL",
        "AWS_DOC_SYNC_LOG_FORMAT",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_source(url: str = "https://docs.aws.amazon.com/example/latest/dg/page.html", **kwargs):
    kwargs.setdefault("bundle_id", "test_bundle")
    kwargs.setdefault("collection_id", "test_collection")
    return DocumentSource(url=url, **kwargs)


def make_raw(
    content: str,
    *,
    kind: ContentKind = ContentKind.MARKDOWN,
    url: str = "https://docs.aws.amazon.com/example/latest/dg/page.html",
    retrieved_at: datetime | None = None,
    fetcher: str = "markdown",
) -> RawDocument:
    return RawDocument(
        source=make_source(url),
        content=content,
        kind=kind,
        fetched_from=url,
        fetcher=fetcher,
        retrieved_at=retrieved_at or datetime(2026, 1, 1, tzinfo=UTC),
    )


def mock_client(handler, *, max_retries: int = 0, sleeps: list[float] | None = None) -> HttpClient:
    """An ``HttpClient`` whose transport is a callable, with no real delays."""
    recorded = sleeps if sleeps is not None else []
    return HttpClient(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        policy=RetryPolicy(
            max_retries=max_retries,
            backoff_base_seconds=0.01,
            backoff_max_seconds=0.02,
            jitter=False,
        ),
        requests_per_second=None,
        sleep=recorded.append,
    )


def markdown_response(body: str = "# Title\n\n" + "content " * 60) -> httpx.Response:
    return httpx.Response(
        200,
        text=body,
        headers={"Content-Type": "text/markdown; charset=utf-8", "ETag": '"abc"'},
    )


def html_404() -> httpx.Response:
    """What AWS actually returns for a missing ``.md`` sibling."""
    return httpx.Response(
        404,
        text="<html><body>page not found</body></html>",
        headers={"Content-Type": "text/html"},
    )


def default_settings(**overrides) -> Settings:
    settings = Settings()
    for key, value in overrides.items():
        setattr(settings.app, key, value)
    return settings
