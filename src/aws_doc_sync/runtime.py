"""Composition root.

The one place that knows which concrete adapter satisfies which port. Keeping it
out of the CLI means the same wiring is reused by tests and by any future entry
point (a Lambda handler, a scheduler task) without dragging Typer along.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .bundling.builder import BundleBuilder
from .change_detection.rss import RssChangeCandidates
from .config.registry import load_registry
from .config.settings import Settings, load_settings
from .domain.models import SourceRegistry
from .fetchers.chain import FallbackFetcher, build_fetcher_chain
from .fetchers.http_client import HttpClient, RetryPolicy
from .google.fake import FakeDocumentStore, ReadOnlyStore
from .logging_setup import configure_logging, get_logger
from .manifest.content_cache import ContentCache
from .manifest.repository import JsonManifestRepository
from .normalize.aws_docs import AwsDocsNormalizer
from .sync.service import SyncService

log = get_logger("runtime")

DEFAULT_SOURCES = Path("config/sources.yaml")
DEFAULT_SETTINGS = Path("config/settings.yaml")


@dataclass
class Runtime:
    """Everything a command needs, already wired."""

    settings: Settings
    registry: SourceRegistry
    http: HttpClient
    fetcher: FallbackFetcher
    normalizer: AwsDocsNormalizer
    manifests: JsonManifestRepository
    rss: RssChangeCandidates
    content_cache: ContentCache
    stack: ExitStack

    def close(self) -> None:
        self.stack.close()

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- stores ------------------------------------------------------------------

    def document_store(self, *, dry_run: bool, offline_ok: bool = True):
        """Build the destination store for this run.

        Dry runs still read from Drive when credentials are available, because a
        plan built against imaginary state is not a plan. When credentials are
        missing, the run degrades to manifest-only reasoning and says so instead
        of failing -- being able to review a plan without Google access is the
        whole point of ``dry-run``.
        """
        from .google.auth import build_credential_provider
        from .google.drive import DriveDocumentStore

        problems = self.settings.google.validate_for_sync()
        if problems:
            if not dry_run and not offline_ok:
                raise RuntimeError("; ".join(problems))
            if not dry_run:
                raise RuntimeError(
                    "Google is not configured: " + "; ".join(problems)
                )
            log.warning(
                "google_not_configured",
                extra={"problems": problems, "effect": "planning from the manifest only"},
            )
            return None

        provider = build_credential_provider(self.settings.google)
        store = DriveDocumentStore(
            credential_provider=provider,
            folder_id=self.settings.google.drive_folder_id or "",
            import_mime_type=self.settings.google_docs.import_mime_type,
        )
        return ReadOnlyStore(store) if dry_run else store

    def sync_service(self, store=None) -> SyncService:
        return SyncService(
            settings=self.settings,
            fetcher=self.fetcher,
            normalizer=self.normalizer,
            manifest_repository=self.manifests,
            store=store,
            rss=self.rss,
            builder=BundleBuilder(),
            content_cache=self.content_cache,
        )


def build_runtime(
    *,
    sources_path: Path | str | None = None,
    settings_path: Path | str | None = None,
    log_level: str | None = None,
    log_format: str | None = None,
    log_stream: TextIO | None = None,
) -> Runtime:
    """Load configuration and construct every adapter."""
    resolved_settings = Path(settings_path) if settings_path else DEFAULT_SETTINGS
    settings = load_settings(resolved_settings if resolved_settings.exists() else None)

    configure_logging(
        level=log_level or settings.app.logging.level,
        fmt=log_format or settings.app.logging.format,
        stream=log_stream,
    )

    resolved_sources = Path(sources_path) if sources_path else DEFAULT_SOURCES
    registry = load_registry(resolved_sources)

    stack = ExitStack()
    http = HttpClient(
        policy=RetryPolicy(
            max_retries=settings.http.max_retries,
            backoff_base_seconds=settings.http.backoff_base_seconds,
            backoff_max_seconds=settings.http.backoff_max_seconds,
        ),
        requests_per_second=settings.http.requests_per_second,
        user_agent=settings.http.user_agent,
        timeout=settings.http.timeout_seconds,
        connect_timeout=settings.http.connect_timeout_seconds,
        follow_redirects=settings.http.follow_redirects,
    )
    stack.callback(http.close)

    fetcher = build_fetcher_chain(settings, client=http)
    for backend in getattr(fetcher, "_fetchers", []):
        closer = getattr(backend, "close", None)
        if callable(closer):
            stack.callback(closer)

    return Runtime(
        settings=settings,
        registry=registry,
        http=http,
        fetcher=fetcher,
        normalizer=AwsDocsNormalizer(
            strip_anchor_tags=settings.app.normalize.strip_anchor_tags,
            max_consecutive_blank_lines=settings.app.normalize.max_consecutive_blank_lines,
            absolutize_links=settings.app.normalize.absolutize_links,
        ),
        manifests=JsonManifestRepository(
            settings.manifest_path,
            backup=settings.app.manifest.backup,
            lock_timeout=settings.app.manifest.lock_timeout_seconds,
        ),
        rss=RssChangeCandidates(
            http, lookback_days=settings.app.change_detection.rss_lookback_days
        ),
        content_cache=ContentCache(
            settings.content_cache_path,
            enabled=settings.app.manifest.content_cache_enabled,
        ),
        stack=stack,
    )


__all__ = ["FakeDocumentStore", "Runtime", "build_runtime"]
