"""CLI surface: exit codes, selection errors, and the no-write guarantee."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from aws_doc_sync.cli import app

runner = CliRunner()

SOURCES = """
collections:
  demo:
    bundles:
      demo_bundle:
        output: AWS_Demo
        sources:
          - https://docs.aws.amazon.com/example/latest/dg/alpha.html
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "sources.yaml").write_text(SOURCES, encoding="utf-8")
    (tmp_path / "config" / "settings.yaml").write_text(
        "manifest:\n  path: .state/manifest.json\nlogging:\n  level: CRITICAL\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    # Make sure a developer's real credentials never leak into a test run.
    for key in ("GOOGLE_DRIVE_FOLDER_ID", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                "GOOGLE_CLIENT_SECRETS_FILE"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_version_flag(project):
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_validate_config_accepts_a_valid_registry(project):
    result = runner.invoke(app, ["validate-config"])
    assert result.exit_code == 0
    assert "demo_bundle -> AWS_Demo" in result.stdout
    # It must say plainly that Google is not ready, rather than implying success.
    assert "GOOGLE_DRIVE_FOLDER_ID is not set" in result.stdout


def test_validate_config_reports_a_broken_registry(project):
    (project / "config" / "sources.yaml").write_text("collections: []\n", encoding="utf-8")
    result = runner.invoke(app, ["validate-config"])
    assert result.exit_code == 2
    # Errors go to stderr so that --json output stays parseable on stdout.
    assert "error:" in result.stderr


def test_validate_config_never_prints_secret_values(project, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "top-secret-value")
    result = runner.invoke(app, ["validate-config"])
    assert "top-secret-value" not in result.stdout


def test_missing_selection_is_an_error(project):
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 2
    assert "select at least one" in result.stderr


def test_unknown_bundle_is_an_error(project):
    result = runner.invoke(app, ["dry-run", "--bundle", "nope"])
    assert result.exit_code == 2
    assert "unknown bundle" in result.stderr


def test_sync_without_google_credentials_refuses_rather_than_pretending(project):
    result = runner.invoke(app, ["sync", "--all"])
    assert result.exit_code == 2
    assert "Google is not configured" in result.stderr


def test_status_on_a_fresh_checkout_reports_nothing_synced(project):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "not created yet" in result.stdout


def test_status_json_is_machine_readable(project):
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["registry"]["bundles"] == 1
    assert payload["bundles"][0]["bundle_id"] == "demo_bundle"


def test_dry_run_leaves_no_manifest_behind(project, monkeypatch):
    """A dry run must not change local state either."""
    import aws_doc_sync.runtime as runtime_module
    from tests.unit.test_sync_service import ScriptedFetcher, body

    BASE = "https://docs.aws.amazon.com/example/latest/dg"
    original = runtime_module.build_runtime

    def patched(**kwargs):
        rt = original(**kwargs)
        rt.fetcher = ScriptedFetcher({f"{BASE}/alpha.html": body("alpha")})
        return rt

    monkeypatch.setattr(runtime_module, "build_runtime", patched)
    monkeypatch.setattr("aws_doc_sync.cli.build_runtime", patched)

    result = runner.invoke(app, ["dry-run", "--bundle", "demo_bundle"])
    assert result.exit_code == 0
    assert "CREATE" in result.stdout
    assert not (project / ".state" / "manifest.json").exists()


def test_help_lists_every_documented_command(project):
    result = runner.invoke(app, ["--help"])
    for command in ("validate-config", "fetch", "sync", "dry-run", "scan", "status"):
        assert command in result.stdout


def test_json_reports_are_parseable_because_logs_go_to_stderr(project, monkeypatch):
    """A structured log line on stdout would corrupt the JSON document."""
    import aws_doc_sync.runtime as runtime_module
    from tests.unit.test_sync_service import ScriptedFetcher, body

    BASE = "https://docs.aws.amazon.com/example/latest/dg"
    original = runtime_module.build_runtime

    def patched(**kwargs):
        rt = original(**kwargs)
        rt.fetcher = ScriptedFetcher({f"{BASE}/alpha.html": body("alpha")})
        return rt

    monkeypatch.setattr("aws_doc_sync.cli.build_runtime", patched)
    # Logging is deliberately left at INFO here: the point is that a noisy run
    # still produces clean stdout.
    (project / "config" / "settings.yaml").write_text(
        "logging:\n  level: INFO\n  format: json\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["dry-run", "--bundle", "demo_bundle", "--json"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["command"] == "dry-run"
    assert payload["documents"][0]["action"] == "CREATE"
    # The logs did happen -- they just went somewhere else.
    assert "source_fetch" in result.stderr or "sync_completed" in result.stderr
