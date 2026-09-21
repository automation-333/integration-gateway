import hashlib
import json
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, init_db
from app.delivery import deliver_event
from app.models import EventRecord
from app.schemas import EventIn, EventOut, HealthOut, RetryOut


def normalize_payload(payload: EventIn) -> tuple[dict, str, str]:
    payload_dict = payload.model_dump(mode="json")
    normalized = json.dumps(
        payload_dict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return payload_dict, normalized, digest


def to_event_out(record: EventRecord, *, replayed: bool = False) -> EventOut:
    return EventOut(
        id=record.id,
        event_type=record.event_type,
        source=record.source,
        status=record.status,
        attempts=record.attempts,
        target_url=record.target_url,
        response_code=record.response_code,
        last_error=record.last_error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        replayed=replayed,
    )


async def execute_delivery(
    *,
    record: EventRecord,
    payload_dict: dict,
    db: Session,
    settings: Settings,
) -> None:
    if not record.target_url:
        record.status = "stored"
        db.commit()
        db.refresh(record)
        return

    record.status = "pending"
    db.commit()
    db.refresh(record)

    result = await deliver_event(
        url=record.target_url,
        event_id=record.id,
        idempotency_key=record.idempotency_key,
        payload=payload_dict,
        timeout_seconds=settings.delivery_timeout_seconds,
        max_attempts=settings.delivery_max_attempts,
        base_backoff_seconds=settings.delivery_base_backoff_seconds,
    )

    record.attempts += result.attempts
    record.response_code = result.status_code
    record.response_body = result.response_body
    record.last_error = result.error
    record.status = "delivered" if result.success else "failed"

    db.commit()
    db.refresh(record)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Integration Gateway",
    version="0.1.0",
    description="Webhook gateway with idempotency, persistence and retryable delivery.",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {
        "service": "integration-gateway",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(status="ok")


@app.get("/ready", response_model=HealthOut)
def ready(db: Session = Depends(get_db)) -> HealthOut:
    db.execute(text("SELECT 1"))
    return HealthOut(status="ready")


@app.post("/v1/events", response_model=EventOut)
async def ingest_event(
    payload: EventIn,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> EventOut:
    payload_dict, normalized, digest = normalize_payload(payload)

    existing = db.scalar(
        select(EventRecord).where(EventRecord.idempotency_key == idempotency_key)
    )

    if existing:
        if existing.payload_hash != digest:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key was already used with a different payload.",
            )

        response.status_code = status.HTTP_200_OK
        return to_event_out(existing, replayed=True)

    record = EventRecord(
        idempotency_key=idempotency_key,
        payload_hash=digest,
        event_type=payload.event_type,
        source=payload.source,
        payload_json=normalized,
        target_url=settings.target_webhook_url or None,
        status="pending" if settings.target_webhook_url else "stored",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    await execute_delivery(
        record=record,
        payload_dict=payload_dict,
        db=db,
        settings=settings,
    )

    response.status_code = status.HTTP_202_ACCEPTED
    return to_event_out(record)


@app.get("/v1/events/{event_id}", response_model=EventOut)
def get_event(event_id: str, db: Session = Depends(get_db)) -> EventOut:
    record = db.get(EventRecord, event_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found.",
        )
    return to_event_out(record)


@app.post("/v1/events/{event_id}/retry", response_model=RetryOut)
async def retry_event(
    event_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RetryOut:
    record = db.get(EventRecord, event_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found.",
        )

    if record.status == "delivered":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Delivered events are not retried automatically.",
        )

    target_url = record.target_url or settings.target_webhook_url
    if not target_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No TARGET_WEBHOOK_URL is configured.",
        )

    record.target_url = target_url
    payload_dict = json.loads(record.payload_json)

    await execute_delivery(
        record=record,
        payload_dict=payload_dict,
        db=db,
        settings=settings,
    )

    return RetryOut(**to_event_out(record).model_dump())
