import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aws_doc_sync.domain.errors import ConfigError
from aws_doc_sync.manifest.models import BundleState, SourceState, SyncManifest
from aws_doc_sync.manifest.repository import JsonManifestRepository

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def sample() -> SyncManifest:
    manifest = SyncManifest()
    manifest.put_source(
        SourceState(
            source_url="https://docs.aws.amazon.com/x/latest/dg/a.html",
            bundle_id="b",
            collection_id="c",
            title="A",
            sha256="sha256:aaa",
            last_checked=T0,
            last_changed=T0,
            content_retrieved_at=T0,
        )
    )
    manifest.put_bundle(
        BundleState(
            bundle_id="b",
            collection_id="c",
            output="OUT",
            document_ids={"OUT": "doc-1"},
            composition_hashes={"OUT": "sha256:bbb"},
            last_synced=T0,
        )
    )
    return manifest


def test_missing_manifest_loads_as_empty(tmp_path):
    repo = JsonManifestRepository(tmp_path / "manifest.json")
    manifest = repo.load()
    assert manifest.sources == {} and manifest.bundles == {}


def test_round_trip_preserves_state(tmp_path):
    repo = JsonManifestRepository(tmp_path / "manifest.json")
    repo.save(sample())
    loaded = repo.load()

    source = loaded.source("https://docs.aws.amazon.com/x/latest/dg/a.html")
    assert source.sha256 == "sha256:aaa"
    assert source.content_retrieved_at == T0
    assert loaded.bundle("b").document_ids == {"OUT": "doc-1"}
    assert loaded.bundle("b").composition_hashes == {"OUT": "sha256:bbb"}


def test_save_creates_parent_directories(tmp_path):
    repo = JsonManifestRepository(tmp_path / "nested" / "deeper" / "manifest.json")
    repo.save(SyncManifest())
    assert (tmp_path / "nested" / "deeper" / "manifest.json").exists()


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    repo = JsonManifestRepository(tmp_path / "manifest.json")
    repo.save(sample())
    repo.save(sample())
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_previous_version_is_kept_as_a_backup(tmp_path):
    repo = JsonManifestRepository(tmp_path / "manifest.json", backup=True)
    repo.save(sample())
    second = sample()
    second.sources["https://docs.aws.amazon.com/x/latest/dg/a.html"].sha256 = "sha256:ccc"
    repo.save(second)

    backup = json.loads((tmp_path / "manifest.json.bak").read_text())
    stored = backup["sources"]["https://docs.aws.amazon.com/x/latest/dg/a.html"]
    assert stored["sha256"] == "sha256:aaa"


def test_corrupt_manifest_is_reported_not_silently_discarded(tmp_path):
    # Silently starting from empty would re-upload every bundle.
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="unreadable"):
        JsonManifestRepository(path).load()


def test_manifest_from_a_newer_version_is_refused(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 999, "sources": {}, "bundles": {}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="newer version"):
        JsonManifestRepository(path).load()


def test_orphans_are_reported(tmp_path):
    manifest = sample()
    manifest.sources["https://docs.aws.amazon.com/x/latest/dg/a.html"].orphaned = True
    assert len(manifest.orphans()) == 1
    assert manifest.summary()["orphaned"] == 1


def test_unknown_fields_are_ignored_for_forward_compatibility(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"version": 1, "sources": {}, "bundles": {}, "future_field": 1}),
        encoding="utf-8",
    )
    assert JsonManifestRepository(path).load().version == 1


# -- concurrent runs -------------------------------------------------------------


def test_the_lock_is_exclusive_between_processes(tmp_path):
    """Two runs must not both read, both write, and lose one side's doc ids.

    A second holder of the lock would go on to create duplicate documents in
    Drive for every id the loser's write discarded.
    """
    import subprocess
    import sys
    import textwrap

    path = tmp_path / "manifest.json"
    repo = JsonManifestRepository(path, lock_timeout=1.0)

    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).parents[2] / "src")!r})
        from aws_doc_sync.manifest.repository import JsonManifestRepository, ManifestLocked
        repo = JsonManifestRepository({str(path)!r}, lock_timeout=0.5)
        try:
            with repo.lock():
                print("ACQUIRED")
        except ManifestLocked:
            print("BLOCKED")
        """
    )

    with repo.lock():
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )

    assert "BLOCKED" in result.stdout, result.stdout + result.stderr


def test_the_lock_is_released_afterwards(tmp_path):
    repo = JsonManifestRepository(tmp_path / "manifest.json", lock_timeout=1.0)
    with repo.lock():
        pass
    with repo.lock():   # a second acquisition must succeed
        pass


def test_the_lock_is_released_even_when_the_run_raises(tmp_path):
    repo = JsonManifestRepository(tmp_path / "manifest.json", lock_timeout=1.0)
    with pytest.raises(RuntimeError), repo.lock():
        raise RuntimeError("boom")
    with repo.lock():
        pass


def test_locking_creates_the_state_directory(tmp_path):
    repo = JsonManifestRepository(tmp_path / "nested" / "manifest.json", lock_timeout=1.0)
    with repo.lock():
        assert (tmp_path / "nested").is_dir()


def test_the_in_memory_repository_exposes_a_no_op_lock():
    from aws_doc_sync.manifest.repository import InMemoryManifestRepository

    repo = InMemoryManifestRepository()
    with repo.lock():
        assert repo.load() is not None
