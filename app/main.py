from fastapi import FastAPI

from app.api import drivers, health, rides
from app.api.errors import register_error_handlers

app = FastAPI(title="Carbon Credits Service")
register_error_handlers(app)
app.include_router(health.router)
app.include_router(rides.router)
app.include_router(drivers.router)
