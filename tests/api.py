"""HTTP helpers for API-level tests: request shapes and defaults live in one place.

Use as `from tests import api`, then `api.complete_ride(client)`, `api.balance(client)`, ...
"""

from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response


def ride_payload(ride_id: str = "r1", **overrides: Any) -> dict[str, Any]:
    """A valid completion event: 12.3 km electric ride (1.476 credits) by driver d1."""
    payload: dict[str, Any] = {
        "ride_id": ride_id,
        "driver_id": "d1",
        "distance_km": "12.3",
        "vehicle_type": "electric",
        "completed_at": "2026-05-31T09:00:00Z",
    }
    return payload | overrides


def complete_ride(client: TestClient, ride_id: str = "r1", **overrides: Any) -> Response:
    return client.post("/rides/completed", json=ride_payload(ride_id, **overrides))


def cancel_ride(client: TestClient, ride_id: str = "r1", reason: str | None = None) -> Response:
    return client.post("/rides/cancelled", json={"ride_id": ride_id, "reason": reason})


def redeem(client: TestClient, amount: Any, key: str | None = "key-1", driver_id: str = "d1", **body: Any) -> Response:
    """`key=None` sends no Idempotency-Key header."""
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post(f"/drivers/{driver_id}/redeem", json={"amount": amount, **body}, headers=headers)


def balance(client: TestClient, driver_id: str = "d1") -> str:
    return client.get(f"/drivers/{driver_id}/balance").json()["balance"]


def history(client: TestClient, driver_id: str = "d1", **params: Any) -> list[dict[str, Any]]:
    response = client.get(f"/drivers/{driver_id}/history", params=params)
    assert response.status_code == 200
    return response.json()["entries"]
