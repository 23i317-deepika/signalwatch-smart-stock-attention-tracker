"""
SignalWatch FastAPI application entrypoint: app instance, CORS, DB
initialization on startup, health check, and router registration.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db
from app.routers import dashboard, watchlist

# TEMPORARY: makes the app.* loggers (market data fetch/cache/dashboard
# warnings) visible in the uvicorn console. Safe to remove once market data
# issues are no longer being actively diagnosed.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="SignalWatch API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(watchlist.router)
app.include_router(dashboard.router)

# Registered here in a later phase:
# from app.routers import stocks
# app.include_router(stocks.router)
