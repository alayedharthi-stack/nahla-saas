"""
DEPRECATED (ADR 0001, 2026-04-16)
─────────────────────────────────
The standalone coupon microservice is superseded by
`backend/services/coupon_generator.py`. All coupon issuance now happens
in-process, atomically with customer-intelligence updates and with the
new NH*** short-code format (see ADR 0001).

Do NOT add new routes here. If you need to extend coupon behaviour, do it
in `backend/services/coupon_generator.py` so it stays transactional with
the rest of the pipeline (orders → customer profile → pool → Salla).
"""
import warnings

from fastapi import FastAPI
from api import router as coupon_router

warnings.warn(
    "coupon-service/ is deprecated; see docs/adr/0001-durable-webhook-queue-and-nh-coupons.md",
    DeprecationWarning,
    stacklevel=2,
)

app = FastAPI(
    title="Nahla SaaS Coupon Service [DEPRECATED]",
    description=(
        "DEPRECATED — superseded by backend/services/coupon_generator.py. "
        "Kept running only for backward compatibility during migration."
    ),
)
app.include_router(coupon_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "coupon-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8005, reload=True)
