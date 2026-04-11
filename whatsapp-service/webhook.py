from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

import logging
import os

import httpx

router = APIRouter()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("whatsapp-webhook")

_BACKEND_BASE = os.getenv("BACKEND_URL", "http://localhost:8000")
_BACKEND_WA_WEBHOOK = f"{_BACKEND_BASE}/webhook/whatsapp"

@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token")
):
    """
    Deprecated compatibility endpoint.

    Forward verification requests to the canonical webhook owner:
      backend/routers/whatsapp_webhook.py  →  GET /webhook/whatsapp
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _BACKEND_WA_WEBHOOK,
                params={
                    "hub.mode": hub_mode,
                    "hub.challenge": hub_challenge,
                    "hub.verify_token": hub_verify_token,
                },
            )
        logger.warning(
            "[whatsapp-service] deprecated webhook verify path used — forwarded to backend"
        )
        return PlainTextResponse(resp.text, status_code=resp.status_code)
    except httpx.HTTPError as exc:
        logger.error("[whatsapp-service] backend webhook verify unavailable: %s", exc)
        raise HTTPException(status_code=502, detail="Canonical WhatsApp webhook unavailable")


@router.post("/webhook")
async def receive_message(request: Request):
    try:
        data = await request.json()
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(_BACKEND_WA_WEBHOOK, json=data)
        logger.warning(
            "[whatsapp-service] deprecated webhook POST path used — forwarded to backend"
        )
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except httpx.HTTPError as exc:
        logger.error("[whatsapp-service] backend webhook unavailable: %s", exc)
        return JSONResponse(content={"status": "error", "detail": "Canonical WhatsApp webhook unavailable"}, status_code=502)
    except Exception as e:
        logger.error(f"Error forwarding WhatsApp message: {e}")
        return JSONResponse(content={"status": "error"}, status_code=500)
