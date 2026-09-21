import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

RETRYABLE_STATUS_CODES = {408, 425, 429}


@dataclass(slots=True)
class DeliveryResult:
    success: bool
    attempts: int
    status_code: int | None = None
    response_body: str | None = None
    error: str | None = None


def is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES or status_code >= 500


async def deliver_event(
    *,
    url: str,
    event_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    max_attempts: int,
    base_backoff_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DeliveryResult:
    max_attempts = max(1, max_attempts)
    headers = {
        "Idempotency-Key": idempotency_key,
        "X-Integration-Event-Id": event_id,
        "User-Agent": "integration-gateway/0.1",
    }

    last_error: str | None = None
    last_status: int | None = None
    last_body: str | None = None

    async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.post(url, json=payload, headers=headers)
                last_status = response.status_code
                last_body = response.text[:1000]

                if 200 <= response.status_code < 300:
                    return DeliveryResult(
                        success=True,
                        attempts=attempt,
                        status_code=response.status_code,
                        response_body=last_body,
                    )

                if not is_retryable_status(response.status_code):
                    return DeliveryResult(
                        success=False,
                        attempts=attempt,
                        status_code=response.status_code,
                        response_body=last_body,
                        error=f"non-retryable HTTP {response.status_code}",
                    )

                last_error = f"retryable HTTP {response.status_code}"
            except httpx.RequestError as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < max_attempts:
                delay = base_backoff_seconds * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

    return DeliveryResult(
        success=False,
        attempts=max_attempts,
        status_code=last_status,
        response_body=last_body,
        error=last_error or "delivery failed",
    )
