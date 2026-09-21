from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    event_type: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=120)
    data: dict[str, Any] = Field(default_factory=dict)


class EventOut(BaseModel):
    id: str
    event_type: str
    source: str
    status: str
    attempts: int
    target_url: str | None
    response_code: int | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    replayed: bool = False


class HealthOut(BaseModel):
    status: str


class RetryOut(EventOut):
    replayed: bool = False
