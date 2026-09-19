import httpx
import pytest

from aws_doc_sync.domain.errors import FetchError, NotFoundError
from aws_doc_sync.fetchers.http_client import RetryPolicy, parse_retry_after
from tests.conftest import mock_client

POLICY = RetryPolicy(
    max_retries=3, backoff_base_seconds=1.0, backoff_max_seconds=10.0, jitter=False
)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retries_throttling_and_server_errors(status):
    assert POLICY.should_retry(attempt=0, status=status, transport_error=False)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
def test_never_retries_other_client_errors(status):
    # Repeating a 403 or a 404 is useless and impolite.
    assert not POLICY.should_retry(attempt=0, status=status, transport_error=False)


def test_retries_transport_errors_but_stops_at_the_limit():
    assert POLICY.should_retry(attempt=0, status=None, transport_error=True)
    assert not POLICY.should_retry(attempt=3, status=None, transport_error=True)


def test_backoff_is_exponential_and_capped():
    assert [POLICY.delay_for(i) for i in range(5)] == [1.0, 2.0, 4.0, 8.0, 10.0]


def test_jitter_never_exceeds_the_capped_backoff():
    jittered = RetryPolicy(backoff_base_seconds=1.0, backoff_max_seconds=4.0, jitter=True)
    assert all(0.0 <= jittered.delay_for(3) <= 4.0 for _ in range(50))


def test_retry_after_header_wins_over_backoff():
    assert POLICY.delay_for(0, retry_after=7.0) == 7.0
    assert POLICY.delay_for(0, retry_after=99.0) == 10.0  # still capped


def test_parse_retry_after_accepts_seconds_and_http_dates():
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("not a date") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_client_retries_then_succeeds():
    attempts = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text="ok")

    client = mock_client(handler, max_retries=3, sleeps=sleeps)
    assert client.get("https://docs.aws.amazon.com/x.md").text == "ok"
    assert attempts["n"] == 3
    assert len(sleeps) == 2


def test_client_gives_up_after_max_retries():
    sleeps: list[float] = []
    client = mock_client(lambda r: httpx.Response(503), max_retries=2, sleeps=sleeps)
    with pytest.raises(FetchError, match="503"):
        client.get("https://docs.aws.amazon.com/x.md")
    assert len(sleeps) == 2


def test_client_raises_not_found_immediately_without_sleeping():
    sleeps: list[float] = []
    client = mock_client(lambda r: httpx.Response(404), max_retries=5, sleeps=sleeps)
    with pytest.raises(NotFoundError):
        client.get("https://docs.aws.amazon.com/x.md")
    assert sleeps == []


def test_transport_errors_are_wrapped_not_leaked():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = mock_client(handler, max_retries=1)
    with pytest.raises(FetchError, match="ConnectError"):
        client.get("https://docs.aws.amazon.com/x.md")
