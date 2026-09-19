"""Google credentials.

OAuth is the default because this is meant to run on a developer's machine
against their own Drive. Service accounts are supported for unattended execution
behind the same ``CredentialProvider`` port, so switching does not touch the Drive
or Docs adapters.

Security rules enforced here:

* Nothing credential-bearing is read from YAML -- only from the environment.
* The token cache is created with owner-only permissions.
* No credential value is ever logged; only whether one is present.

Scope choice: ``drive.file`` is requested rather than full ``drive``. It grants
access only to files this application created, which is exactly the set of
documents it must manage, and means a mistake here cannot touch the rest of the
user's Drive.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..config.settings import GoogleSettings
from ..domain.errors import AuthError
from ..logging_setup import get_logger

log = get_logger("google.auth")

DRIVE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents.readonly",
)


class OAuthCredentialProvider:
    """Installed-application OAuth 2.0 flow with a cached refresh token."""

    def __init__(self, settings: GoogleSettings) -> None:
        self._settings = settings
        self._cached: Any = None

    def credentials(self, scopes: Iterable[str] = DRIVE_SCOPES) -> Any:
        if self._cached is not None:
            return self._cached

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise AuthError(f"Google auth libraries are not installed: {exc}") from exc

        scopes = list(scopes)
        token_path = Path(self._settings.token_path)
        creds = None

        if token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), scopes)
            except (ValueError, json.JSONDecodeError) as exc:
                log.warning("oauth_token_unreadable", extra={"path": str(token_path)})
                raise AuthError(
                    f"cached token at {token_path} is unreadable; delete it and re-authorize"
                ) from exc

        if creds and creds.valid:
            self._cached = creds
            return creds

        refresh_failure: str | None = None
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._persist(creds, token_path)
                log.info("oauth_token_refreshed")
                self._cached = creds
                return creds
            except Exception as exc:
                refresh_failure = str(exc)
                log.warning(
                    "oauth_token_refresh_failed",
                    extra={
                        "reason": type(exc).__name__,
                        "expired_refresh_token": _is_expired_grant(refresh_failure),
                        "token_age_days": _age_days(token_path),
                    },
                )

        flow = InstalledAppFlow.from_client_config(self._client_config(), scopes)
        try:
            creds = flow.run_local_server(port=0, open_browser=True)
        except Exception as exc:
            raise AuthError(_consent_failure_message(refresh_failure, token_path)) from exc

        self._persist(creds, token_path)
        log.info("oauth_authorized", extra={"token_path": str(token_path)})
        self._cached = creds
        return creds

    def _client_config(self) -> dict[str, Any]:
        settings = self._settings
        if settings.client_secrets_file:
            path = Path(settings.client_secrets_file)
            if not path.exists():
                raise AuthError(f"client secrets file not found: {path}")
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise AuthError(f"client secrets file is not valid JSON: {path}") from exc

        if settings.client_id and settings.client_secret:
            return {
                "installed": {
                    "client_id": settings.client_id,
                    "client_secret": settings.client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }

        raise AuthError(
            "no OAuth client configured: set GOOGLE_CLIENT_SECRETS_FILE, "
            "or both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
        )

    @staticmethod
    def _persist(creds: Any, token_path: Path) -> None:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        # Owner-only: a cached refresh token is a credential.
        with contextlib.suppress(OSError):  # chmod is a no-op on some platforms
            os.chmod(token_path, 0o600)


class ServiceAccountCredentialProvider:
    """Service-account credentials, optionally impersonating a Workspace user."""

    def __init__(self, settings: GoogleSettings) -> None:
        self._settings = settings
        self._cached: Any = None

    def credentials(self, scopes: Iterable[str] = DRIVE_SCOPES) -> Any:
        if self._cached is not None:
            return self._cached

        try:
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise AuthError(f"Google auth libraries are not installed: {exc}") from exc

        path = self._settings.service_account_file
        if not path or not Path(path).exists():
            raise AuthError(f"service account file not found: {path}")

        creds = service_account.Credentials.from_service_account_file(
            str(path), scopes=list(scopes)
        )
        if self._settings.impersonate_subject:
            creds = creds.with_subject(self._settings.impersonate_subject)

        log.info(
            "service_account_loaded",
            extra={"impersonating": bool(self._settings.impersonate_subject)},
        )
        self._cached = creds
        return creds


def _age_days(token_path: Path) -> float | None:
    """How long ago the cached token was written."""
    try:
        return round((time.time() - token_path.stat().st_mtime) / 86400.0, 1)
    except OSError:
        return None


def _is_expired_grant(message: str | None) -> bool:
    """Whether a refresh failure is Google rejecting the refresh token itself."""
    return bool(message) and "invalid_grant" in (message or "").lower()


def _consent_failure_message(refresh_failure: str | None, token_path: Path) -> str:
    """Explain a failed authorization in terms of its most likely cause.

    The seven-day case is worth naming explicitly: a project whose consent
    screen is *External* with a publishing status of *Testing* is issued refresh
    tokens that expire after a week, so a scheduled sync works for six days and
    then fails with nothing in the message to suggest why.
    """
    lines = ["interactive OAuth consent failed."]

    if _is_expired_grant(refresh_failure):
        age = _age_days(token_path)
        aged = f" (the cached token is {age} days old)" if age is not None else ""
        lines.append(
            f"Google rejected the cached refresh token{aged}. A consent screen set to "
            "External with a publishing status of Testing issues refresh tokens that "
            "expire after 7 days. To stop this recurring, set the audience to Internal, "
            "publish the app, or switch GOOGLE_AUTH_METHOD to service_account."
        )

    lines.append(
        "On a headless machine, authorize once on a desktop and copy the resulting "
        "token file to GOOGLE_TOKEN_PATH, or use a service account."
    )
    return " ".join(lines)


def build_credential_provider(settings: GoogleSettings):
    """Select the provider named by ``GOOGLE_AUTH_METHOD``."""
    if settings.auth_method == "service_account":
        return ServiceAccountCredentialProvider(settings)
    return OAuthCredentialProvider(settings)
