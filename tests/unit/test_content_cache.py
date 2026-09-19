"""The content-addressed cache that makes a 304 actionable."""

from __future__ import annotations

from aws_doc_sync.manifest.content_cache import ContentCache
from aws_doc_sync.normalize.common import compute_content_hash

TEXT = "# Page\n\nBody paragraph.\n"
HASH = compute_content_hash(TEXT)


def cache(tmp_path, **kwargs) -> ContentCache:
    return ContentCache(tmp_path / "content-cache", **kwargs)


def test_round_trip(tmp_path):
    c = cache(tmp_path)
    c.put(HASH, TEXT)
    assert c.has(HASH) is True
    assert c.get(HASH) == TEXT


def test_missing_entry_is_a_miss_not_an_error(tmp_path):
    c = cache(tmp_path)
    assert c.get("sha256:" + "0" * 64) is None
    assert c.has("sha256:" + "0" * 64) is False


def test_storing_the_same_content_twice_is_a_no_op(tmp_path):
    c = cache(tmp_path)
    c.put(HASH, TEXT)
    c.put(HASH, TEXT)
    assert c.stats()["entries"] == 1


def test_a_damaged_entry_is_detected_and_self_heals(tmp_path):
    """The key is the hash, so corruption is provable rather than assumed.

    A mismatch becomes a cache miss, which the caller answers with a normal
    fetch -- never with wrong content presented as unchanged.
    """
    c = cache(tmp_path)
    c.put(HASH, TEXT)
    entry = next((tmp_path / "content-cache").iterdir())
    entry.write_text("truncated", encoding="utf-8")

    assert c.get(HASH) is None
    assert not entry.exists()


def test_keys_that_are_not_hashes_are_refused(tmp_path):
    """A manifest value must never be usable as a path fragment."""
    c = cache(tmp_path)
    for bad in ("../../etc/passwd", "sha256:../evil", "sha256:nothex", "", "abc"):
        assert c.get(bad) is None
        assert c.has(bad) is False
        c.put(bad, "x")
    assert c.stats()["entries"] == 0


def test_prune_keeps_referenced_entries_and_drops_the_rest(tmp_path):
    c = cache(tmp_path)
    other = "# Other\n"
    other_hash = compute_content_hash(other)
    c.put(HASH, TEXT)
    c.put(other_hash, other)

    removed = c.prune({HASH})

    assert removed == 1
    assert c.get(HASH) == TEXT
    assert c.get(other_hash) is None


def test_prune_on_an_absent_directory_does_nothing(tmp_path):
    assert cache(tmp_path).prune({HASH}) == 0


def test_a_disabled_cache_stores_nothing_and_never_hits(tmp_path):
    c = cache(tmp_path, enabled=False)
    c.put(HASH, TEXT)
    assert c.get(HASH) is None
    assert c.has(HASH) is False
    assert not (tmp_path / "content-cache").exists()


def test_an_unwritable_cache_does_not_raise(tmp_path):
    # A cache that cannot be written must never fail a sync.
    blocked = tmp_path / "blocked"
    blocked.write_text("I am a file, not a directory", encoding="utf-8")
    c = ContentCache(blocked)
    c.put(HASH, TEXT)
    assert c.get(HASH) is None


def test_content_is_stored_verbatim_including_code_fences(tmp_path):
    payload = '# T\n\n```json\n{\n  "a": 1\t\n}\n```\n'
    c = cache(tmp_path)
    key = compute_content_hash(payload)
    c.put(key, payload)
    assert c.get(key) == payload


def test_a_failed_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    """The cache directory is meant to be self-maintaining.

    A partial file left behind on every full disk turns the one directory that
    prunes itself into one that accumulates junk.
    """
    import os

    c = cache(tmp_path)
    c.put(HASH, TEXT)  # create the directory
    (tmp_path / "content-cache" / "x.md").unlink(missing_ok=True)

    def explode(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", explode)
    other = "# Other content\n"
    c.put(compute_content_hash(other), other)

    leftovers = [p.name for p in (tmp_path / "content-cache").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
