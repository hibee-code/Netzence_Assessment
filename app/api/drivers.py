from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import Engine

from app.clock import Clock, get_clock
from app.db import get_engine
from app.money import from_milli, to_milli
from app.schemas import AllocationOut, BalanceOut, RedeemIn, RedeemOut
from app.services.balances import get_balance_milli
from app.services.redemptions import redeem

router = APIRouter(prefix="/drivers", tags=["drivers"])


@router.get("/{driver_id}/balance")
def get_balance(
    driver_id: str,
    engine: Annotated[Engine, Depends(get_engine)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> BalanceOut:
    # A transaction, not a plain read: lazy expiry may write EXPIRY entries.
    with engine.begin() as conn:
        balance = get_balance_milli(conn, driver_id, clock.now())
    return BalanceOut(driver_id=driver_id, balance=from_milli(balance))


@router.post(
    "/{driver_id}/redeem",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Repeated Idempotency-Key; original redemption replayed"},
        400: {"description": "Idempotency-Key header missing"},
        409: {"description": "insufficient_balance, or Idempotency-Key reused with a different amount"},
    },
)
def redeem_credits(
    driver_id: str,
    body: RedeemIn,
    response: Response,
    engine: Annotated[Engine, Depends(get_engine)],
    clock: Annotated[Clock, Depends(get_clock)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> RedeemOut:
    if not idempotency_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="idempotency_key_required")

    with engine.begin() as conn:
        result = redeem(conn, driver_id, to_milli(body.amount), idempotency_key, body.reason, clock.now())

    if not result.created:
        response.status_code = status.HTTP_200_OK
    return RedeemOut(
        redemption_id=result.redemption_id,
        amount=from_milli(result.amount_milli),
        balance_after=from_milli(result.balance_after_milli),
        allocations=[
            AllocationOut(lot_id=a.lot_id, ride_id=a.ride_id, amount=from_milli(a.amount_milli))
            for a in result.allocations
        ],
    )
