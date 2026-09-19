"""Content-addressed cache of normalized documents.

Exists to make HTTP 304 useful. A conditional request that answers "not
modified" saves the body transfer, but this pipeline needs the *content* to
re-render a bundle whenever any of its other sources changed. Without somewhere
to read that content from, a 304 would have to be followed by a full fetch --
strictly more work than never asking.

The key is the SHA-256 of the content it stores, which is what makes this safe:

* An entry cannot go stale. If the content changes, so does its hash, so the new
  content lands at a new key and the old entry is simply never asked for again.
  There is no invalidation logic to get wrong.
* A corrupted or truncated entry is detected on read, because the content is
  re-hashed and compared. A mismatch is treated as a miss and the entry removed,
  so corruption self-heals into a normal fetch.
* A miss is never a failure. The caller falls back to an unconditional fetch.

Nothing here is authoritative. The manifest holds the hashes; this only holds
bytes that some hash already vouched for.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..logging_setup import get_logger
from ..normalize.common import compute_content_hash

log = get_logger("cache")

_PREFIX = "sha256:"


class ContentCache:
    """Stores normalized document content under its own hash."""

    def __init__(self, path: Path | str, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled

    # -- keys --------------------------------------------------------------------

    def _entry(self, content_hash: str) -> Path | None:
        """Filesystem location for a hash, or None if it is not one we wrote."""
        if not content_hash.startswith(_PREFIX):
            return None
        digest = content_hash[len(_PREFIX) :]
        # Guard against a manifest value being used as a path fragment.
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            return None
        return self.path / f"{digest}.md"

    # -- access ------------------------------------------------------------------

    def get(self, content_hash: str) -> str | None:
        """Return the cached content for ``content_hash``, or None."""
        if not self.enabled:
            return None
        entry = self._entry(content_hash)
        if entry is None or not entry.exists():
            return None

        try:
            content = entry.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("cache_read_failed", extra={"reason": str(exc)})
            return None

        if compute_content_hash(content) != content_hash:
            # The only way this happens is damage. Drop it and fetch instead.
            log.warning("cache_entry_corrupt", extra={"content_hash": content_hash})
            entry.unlink(missing_ok=True)
            return None

        return content

    def has(self, content_hash: str) -> bool:
        """Whether an entry exists, without reading or verifying it.

        Used to decide whether a conditional request is worth making at all: a
        304 is only useful if the content it refers to can be recovered.
        """
        if not self.enabled:
            return False
        entry = self._entry(content_hash)
        return entry is not None and entry.exists()

    def put(self, content_hash: str, content: str) -> None:
        """Store ``content`` under its hash. A no-op if already present."""
        if not self.enabled:
            return
        entry = self._entry(content_hash)
        if entry is None or entry.exists():
            return

        try:
            self.path.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(dir=str(self.path), suffix=".tmp")
        except OSError as exc:
            # A cache that cannot be written must not fail a sync.
            log.warning("cache_write_failed", extra={"reason": str(exc)})
            return

        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(content)
            os.replace(tmp_name, entry)
        except OSError as exc:
            # Leaving the partial file behind would accumulate .tmp entries every
            # time the disk is full, in the one directory that is supposed to be
            # self-maintaining.
            Path(tmp_name).unlink(missing_ok=True)
            log.warning("cache_write_failed", extra={"reason": str(exc)})

    def prune(self, keep: set[str]) -> int:
        """Delete entries no manifest hash refers to. Returns the count removed."""
        if not self.enabled or not self.path.exists():
            return 0

        wanted = {entry.name for h in keep if (entry := self._entry(h)) is not None}
        removed = 0
        try:
            for entry in self.path.iterdir():
                if entry.is_file() and entry.name not in wanted:
                    entry.unlink(missing_ok=True)
                    removed += 1
        except OSError as exc:
            log.warning("cache_prune_failed", extra={"reason": str(exc)})

        if removed:
            log.info("cache_pruned", extra={"removed": removed, "kept": len(wanted)})
        return removed

    def stats(self) -> dict[str, int]:
        if not self.path.exists():
            return {"entries": 0, "bytes": 0}
        files = [f for f in self.path.iterdir() if f.is_file()]
        return {"entries": len(files), "bytes": sum(f.stat().st_size for f in files)}
