from fastapi import FastAPI
from api.routes import router as marketplace_router

app = FastAPI(
    title="Nahla SaaS Marketplace Service",
    description="Manage third-party apps, installs, developer accounts, commissions, and marketplace discovery.",
)
app.include_router(marketplace_router)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "marketplace-service"}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8013, reload=True)
