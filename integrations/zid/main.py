"""Zid integration service — FastAPI app (port 8013)."""

import sys
import os

_ZID_ROOT = os.path.dirname(__file__)
_REPO_ROOT = os.path.abspath(os.path.join(_ZID_ROOT, "..", ".."))
for path in (_ZID_ROOT, _REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi import FastAPI
from api.routes import router

app = FastAPI(
    title="Nahla – Zid Integration",
    description="OAuth, webhooks, and product/order/customer sync for Zid stores.",
    version="1.0.0",
)

app.include_router(router)


@app.get("/health")
async def health():
    return {"service": "zid-integration", "status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8015, reload=True)
