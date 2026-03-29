import httpx
import os
import logging

WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL", "https://graph.facebook.com/v17.0")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "your_whatsapp_token")

logger = logging.getLogger("whatsapp-client")

def get_whatsapp_url(phone_number_id: str) -> str:
    return f"{WHATSAPP_API_URL}/{phone_number_id}/messages"

async def send_whatsapp_message(phone_number_id: str, to: str, text: str):
    url = get_whatsapp_url(phone_number_id)
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=10)
        logger.info(f"Sent message to WhatsApp API: {payload}, response: {response.status_code}, {response.text}")
        response.raise_for_status()
        return response.json()
