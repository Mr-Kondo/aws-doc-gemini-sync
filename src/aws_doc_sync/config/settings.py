"""Application settings.

Two sources, kept apart on purpose:

* ``settings.yaml`` -- behaviour knobs, safe to commit and review.
* environment / ``.env`` -- credentials and the destination folder id, never committed.

Nothing that could identify or authenticate an account is read from YAML.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..domain.errors import ConfigError
from ..domain.models import FetchStrategy


class HttpSettings(BaseModel):
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0
    max_retries: int = Field(default=4, ge=0, le=10)
    backoff_base_seconds: float = Field(default=0.5, gt=0)
    backoff_max_seconds: float = Field(default=30.0, gt=0)
    #: Politeness throttle applied across all AWS requests in a run.
    requests_per_second: float = Field(default=3.0, gt=0)
    user_agent: str = "aws-doc-sync/0.1 (+https://docs.aws.amazon.com; documentation mirror)"
    follow_redirects: bool = True


class FetchSettings(BaseModel):
    #: Priority order of fetch backends for sources set to ``auto``.
    strategy_order: tuple[FetchStrategy, ...] = (
        FetchStrategy.MARKDOWN,
        FetchStrategy.MCP,
        FetchStrategy.HTML,
    )
    #: Reject a ``.md`` response whose Content-Type is not markdown/plain. AWS
    #: serves an HTML 404 page for missing ``.md`` siblings, so this matters.
    require_markdown_content_type: bool = True
    #: A ``.md`` response shorter than this is treated as a stub and the chain
    #: falls through to the next backend.
    min_markdown_chars: int = Field(default=200, ge=0)

    @field_validator("strategy_order")
    @classmethod
    def _no_auto_in_order(cls, v: tuple[FetchStrategy, ...]) -> tuple[FetchStrategy, ...]:
        if FetchStrategy.AUTO in v:
            raise ValueError("fetch.strategy_order must not contain 'auto'")
        if not v:
            raise ValueError("fetch.strategy_order must not be empty")
        return v


class NormalizeSettings(BaseModel):
    #: Strip the ``<a name="..."></a>`` anchors AWS emits in its markdown.
    strip_anchor_tags: bool = True
    #: Maximum consecutive blank lines outside fenced code.
    max_consecutive_blank_lines: int = Field(default=1, ge=1)
    #: Rewrite relative ``foo.md`` links to absolute ``foo.html`` URLs so the
    #: reference survives outside the docs site.
    absolutize_links: bool = True


class GoogleDocsSettings(BaseModel):
    target_max_chars: int = Field(default=250_000, gt=0)
    hard_max_chars: int = Field(default=500_000, gt=0)
    #: ``auto`` probes Drive's importFormats and prefers text/markdown.
    import_mime_type: Literal["auto", "text/markdown", "text/plain"] = "auto"
    #: Suffix format for split parts. Must stay stable across runs.
    part_suffix_format: str = "_{part:02d}"

    @field_validator("hard_max_chars")
    @classmethod
    def _hard_ge_target(cls, v: int, info) -> int:
        target = info.data.get("target_max_chars")
        if target is not None and v < target:
            raise ValueError("google_docs.hard_max_chars must be >= target_max_chars")
        return v


class ChangeDetectionSettings(BaseModel):
    #: Use document-history RSS as a fast path. Hash comparison stays the
    #: source of truth either way.
    use_rss: bool = True
    rss_lookback_days: int = Field(default=30, ge=1)
    #: Sources never seen before are always fetched, regardless of RSS.
    always_fetch_unknown: bool = True
    #: Send If-None-Match / If-Modified-Since for sources whose validators are
    #: known. Like RSS, this is an optimization: ``scan`` and ``sync --full``
    #: ignore it so a wrong ETag upstream cannot hide a change indefinitely.
    use_conditional_requests: bool = True


class ManifestSettings(BaseModel):
    path: Path = Path(".state/manifest.json")
    #: Keep the previous manifest as ``<name>.bak`` before each write.
    backup: bool = True
    #: Content-addressed store of normalized documents, which is what makes a
    #: 304 usable. Defaults to a ``content-cache`` directory beside the manifest.
    content_cache_path: Path | None = None
    content_cache_enabled: bool = True
    #: Drop cached entries no manifest hash refers to, at the end of a run.
    prune_content_cache: bool = True
    #: How long to wait for a concurrent run to release the manifest lock.
    lock_timeout_seconds: float = Field(default=30.0, gt=0)

    def cache_path(self) -> Path:
        return self.content_cache_path or (self.path.parent / "content-cache")


class McpSettings(BaseModel):
    """AWS Documentation MCP Server backend.

    Disabled by default: the pipeline must work on a machine with no MCP runtime.
    """

    enabled: bool = False
    command: str = "uvx"
    args: tuple[str, ...] = ("awslabs.aws-documentation-mcp-server@latest",)
    tool_name: str = "read_documentation"
    startup_timeout_seconds: float = 60.0
    request_timeout_seconds: float = 120.0
    #: Characters requested per call. The tool's own default is 5000, which
    #: silently truncates most pages, so this is always sent explicitly and
    #: pagination continues until the server reports exhaustion.
    max_length: int = Field(default=100_000, gt=0, le=999_999)


class LoggingSettings(BaseModel):
    level: str = "INFO"
    format: Literal["json", "text"] = "json"


class AppSettings(BaseModel):
    """Everything that comes from ``settings.yaml``."""

    http: HttpSettings = HttpSettings()
    fetch: FetchSettings = FetchSettings()
    normalize: NormalizeSettings = NormalizeSettings()
    google_docs: GoogleDocsSettings = GoogleDocsSettings()
    change_detection: ChangeDetectionSettings = ChangeDetectionSettings()
    manifest: ManifestSettings = ManifestSettings()
    mcp: McpSettings = McpSettings()
    logging: LoggingSettings = LoggingSettings()


class GoogleSettings(BaseSettings):
    """Credential-bearing settings. Environment / ``.env`` only."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: Any) -> Any:
        """Treat ``NAME=`` in .env as absent rather than as an empty value.

        ``.env.example`` ships every variable with an empty value, which is the
        point of an example file. Without this, copying it and filling in only
        the two required values makes ``GOOGLE_AUTH_METHOD=`` fail a Literal
        check and ``GOOGLE_TOKEN_PATH=`` resolve to the current directory -- the
        first crashing loudly and the second silently wrong.
        """
        if isinstance(data, dict):
            return {
                key: value
                for key, value in data.items()
                if not (isinstance(value, str) and not value.strip())
            }
        return data

    auth_method: Literal["oauth", "service_account"] = Field(
        default="oauth", validation_alias="GOOGLE_AUTH_METHOD"
    )
    client_secrets_file: Path | None = Field(
        default=None, validation_alias="GOOGLE_CLIENT_SECRETS_FILE"
    )
    client_id: str | None = Field(default=None, validation_alias="GOOGLE_CLIENT_ID")
    client_secret: str | None = Field(default=None, validation_alias="GOOGLE_CLIENT_SECRET")
    token_path: Path = Field(
        default=Path(".state/token.json"), validation_alias="GOOGLE_TOKEN_PATH"
    )
    drive_folder_id: str | None = Field(
        default=None, validation_alias="GOOGLE_DRIVE_FOLDER_ID"
    )
    service_account_file: Path | None = Field(
        default=None, validation_alias="GOOGLE_SERVICE_ACCOUNT_FILE"
    )
    impersonate_subject: str | None = Field(
        default=None, validation_alias="GOOGLE_IMPERSONATE_SUBJECT"
    )

    def describe(self) -> dict[str, Any]:
        """Redacted summary, safe for logs and ``validate-config`` output."""
        return {
            "auth_method": self.auth_method,
            "client_secrets_file": str(self.client_secrets_file)
            if self.client_secrets_file
            else None,
            "client_id_set": bool(self.client_id),
            "client_secret_set": bool(self.client_secret),
            "token_path": str(self.token_path),
            "token_cached": self.token_path.exists(),
            "token_age_days": _token_age_days(self.token_path),
            "drive_folder_id_set": bool(self.drive_folder_id),
            "service_account_file_set": bool(self.service_account_file),
        }

    def warnings(self) -> list[str]:
        """Concerns that do not block a sync but are worth saying out loud.

        The one that matters: a credential file sitting inside the repository.
        ``.gitignore`` covers the names these files usually arrive with, but a
        browser-assigned name like ``downloaded (1).json`` matches no rule that
        could have been written in advance. Keeping the file outside the project
        is the control; pattern matching is only the safety net.
        """
        notes: list[str] = []
        try:
            project = Path.cwd().resolve()
        except OSError:  # pragma: no cover - unusual filesystem state
            return notes

        for label, value in (
            ("GOOGLE_CLIENT_SECRETS_FILE", self.client_secrets_file),
            ("GOOGLE_SERVICE_ACCOUNT_FILE", self.service_account_file),
        ):
            if value is None:
                continue
            resolved = Path(value).expanduser()
            try:
                resolved = resolved.resolve()
            except OSError:  # pragma: no cover
                continue
            if resolved.is_relative_to(project):
                notes.append(
                    f"{label} points inside the project directory "
                    f"({resolved.name}). Credentials are safer outside the "
                    f"repository, for example ~/.config/aws-doc-sync/"
                )
        return notes

    def validate_for_sync(self) -> list[str]:
        """Problems that would stop a real (non dry-run) sync. Never raises."""
        problems: list[str] = []
        if not self.drive_folder_id:
            problems.append("GOOGLE_DRIVE_FOLDER_ID is not set")
        if self.auth_method == "oauth":
            has_file = self.client_secrets_file is not None
            has_pair = bool(self.client_id and self.client_secret)

            # GOOGLE_CLIENT_SECRET and GOOGLE_CLIENT_SECRETS_FILE differ by three
            # characters and sit next to each other in any example file. Putting
            # the path in the wrong one otherwise fails much later, during the
            # OAuth handshake, with an error that says nothing about the cause.
            if self.client_secret and _looks_like_a_path(self.client_secret):
                problems.append(
                    "GOOGLE_CLIENT_SECRET looks like a file path. That variable takes "
                    "the secret string itself; the downloaded JSON belongs in "
                    "GOOGLE_CLIENT_SECRETS_FILE (note: SECRETS, and FILE)"
                )
            if self.client_id and _looks_like_a_path(self.client_id):
                problems.append(
                    "GOOGLE_CLIENT_ID looks like a file path; it takes the client id "
                    "string (it ends in .apps.googleusercontent.com)"
                )
            if not (has_file or has_pair):
                problems.append(
                    "OAuth requires GOOGLE_CLIENT_SECRETS_FILE, "
                    "or both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
                )
            if has_file and not self.client_secrets_file.exists():  # type: ignore[union-attr]
                problems.append(f"client secrets file not found: {self.client_secrets_file}")
        else:
            if not self.service_account_file:
                problems.append("GOOGLE_SERVICE_ACCOUNT_FILE is required for service_account auth")
            elif not self.service_account_file.exists():
                problems.append(f"service account file not found: {self.service_account_file}")
        return problems


def _token_age_days(path: Path) -> float | None:
    """Age of the cached OAuth token, or None if there is not one yet.

    Reported because an External + Testing consent screen issues refresh tokens
    that expire after 7 days; seeing the age makes that failure predictable
    instead of mysterious.
    """
    try:
        return round((time.time() - path.stat().st_mtime) / 86400.0, 1)
    except OSError:
        return None


def _looks_like_a_path(value: str) -> bool:
    """Whether a value that should be an opaque string is obviously a path."""
    cleaned = value.strip().strip("'\"")
    return (
        cleaned.startswith(("/", "~", "./", "../"))
        or cleaned.endswith(".json")
        or "\\" in cleaned
    )


class Settings(BaseModel):
    """Merged view handed to the rest of the application."""

    app: AppSettings = AppSettings()
    google: GoogleSettings = Field(default_factory=GoogleSettings)
    settings_path: Path | None = None

    # Convenience passthroughs keep call sites from reaching three levels deep.
    @property
    def http(self) -> HttpSettings:
        return self.app.http

    @property
    def fetch(self) -> FetchSettings:
        return self.app.fetch

    @property
    def google_docs(self) -> GoogleDocsSettings:
        return self.app.google_docs

    @property
    def manifest_path(self) -> Path:
        override = os.environ.get("AWS_DOC_SYNC_MANIFEST_PATH")
        return Path(override) if override else self.app.manifest.path

    @property
    def content_cache_path(self) -> Path:
        if self.app.manifest.content_cache_path is not None:
            return self.app.manifest.content_cache_path
        return self.manifest_path.parent / "content-cache"


def load_settings(path: Path | str | None = None) -> Settings:
    """Load settings from YAML (if present) plus environment.

    A missing settings file is not an error: every knob has a defensible default,
    and requiring a file would make ``--help`` and unit tests need one.
    """
    app = AppSettings()
    resolved: Path | None = None
    if path is not None:
        resolved = Path(path)
        if not resolved.exists():
            raise ConfigError(f"settings file not found: {resolved}")
        try:
            data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"settings file is not valid YAML: {resolved}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"settings file must contain a mapping: {resolved}")
        try:
            app = AppSettings.model_validate(data)
        except Exception as exc:  # pydantic ValidationError
            raise ConfigError(f"invalid settings in {resolved}: {exc}") from exc

    return Settings(app=app, google=GoogleSettings(), settings_path=resolved)
