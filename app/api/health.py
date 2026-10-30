from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.db import get_engine

router = APIRouter()


@router.get("/health")
def health(engine: Annotated[Engine, Depends(get_engine)]) -> JSONResponse:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(
            status_code=503, content={"status": "error", "database": "unavailable"}
        )
    return JSONResponse(status_code=200, content={"status": "ok", "database": "ok"})
