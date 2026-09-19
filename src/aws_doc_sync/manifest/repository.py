"""Manifest persistence.

JSON on the local filesystem is the default. The interesting requirement is not
the format but the write: a manifest truncated by a crash would make the next run
re-upload every bundle, so writes go to a temp file in the same directory and are
atomically renamed into place.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from ..domain.errors import AwsDocSyncError, ConfigError
from ..logging_setup import get_logger
from .models import MANIFEST_VERSION, SyncManifest

log = get_logger("manifest")


class ManifestLocked(AwsDocSyncError):
    """Another run holds the manifest lock."""


class JsonManifestRepository:
    """Stores the manifest as a single JSON document."""

    def __init__(
        self, path: Path | str, *, backup: bool = True, lock_timeout: float = 30.0
    ) -> None:
        self.path = Path(path)
        self._backup = backup
        self._lock_timeout = lock_timeout

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold an exclusive lock for the whole load-modify-save cycle.

        Two concurrent runs would otherwise both read the manifest, both write
        it, and the loser's document ids would vanish -- so the next run would
        not find those documents and would create duplicates of them in Drive.

        ``flock`` is used rather than a plain marker file because the kernel
        releases it when the holder dies; a crashed run leaves nothing to clean
        up by hand. Platforms without ``fcntl`` degrade to no locking, with a
        warning, rather than failing.
        """
        if fcntl is None:  # pragma: no cover - non-POSIX
            log.warning("manifest_lock_unavailable", extra={"platform": sys.platform})
            yield
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        deadline = time.monotonic() + self._lock_timeout

        with open(lock_path, "w", encoding="utf-8") as handle:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ManifestLocked(
                            f"another aws-doc-sync run is holding {lock_path}; "
                            f"waited {self._lock_timeout:.0f}s"
                        ) from None
                    time.sleep(0.2)
            try:
                handle.write(f"{os.getpid()}\n")
                handle.flush()
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self) -> SyncManifest:
        """Read the manifest, or return an empty one if it does not exist yet."""
        if not self.path.exists():
            log.info("manifest_absent", extra={"path": str(self.path)})
            return SyncManifest()

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"manifest is unreadable: {self.path}: {exc}") from exc

        version = data.get("version", 0)
        if version > MANIFEST_VERSION:
            raise ConfigError(
                f"manifest {self.path} was written by a newer version "
                f"(found {version}, supported {MANIFEST_VERSION})"
            )

        try:
            manifest = SyncManifest.model_validate(data)
        except Exception as exc:
            raise ConfigError(f"manifest is malformed: {self.path}: {exc}") from exc

        log.info("manifest_loaded", extra={"path": str(self.path), **manifest.summary()})
        return manifest

    def save(self, manifest: SyncManifest) -> None:
        """Atomically replace the manifest on disk."""
        manifest.version = MANIFEST_VERSION
        manifest.updated_at = datetime.now(UTC)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self._backup and self.path.exists():
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            try:
                backup.write_bytes(self.path.read_bytes())
            except OSError as exc:  # pragma: no cover - best effort
                log.warning("manifest_backup_failed", extra={"detail": str(exc)})

        payload = manifest.model_dump(mode="json", exclude_none=False)
        handle, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

        log.info("manifest_saved", extra={"path": str(self.path), **manifest.summary()})


class InMemoryManifestRepository:
    """Non-persistent manifest, used by dry runs and unit tests."""

    def __init__(self, manifest: SyncManifest | None = None) -> None:
        self._manifest = manifest or SyncManifest()
        self.saves = 0

    @contextmanager
    def lock(self) -> Iterator[None]:
        """No-op: nothing else can reach an in-memory manifest."""
        yield

    def load(self) -> SyncManifest:
        return self._manifest.model_copy(deep=True)

    def save(self, manifest: SyncManifest) -> None:
        self._manifest = manifest.model_copy(deep=True)
        self.saves += 1
