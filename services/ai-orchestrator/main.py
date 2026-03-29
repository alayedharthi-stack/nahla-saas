"""
Nahla AI Orchestrator — port 8016

The Customer Intelligence Hub.
Replaces the stateless ai-engine for all multi-turn, personalised conversations.

Flow per message:
  1. Load customer memory   (profile, preferences, affinity, history)
  2. Build system prompt    (context-aware, personalised)
  3. Call Claude            (with tool-use for structured suggestions)
  4. Policy guard           (enforce store discount/category rules)
  5. Update customer memory (background task — non-blocking)
  6. Return reply + actions

whatsapp-service calls this endpoint first.
Falls back to ai-engine (port 8002) if this service is unavailable.
"""

import logging
import os
import sys

_ORCH_ROOT = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT  = os.path.abspath(os.path.join(_ORCH_ROOT, "..", ".."))
for _p in (_ORCH_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI
from api.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai-orchestrator")

app = FastAPI(
    title="Nahla AI Orchestrator",
    description=(
        "Customer-intelligent response orchestration. "
        "Loads customer memory → builds context-aware Claude prompt → "
        "enforces store policy → returns personalised reply + structured actions."
    ),
    version="1.0.0",
)

app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Nahla AI Orchestrator...")
    uvicorn.run("main:app", host="0.0.0.0", port=8016, reload=True)
