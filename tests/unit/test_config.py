from pathlib import Path

import pytest

from aws_doc_sync.config.registry import load_registry, resolve_selection
from aws_doc_sync.config.settings import load_settings
from aws_doc_sync.domain.errors import ConfigError

VALID = """
collections:
  c1:
    description: first
    bundles:
      b1:
        output: OUT_ONE
        rss_feeds: https://docs.aws.amazon.com/x/latest/dg/feed.rss
        sources:
          - https://docs.aws.amazon.com/x/latest/dg/a.html
          - url: https://docs.aws.amazon.com/x/latest/dg/b.html
            title: Custom title
      b2:
        output: OUT_TWO
        sources:
          - https://docs.aws.amazon.com/x/latest/dg/c.html
"""


def write(tmp_path, text, name="sources.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_three_level_structure(tmp_path):
    registry = load_registry(write(tmp_path, VALID))
    assert [c.id for c in registry.collections] == ["c1"]
    assert [b.id for b in registry.iter_bundles()] == ["b1", "b2"]
    assert len(list(registry.iter_sources())) == 3

    b1 = registry.bundle("b1")
    assert b1.output == "OUT_ONE"
    assert b1.rss_feeds == ("https://docs.aws.amazon.com/x/latest/dg/feed.rss",)
    assert b1.sources[1].title_override == "Custom title"


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_registry(tmp_path / "nope.yaml")


def test_rejects_duplicate_output_names(tmp_path):
    text = VALID.replace("output: OUT_TWO", "output: OUT_ONE")
    # Two bundles writing one Drive file would silently overwrite each other.
    with pytest.raises(ConfigError, match="output name"):
        load_registry(write(tmp_path, text))


def test_rejects_the_same_source_in_two_bundles(tmp_path):
    text = VALID.replace(
        "          - https://docs.aws.amazon.com/x/latest/dg/c.html",
        "          - https://docs.aws.amazon.com/x/latest/dg/a.html",
    )
    with pytest.raises(ConfigError, match="appears in both"):
        load_registry(write(tmp_path, text))


def test_rejects_duplicate_source_within_a_bundle(tmp_path):
    text = VALID.replace(
        "          - url: https://docs.aws.amazon.com/x/latest/dg/b.html\n"
        "            title: Custom title",
        "          - https://docs.aws.amazon.com/x/latest/dg/a.md",
    )
    with pytest.raises(ConfigError, match="duplicate source"):
        load_registry(write(tmp_path, text))


def test_rejects_relative_source_urls(tmp_path):
    text = VALID.replace("https://docs.aws.amazon.com/x/latest/dg/a.html", "/x/latest/dg/a.html")
    with pytest.raises(ConfigError, match="absolute"):
        load_registry(write(tmp_path, text))


def test_rejects_unknown_fetch_strategy(tmp_path):
    text = VALID.replace(
        "          - url: https://docs.aws.amazon.com/x/latest/dg/b.html",
        "          - url: https://docs.aws.amazon.com/x/latest/dg/b.html\n"
        "            strategy: telepathy",
    )
    with pytest.raises(ConfigError, match="unknown strategy"):
        load_registry(write(tmp_path, text))


def test_rejects_bundle_without_sources(tmp_path):
    with pytest.raises(ConfigError, match="sources"):
        load_registry(
            write(tmp_path, "collections:\n  c:\n    bundles:\n      b:\n        output: X\n")
        )


def test_selection_by_collection_bundle_and_all(tmp_path):
    registry = load_registry(write(tmp_path, VALID))
    assert {b.id for b in resolve_selection(registry, collections=["c1"])} == {"b1", "b2"}
    assert [b.id for b in resolve_selection(registry, bundles=["b2"])] == ["b2"]
    assert len(resolve_selection(registry, all_=True)) == 2


def test_empty_selection_is_an_error_not_a_silent_no_op(tmp_path):
    registry = load_registry(write(tmp_path, VALID))
    with pytest.raises(ConfigError, match="select at least one"):
        resolve_selection(registry)
    with pytest.raises(ConfigError, match="unknown collection"):
        resolve_selection(registry, collections=["nope"])


def test_settings_defaults_do_not_require_a_file():
    settings = load_settings(None)
    assert settings.google_docs.target_max_chars == 250_000
    assert settings.fetch.strategy_order[0].value == "markdown"


def test_settings_reject_hard_limit_below_target(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("google_docs:\n  target_max_chars: 100\n  hard_max_chars: 10\n")
    with pytest.raises(ConfigError, match="hard_max_chars"):
        load_settings(path)


def test_settings_reject_auto_inside_strategy_order(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("fetch:\n  strategy_order: [auto, html]\n")
    with pytest.raises(ConfigError, match="strategy_order"):
        load_settings(path)


def test_google_settings_never_expose_secret_values(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "super-secret")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "an-id")
    settings = load_settings(None)
    described = settings.google.describe()
    assert described["client_secret_set"] is True
    assert "super-secret" not in str(described)
    assert "an-id" not in str(described)


# -- .env ergonomics -----------------------------------------------------------------


def test_copying_env_example_verbatim_does_not_crash(tmp_path, monkeypatch):
    """`.env.example` ships every variable blank; that must mean "unset".

    Without this, filling in only the two required values leaves
    GOOGLE_AUTH_METHOD= as an empty string, which fails a Literal check and
    aborts before the tool can say anything useful.
    """
    from aws_doc_sync.config.settings import GoogleSettings

    env = tmp_path / ".env"
    env.write_text(
        "GOOGLE_CLIENT_SECRETS_FILE=/tmp/secret.json\n"
        "GOOGLE_DRIVE_FOLDER_ID=1RealFolderId\n"
        "GOOGLE_AUTH_METHOD=\n"
        "GOOGLE_TOKEN_PATH=\n"
        "GOOGLE_CLIENT_ID=\n"
        "GOOGLE_CLIENT_SECRET=\n"
        "GOOGLE_SERVICE_ACCOUNT_FILE=\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(GoogleSettings.model_config, "env_file", str(env))

    settings = GoogleSettings()

    assert settings.auth_method == "oauth"
    assert settings.drive_folder_id == "1RealFolderId"
    assert settings.client_id is None
    # Blank must fall back to the default, not to Path("") -- which is the
    # current directory, and would make the token unwritable.
    assert settings.token_path == Path(".state/token.json")


def test_a_blank_token_path_never_resolves_to_a_directory(tmp_path, monkeypatch):
    from aws_doc_sync.config.settings import GoogleSettings

    env = tmp_path / ".env"
    env.write_text("GOOGLE_TOKEN_PATH=   \n", encoding="utf-8")
    monkeypatch.setitem(GoogleSettings.model_config, "env_file", str(env))
    assert GoogleSettings().token_path != Path("")


def test_a_path_in_client_secret_is_diagnosed_by_name(monkeypatch):
    """GOOGLE_CLIENT_SECRET and GOOGLE_CLIENT_SECRETS_FILE differ by three chars.

    Getting them the wrong way round otherwise surfaces during the OAuth
    handshake as an error that says nothing about the cause.
    """
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "123.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "/Users/me/client_secret_abc.json")
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "1Folder")

    problems = load_settings(None).google.validate_for_sync()

    assert any("GOOGLE_CLIENT_SECRETS_FILE" in p for p in problems)
    assert any("SECRETS, and FILE" in p for p in problems)


def test_a_quoted_path_is_diagnosed_too(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "123.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "'/Users/me/secret.json'")
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "1Folder")
    assert any(
        "GOOGLE_CLIENT_SECRETS_FILE" in p
        for p in load_settings(None).google.validate_for_sync()
    )


def test_a_real_client_secret_is_not_flagged(monkeypatch, tmp_path):
    secret_file = tmp_path / "client_secret.json"
    secret_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "123.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-abcdefghijklmnop")
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "1Folder")

    assert load_settings(None).google.validate_for_sync() == []


def test_a_credential_inside_the_project_is_flagged(monkeypatch, tmp_path):
    """No .gitignore rule can catch a browser-assigned name like "downloaded (1).json".

    Keeping the file outside the repository is the actual control, so the tool
    says so rather than relying on pattern matching alone.
    """
    monkeypatch.chdir(tmp_path)
    secret = tmp_path / "downloaded (1).json"
    secret.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRETS_FILE", str(secret))
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "1Folder")

    settings = load_settings(None).google
    notes = settings.warnings()

    assert any("inside the project directory" in n for n in notes)
    # A warning, never a blocker: the configuration is still usable.
    assert settings.validate_for_sync() == []


def test_a_credential_outside_the_project_is_not_flagged(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "secrets"
    elsewhere.mkdir()
    secret = elsewhere / "client_secret.json"
    secret.write_text("{}", encoding="utf-8")

    monkeypatch.chdir(project)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRETS_FILE", str(secret))
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "1Folder")

    assert load_settings(None).google.warnings() == []
