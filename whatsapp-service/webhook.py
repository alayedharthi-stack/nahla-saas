from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

import logging
from ai_client import get_ai_response
from whatsapp_client import send_whatsapp_message
from branding import apply_branding, get_tenant_branding_config, is_welcome_input
import asyncio

router = APIRouter()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("whatsapp-webhook")

VERIFY_TOKEN = "nahla_verify_token"  # Set this securely in production

@router.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token")
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully.")
        return PlainTextResponse(hub_challenge or "", status_code=200)
    logger.warning("Webhook verification failed.")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook")
async def receive_message(request: Request):
    data = await request.json()
    logger.info(f"Received webhook payload: {data}")
    try:
        entry = data.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        phone_number_id = value.get("metadata", {}).get("phone_number_id")
        if messages:
            msg = messages[0]
            phone_number = msg.get("from")
            text = msg.get("text", {}).get("body")
            tenant = value.get("metadata", {}).get("display_phone_number", "unknown_tenant")
            logger.info(f"Incoming WhatsApp message | Tenant: {tenant} | From: {phone_number} | Text: {text}")

            # Call AI engine for response
            ai_response = await get_ai_response(tenant, phone_number, text)
            branding_config = get_tenant_branding_config(tenant)
            if is_welcome_input(text):
                ai_response = apply_branding(ai_response, branding_config, force_footer=True)
            else:
                ai_response = apply_branding(ai_response, branding_config)
            logger.info(f"AI response: {ai_response}")

            # Send response back via WhatsApp Cloud API
            if phone_number_id:
                await send_whatsapp_message(phone_number_id, phone_number, ai_response)
                logger.info(f"Sent response to WhatsApp user {phone_number}")
            else:
                logger.warning("No phone_number_id found in webhook payload; cannot send WhatsApp message.")
        else:
            logger.info("No messages found in webhook payload.")
    except Exception as e:
        logger.error(f"Error processing WhatsApp message: {e}")
    return JSONResponse(content={"status": "received"}, status_code=200)
