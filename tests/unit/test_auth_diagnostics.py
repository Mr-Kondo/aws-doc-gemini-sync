"""Diagnostics around OAuth token expiry.

The seven-day rule cannot be fixed in code -- it is a property of the consent
screen's configuration. What code can do is make the resulting failure legible,
so a scheduled sync that works for six days and then stops says why.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from aws_doc_sync.config.settings import _token_age_days, load_settings
from aws_doc_sync.google.auth import (
    _age_days,
    _consent_failure_message,
    _is_expired_grant,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("('invalid_grant: Token has been expired or revoked.', {})", True),
        ("invalid_grant", True),
        ("INVALID_GRANT: bad", True),
        ("invalid_client: wrong secret", False),
        ("connection reset", False),
        (None, False),
    ],
)
def test_an_expired_refresh_token_is_recognised(message, expected):
    assert _is_expired_grant(message) is expected


def test_the_seven_day_rule_is_named_when_the_grant_expired(tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")

    message = _consent_failure_message("invalid_grant: Token has been expired", token)

    assert "7 days" in message
    assert "Testing" in message
    assert "service_account" in message


def test_an_unrelated_failure_does_not_blame_the_seven_day_rule(tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")

    message = _consent_failure_message("connection refused", token)

    assert "7 days" not in message
    # The headless advice still applies whatever the cause.
    assert "headless" in message


def test_the_message_reports_the_cached_token_age(tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    eight_days_ago = time.time() - 8 * 86400
    os.utime(token, (eight_days_ago, eight_days_ago))

    message = _consent_failure_message("invalid_grant", token)

    assert "8.0 days old" in message


def test_token_age_is_none_when_there_is_no_token(tmp_path):
    assert _age_days(tmp_path / "absent.json") is None
    assert _token_age_days(tmp_path / "absent.json") is None


def test_token_age_is_reported_in_the_config_summary(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    three_days_ago = time.time() - 3 * 86400
    os.utime(token, (three_days_ago, three_days_ago))

    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token))
    described = load_settings(None).google.describe()

    assert described["token_cached"] is True
    assert described["token_age_days"] == pytest.approx(3.0, abs=0.1)


def test_the_summary_still_hides_secret_values(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-do-not-print-me")
    described = load_settings(None).google.describe()
    assert "GOCSPX-do-not-print-me" not in str(described)


def test_no_token_is_reported_as_absent_rather_than_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "nothing.json"))
    described = load_settings(None).google.describe()
    assert described["token_cached"] is False
    assert described["token_age_days"] is None


def test_age_helper_rounds_to_one_decimal(tmp_path):
    token = tmp_path / "t.json"
    token.write_text("{}", encoding="utf-8")
    os.utime(token, (time.time() - 36 * 3600, time.time() - 36 * 3600))
    assert _age_days(Path(token)) == pytest.approx(1.5, abs=0.1)
