from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import Engine

from app.clock import Clock, get_clock
from app.db import get_engine
from app.money import from_milli
from app.schemas import RideCancelledIn, RideCancelledOut, RideCompletedIn, RideCompletedOut
from app.services.cancellations import cancel_ride
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
        status="duplicate" if not result.created else "cancelled" if result.cancelled else "recorded",
    )


@router.post(
    "/cancelled",
    responses={
        200: {"description": "Ride cancelled (or already was; original outcome replayed)"},
        202: {"description": "Ride not completed yet; cancellation stored and applied on completion"},
    },
)
def cancel_completed_ride(
    body: RideCancelledIn,
    response: Response,
    engine: Annotated[Engine, Depends(get_engine)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> RideCancelledOut:
    with engine.begin() as conn:
        result = cancel_ride(conn, body.ride_id, body.reason, clock.now())

    if result.status == "pending_completion":
        response.status_code = status.HTTP_202_ACCEPTED
    return RideCancelledOut(
        ride_id=result.ride_id,
        status=result.status,
        reversed=None if result.reversed_milli is None else from_milli(result.reversed_milli),
        shortfall=None if result.shortfall_milli is None else from_milli(result.shortfall_milli),
    )
