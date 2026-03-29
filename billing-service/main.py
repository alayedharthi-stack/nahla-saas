from fastapi import FastAPI
from api.routes import router as billing_router

app = FastAPI(
    title="Nahla SaaS Billing Service",
    description="Tenant-aware billing, subscription, invoice, and payments API for Saudi SaaS merchants.",
)
app.include_router(billing_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "billing-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8011, reload=True)
