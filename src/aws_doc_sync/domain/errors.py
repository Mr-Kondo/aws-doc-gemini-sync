"""Typed error hierarchy.

Failure isolation depends on being able to tell *which* failures are confined to a
single source (and therefore survivable) from those that invalidate the whole run.
"""

from __future__ import annotations


class AwsDocSyncError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(AwsDocSyncError):
    """Configuration is missing, malformed, or internally inconsistent.

    Always fatal: a bad registry cannot be partially applied.
    """


class SourceError(AwsDocSyncError):
    """A failure scoped to one document source. Isolated, never fatal."""

    def __init__(self, message: str, *, source_url: str | None = None) -> None:
        super().__init__(message)
        self.source_url = source_url


class FetchError(SourceError):
    """The document could not be retrieved from AWS."""


class NotFoundError(FetchError):
    """The document does not exist upstream (HTTP 404/410).

    Distinct from FetchError because it is the signal for *orphan* detection
    rather than a transient failure worth retrying.
    """


class NormalizationError(SourceError):
    """Retrieved bytes could not be turned into a normalized document."""


class StoreError(AwsDocSyncError):
    """A failure in the destination document store (Google Drive/Docs)."""


class AuthError(StoreError):
    """Credentials are absent, invalid, or lack the required scopes."""
