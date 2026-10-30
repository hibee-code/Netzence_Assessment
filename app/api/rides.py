from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import Engine

from app.clock import Clock, get_clock
from app.db import get_engine
from app.money import from_milli
from app.schemas import RideCompletedIn, RideCompletedOut
from app.services.rides import RideCompleted, record_completed_ride

router = APIRouter(prefix="/rides", tags=["rides"])


@router.post(
    "/completed",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Duplicate event; original result replayed"},
        409: {"description": "ride_id already recorded with different details"},
    },
)
def complete_ride(
    body: RideCompletedIn,
    response: Response,
    engine: Annotated[Engine, Depends(get_engine)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> RideCompletedOut:
    event = RideCompleted(
        ride_id=body.ride_id,
        driver_id=body.driver_id,
        distance_km=body.distance_km,
        vehicle_type=body.vehicle_type,
        completed_at=body.completed_at,
    )
    # One transaction; committed before the response is built, so a 2xx means it is durable.
    with engine.begin() as conn:
        result = record_completed_ride(conn, event, clock.now())

    if not result.created:
        response.status_code = status.HTTP_200_OK
    return RideCompletedOut(
        ride_id=result.ride_id,
        driver_id=result.driver_id,
        credits=from_milli(result.credits_milli),
        expires_at=result.expires_at,
        status="recorded" if result.created else "duplicate",
    )
