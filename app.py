"""FastAPI application for the Store Anomaly Monitor dashboard API."""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers.dashboard import router as dashboard_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Store Anomaly Monitor API",
    version="1.0.0",
    description="Dashboard API for store anomaly monitoring and KRA tracking.",
)

# Allow requests from any Vercel preview or production deployment.
# The regex covers *.vercel.app but never uses a bare wildcard.
_VERCEL_ORIGIN_REGEX = r"https://[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)*\.vercel\.app"

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_VERCEL_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(dashboard_router)


@app.get("/health")
def health_check() -> dict:
    """Return a simple health-check payload.

    Returns
    -------
    dict
        ``{"status": "ok", "service": "store-anomaly-api", "version": "1.0.0"}``
    """
    return {"status": "ok", "service": "store-anomaly-api", "version": "1.0.0"}
