from fastapi import FastAPI

from app.api import health

app = FastAPI(title="Carbon Credits Service")
app.include_router(health.router)
