
"""
whatsapp-service/main.py
────────────────────────
Deprecated compatibility shell for the old WhatsApp service.

Canonical runtime owner:
  backend/routers/whatsapp_webhook.py mounted in backend/main.py

This service remains only as a thin forwarding layer for deployments that
still target port 8001.  All real webhook processing should now happen in the
backend monolith.
"""

from fastapi import FastAPI
import logging
from webhook import router as webhook_router

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("whatsapp-service-main")

app = FastAPI(title="Nahla WhatsApp Webhook Service (Deprecated Compatibility Shell)")
app.include_router(webhook_router)

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting WhatsApp Service...")
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
