import httpx
import pytest

from app.delivery import deliver_event


@pytest.mark.asyncio
async def test_transient_failure_is_retried() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="temporary")
        return httpx.Response(204)

    result = await deliver_event(
        url="https://target.test/webhook",
        event_id="event-1",
        idempotency_key="key-1",
        payload={"event_type": "demo", "source": "test", "data": {}},
        timeout_seconds=1,
        max_attempts=3,
        base_backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    assert result.success is True
    assert result.attempts == 2
    assert result.status_code == 204
    assert calls == 2


@pytest.mark.asyncio
async def test_non_retryable_client_error_stops_immediately() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    result = await deliver_event(
        url="https://target.test/webhook",
        event_id="event-1",
        idempotency_key="key-1",
        payload={"event_type": "demo", "source": "test", "data": {}},
        timeout_seconds=1,
        max_attempts=3,
        base_backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    assert result.success is False
    assert result.attempts == 1
    assert result.status_code == 400
    assert calls == 1
