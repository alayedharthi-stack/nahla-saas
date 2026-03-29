"""Salla integration service — FastAPI app (port 8014)."""

import sys
import os

# Make the salla package root and repo root importable
_SALLA_ROOT = os.path.dirname(__file__)
_REPO_ROOT = os.path.abspath(os.path.join(_SALLA_ROOT, "..", ".."))
for path in (_SALLA_ROOT, _REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi import FastAPI
from api.routes import router

app = FastAPI(
    title="Nahla – Salla Integration",
    description="OAuth, webhooks, and product/order/customer sync for Salla stores.",
    version="1.0.0",
)

app.include_router(router)


@app.get("/health")
async def health():
    return {"service": "salla-integration", "status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8014, reload=True)
