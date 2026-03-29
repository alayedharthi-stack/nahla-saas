
from fastapi import FastAPI
import logging
from webhook import router as webhook_router

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("whatsapp-service-main")

app = FastAPI(title="Nahla WhatsApp Webhook Service")
app.include_router(webhook_router)

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting WhatsApp Service...")
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
