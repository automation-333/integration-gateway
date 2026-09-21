from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import Base, get_db
from app.main import app


def configure_test_dependencies(tmp_path):
    test_db_url = f"sqlite:///{tmp_path / 'test.db'}"
    test_engine = create_engine(
        test_db_url,
        connect_args={"check_same_thread": False},
    )
    testing_session = sessionmaker(
        bind=test_engine,
        autoflush=False,
        expire_on_commit=False,
    )
    Base.metadata.create_all(bind=test_engine)

    def override_get_db() -> Generator[Session, None, None]:
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    def override_settings() -> Settings:
        return Settings(
            database_url=test_db_url,
            target_webhook_url=None,
            delivery_base_backoff_seconds=0,
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = override_settings


def clear_test_dependencies() -> None:
    app.dependency_overrides.clear()


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]


def test_idempotent_replay_and_conflict(tmp_path) -> None:
    configure_test_dependencies(tmp_path)

    payload = {
        "event_type": "order.created",
        "source": "crm",
        "data": {"order_id": 42},
    }
    headers = {"Idempotency-Key": "order-42-created"}

    try:
        with TestClient(app) as client:
            first = client.post("/v1/events", json=payload, headers=headers)
            second = client.post("/v1/events", json=payload, headers=headers)

            changed_payload = {
                **payload,
                "data": {"order_id": 43},
            }
            conflict = client.post(
                "/v1/events",
                json=changed_payload,
                headers=headers,
            )
    finally:
        clear_test_dependencies()

    assert first.status_code == 202
    assert first.json()["status"] == "stored"
    assert first.json()["replayed"] is False

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["replayed"] is True

    assert conflict.status_code == 409


def test_retry_requires_target_url(tmp_path) -> None:
    configure_test_dependencies(tmp_path)

    try:
        with TestClient(app) as client:
            created = client.post(
                "/v1/events",
                headers={"Idempotency-Key": "event-without-target"},
                json={
                    "event_type": "lead.created",
                    "source": "website",
                    "data": {"lead_id": 10},
                },
            )
            retry = client.post(
                f"/v1/events/{created.json()['id']}/retry"
            )
    finally:
        clear_test_dependencies()

    assert created.status_code == 202
    assert retry.status_code == 409
    assert retry.json()["detail"] == "No TARGET_WEBHOOK_URL is configured."
