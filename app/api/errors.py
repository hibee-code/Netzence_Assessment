"""Maps domain exceptions to HTTP responses, so endpoints stay thin."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import MAX_COMPLETED_AT_FUTURE_SKEW
from app.money import from_milli
from app.services.redemptions import IdempotencyKeyReused, InsufficientBalance
from app.services.rides import CompletedAtInFuture, RideConflict


def _ride_conflict(_: Request, exc: RideConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": "ride_conflict", "ride_id": exc.ride_id, "conflicting_fields": exc.fields},
    )


def _completed_at_in_future(_: Request, __: CompletedAtInFuture) -> JSONResponse:
    minutes = int(MAX_COMPLETED_AT_FUTURE_SKEW.total_seconds() // 60)
    # Same shape as FastAPI's own request-validation errors.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "type": "completed_at_in_future",
                    "loc": ["body", "completed_at"],
                    "msg": f"completed_at is more than {minutes} minutes in the future",
                }
            ]
        },
    )


def _insufficient_balance(_: Request, exc: InsufficientBalance) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": "insufficient_balance", "available": from_milli(exc.available_milli)},
    )


def _idempotency_key_reused(_: Request, __: IdempotencyKeyReused) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "idempotency_key_reused_with_different_amount"})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RideConflict, _ride_conflict)  # type: ignore[arg-type]
    app.add_exception_handler(CompletedAtInFuture, _completed_at_in_future)  # type: ignore[arg-type]
    app.add_exception_handler(InsufficientBalance, _insufficient_balance)  # type: ignore[arg-type]
    app.add_exception_handler(IdempotencyKeyReused, _idempotency_key_reused)  # type: ignore[arg-type]
